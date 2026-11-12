"""Categorized theme browser with guided and advanced custom-theme editors.

The browser is intentionally independent from :class:`GUCmailApp`.  It accepts
small callbacks for runtime preview/apply/cancel and can therefore be exercised
with Textual's pilot without an Exchange account or application bootstrap.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import ClassVar, Protocol, cast

from rapidfuzz import fuzz
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.security import sanitize_terminal
from gucmail.tui.area_common import (
    ConfirmationScreen,
    OperationalScreen,
    ViewState,
    current_row_highlight,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.theme_repository import (
    CustomThemeRepository,
    ThemeRepositorySnapshot,
)
from gucmail.tui.themes import (
    ColorCapability,
    ThemeDefinition,
    ThemeIssue,
    ThemePreferences,
    ThemeRegistry,
    ThemeSession,
    ThemeValidationError,
    ValidationSeverity,
    adapt_theme,
    contrast_ratio,
    export_theme,
    import_theme,
    validate_theme,
)
from gucmail.tui.write_coordinator import PENDING_CHANGE_MESSAGE


@dataclass(frozen=True, slots=True)
class ThemeEntryView:
    theme_id: str
    name: str
    categories: tuple[str, ...]
    mode: str
    contrast: str
    swatches: tuple[str, ...]
    favorite: bool
    recently_used: bool
    custom: bool
    warning_count: int


@dataclass(frozen=True, slots=True)
class ThemeBrowserSnapshot:
    categories: tuple[tuple[str, str], ...]
    entries: tuple[ThemeEntryView, ...]
    applied_theme_id: str
    preview_theme_id: str | None
    repository_revision: int
    custom_theme_count: int = 0
    repository_recovered: bool = False
    quarantined: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ThemePreviewView:
    theme: ThemeDefinition
    display_theme: ThemeDefinition
    issues: tuple[ThemeIssue, ...]
    monochrome: bool = False


class ThemeBrowserBackendProtocol(Protocol):
    def browse(
        self,
        *,
        query: str = "",
        category: str = "all",
        monochrome: bool = False,
    ) -> ThemeBrowserSnapshot: ...

    def inspect(self, theme_id: str, *, monochrome: bool = False) -> ThemePreviewView: ...

    def preview(self, theme_id: str, *, monochrome: bool = False) -> ThemePreviewView: ...

    def preview_definition(
        self, theme: ThemeDefinition, *, monochrome: bool = False
    ) -> ThemePreviewView: ...

    def cancel_preview(self) -> ThemePreviewView: ...

    def apply(self, theme_id: str | None = None) -> ThemePreviewView: ...

    def toggle_favorite(self, theme_id: str) -> bool: ...

    def preview_random(
        self, *, monochrome: bool = False, source: random.Random | None = None
    ) -> ThemePreviewView: ...

    def cycle(self, offset: int, *, monochrome: bool = False) -> ThemePreviewView: ...

    def duplicate(self, theme_id: str, *, name: str) -> ThemeDefinition: ...

    def replace_custom(self, payload: str) -> ThemeDefinition: ...

    def rename_custom(self, theme_id: str, name: str) -> ThemeDefinition: ...

    def delete_custom(self, theme_id: str) -> ThemeDefinition: ...

    def reset_custom(self) -> None: ...

    def import_file(self, path: Path) -> ThemeDefinition: ...

    def export_file(self, theme_id: str, path: Path, *, overwrite: bool = False) -> Path: ...

    def export_payload(self, theme_id: str) -> str: ...


PreviewCallback = Callable[[ThemeDefinition], None]


_CATEGORY_GROUPS: tuple[tuple[str, str], ...] = (
    ("All themes", "all"),
    ("Favorites", "favorites"),
    ("Recently used", "recently-used"),
    ("Built-in", "built-in"),
    ("GUC / Germany", "guc-germany"),
    ("Egypt / Cairo", "egypt-cairo"),
    ("Dark", "dark"),
    ("Light", "light"),
    ("High Contrast", "high-contrast"),
    ("Monochrome", "monochrome"),
    ("Blue", "blue"),
    ("Purple", "purple"),
    ("Magenta / Pink", "magenta-pink"),
    ("Red", "red"),
    ("Orange", "orange"),
    ("Yellow / Gold", "yellow-gold"),
    ("Green", "green"),
    ("Cyan / Teal", "cyan-teal"),
    ("Earth / Warm", "earth-warm"),
    ("Cool", "cool"),
    ("Pastel", "pastel"),
    ("Neon / Cyber", "neon-cyber"),
    ("Retro", "retro"),
    ("Terminal", "terminal"),
    ("Modern OS", "modern-os"),
    ("Developer", "developer"),
    ("Low-light", "low-light"),
    ("Space", "space"),
    ("Nature", "nature"),
    ("Seasonal", "seasonal"),
    ("Luxury", "luxury"),
    ("Professional", "professional"),
    ("Academic", "academic"),
    ("Minimal", "minimal"),
    ("Playful", "playful"),
    ("Special purpose", "special-purpose"),
    ("Custom", "custom"),
)


class ThemeBrowserBackend:
    """Local controller joining the registry, staged session, and repository."""

    def __init__(
        self,
        repository: CustomThemeRepository,
        *,
        registry: ThemeRegistry | None = None,
        session: ThemeSession | None = None,
        preferences: ThemePreferences | None = None,
        persist_preferences: Callable[[ThemePreferences], None] | None = None,
        on_preview: PreviewCallback | None = None,
        on_apply: PreviewCallback | None = None,
        on_cancel: PreviewCallback | None = None,
        fuzzy_threshold: float = 47.0,
    ) -> None:
        if not 0 <= fuzzy_threshold <= 100:
            raise ValueError("Theme fuzzy threshold must be between 0 and 100")
        self.repository = repository
        repository_state = repository.load()
        self.registry = registry or ThemeRegistry(repository_state.themes)
        if registry is not None:
            existing = {theme.id: theme for theme in registry.custom}
            for theme in repository_state.themes:
                prior = existing.get(theme.id)
                if prior is None:
                    registry.add_custom(theme)
                elif prior.visual_signature != theme.visual_signature or prior.name != theme.name:
                    raise ThemeValidationError(
                        f"Custom theme differs between memory and repository: {theme.id}"
                    )
        if session is not None and session.registry is not self.registry:
            raise ValueError("Theme session and browser must share the same registry")
        self.session = session or ThemeSession(
            self.registry,
            preferences,
            persist=persist_preferences,
        )
        self._repository_state = repository_state
        self._on_preview = on_preview
        self._on_apply = on_apply
        self._on_cancel = on_cancel
        self.fuzzy_threshold = fuzzy_threshold

    @property
    def repository_state(self) -> ThemeRepositorySnapshot:
        return self._repository_state

    def browse(
        self,
        *,
        query: str = "",
        category: str = "all",
        monochrome: bool = False,
    ) -> ThemeBrowserSnapshot:
        normalized_query = _normalize_query(query)
        favorite_ids = set(self.session.preferences.favorites)
        recent_ids = set(self.session.preferences.recently_used)
        candidates = [
            theme
            for theme in self.registry.themes
            if self._matches_category(theme, category, favorite_ids, recent_ids)
        ]
        scored: list[tuple[float, ThemeDefinition]] = []
        for theme in candidates:
            score = _theme_search_score(normalized_query, theme)
            if normalized_query and score < self.fuzzy_threshold:
                continue
            scored.append((score, theme))
        scored.sort(key=lambda item: (-item[0], item[1].name.casefold(), item[1].id))
        entries = tuple(
            _entry_for(
                theme,
                favorite=theme.id in favorite_ids,
                recently_used=theme.id in recent_ids,
                monochrome=monochrome,
            )
            for _, theme in scored
        )
        return ThemeBrowserSnapshot(
            categories=self.categories(),
            entries=entries,
            applied_theme_id=self.session.applied.id,
            preview_theme_id=self.session.current.id if self.session.is_previewing else None,
            repository_revision=self._repository_state.revision,
            custom_theme_count=len(self.registry.custom),
            repository_recovered=self._repository_state.recovered,
            quarantined=self._repository_state.quarantined,
        )

    def categories(self) -> tuple[tuple[str, str], ...]:
        known = {value for _, value in _CATEGORY_GROUPS}
        generated = tuple(
            (category.replace("-", " ").title(), category)
            for category in self.registry.categories
            if category not in known
        )
        return (*_CATEGORY_GROUPS, *generated)

    def inspect(self, theme_id: str, *, monochrome: bool = False) -> ThemePreviewView:
        return _preview_for(self.registry.get(theme_id), monochrome=monochrome)

    def preview(self, theme_id: str, *, monochrome: bool = False) -> ThemePreviewView:
        theme = self.session.preview(theme_id)
        result = _preview_for(theme, monochrome=monochrome)
        if self._on_preview is not None:
            self._on_preview(result.display_theme)
        return result

    def preview_definition(
        self,
        theme: ThemeDefinition,
        *,
        monochrome: bool = False,
    ) -> ThemePreviewView:
        """Preview a validated editor candidate without adding it to the registry."""

        result = _preview_for(theme, monochrome=monochrome)
        if self._on_preview is not None:
            self._on_preview(result.display_theme)
        return result

    def cancel_preview(self) -> ThemePreviewView:
        theme = self.session.cancel()
        result = _preview_for(theme)
        callback = self._on_cancel or self._on_preview
        if callback is not None:
            callback(theme)
        return result

    def apply(self, theme_id: str | None = None) -> ThemePreviewView:
        theme = self.session.apply(theme_id)
        result = _preview_for(theme)
        callback = self._on_apply or self._on_preview
        if callback is not None:
            callback(theme)
        return result

    def toggle_favorite(self, theme_id: str) -> bool:
        return self.session.toggle_favorite(theme_id)

    def preview_random(
        self,
        *,
        monochrome: bool = False,
        source: random.Random | None = None,
    ) -> ThemePreviewView:
        theme = self.session.preview_random(source)
        result = _preview_for(theme, monochrome=monochrome)
        if self._on_preview is not None:
            self._on_preview(result.display_theme)
        return result

    def cycle(self, offset: int, *, monochrome: bool = False) -> ThemePreviewView:
        theme = self.session.cycle(offset)
        result = _preview_for(theme, monochrome=monochrome)
        if self._on_preview is not None:
            self._on_preview(result.display_theme)
        return result

    def duplicate(self, theme_id: str, *, name: str) -> ThemeDefinition:
        result = self._transaction(
            lambda: self.registry.duplicate(theme_id, name=name),
        )
        return cast(ThemeDefinition, result)

    def replace_custom(self, payload: str) -> ThemeDefinition:
        replacement = import_theme(payload)

        def replace_theme() -> ThemeDefinition:
            self.registry.delete_custom(replacement.id)
            return self.registry.add_custom(replacement)

        return cast(ThemeDefinition, self._transaction(replace_theme))

    def rename_custom(self, theme_id: str, name: str) -> ThemeDefinition:
        return cast(
            ThemeDefinition,
            self._transaction(lambda: self.registry.rename_custom(theme_id, name)),
        )

    def delete_custom(self, theme_id: str) -> ThemeDefinition:
        self._leave_custom_theme_before_removal({theme_id})
        return cast(
            ThemeDefinition,
            self._transaction(lambda: self.registry.delete_custom(theme_id)),
        )

    def reset_custom(self) -> None:
        custom_ids = {theme.id for theme in self.registry.custom}
        self._leave_custom_theme_before_removal(custom_ids)
        self._transaction(self.registry.reset_custom)

    def import_file(self, path: Path) -> ThemeDefinition:
        payload = _read_theme_file(path)
        theme = import_theme(payload)

        def add_import() -> ThemeDefinition:
            if theme.id in {item.id for item in self.registry.custom}:
                raise ThemeValidationError(f"Custom theme already exists: {theme.id}")
            return self.registry.add_custom(theme)

        return cast(
            ThemeDefinition,
            self._transaction(add_import),
        )

    def export_file(self, theme_id: str, path: Path, *, overwrite: bool = False) -> Path:
        # Save first so the repository and exported definition can never disagree.
        self._repository_state = self.repository.save(
            self.registry.custom,
            expected_revision=self._repository_state.revision,
        )
        return self.repository.export_file(theme_id, path, overwrite=overwrite)

    def export_payload(self, theme_id: str) -> str:
        return export_theme(self.registry.get(theme_id))

    def _transaction(self, operation: Callable[[], object]) -> object:
        previous = self.registry.custom
        try:
            result = operation()
            self._repository_state = self.repository.save(
                self.registry.custom,
                expected_revision=self._repository_state.revision,
            )
        except BaseException:
            self.registry.reset_custom()
            for theme in previous:
                self.registry.add_custom(theme)
            raise
        return result

    def _leave_custom_theme_before_removal(self, removed: set[str]) -> None:
        if self.session.is_previewing and self.session.current.id in removed:
            self.cancel_preview()
        if self.session.applied.id in removed:
            self.apply("builtin.default-dark")

    @staticmethod
    def _matches_category(
        theme: ThemeDefinition,
        category: str,
        favorite_ids: set[str],
        recent_ids: set[str],
    ) -> bool:
        normalized = category.casefold().strip() or "all"
        if normalized == "all":
            return True
        if normalized == "favorites":
            return theme.id in favorite_ids
        if normalized == "recently-used":
            return theme.id in recent_ids
        if normalized == "built-in":
            return theme.id.startswith("builtin.")
        if normalized == "custom":
            return theme.id.startswith("custom.")
        if normalized == "dark":
            return theme.mode.value == "dark"
        if normalized == "light":
            return theme.mode.value == "light"
        if normalized == "terminal":
            return any("terminal" in item for item in theme.categories)
        return normalized in theme.categories


def _normalize_query(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _theme_search_score(query: str, theme: ThemeDefinition) -> float:
    if not query:
        return 100.0
    name = _normalize_query(theme.name)
    corpus = " ".join(
        (
            name,
            _normalize_query(theme.id),
            *(_normalize_query(item) for item in theme.categories),
            *(_normalize_query(item) for item in theme.search_tags),
            _normalize_query(theme.provenance.family),
            theme.mode.value,
            theme.contrast.value,
        )
    )
    exact_boost = 18.0 if query in name else 0.0
    prefix_boost = 8.0 if name.startswith(query) else 0.0
    score = max(float(fuzz.WRatio(query, name)), float(fuzz.WRatio(query, corpus)))
    token_scores = [float(fuzz.WRatio(token, corpus)) for token in query.split()]
    if token_scores:
        score = max(score, sum(token_scores) / len(token_scores))
    return min(120.0, score + exact_boost + prefix_boost)


def _swatches(theme: ThemeDefinition) -> tuple[str, ...]:
    palette = theme.palette
    return (
        palette.app_background,
        palette.secondary_background,
        palette.primary_text,
        palette.primary_accent,
        palette.secondary_accent,
        palette.success,
        palette.warning,
        palette.error,
    )


def _entry_for(
    theme: ThemeDefinition,
    *,
    favorite: bool,
    recently_used: bool,
    monochrome: bool,
) -> ThemeEntryView:
    display = adapt_theme(theme, ColorCapability.MONOCHROME) if monochrome else theme
    warnings = sum(
        issue.severity is ValidationSeverity.WARNING for issue in validate_theme(display)
    )
    return ThemeEntryView(
        theme_id=theme.id,
        name=theme.name,
        categories=theme.categories,
        mode=theme.mode.value,
        contrast=theme.contrast.value,
        swatches=_swatches(display),
        favorite=favorite,
        recently_used=recently_used,
        custom=theme.id.startswith("custom."),
        warning_count=warnings,
    )


def _preview_for(theme: ThemeDefinition, *, monochrome: bool = False) -> ThemePreviewView:
    display = adapt_theme(theme, ColorCapability.MONOCHROME) if monochrome else theme
    return ThemePreviewView(
        theme=theme,
        display_theme=display,
        issues=validate_theme(display),
        monochrome=monochrome,
    )


def _read_theme_file(path: Path) -> str:
    source = Path(path).expanduser()
    try:
        if source.stat().st_size > 128 * 1024:
            raise ThemeValidationError("Theme import exceeds 128 KiB")
        return source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ThemeValidationError("Theme import must be UTF-8 JSON") from exc
    except OSError as exc:
        raise ThemeValidationError(f"Unable to read theme import: {source}") from exc


def theme_swatch(swatches: Sequence[str], *, label: str = "") -> Text:
    """Build an actual Rich colour swatch rather than a textual colour alias."""

    result = Text()
    for color in swatches:
        result.append("██", style=f"{color} on {color}")
    if label:
        result.append(f"  {sanitize_terminal(label, max_length=80)}")
    return result


def theme_preview_renderable(preview: ThemePreviewView) -> Text:
    palette = preview.display_theme.palette
    result = Text()
    result.append(preview.theme.name, style=f"bold {palette.primary_text} on {palette.header}")
    suffix = " · monochrome simulation" if preview.monochrome else ""
    result.append(
        f"  {preview.theme.mode.value.title()} · {preview.theme.contrast.value}{suffix}\n"
    )
    result.append(" Folder pane ", style=f"{palette.primary_text} on {palette.sidebar_background}")
    result.append(
        "  Unread message  ",
        style=f"bold {palette.unread_message} on {palette.message_list_background}",
    )
    result.append(
        "  Selected  ",
        style=f"{palette.selection_foreground} on {palette.selection_background}",
    )
    result.append(
        "  Link  ",
        style=f"underline {palette.link} on {palette.reading_pane_background}",
    )
    result.append("\n")
    for label, color in (
        ("Success", palette.success),
        ("Info", palette.information),
        ("Warning", palette.warning),
        ("Error", palette.error),
        ("Quota safe", palette.quota_safe),
        ("Quota warning", palette.quota_warning),
        ("Quota critical", palette.quota_critical),
    ):
        result.append(f" {label} ", style=f"bold {color} on {palette.app_background}")
    result.append("\n")

    def highlighted(label: str, background: str) -> None:
        foreground = max(("#000000", "#ffffff"), key=lambda c: contrast_ratio(c, background))
        result.append(f" {label} ", style=f"bold {foreground} on {background}")

    highlighted("Command result", palette.command_result_highlight)
    result.append(" · ")
    highlighted("Search match", palette.search_highlight)
    result.append(" · ")
    result.append(
        "Calendar", style=f"bold {palette.calendar_event_colors[0]} on {palette.app_background}"
    )
    result.append(" · ")
    highlighted("Contact", palette.contact_highlight)
    return result


@dataclass(frozen=True, slots=True)
class ThemeEditorResult:
    payload: str


class ThemeEditorScreen(ModalScreen[ThemeEditorResult | None]):
    """Full-system editor using validated human-readable theme JSON."""

    DEFAULT_CSS = """
    ThemeEditorScreen { align: center middle; }
    #theme-editor-dialog {
        width: 96%; max-width: 118; height: 94%; max-height: 44;
        border: round $border; background: $surface; padding: 1 2;
    }
    #theme-editor-title { height: 2; text-style: bold; }
    #theme-editor-json { height: 1fr; min-height: 10; }
    #theme-editor-guided { height: auto; min-height: 16; }
    #theme-editor-guided Input, #theme-editor-guided Select { height: 3; }
    #theme-editor-style { height: 3; }
    #theme-editor-style Checkbox { width: 1fr; }
    #theme-editor-style Select { width: 1fr; }
    #theme-editor-actions Button { width: 1fr; min-width: 0; }
    #theme-editor-preview { height: auto; min-height: 6; border-top: solid $panel; padding: 1; }
    #theme-editor-state { height: auto; min-height: 2; }
    #theme-editor-actions { height: 3; align-horizontal: right; }
    #theme-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+enter", "save", "Save", show=False),
        Binding("ctrl+p", "preview", "Preview", show=False),
    ]

    def __init__(
        self,
        payload: str,
        *,
        preview: Callable[[ThemeDefinition], None] | None = None,
        restore_preview: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.initial_payload = payload
        self._preview_callback = preview
        self._restore_preview = restore_preview
        self._last_valid: ThemeDefinition | None = None
        self._selected_color = "app_background"
        self._loading_guided = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="theme-editor-dialog"):
            yield Label(
                "Custom theme editor",
                id="theme-editor-title",
            )
            yield Checkbox("Advanced JSON editor", id="theme-editor-advanced")
            with VerticalScroll(id="theme-editor-guided"):
                yield Label("Theme name")
                yield Input(id="theme-editor-name")
                yield Label("Palette color")
                yield Select(
                    (("Application background", "app_background"),),
                    value="app_background",
                    allow_blank=False,
                    id="theme-editor-color-field",
                )
                yield Label("Color · #RRGGBB")
                yield Input(id="theme-editor-color-value", placeholder="#1c2025")
                yield Label("Text and row spacing")
                with Horizontal(id="theme-editor-style"):
                    yield Checkbox("Bold emphasis", id="theme-editor-bold")
                    yield Select(
                        (
                            ("Compact", "compact"),
                            ("Comfortable", "comfortable"),
                            ("Spacious", "spacious"),
                        ),
                        value="comfortable",
                        allow_blank=False,
                        id="theme-editor-density",
                    )
            editor = TextArea(self.initial_payload, language="json", id="theme-editor-json")
            editor.display = False
            yield editor
            yield Static(
                "Choose a palette color, edit its value, then preview. "
                "Advanced mode exposes every field.",
                id="theme-editor-state",
                markup=False,
            )
            yield Static("", id="theme-editor-preview")
            with Horizontal(id="theme-editor-actions"):
                yield Button("Validate", id="theme-editor-validate")
                yield Button("Preview", id="theme-editor-preview-action")
                yield Button("Reset", id="theme-editor-reset")
                yield Button("Cancel", id="theme-editor-cancel")
                yield Button("Save", id="theme-editor-save", variant="primary")

    def on_mount(self) -> None:
        self._load_guided()
        self._validate(update_preview=True)

    def _load_guided(self) -> None:
        self._loading_guided = True
        try:
            raw = json.loads(self.query_one("#theme-editor-json", TextArea).text)
            colors = [
                (key.replace("_", " ").title(), key)
                for key, value in raw["palette"].items()
                if isinstance(value, str)
            ]
            field = self.query_one("#theme-editor-color-field", Select)
            field.set_options(colors)
            self._selected_color = "app_background"
            field.value = self._selected_color
            self.query_one("#theme-editor-color-value", Input).value = raw["palette"][
                self._selected_color
            ]
            self.query_one("#theme-editor-name", Input).value = raw["name"]
            self.query_one("#theme-editor-bold", Checkbox).value = raw["style"]["bold_text"]
            self.query_one("#theme-editor-density", Select).value = raw["style"]["row_density"]
        finally:
            self._loading_guided = False

    def _sync_guided(self) -> None:
        editor = self.query_one("#theme-editor-json", TextArea)
        raw = json.loads(editor.text)
        raw["name"] = self.query_one("#theme-editor-name", Input).value.strip()
        raw["palette"][self._selected_color] = self.query_one(
            "#theme-editor-color-value", Input
        ).value.strip()
        raw["style"]["bold_text"] = self.query_one("#theme-editor-bold", Checkbox).value
        raw["style"]["row_density"] = str(self.query_one("#theme-editor-density", Select).value)
        editor.load_text(json.dumps(raw, indent=2))

    @on(Select.Changed, "#theme-editor-color-field")
    def _color_field_changed(self, event: Select.Changed) -> None:
        if (
            self._loading_guided
            or event.value is Select.BLANK
            or str(event.value) == self._selected_color
        ):
            return
        self._sync_guided()
        self._selected_color = str(event.value)
        raw = json.loads(self.query_one("#theme-editor-json", TextArea).text)
        self.query_one("#theme-editor-color-value", Input).value = raw["palette"][
            self._selected_color
        ]

    @on(Checkbox.Changed, "#theme-editor-advanced")
    def _advanced_changed(self, event: Checkbox.Changed) -> None:
        if event.value:
            if self.query_one("#theme-editor-guided").display:
                self._sync_guided()
        else:
            try:
                import_theme(self.query_one("#theme-editor-json", TextArea).text)
                self._load_guided()
            except ThemeValidationError:
                event.checkbox.value = True
                self.query_one("#theme-editor-state", Static).update(
                    "Correct the JSON before returning to guided editing."
                )
                return
        self.query_one("#theme-editor-json").display = event.value
        self.query_one("#theme-editor-guided").display = not event.value

    def _validate(self, *, update_preview: bool) -> ThemeDefinition | None:
        if not self.query_one("#theme-editor-advanced", Checkbox).value:
            self._sync_guided()
        payload = self.query_one("#theme-editor-json", TextArea).text
        try:
            theme = import_theme(payload)
        except ThemeValidationError as exc:
            self._last_valid = None
            self.query_one("#theme-editor-state", Static).update(
                f"Invalid: {sanitize_terminal(str(exc), max_length=500)}"
            )
            self.query_one("#theme-editor-save", Button).disabled = True
            return None
        self._last_valid = theme
        issues = validate_theme(theme)
        warnings = [issue for issue in issues if issue.severity is ValidationSeverity.WARNING]
        message = "Valid complete theme"
        if warnings:
            message += " · " + "; ".join(
                sanitize_terminal(f"{item.field}: {item.message}", max_length=180)
                for item in warnings
            )
        self.query_one("#theme-editor-state", Static).update(message)
        self.query_one("#theme-editor-save", Button).disabled = False
        if update_preview:
            self.query_one("#theme-editor-preview", Static).update(
                theme_preview_renderable(_preview_for(theme))
            )
        return theme

    def action_preview(self) -> None:
        theme = self._validate(update_preview=True)
        if theme is not None and self._preview_callback is not None:
            self._preview_callback(theme)

    def action_save(self) -> None:
        theme = self._validate(update_preview=True)
        if theme is None:
            return
        self.dismiss(ThemeEditorResult(export_theme(theme)))

    def action_cancel(self) -> None:
        if self._restore_preview is not None:
            self._restore_preview()
        self.dismiss(None)

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "theme-editor-validate":
            self._validate(update_preview=True)
        elif event.button.id == "theme-editor-preview-action":
            self.action_preview()
        elif event.button.id == "theme-editor-reset":
            self.query_one("#theme-editor-json", TextArea).load_text(self.initial_payload)
            self._load_guided()
            self._validate(update_preview=True)
        elif event.button.id == "theme-editor-cancel":
            self.action_cancel()
        elif event.button.id == "theme-editor-save":
            self.action_save()


class ThemePreviewed(Message):
    def __init__(self, preview: ThemePreviewView) -> None:
        super().__init__()
        self.preview = preview


class ThemeApplied(Message):
    def __init__(self, preview: ThemePreviewView) -> None:
        super().__init__()
        self.preview = preview


class ThemeBrowserScreen(OperationalScreen):
    """Searchable, categorized appearance browser with safe staged preview."""

    DEFAULT_CSS = """
    ThemeBrowserScreen { layout: vertical; }
    #theme-browser-root { width: 100%; height: 100%; padding: 1 2; }
    #theme-browser-title { height: 2; text-style: bold; }
    #theme-browser-filter { height: 3; }
    #theme-search { width: 2fr; margin-right: 1; }
    #theme-category { width: 1fr; }
    #theme-table { height: 1fr; min-height: 8; }
    #theme-preview { height: 7; border-top: solid $panel; padding: 1; }
    #theme-detail { height: auto; min-height: 2; max-height: 5; }
    #theme-options, #theme-actions, #theme-custom-actions { height: 3; }
    #theme-options Input { width: 1fr; margin-right: 1; }
    #theme-options Checkbox { width: auto; margin-right: 1; }
    #theme-actions Button, #theme-custom-actions Button { margin-right: 1; }
    #theme-state { height: auto; min-height: 1; }
    ThemeBrowserScreen.narrow #theme-browser-root { padding: 0 1; }
    ThemeBrowserScreen.narrow #theme-actions,
    ThemeBrowserScreen.narrow #theme-custom-actions {
        layout: grid; grid-size: 4; grid-rows: 3; height: 6;
    }
    ThemeBrowserScreen.narrow #theme-actions Button,
    ThemeBrowserScreen.narrow #theme-custom-actions Button { width: 1fr; min-width: 0; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+f", "focus_search", "Search", show=False),
        Binding("enter", "preview", "Preview", show=False),
        Binding("ctrl+enter", "apply", "Apply", show=False),
        Binding("left", "previous_theme", "Previous", show=False),
        Binding("right", "next_theme", "Next", show=False),
        Binding("f", "favorite", "Favorite", show=False),
    ]

    def __init__(self, backend: ThemeBrowserBackendProtocol) -> None:
        super().__init__(area_name="themes")
        self.backend = backend
        self._snapshot: ThemeBrowserSnapshot | None = None
        self._entries: dict[str, ThemeEntryView] = {}
        self._selected_theme_id: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="theme-browser-root"):
            yield Label("Appearance · Themes", id="theme-browser-title")
            with Horizontal(id="theme-browser-filter"):
                yield Input(
                    placeholder="Search names, families, colors, or typo variants",
                    id="theme-search",
                )
                yield Select(
                    (("All themes", "all"),),
                    value="all",
                    allow_blank=False,
                    id="theme-category",
                )
            yield DataTable(
                id="theme-table",
                cursor_type="row",
                zebra_stripes=True,
                cursor_foreground_priority="renderable",
                cursor_background_priority="renderable",
            )
            yield Static("Select a theme to inspect it.", id="theme-preview")
            yield Static("", id="theme-detail", markup=False)
            with Horizontal(id="theme-options"):
                yield Input(placeholder="Custom theme name", id="theme-custom-name")
                yield Input(placeholder="Import/export .json path", id="theme-transfer-path")
                yield Checkbox("Monochrome preview", id="theme-monochrome")
            with Horizontal(id="theme-actions"):
                yield Button("Preview", id="theme-preview-action")
                yield Button("Apply", id="theme-apply", variant="primary")
                yield Button("Cancel preview", id="theme-cancel-preview")
                yield Button("Favorite", id="theme-favorite")
                yield Button("Random", id="theme-random")
                yield Button("Previous", id="theme-previous")
                yield Button("Next", id="theme-next")
            with Horizontal(id="theme-custom-actions"):
                yield Button("Duplicate", id="theme-duplicate")
                yield Button("Edit", id="theme-edit")
                yield Button("Rename", id="theme-rename")
                yield Button("Import", id="theme-import")
                yield Button("Export", id="theme-export")
                yield Button("Delete", id="theme-delete", variant="warning")
                yield Button("Reset custom", id="theme-reset", variant="warning")
                yield Button("Close", id="theme-close")
            yield Static("", id="theme-state", markup=False)

    def on_mount(self) -> None:
        self.set_class(self.size.width < 120, "narrow")
        table = self.query_one("#theme-table", DataTable)
        table.add_columns("★", "Theme", "Palette", "Mode", "Contrast", "Type")
        self._refresh_categories()
        self._refresh_rows()

    def _refresh_categories(self) -> None:
        snapshot = self.backend.browse()
        category = self.query_one("#theme-category", Select)
        selected = category.value
        category.set_options(snapshot.categories)
        category.value = (
            selected if selected in {value for _label, value in snapshot.categories} else "all"
        )

    def _filters(self) -> tuple[str, str, bool]:
        query = self.query_one("#theme-search", Input).value
        selected = self.query_one("#theme-category", Select).value
        category = "all" if selected is Select.BLANK else str(selected)
        monochrome = self.query_one("#theme-monochrome", Checkbox).value
        return query, category, monochrome

    def _refresh_rows(self, *, preserve: str | None = None) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        query, category, monochrome = self._filters()
        try:
            snapshot = self.backend.browse(
                query=query,
                category=category,
                monochrome=monochrome,
            )
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self._snapshot = snapshot
        self._entries = {entry.theme_id: entry for entry in snapshot.entries}
        table = self.query_one("#theme-table", DataTable)
        table.clear()
        for entry in snapshot.entries:
            table.add_row(
                "★" if entry.favorite else "☆",
                sanitize_terminal(entry.name, max_length=100),
                theme_swatch(entry.swatches),
                entry.mode.title(),
                entry.contrast.replace("_", " ").title(),
                "Custom" if entry.custom else "Built-in",
                key=entry.theme_id,
            )
        selected = preserve or self._selected_theme_id
        visible = [entry.theme_id for entry in snapshot.entries]
        if selected not in self._entries:
            selected = visible[0] if visible else None
        self._selected_theme_id = selected
        if selected is not None:
            table.move_cursor(row=visible.index(selected))
            self._show_theme(selected)
        else:
            self.query_one("#theme-preview", Static).update("No themes match this view.")
            self.query_one("#theme-detail", Static).update("")
        recovery = (
            " · recovered last known-good custom themes" if snapshot.repository_recovered else ""
        )
        self.query_one("#theme-state", Static).update(
            f"{len(snapshot.entries)} themes · repository revision "
            f"{snapshot.repository_revision}{recovery}"
        )
        self._update_buttons()

    def _show_theme(self, theme_id: str) -> None:
        _, _, monochrome = self._filters()
        try:
            preview = self.backend.inspect(theme_id, monochrome=monochrome)
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self._selected_theme_id = theme_id
        self.query_one("#theme-preview", Static).update(theme_preview_renderable(preview))
        entry = self._entries.get(theme_id)
        if entry is None:
            return
        warnings = [
            f"{issue.field}: {issue.message}"
            for issue in preview.issues
            if issue.severity is ValidationSeverity.WARNING
        ]
        detail = (
            f"{entry.theme_id} · {', '.join(entry.categories)} · "
            f"{'Favorite' if entry.favorite else 'Not favorite'}"
        )
        if warnings:
            detail += "\nContrast warnings: " + "; ".join(warnings)
        else:
            detail += "\nContrast validation: no warnings"
        self.query_one("#theme-detail", Static).update(sanitize_terminal(detail, max_length=1_000))
        self._update_buttons()

    def _selected(self) -> str | None:
        return self._selected_theme_id

    def _update_buttons(self) -> None:
        selected = self._selected()
        custom = selected is not None and selected.startswith("custom.")
        for selector in ("#theme-edit", "#theme-rename", "#theme-export", "#theme-delete"):
            self.query_one(selector, Button).disabled = not custom
        has_custom = bool(self._snapshot and self._snapshot.custom_theme_count)
        self.query_one("#theme-reset", Button).disabled = not has_custom
        for selector in (
            "#theme-preview-action",
            "#theme-apply",
            "#theme-favorite",
            "#theme-duplicate",
        ):
            self.query_one(selector, Button).disabled = selected is None

    def _show_theme_error(self, error: BaseException) -> None:
        self.query_one("#theme-state", Static).update(
            f"Appearance action failed: {sanitize_terminal(str(error), max_length=500)}"
        )

    def _guard_pending_mutation(self, *, shared: bool = True) -> bool:
        indicator = getattr(self.app, "_theme_change_pending", None)
        if isinstance(indicator, Event) and indicator.is_set():
            self._last_operation = None
            self._set_view_state(
                ViewState.LOADING if self._operation_active else ViewState.ERROR,
                PENDING_CHANGE_MESSAGE,
            )
            return True
        return super()._guard_pending_mutation(shared=shared)

    def _set_view_state(self, state: ViewState, message: str) -> None:
        super()._set_view_state(state, message)
        status = self.query("#theme-state")
        if status:
            status.first(Static).update(sanitize_terminal(message, max_length=1_000))

    def _show_error(self, action: str, error: BaseException) -> None:
        if isinstance(error, ThemeValidationError):
            self.view_state = ViewState.ERROR
            self._show_theme_error(error)
        else:
            super()._show_error(action, error)

    def _local_change(
        self, action: str, operation: Callable[[], object], success: Callable[[object], None]
    ) -> None:
        def complete(value: object) -> None:
            self.view_state = ViewState.READY
            success(value)

        indicator = getattr(self.app, "_theme_change_pending", None)

        def execute(_cancel: Event, _report: object) -> object:
            if isinstance(indicator, Event):
                indicator.set()
            try:
                return operation()
            finally:
                if isinstance(indicator, Event):
                    indicator.clear()

        self._begin_operation(action, execute, complete)

    def _custom_saved(self, theme: ThemeDefinition, action: str) -> None:
        self.query_one("#theme-search", Input).value = ""
        self.query_one("#theme-category", Select).value = "custom"
        self._refresh_rows(preserve=theme.id)
        self.query_one("#theme-state", Static).update(
            f"{action} complete custom theme {sanitize_terminal(theme.name)}."
        )

    def action_refresh(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        self._refresh_categories()
        self._refresh_rows(preserve=self._selected())

    def action_focus_search(self) -> None:
        self.query_one("#theme-search", Input).focus()

    def action_preview(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        selected = self._selected()
        if selected is None:
            return
        try:
            result = self.backend.preview(
                selected,
                monochrome=self.query_one("#theme-monochrome", Checkbox).value,
            )
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self.query_one("#theme-preview", Static).update(theme_preview_renderable(result))
        self.query_one("#theme-state", Static).update(
            f"Previewing {sanitize_terminal(result.theme.name)} · Apply or cancel explicitly."
        )
        self.post_message(ThemePreviewed(result))

    def action_apply(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        def applied(value: object) -> None:
            result = cast(ThemePreviewView, value)
            self._refresh_rows(preserve=result.theme.id)
            self.query_one("#theme-state", Static).update(
                f"Applied {sanitize_terminal(result.theme.name)}."
            )
            self.post_message(ThemeApplied(result))

        self._local_change("Apply theme", lambda: self.backend.apply(selected), applied)

    def action_cancel_preview(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        try:
            result = self.backend.cancel_preview()
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self.query_one("#theme-preview", Static).update(theme_preview_renderable(result))
        self.query_one("#theme-state", Static).update(
            f"Preview cancelled · restored {sanitize_terminal(result.theme.name)} exactly."
        )
        self.post_message(ThemePreviewed(result))

    def action_favorite(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        def saved(favorite: object) -> None:
            self._refresh_rows(preserve=selected)
            self.query_one("#theme-state", Static).update(
                "Added to favorites." if favorite else "Removed from favorites."
            )

        self._local_change("Save favorite", lambda: self.backend.toggle_favorite(selected), saved)

    def action_random_theme(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        try:
            result = self.backend.preview_random(
                monochrome=self.query_one("#theme-monochrome", Checkbox).value
            )
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self._selected_theme_id = result.theme.id
        self.query_one("#theme-preview", Static).update(theme_preview_renderable(result))
        self.query_one("#theme-state", Static).update(
            f"Random preview: {sanitize_terminal(result.theme.name)}."
        )
        self.post_message(ThemePreviewed(result))

    def _cycle(self, offset: int) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        try:
            result = self.backend.cycle(
                offset,
                monochrome=self.query_one("#theme-monochrome", Checkbox).value,
            )
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return
        self._selected_theme_id = result.theme.id
        self.query_one("#theme-preview", Static).update(theme_preview_renderable(result))
        self.query_one("#theme-state", Static).update(
            f"Previewing {sanitize_terminal(result.theme.name)}."
        )
        self.post_message(ThemePreviewed(result))

    def action_previous_theme(self) -> None:
        self._cycle(-1)

    def action_next_theme(self) -> None:
        self._cycle(1)

    def action_duplicate(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        source = self._entries[selected]
        name = self.query_one("#theme-custom-name", Input).value.strip() or f"{source.name} Custom"
        self._local_change(
            "Duplicate theme",
            lambda: self.backend.duplicate(selected, name=name),
            lambda value: self._custom_saved(cast(ThemeDefinition, value), "Created"),
        )

    def action_create_custom_theme(self) -> None:
        """Persist a complete custom definition before opening its editor."""
        selected = self._selected()
        if selected is None:
            return
        name = self.query_one("#theme-custom-name", Input).value.strip() or "New Custom Theme"

        def created(value: object) -> None:
            self._custom_saved(cast(ThemeDefinition, value), "Created")
            self.action_edit()

        self._local_change(
            "Create custom theme", lambda: self.backend.duplicate(selected, name=name), created
        )

    def action_edit(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        selected = self._selected()
        if selected is None or not selected.startswith("custom."):
            return
        try:
            payload = self.backend.export_payload(selected)
        except ThemeValidationError as exc:
            self._show_theme_error(exc)
            return

        def preview(candidate: ThemeDefinition) -> None:
            result = self.backend.preview_definition(
                candidate,
                monochrome=self.query_one("#theme-monochrome", Checkbox).value,
            )
            self.post_message(ThemePreviewed(result))

        def restore() -> None:
            result = self.backend.cancel_preview()
            self.post_message(ThemePreviewed(result))

        def saved(result: ThemeEditorResult | None) -> None:
            if result is None:
                return
            self._local_change(
                "Save custom theme",
                lambda: self.backend.replace_custom(result.payload),
                lambda value: self._custom_saved(cast(ThemeDefinition, value), "Saved"),
            )

        self.app.push_screen(
            ThemeEditorScreen(payload, preview=preview, restore_preview=restore),
            saved,
        )

    def action_rename(self) -> None:
        selected = self._selected()
        if selected is None or not selected.startswith("custom."):
            return
        name = self.query_one("#theme-custom-name", Input).value.strip()
        if not name:
            self._show_theme_error(ThemeValidationError("Enter a new custom theme name"))
            return
        self._local_change(
            "Rename custom theme",
            lambda: self.backend.rename_custom(selected, name),
            lambda value: self._custom_saved(cast(ThemeDefinition, value), "Renamed"),
        )

    def _transfer_path(self) -> Path | None:
        value = self.query_one("#theme-transfer-path", Input).value.strip()
        if not value:
            self._show_theme_error(ThemeValidationError("Enter an explicit theme JSON path"))
            return None
        return Path(value).expanduser()

    def action_import_theme(self) -> None:
        if self._guard_pending_mutation():
            return
        path = self._transfer_path()
        if path is None:
            return
        self._local_change(
            "Import theme",
            lambda: self.backend.import_file(path),
            lambda value: self._custom_saved(cast(ThemeDefinition, value), "Imported"),
        )

    def action_export_theme(self) -> None:
        if self._guard_pending_mutation():
            return
        selected = self._selected()
        path = self._transfer_path()
        if selected is None or path is None:
            return
        self._local_change(
            "Export theme",
            lambda: self.backend.export_file(selected, path),
            lambda exported: self.query_one("#theme-state", Static).update(
                f"Exported custom theme to {exported}."
            ),
        )

    def action_delete_theme(self) -> None:
        if self._guard_pending_mutation():
            return
        selected = self._selected()
        if selected is None or not selected.startswith("custom."):
            return

        def removed(value: object) -> None:
            theme = cast(ThemeDefinition, value)
            self._selected_theme_id = None
            self._refresh_rows()
            self.query_one("#theme-state", Static).update(
                f"Deleted custom theme {sanitize_terminal(theme.name)}."
            )

        self.app.push_screen(
            ConfirmationScreen(
                "Delete custom theme?",
                "This removes only the local custom definition; built-in themes are immutable.",
                confirm_label="Delete",
            ),
            lambda approved: (
                self._local_change(
                    "Delete custom theme", lambda: self.backend.delete_custom(selected), removed
                )
                if approved
                else None
            ),
        )

    def action_reset_custom(self) -> None:
        if self._guard_pending_mutation():
            return

        def reset(_value: object) -> None:
            self._selected_theme_id = None
            self._refresh_rows()
            self.query_one("#theme-state", Static).update("All custom themes were reset.")

        self.app.push_screen(
            ConfirmationScreen(
                "Reset all custom themes?",
                "Every local custom definition will be removed. Built-ins are unaffected.",
                confirm_label="Reset custom themes",
            ),
            lambda approved: (
                self._local_change("Reset custom themes", self.backend.reset_custom, reset)
                if approved
                else None
            ),
        )

    def action_close(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        if self.backend.browse().preview_theme_id is not None:
            self.backend.cancel_preview()
        super().action_close()

    @on(DataTable.RowHighlighted, "#theme-table")
    def _theme_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._show_theme(str(event.row_key.value))

    @on(Input.Changed, "#theme-search")
    def _search_changed(self) -> None:
        self._selected_theme_id = None
        self._refresh_rows()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 120, "narrow")

    @on(Select.Changed, "#theme-category")
    def _category_changed(self) -> None:
        self._selected_theme_id = None
        self._refresh_rows()

    @on(Checkbox.Changed, "#theme-monochrome")
    def _monochrome_changed(self) -> None:
        self._refresh_rows(preserve=self._selected())

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions: Mapping[str, Callable[[], None]] = {
            "theme-preview-action": self.action_preview,
            "theme-apply": self.action_apply,
            "theme-cancel-preview": self.action_cancel_preview,
            "theme-favorite": self.action_favorite,
            "theme-random": self.action_random_theme,
            "theme-previous": self.action_previous_theme,
            "theme-next": self.action_next_theme,
            "theme-duplicate": self.action_duplicate,
            "theme-edit": self.action_edit,
            "theme-rename": self.action_rename,
            "theme-import": self.action_import_theme,
            "theme-export": self.action_export_theme,
            "theme-delete": self.action_delete_theme,
            "theme-reset": self.action_reset_custom,
            "theme-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


__all__ = [
    "ThemeApplied",
    "ThemeBrowserBackend",
    "ThemeBrowserBackendProtocol",
    "ThemeBrowserScreen",
    "ThemeBrowserSnapshot",
    "ThemeEditorResult",
    "ThemeEditorScreen",
    "ThemeEntryView",
    "ThemePreviewView",
    "ThemePreviewed",
    "theme_preview_renderable",
    "theme_swatch",
]
