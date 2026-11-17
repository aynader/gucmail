"""Main window: themes, layout, columns, shortcuts and saved preferences."""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import get_ident
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import (
    Static,
)

from gucmail.config import normalize_theme_identifier
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.security import sanitize_terminal
from gucmail.settings import DEFAULT_SETTINGS, SettingsContext, SettingsService
from gucmail.tui.app_core import AppCore, TextualBorderStyle
from gucmail.tui.area_common import (
    OperationalScreen,
)
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.shell_widgets import CompactHeader, PaneDivider

if TYPE_CHECKING:
    from gucmail.help import HelpRuntimeContext
    from gucmail.services.message_columns import (
        MessageColumn,
    )
    from gucmail.tui.branding import (
        BrandingPreferences,
        RenderedBranding,
    )
    from gucmail.tui.help_screen import SettingsHelpStateStore
    from gucmail.tui.themes import (
        ColorCapability,
        ThemeDefinition,
        ThemePreferences,
    )


class PreferencesMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _resolve_config_theme(self, configured: str) -> ThemeDefinition:
        from gucmail.tui.themes import ThemePreferences, ThemeValidationError

        normalized = normalize_theme_identifier(configured)
        try:
            selected = self.theme_registry.get(normalized)
        except ThemeValidationError:
            self._theme_fallback_from = normalized
            return self.theme_registry.get(ThemePreferences().selected_theme_id)
        self._theme_fallback_from = None
        return selected

    def _load_theme_preferences(self, selected_theme_id: str) -> ThemePreferences:
        from gucmail.tui.themes import ThemePolicy, ThemePreferences

        try:
            settings = self.context.config_store.load_document().settings
        except Exception:
            settings = {}
        existing = {theme.id for theme in self.theme_registry.themes}

        def identifiers(key: str, maximum: int) -> tuple[str, ...]:
            candidates = settings.get(key, [])
            if not isinstance(candidates, (list, tuple)):
                return ()
            result: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                normalized = normalize_theme_identifier(candidate)
                if normalized in existing and normalized not in result:
                    result.append(normalized)
                if len(result) >= maximum:
                    break
            return tuple(result)

        def flag(key: str) -> bool:
            value = settings.get(key, False)
            return value if isinstance(value, bool) else False

        return ThemePreferences(
            selected_theme_id=selected_theme_id,
            favorites=identifiers("appearance.theme_favorites", 100),
            recently_used=identifiers("appearance.recent_themes", 12),
            policy=ThemePolicy.EXPLICIT,
            high_contrast=flag("accessibility.high_contrast"),
            monochrome=self.context.config.no_color,
        )

    def _persist_theme_preferences(self, preferences: ThemePreferences) -> None:
        document = self.context.config_store.load_document()
        settings = dict(document.settings)
        settings["appearance.theme_favorites"] = list(preferences.favorites)
        settings["appearance.recent_themes"] = list(preferences.recently_used)
        settings["accessibility.high_contrast"] = preferences.high_contrast
        stored_config = document.stored_config
        if document.value_sources.get("theme") != "environment":
            stored_config = replace(
                stored_config,
                theme=preferences.selected_theme_id,
            ).validate()
        self.context.config_store.save_document(
            stored_config,
            settings,
            expected_revision=document.revision,
        )
        self.context.config = replace(
            self.context.config,
            theme=preferences.selected_theme_id,
        ).validate()

    def _settings_service(self) -> SettingsService:
        return SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )

    def _setting_value(self, setting_id: str, fallback: object) -> object:
        try:
            return self._settings_service().value(setting_id).effective_value
        except (ConfigurationError, KeyError, ValueError):
            return fallback

    def _load_message_columns(self) -> tuple[MessageColumn, ...]:
        from gucmail.services.message_columns import DEFAULT_COLUMNS, columns_value, parse_columns

        try:
            return parse_columns(self._setting_value("layout.message_columns", columns_value()))
        except ConfigurationError:
            return DEFAULT_COLUMNS

    def _apply_columns_preview(self, columns: tuple[MessageColumn, ...]) -> None:
        self._message_columns = columns
        if self.is_running:
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)

    def _preview_message_columns(self, value: Any) -> Callable[[], None]:
        from gucmail.services.message_columns import parse_columns

        columns = parse_columns(value)

        def apply() -> tuple[MessageColumn, ...]:
            previous = self._message_columns
            self._apply_columns_preview(columns)
            return previous

        try:
            previous = self.call_from_thread(apply)
        except RuntimeError:
            previous = apply()

        def revert() -> None:
            if not self.is_running:
                self._message_columns = previous
                return
            try:
                self.call_from_thread(self._apply_columns_preview, previous)
            except RuntimeError:
                self._apply_columns_preview(previous)

        return revert

    def _preview_settings_theme(self, value: Any) -> Callable[[], None]:
        """Preview on the UI thread and retain the exact prior preview state."""

        def apply() -> tuple[str, bool]:
            previous = (self.theme_session.current.id, self.theme_session.is_previewing)
            self.preview_theme(str(value))
            return previous

        previous = self.call_from_thread(apply) if get_ident() != self._thread_id else apply()

        def restore() -> None:
            if previous[1]:
                self.preview_theme(previous[0])
            else:
                self.cancel_theme_preview()

        def revert() -> None:
            if self.is_running and get_ident() != self._thread_id:
                self.call_from_thread(restore)
            else:
                restore()

        return revert

    def _setting_value_with_legacy(
        self,
        setting_id: str,
        fallback: object,
        *,
        legacy_key: str,
        use_fallback_when_default: bool = False,
    ) -> object:
        """Read a registered setting, falling back to the former SQLite key once."""

        try:
            effective = self._settings_service().value(setting_id)
        except (ConfigurationError, KeyError, ValueError):
            return fallback
        if effective.source.value != "default":
            return effective.effective_value
        marker = object()
        legacy = self._read_ui_preference(legacy_key, marker)
        if legacy is not marker:
            return legacy
        return fallback if use_fallback_when_default else effective.effective_value

    def _configured_message_ratio(self) -> float:
        """Resolve both registered pane ratios, preserving the old single-ratio value."""

        service = self._settings_service()
        try:
            message = service.value("layout.message_list_ratio")
            preview = service.value("layout.preview_ratio")
        except (ConfigurationError, KeyError, ValueError):
            return 0.40
        if message.source.value == "default" and preview.source.value == "default":
            marker = object()
            legacy = self._read_ui_preference("layout.message_ratio", marker)
            if legacy is not marker:
                return self._validated_ratio(legacy, default=0.40)
        try:
            message_ratio = float(message.effective_value)
            preview_ratio = float(preview.effective_value)
        except (TypeError, ValueError):
            return 0.40
        total = message_ratio + preview_ratio
        if not math.isfinite(total) or total <= 0:
            return 0.40
        return self._validated_ratio(message_ratio / total, default=0.40)

    def _persist_setting_values(self, values: Mapping[str, object], *, reason: str) -> bool:
        """Persist runtime-owned preferences through the canonical Settings service."""

        try:
            session = self._settings_service().begin()
            for setting_id, value in values.items():
                session.set(setting_id, value)
            session.apply(reason=reason)
            return True
        except (ConfigurationError, KeyError, OSError, ValueError):
            # A policy/environment override or unwritable config must not make
            # the interactive mail surface unusable. The in-memory change is
            # still valid for this session and Settings reports the true source.
            if self.is_running:
                self.notify(
                    "This change applies for this session but could not be saved. "
                    "Review Settings for a policy, environment override, "
                    "or unwritable configuration.",
                    title="Preference not saved",
                    severity="warning",
                    timeout=10,
                )
            return False

    def _persist_message_layout(self, *, reason: str = "Mail layout updated") -> bool:
        return self._persist_setting_values(
            {
                "layout.message_list_ratio": self._message_ratio,
                "layout.preview_ratio": 1.0 - self._message_ratio,
                "layout.split_orientation": self._split_orientation,
                "layout.preview_visible": self._preview_visible,
                "layout.sidebar_visible": self._sidebar_visible,
                "layout.sidebar_ratio": self._sidebar_ratio,
                "layout.status_bar_visible": self._status_bar_visible,
                "layout.last_preset": self._layout_last_preset,
            },
            reason=reason,
        )

    def _settings_applied_callback(self, setting_ids: tuple[str, ...]) -> None:
        """Marshal the Settings worker notification back onto Textual's UI thread."""

        if not self.is_running:
            self._apply_committed_settings(setting_ids)
            return
        try:
            self.call_from_thread(self._apply_committed_settings, setting_ids)
        except RuntimeError:
            # Direct backend calls in tests may already run on the UI thread.
            self.call_later(self._apply_committed_settings, setting_ids)

    def _apply_committed_settings(self, setting_ids: tuple[str, ...]) -> None:
        """Reload committed values and apply every mounted shell consumer immediately."""

        from gucmail.help import HelpUserState
        from gucmail.services.attachment_preferences import AttachmentPreferences
        from gucmail.services.calendar_preferences import CALENDAR_OPERATION_SETTINGS
        from gucmail.services.contacts_display import CONTACT_DISPLAY_SETTINGS
        from gucmail.services.keyboard_bindings import KEYBOARD_SETTING
        from gucmail.services.mail_preferences import MailSortPreferences
        from gucmail.services.shell_preferences import (
            HEADER_DEFAULT,
            STATUS_DEFAULT,
            parse_shell_fields,
        )
        from gucmail.services.shortcuts import SHORTCUT_DEFAULTS
        from gucmail.services.tasks_display import TASK_DISPLAY_SETTINGS
        from gucmail.tui.branding_screen import BrandingBrowserBackend
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.pim_screens import CalendarScreen, ContactsScreen, TasksScreen

        changed = set(setting_ids)
        service = self._settings_service()
        try:
            self.context.config = service.materialize_config()
        except ConfigurationError:
            return

        if changed & {
            "layout.message_list_ratio",
            "layout.preview_ratio",
        }:
            self._message_ratio = self._configured_message_ratio()
        if "layout.split_orientation" in changed:
            orientation = str(self._setting_value("layout.split_orientation", "vertical"))
            self._split_orientation = (
                orientation if orientation in {"vertical", "horizontal"} else "vertical"
            )
        if "layout.preview_visible" in changed:
            self._preview_visible = bool(self._setting_value("layout.preview_visible", True))
        if "layout.sidebar_visible" in changed:
            self._sidebar_visible = bool(self._setting_value("layout.sidebar_visible", True))
            if self.has_class("narrow"):
                self.set_class(self._sidebar_visible, "sidebar-visible")
        if "layout.sidebar_ratio" in changed:
            self._sidebar_ratio = self._validated_sidebar_ratio(
                self._setting_value("layout.sidebar_ratio", 0.22)
            )
        if "layout.status_bar_visible" in changed:
            self._status_bar_visible = bool(self._setting_value("layout.status_bar_visible", True))
        if "layout.header_contents" in changed:
            self._header_contents = parse_shell_fields(
                self._setting_value("layout.header_contents", HEADER_DEFAULT)
            )
        if "layout.header_compactness" in changed:
            self._header_compactness = str(
                self._setting_value("layout.header_compactness", "adaptive")
            )
        if "layout.status_bar_contents" in changed:
            self._status_bar_contents = parse_shell_fields(
                self._setting_value("layout.status_bar_contents", STATUS_DEFAULT), status=True
            )
        if changed & {
            "layout.header_contents",
            "layout.header_compactness",
            "layout.status_bar_contents",
        }:
            self._refresh_shell_status()
        if "layout.reader_mode" in changed:
            self._reader_mode = str(self._setting_value("layout.reader_mode", "preview"))
        if "layout.last_preset" in changed:
            self._layout_last_preset = str(self._setting_value("layout.last_preset", "default"))
        if "layout.message_columns" in changed:
            self._message_columns = self._load_message_columns()
            if self.is_running:
                self._render_message_rows(self._message_page_rows)

        if changed & {"mail.mark_read_on_open", "mail.mark_read_delay_seconds"}:
            # Apply takes effect for the next opening; an existing countdown cannot
            # silently inherit a shorter delay or survive disabling the preference.
            self._cancel_automatic_read()

        if "mail.fill_viewport" in changed:
            self._fill_message_viewport = bool(self._setting_value("mail.fill_viewport", True))
            if self._fill_message_viewport:
                self.call_after_refresh(self._schedule_viewport_fill)

        if changed & {"mail.sort_field", "mail.sort_direction"}:
            self._mail_sort_order = MailSortPreferences.from_settings(service).order_by
            if self.is_running and not self.offline:
                self.call_after_refresh(self.refresh_mailbox)

        if "mail.group_conversations" in changed:
            self._group_conversations = bool(
                service.value("mail.group_conversations").effective_value
            )
            if self.is_running:
                self._render_message_rows(self._message_page_rows)

        if "mail.attachment_collision" in changed:
            preference = AttachmentPreferences.from_settings(service)
            for screen in self.screen_stack:
                if isinstance(screen, DraftAttachmentScreen):
                    screen.update_collision_default(preference.collision)

        if "mail.last_folder" in changed:
            folder = self._setting_value("mail.last_folder", self.context.config.default_folder)
            if isinstance(folder, str) and folder.strip():
                self.ui.folder = folder.strip()
        if "mail.folder_expansion" in changed:
            expanded = self._setting_value("mail.folder_expansion", ())
            self._expanded_folders = (
                {str(reference) for reference in expanded if isinstance(reference, str)}
                if isinstance(expanded, (list, tuple))
                else set()
            )

        if changed & {"help.favorites", "help.recent_pages"}:
            current_help = self.context.state.load_help_state()
            favorites = self._setting_value("help.favorites", ())
            recent_pages = self._setting_value("help.recent_pages", ())
            self.context.state.save_help_state(
                HelpUserState(
                    recent_page_ids=(
                        tuple(str(value) for value in recent_pages)
                        if isinstance(recent_pages, (list, tuple))
                        else ()
                    ),
                    favorite_page_ids=(
                        frozenset(str(value) for value in favorites)
                        if isinstance(favorites, (list, tuple))
                        else frozenset()
                    ),
                    recent_queries=current_help.recent_queries,
                )
            )

        if changed & (SHORTCUT_DEFAULTS.keys() | {KEYBOARD_SETTING}):
            self._load_shortcut_preferences()
            self._bind_palette_shortcuts()
            self._bind_theme_shortcuts()
            self._apply_keyboard_bindings()

        if changed & {
            "appearance.theme",
            "accessibility.high_contrast",
            "accessibility.monochrome",
        }:
            self.theme_session.preferences = replace(
                self.theme_session.preferences,
                high_contrast=bool(self._setting_value("accessibility.high_contrast", False)),
                monochrome=self.context.config.no_color,
            )
            if "appearance.theme" in changed:
                selected = self._resolve_config_theme(self.context.config.theme)
                self.theme_session.preferences = replace(
                    self.theme_session.preferences,
                    selected_theme_id=selected.id,
                )
                self.theme_session.cancel()
            else:
                selected = self.theme_session.current
            self._active_theme = selected
            self._activate_theme(selected)
        if changed & {"appearance.density", "appearance.border_style"} and self.is_running:
            self._apply_theme_structure(self._runtime_theme)
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)
            self.call_after_refresh(self._schedule_viewport_fill)
        if changed & {
            "appearance.branding",
            "appearance.custom_branding",
            "appearance.banner_visible",
            "appearance.banner_alignment",
            "appearance.branding_size",
            "appearance.branding_color_behavior",
            "appearance.branding_width",
            "appearance.branding_height",
        }:
            self.branding_preferences = self._load_branding_preferences()
            self.branding_browser_backend = BrandingBrowserBackend(
                lambda: self.active_theme,
                preferences=self.branding_preferences,
                persist_preferences=self._persist_branding_preferences,
                on_preview=self._branding_preferences_changed,
                on_apply=self._branding_preferences_changed,
                on_cancel=self._branding_preferences_changed,
            )
            self._render_expanded_branding()

        if changed & {
            "layout.message_list_ratio",
            "layout.preview_ratio",
            "layout.split_orientation",
            "layout.preview_visible",
            "layout.sidebar_visible",
            "layout.sidebar_ratio",
            "layout.status_bar_visible",
        }:
            self._apply_layout()
        if changed & {"mail.last_folder", "mail.folder_expansion"} and self.is_running:
            self.call_after_refresh(self.action_refresh)
        if changed & {"calendar.timezone", "calendar.week_start"}:
            for screen in self.screen_stack:
                if isinstance(screen, CalendarScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Calendar preferences", exc)
        if changed & set(CALENDAR_OPERATION_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, CalendarScreen):
                    try:
                        screen.refresh_operation_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Calendar preferences", exc)
        if changed & set(CONTACT_DISPLAY_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, ContactsScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Contacts preferences", exc)
        if changed & set(TASK_DISPLAY_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, TasksScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Task preferences", exc)

    def _shortcut_label(self) -> str:
        shortcut = sanitize_terminal(self._palette_primary_shortcut, max_length=40)
        return "+".join(
            part.upper() if len(part) == 1 else part.capitalize() for part in shortcut.split("+")
        )

    def _load_shortcut_preferences(self) -> None:
        from gucmail.services.keyboard_bindings import (
            KEYBOARD_SETTING,
            KEYBOARD_SETTING_IDS,
            validate_binding_configuration,
        )
        from gucmail.services.shortcuts import SHORTCUT_DEFAULTS, validate_shortcuts

        self._shortcut_notice = ""
        self._binding_overrides: dict[str, str] = {}
        try:
            service = self._settings_service()
            values = {
                value.spec.setting_id: value.effective_value
                for value in service.snapshot(KEYBOARD_SETTING_IDS).values
            }
            keys = validate_shortcuts(
                {identifier: values[identifier] for identifier in SHORTCUT_DEFAULTS}
            )
            self._binding_overrides = validate_binding_configuration(values[KEYBOARD_SETTING], keys)
        except ConfigurationError as exc:
            keys = dict(SHORTCUT_DEFAULTS)
            self._shortcut_notice = (
                f"{exc} Global defaults are active; repair shortcuts in Settings. "
                "General action defaults are also restored."
            )
        self._palette_primary_shortcut = keys["palette.primary_shortcut"]
        self._palette_secondary_shortcut = keys["palette.secondary_shortcut"]
        self._previous_theme_shortcut = keys["appearance.previous_theme_shortcut"]
        self._next_theme_shortcut = keys["appearance.next_theme_shortcut"]

    def _current_shortcuts(self) -> dict[str, str]:
        return {
            "palette.primary_shortcut": self._palette_primary_shortcut,
            "palette.secondary_shortcut": self._palette_secondary_shortcut,
            "appearance.previous_theme_shortcut": self._previous_theme_shortcut,
            "appearance.next_theme_shortcut": self._next_theme_shortcut,
        }

    def _apply_keyboard_bindings(self) -> None:
        from gucmail.tui.keyboard_bindings import apply_keyboard_bindings

        apply_keyboard_bindings(self, self._binding_overrides)
        for screen in self.screen_stack:
            apply_keyboard_bindings(screen, self._binding_overrides)
        self.refresh_bindings()

    @staticmethod
    def _valid_shortcut(shortcut: str) -> bool:
        from gucmail.services.shortcuts import normalize_shortcut

        try:
            return bool(normalize_shortcut(shortcut))
        except ConfigurationError:
            return False

    def _bind_theme_shortcuts(self) -> None:
        from gucmail.services.shortcuts import normalize_shortcut

        for key, entries in tuple(self._bindings.key_to_bindings.items()):
            retained = [
                binding
                for binding in entries
                if binding.action not in {"previous_theme", "next_theme"}
            ]
            if retained:
                self._bindings.key_to_bindings[key] = retained
            else:
                self._bindings.key_to_bindings.pop(key, None)
        for shortcut, action, description in (
            (self._previous_theme_shortcut, "previous_theme", "Previous theme"),
            (self._next_theme_shortcut, "next_theme", "Next theme"),
        ):
            if self._valid_shortcut(shortcut):
                with contextlib.suppress(Exception):
                    self.bind(
                        normalize_shortcut(shortcut), action, description=description, show=False
                    )
        self.refresh_bindings()

    def _load_branding_preferences(self) -> BrandingPreferences:
        from gucmail.tui.branding import (
            BrandAlignment,
            BrandColorBehavior,
            BrandingPreferences,
            BrandSize,
        )

        settings = self._settings_service()

        def value(setting_id: str, fallback: object) -> object:
            try:
                return settings.value(setting_id).effective_value
            except (ConfigurationError, KeyError, ValueError):
                return fallback

        preset = value("appearance.branding", "builtin.guc-mail-minimal")
        custom_ascii = value("appearance.custom_branding", "")
        size = value("appearance.branding_size", "auto")
        color = str(value("appearance.branding_color_behavior", "theme")).replace("_", "-")
        alignment = value("appearance.banner_alignment", "left")
        width = value("appearance.branding_width", None)
        height = value("appearance.branding_height", None)
        try:
            return BrandingPreferences(
                preset_id=str(preset),
                visible=bool(value("appearance.banner_visible", True)),
                size=BrandSize(str(size)),
                color_behavior=BrandColorBehavior(color),
                alignment=BrandAlignment(str(alignment)),
                custom_ascii=str(custom_ascii or ""),
                target_width=int(width) if isinstance(width, (int, float)) else None,
                target_height=int(height) if isinstance(height, (int, float)) else None,
            )
        except ValueError:
            return BrandingPreferences()

    def _persist_branding_preferences(self, preferences: BrandingPreferences) -> None:
        service = self._settings_service()
        session = service.begin()
        session.set("appearance.branding", preferences.preset_id)
        session.set("appearance.custom_branding", preferences.custom_ascii or None)
        session.set("appearance.banner_visible", preferences.visible)
        session.set("appearance.banner_alignment", preferences.alignment.value)
        session.set("appearance.branding_size", preferences.size.value)
        session.set(
            "appearance.branding_color_behavior",
            preferences.color_behavior.value.replace("-", "_"),
        )
        session.set("appearance.branding_width", preferences.target_width)
        session.set("appearance.branding_height", preferences.target_height)
        session.apply(reason="ASCII branding updated")
        self.branding_preferences = preferences

    def _branding_preferences_changed(self, preferences: BrandingPreferences) -> None:
        if get_ident() != self._thread_id and self.is_running:
            self.call_from_thread(self._branding_preferences_changed, preferences)
            return
        self.branding_preferences = preferences
        if self.is_running:
            self._render_expanded_branding()

    def _activate_theme(self, definition: ThemeDefinition) -> ThemeDefinition:
        from gucmail.tui.themes import adapt_theme, adapt_theme_high_contrast, to_textual_theme

        self.animation_level = (
            "none" if bool(self._setting_value("accessibility.reduced_motion", False)) else "full"
        )
        runtime_definition = definition
        if bool(self._setting_value("accessibility.high_contrast", False)):
            runtime_definition = adapt_theme_high_contrast(runtime_definition)
        runtime_definition = adapt_theme(runtime_definition, self._terminal_color_capability())
        self._runtime_theme = runtime_definition
        textual_theme = to_textual_theme(runtime_definition)
        already_active = self.theme == textual_theme.name
        self.register_theme(textual_theme)
        if already_active:
            self.refresh_css(animate=False)
        else:
            self.theme = textual_theme.name
        self._active_theme = definition
        # Theme selection also runs during ``__init__``, before Textual has a
        # screen stack or mounted widget tree.  Querying there raises
        # ``ScreenStackError`` rather than ``NoMatches``.  Runtime previews do
        # need to recolor the already-mounted brand, so only query once the app
        # is actually running.
        if self.is_running:
            with contextlib.suppress(NoMatches):
                self.query_one(CompactHeader).set_branding(self._slim_branding(definition))
            self._render_expanded_branding()
            self._apply_theme_structure(runtime_definition)
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)
        return definition

    def _activate_theme_callback(self, definition: ThemeDefinition) -> None:
        # Appearance persistence may complete in an operational worker. Runtime
        # widgets and CSS still belong to the app's UI thread.
        if get_ident() != self._thread_id:
            if self.is_running:
                self.call_from_thread(self._activate_theme, definition)
            return
        self._activate_theme(definition)

    @staticmethod
    def _textual_border_style(value: str) -> TextualBorderStyle:
        styles: dict[str, TextualBorderStyle] = {
            "none": "none",
            "solid": "solid",
            "rounded": "round",
            "round": "round",
            "double": "double",
            "heavy": "heavy",
            "dashed": "dashed",
            "subtle": "solid",
        }
        return styles.get(value, "solid")

    def _apply_theme_structure(self, definition: ThemeDefinition) -> None:
        """Apply the non-colour portion of one complete theme to mounted mail UI."""

        from gucmail.tui.reader import FloatingReader

        theme_style = definition.style
        density_setting = str(self._setting_value("appearance.density", "theme"))
        density = theme_style.row_density if density_setting == "theme" else density_setting
        self._message_row_density = density
        border_setting = str(self._setting_value("appearance.border_style", "theme"))
        border = theme_style.border_style if border_setting == "theme" else border_setting
        border_style: TextualBorderStyle = self._textual_border_style(border)
        divider = self._textual_border_style(theme_style.divider_style)
        if bool(self._setting_value("accessibility.unicode_fallback", False)):
            border_style = "ascii" if border_style != "none" else "none"
            divider = "ascii" if divider != "none" else "none"
        palette = definition.palette

        for mounted_screen in self.screen_stack:
            if isinstance(mounted_screen, OperationalScreen):
                mounted_screen.refresh_workspace_theme()

        with contextlib.suppress(NoMatches):
            table = self.query_one("#message-table", DataTable)
            table.cell_padding = {"compact": 0, "comfortable": 1, "spacious": 2}.get(density, 1)
            cursor = table.get_component_styles("datatable--cursor")
            selection = theme_style.selection_style
            if selection == "reverse":
                cursor.background = palette.selection_foreground
                cursor.color = palette.selection_background
                cursor.text_style = "reverse"
            elif selection == "outline":
                cursor.background = palette.message_list_background
                cursor.color = palette.selection_background
                cursor.text_style = "bold underline"
            elif selection == "accent-bar":
                cursor.background = palette.active
                cursor.color = palette.selection_foreground
                cursor.text_style = "bold"
            else:
                cursor.background = palette.selection_background
                cursor.color = palette.selection_foreground
                cursor.text_style = "bold" if theme_style.bold_text else "none"

        for selector in ("#navigation", "#messages", "#preview-scroll"):
            with contextlib.suppress(NoMatches):
                widget = self.query_one(selector)
                widget.styles.border = (border_style, palette.border)
                if (
                    definition.id in {"builtin.default-dark", "builtin.default-light"}
                    and border_setting == "theme"
                    and not bool(self._setting_value("accessibility.unicode_fallback", False))
                ):
                    widget.styles.border = ("none", palette.border)
                    if selector == "#navigation":
                        widget.styles.border_right = ("solid", palette.divider)
        with contextlib.suppress(NoMatches):
            pane_divider = self.query_one("#pane-divider")
            pane_divider.styles.background = palette.divider
            pane_divider.styles.border_left = (divider, palette.divider)
        with contextlib.suppress(NoMatches):
            banner = self.query_one("#expanded-brand")
            treatment = theme_style.banner_treatment
            banner.styles.border_bottom = (
                ("none", palette.header)
                if treatment == "flat"
                else (
                    self._textual_border_style(theme_style.border_style),
                    palette.logo_accent if treatment == "outlined" else palette.divider,
                )
            )
        with contextlib.suppress(NoMatches):
            floating = self.query_one("#floating-reader", FloatingReader)
            elevation = theme_style.surface_elevation
            floating_border: TextualBorderStyle = (
                "none"
                if elevation == "flat"
                else "double"
                if elevation == "strong"
                else self._textual_border_style(theme_style.border_style)
            )
            floating.styles.background = palette.elevated_surface
            floating.styles.border = (
                floating_border,
                palette.primary_accent
                if theme_style.accent_intensity == "vivid"
                else palette.border,
            )

    def _monochrome_output(self) -> bool:
        """Whether colours are suppressed by configuration or by ``NO_COLOR``.

        Textual honours ``NO_COLOR`` by greyscaling every rendered cell.  The
        tricolour brand must then use the theme foreground: the luminance of
        German red on a dark header is almost the header colour itself.
        """

        return (
            self.context.config.no_color
            or bool(getattr(self, "no_color", False))
            or os.environ.get("NO_COLOR") is not None
        )

    def _terminal_color_capability(self) -> ColorCapability:
        from gucmail.tui.themes import ColorCapability

        if self._monochrome_output():
            return ColorCapability.MONOCHROME
        return {
            "truecolor": ColorCapability.TRUECOLOR,
            "256": ColorCapability.COLORS_256,
            "standard": ColorCapability.COLORS_16,
            "windows": ColorCapability.COLORS_16,
        }.get(self.console.color_system or "", ColorCapability.TRUECOLOR)

    def _slim_branding(self, definition: ThemeDefinition) -> RenderedBranding:
        from gucmail.tui.branding import slim_brand

        capability = self._terminal_color_capability()
        return slim_brand(definition, capability=capability)

    @staticmethod
    def _branding_text(rendered: RenderedBranding) -> Text:
        value = Text(no_wrap=True, overflow="crop")
        for line_index, line in enumerate(rendered.lines):
            for span in line.spans:
                style = span.foreground
                if span.background:
                    style = f"{style} on {span.background}"
                value.append(span.text, style=style)
            if line_index + 1 < len(rendered.lines):
                value.append("\n")
        return value

    def _render_expanded_branding(self) -> None:
        from gucmail.tui.branding import BrandSize, RenderedBranding, render_branding

        try:
            widget = self.query_one("#expanded-brand", Static)
        except NoMatches:
            return
        capability = self._terminal_color_capability()
        max_height = min(6, max(2, self.size.height // 8))
        try:
            rendered = render_branding(
                self.branding_preferences,
                self.active_theme,
                terminal_width=max(1, self.size.width - 2),
                max_height=max_height,
                capability=capability,
                unicode_supported=not bool(
                    self._setting_value("accessibility.unicode_fallback", False)
                ),
            )
        except Exception:
            rendered = RenderedBranding(
                preset_id="none",
                variant=BrandSize.MINIMAL,
                lines=(),
                color_behavior=self.branding_preferences.color_behavior,
            )
        # The ordinary wordmark belongs in the same row as connection status.
        # Keep explicitly selected multi-line art as a separate optional banner.
        inline_wordmark = self.branding_preferences.preset_id in {
            "builtin.guc-mail-minimal",
            "builtin.guc-original-mark",
        }
        show_banner = bool(rendered.lines) and not inline_wordmark
        widget.display = show_banner
        widget.update(self._branding_text(rendered) if show_banner else "")
        with contextlib.suppress(NoMatches):
            self.query_one(CompactHeader).query_one("#compact-title").display = not show_banner

    def _background_watcher_state(self) -> str | None:
        """State of a live background watcher (start at login), from its own status file."""

        from gucmail.headless import WatcherRunState, WatcherStatusStore, process_is_alive

        directory = (self.context.paths.data_dir / "watcher").resolve()
        prefix, suffix = "watcher-", ".status.json"
        with contextlib.suppress(OSError):
            for path in sorted(directory.glob(f"{prefix}*{suffix}")):
                scope = path.name[len(prefix) : -len(suffix)]
                status = None
                with contextlib.suppress(Exception):
                    status = WatcherStatusStore(directory, scope).read()
                if status is None or status.state in {
                    WatcherRunState.STOPPED,
                    WatcherRunState.FAILED,
                }:
                    continue
                if process_is_alive(status.pid):
                    return status.state.value
        return None

    def _watcher_header_label(self) -> str:
        background = self._background_watcher_state()
        if background is not None:
            labels = {"running": "on", "reconnecting": "reconnecting", "stopping": "stopping"}
            return f"Alerts {labels.get(background, background)}"
        try:
            records = self.context.state.list_watcher_status()
        except Exception:
            return "Alerts unknown"
        if not records:
            return "Alerts off"
        active = next(
            (
                record
                for record in records
                if str(getattr(record, "state", "")).casefold() not in {"stopped", "failed"}
            ),
            records[0],
        )
        state = str(getattr(active, "state", "unknown")).casefold()
        labels = {"running": "on", "starting": "starting", "stopped": "off", "failed": "failed"}
        return f"Alerts {labels.get(state, sanitize_terminal(state, max_length=30))}"

    def _help_runtime_context(self, active_view: str) -> HelpRuntimeContext:
        from gucmail.help import HelpRuntimeContext
        from gucmail.tui.shortcuts_screen import runtime_shortcut_records

        settings_service = self._settings_service()
        try:
            values = {
                value.spec.setting_id: value.effective_value
                for value in settings_service.values()
                if value.effective_value is not None
            }
        except ConfigurationError:
            values = {}
        facts: dict[str, object] = {}
        with contextlib.suppress(Exception):
            for observation in self.context.state.latest_quota_observations():
                prefix = f"quota.{observation.metric}"
                facts[f"{prefix}.bytes"] = observation.value_bytes
                facts[f"{prefix}.source"] = observation.source
                facts[f"{prefix}.confidence"] = observation.confidence
                facts[f"{prefix}.observed_at"] = observation.observed_at.isoformat()
        contextual = bool(values.get("help.contextual_suggestions", True))
        return HelpRuntimeContext(
            active_view=active_view if contextual else None,
            capabilities={
                "online": self.ui.online and not self.offline,
                "offline": self.offline,
            },
            settings=values,
            shortcut_assignments=runtime_shortcut_records(self, self._current_shortcuts()),
            facts=facts,
        )

    def _help_state_store(self) -> SettingsHelpStateStore:
        from gucmail.tui.help_screen import SettingsHelpStateStore

        return SettingsHelpStateStore(self.context.state, self._settings_service())

    def preview_theme(self, theme_id: str) -> ThemeDefinition:
        """Preview a theme live without changing configuration or durable UI state."""

        selected = self.theme_session.preview(normalize_theme_identifier(theme_id))
        return self._activate_theme(selected)

    def _cycle_theme(self, offset: int) -> None:
        selected = self.theme_registry.next(self.active_theme.id, offset=offset)
        self.preview_theme(selected.id)
        self._set_status(
            f"Previewing theme {sanitize_terminal(selected.name, max_length=80)} · "
            "open Appearance → Themes to apply or cancel"
        )

    def action_previous_theme(self) -> None:
        self._cycle_theme(-1)

    def action_next_theme(self) -> None:
        self._cycle_theme(1)

    def cancel_theme_preview(self) -> ThemeDefinition:
        """Restore the last applied theme exactly, leaving persistence untouched."""

        return self._activate_theme(self.theme_session.cancel())

    def apply_theme(self, theme_id: str | None = None) -> ThemeDefinition:
        """Apply and durably persist either the preview or an explicit theme."""

        normalized = normalize_theme_identifier(theme_id) if theme_id is not None else None
        selected = self.theme_session.apply(normalized)
        return self._activate_theme(selected)

    def _read_ui_preference(self, key: str, default: Any) -> Any:
        """Read non-secret UI session state without making it configuration truth."""

        try:
            self.context.state.initialize()
            with self.context.state.connection() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    (f"ui:{key}",),
                ).fetchone()
            return json.loads(str(row["value"])) if row is not None else default
        except Exception:
            return default

    def _write_ui_preference(self, key: str, value: Any) -> None:
        """Atomically persist bounded, non-secret UI state."""

        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            with self.context.state.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (f"ui:{key}", payload),
                )
        except Exception:
            # Layout persistence must never make mail unusable.
            return

    @staticmethod
    def _validated_ratio(value: Any, *, default: float) -> float:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(ratio):
            return default
        # The complementary preview ratio is persisted too; these bounds
        # satisfy both registered pane constraints.
        return min(max(ratio, 0.25), 0.70)

    @staticmethod
    def _validated_sidebar_ratio(value: Any) -> float:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return 0.22
        if not math.isfinite(ratio):
            return 0.22
        return min(max(ratio, 0.12), 0.45)

    def _apply_breakpoint(self, width: int) -> None:
        self.remove_class("narrow", "medium", "wide")
        self.add_class("wide" if width >= 120 else "medium" if width >= 80 else "narrow")

    def _apply_layout(self) -> None:
        try:
            navigation = self.query_one("#navigation", Vertical)
            messages = self.query_one("#messages", Vertical)
            preview = self.query_one("#preview-scroll", VerticalScroll)
            divider = self.query_one("#pane-divider", PaneDivider)
            status_line = self.query_one("#status-line", Horizontal)
        except NoMatches:
            return

        self.set_class(self._split_orientation == "horizontal", "split-horizontal")
        self.set_class(not self._preview_visible, "preview-hidden")
        self.set_class(not self._sidebar_visible, "sidebar-hidden")
        if not self.has_class("narrow") or not self._sidebar_visible:
            self.remove_class("sidebar-visible")
        navigation.styles.width = f"{self._sidebar_ratio * 100.0:.3f}%"
        status_line.display = self._status_bar_visible
        pane_available = self.has_class("wide") and self._preview_visible
        if (
            not pane_available
            and self._automatic_read_request is not None
            and self._automatic_read_request.view.id == "preview"
        ):
            self._cancel_automatic_read()
        if not pane_available:
            messages.styles.width = "1fr"
            messages.styles.height = "1fr"
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"
            divider.styles.width = 1
            divider.styles.height = "1fr"
            return

        message_percent = self._message_ratio * 100.0
        if self._split_orientation == "horizontal":
            messages.styles.width = "1fr"
            messages.styles.height = f"{message_percent:.3f}%"
            divider.styles.width = "1fr"
            divider.styles.height = 1
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"
        else:
            messages.styles.width = f"{message_percent:.3f}%"
            messages.styles.height = "1fr"
            divider.styles.width = 1
            divider.styles.height = "1fr"
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"

        self.call_after_refresh(self._fit_message_columns)

    def _resize_message_ratio(self, delta: float, *, persist: bool = True) -> None:
        self._message_ratio = self._validated_ratio(
            self._message_ratio + delta,
            default=0.40,
        )
        self._preview_visible = True
        self._layout_last_preset = "custom"
        self._apply_layout()
        if persist:
            self._persist_message_layout()

    def action_preview_wider(self) -> None:
        self._resize_message_ratio(-0.05)

    def action_preview_narrower(self) -> None:
        self._resize_message_ratio(0.05)

    def action_toggle_preview(self) -> None:
        self._preview_visible = not self._preview_visible
        self._layout_last_preset = "custom"
        self._persist_message_layout()
        self._apply_layout()

    def action_reset_layout(self) -> None:
        self._message_ratio = self._validated_ratio(0.36 / (0.36 + 0.42), default=0.40)
        self._split_orientation = "vertical"
        self._preview_visible = True
        self._sidebar_visible = True
        self._sidebar_ratio = 0.22
        self._status_bar_visible = True
        self._layout_last_preset = "default"
        saved = self._persist_message_layout(reason="Mail layout reset")
        self._apply_layout()
        self._set_status("Layout reset" if saved else "Layout reset for this session · not saved")

    def action_toggle_sidebar(self) -> None:
        if self.has_class("narrow"):
            self.toggle_class("sidebar-visible")
            self._sidebar_visible = self.has_class("sidebar-visible")
        else:
            self._sidebar_visible = not self._sidebar_visible
        self._layout_last_preset = "custom"
        self._persist_message_layout()
        self._apply_layout()

    def _layout_preset_values(self) -> dict[str, Any]:
        from gucmail.services.layout_presets import PRESET_FIELDS
        from gucmail.services.message_columns import columns_value

        values = {
            key: self._setting_value(key, DEFAULT_SETTINGS.get(key).default)
            for key in PRESET_FIELDS
        }
        values.update(
            {
                "layout.sidebar_visible": self._sidebar_visible,
                "layout.sidebar_ratio": self._sidebar_ratio,
                "layout.message_list_ratio": self._message_ratio,
                "layout.preview_ratio": 1.0 - self._message_ratio,
                "layout.split_orientation": self._split_orientation,
                "layout.preview_visible": self._preview_visible,
                "layout.reader_mode": self._reader_mode,
                "layout.status_bar_visible": self._status_bar_visible,
                "layout.header_contents": self._header_contents,
                "layout.header_compactness": self._header_compactness,
                "layout.status_bar_contents": self._status_bar_contents,
                "layout.message_columns": columns_value(self._message_columns),
            }
        )
        return values

    def action_layout_presets(self) -> None:
        from gucmail.services.layout_presets import LayoutPresetService
        from gucmail.tui.layout_presets_screen import LayoutPresetsScreen
        from gucmail.tui.settings_screen import SettingsScreen

        if isinstance(self.screen, SettingsScreen):
            self.notify(
                "Apply or cancel staged settings and close Settings before opening saved layouts.",
                title="Saved layouts",
            )
            return
        self.push_screen(
            LayoutPresetsScreen(
                LayoutPresetService(self._settings_service()),
                capture=self._layout_preset_values,
                on_applied=self._apply_committed_settings,
            )
        )

    def _fit_message_columns(self, *, repopulate: bool = True) -> None:
        from gucmail.services.message_columns import COLUMN_LABELS, fitted_columns

        try:
            table = self.query_one("#message-table", DataTable)
        except NoMatches:
            return
        width = max(18, table.content_size.width - 1)
        fitted = fitted_columns(self._message_columns, width, table.cell_padding)
        changed = tuple(key.value for key in table.columns) != tuple(c.id for c, _ in fitted)
        if changed:
            table.clear(columns=True)
            for column, _ in fitted:
                table.add_column(
                    "" if column.id == "state" else COLUMN_LABELS[column.id], key=column.id
                )
        for displayed_column, (_, size) in zip(table.columns.values(), fitted, strict=True):
            displayed_column.width = size
            displayed_column.auto_width = False
        table.refresh(layout=True)
        if changed and repopulate:
            self._render_message_rows(self._message_page_rows)

    def _settings_area(self) -> str:
        values = self.context.config.redacted_dict()
        return "\n".join(f"{key}: {values[key]}" for key in sorted(values))

    def _open_shortcuts(self) -> None:
        from gucmail.tui.shortcuts_screen import ShortcutsScreen, runtime_shortcut_records

        self.push_screen(
            ShortcutsScreen(
                runtime_shortcut_records(self, self._current_shortcuts()),
                notice=self._shortcut_notice,
                record_provider=lambda: runtime_shortcut_records(self, self._current_shortcuts()),
            )
        )

    def _refresh_appearance_settings(self) -> None:
        from gucmail.tui.settings_screen import SettingsScreen

        if isinstance(self.screen, SettingsScreen):
            self.screen.refresh_after_external_change(changed="Appearance preferences")

    def _open_settings_surface(
        self,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> None:
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.settings_screen import SettingsScreen

        screen_open = isinstance(self._route_screen(RouteTarget.SETTINGS), SettingsScreen)
        if not screen_open:
            self._open_area("settings")

        def configure() -> None:
            screen = self._route_screen(RouteTarget.SETTINGS)
            if not isinstance(screen, SettingsScreen):
                return
            try:
                if category:
                    screen.action_open_category(category)
                elif query is not None:
                    screen.action_search(query)
                else:
                    screen.action_focus_search()
            except NoMatches:
                return

        if screen_open:
            configure()
        else:
            self.set_timer(0.05, configure)
