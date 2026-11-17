"""Standalone browser/editor for the adaptive GUC ASCII branding system."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.security import sanitize_terminal
from gucmail.tui.area_common import (
    OperationalScreen,
    ViewState,
    current_row_highlight,
)
from gucmail.tui.branding import (
    BrandAlignment,
    BrandColorBehavior,
    BrandingCatalog,
    BrandingPreferences,
    BrandingSession,
    BrandingValidationError,
    BrandSize,
    RenderedBranding,
    render_branding,
    sanitize_custom_ascii,
    validate_theme_branding,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.themes import ColorCapability, ThemeDefinition
from gucmail.tui.write_coordinator import PENDING_CHANGE_MESSAGE


@dataclass(frozen=True, slots=True)
class BrandingEntryView:
    preset_id: str
    name: str
    category: str
    description: str
    provenance: str
    custom: bool = False
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class BrandingSnapshot:
    categories: tuple[tuple[str, str], ...]
    entries: tuple[BrandingEntryView, ...]
    applied: BrandingPreferences
    current: BrandingPreferences
    is_previewing: bool


@dataclass(frozen=True, slots=True)
class BrandingPreviewView:
    preferences: BrandingPreferences
    rendered: RenderedBranding
    warnings: tuple[str, ...]


class BrandingBrowserBackendProtocol(Protocol):
    def browse(self, *, query: str = "", category: str = "all") -> BrandingSnapshot: ...

    def preview(
        self,
        *,
        preset_id: str,
        visible: bool,
        size: str,
        color_behavior: str,
        alignment: str,
        custom_ascii: str,
        target_width: int | None,
        target_height: int | None,
        terminal_width: int,
        max_height: int,
        capability: ColorCapability = ColorCapability.TRUECOLOR,
    ) -> BrandingPreviewView: ...

    def render_current(
        self,
        *,
        terminal_width: int,
        max_height: int,
        capability: ColorCapability = ColorCapability.TRUECOLOR,
    ) -> BrandingPreviewView: ...

    def apply(self) -> BrandingPreviewView: ...

    def cancel(self) -> BrandingPreviewView: ...

    def reset(self, *, terminal_width: int, max_height: int) -> BrandingPreviewView: ...


BrandingCallback = Callable[[BrandingPreferences], None]
ThemeProvider = Callable[[], ThemeDefinition]


class BrandingBrowserBackend:
    """Stage, render, and persist branding without depending on the main app."""

    def __init__(
        self,
        theme_provider: ThemeProvider,
        *,
        catalog: BrandingCatalog | None = None,
        session: BrandingSession | None = None,
        preferences: BrandingPreferences | None = None,
        persist_preferences: BrandingCallback | None = None,
        on_preview: BrandingCallback | None = None,
        on_apply: BrandingCallback | None = None,
        on_cancel: BrandingCallback | None = None,
        fuzzy_threshold: float = 45.0,
    ) -> None:
        if not 0 <= fuzzy_threshold <= 100:
            raise ValueError("Branding fuzzy threshold must be between 0 and 100")
        self.theme_provider = theme_provider
        self.catalog = catalog or BrandingCatalog()
        self.session = session or BrandingSession(
            preferences,
            persist=persist_preferences,
        )
        self._on_preview = on_preview
        self._on_apply = on_apply
        self._on_cancel = on_cancel
        self.fuzzy_threshold = fuzzy_threshold
        self._last_geometry = (100, 8, ColorCapability.TRUECOLOR)

    def browse(self, *, query: str = "", category: str = "all") -> BrandingSnapshot:
        entries = [
            BrandingEntryView(
                preset_id=preset.id,
                name=preset.name,
                category=preset.category,
                description=preset.description,
                provenance=preset.provenance,
            )
            for preset in self.catalog.presets
        ]
        entries.extend(
            (
                BrandingEntryView(
                    preset_id="custom",
                    name="Custom ASCII art",
                    category="custom",
                    description="Terminal-safe user-authored ASCII with adaptive resizing.",
                    provenance="local preferences",
                    custom=True,
                ),
                BrandingEntryView(
                    preset_id="none",
                    name="No banner",
                    category="none",
                    description="Hide expanded ASCII branding while retaining the slim title.",
                    provenance="built-in visibility option",
                    hidden=True,
                ),
            )
        )
        normalized = _normalize(query)
        result: list[tuple[float, BrandingEntryView]] = []
        for entry in entries:
            if category not in {"", "all"} and entry.category != category:
                continue
            score = _branding_score(normalized, entry)
            if normalized and score < self.fuzzy_threshold:
                continue
            result.append((score, entry))
        result.sort(key=lambda item: (-item[0], item[1].name.casefold()))
        return BrandingSnapshot(
            categories=(
                ("All branding", "all"),
                ("GUC logo", "logo"),
                ("Stylized GUC", "wordmark"),
                ("GUC Mail banners", "banner"),
                ("Generated GUC", "generated-wordmark"),
                ("Generated GUC Mail", "generated-banner"),
                ("Featured FIGlet", "featured-figlet"),
                ("ANSI FIGlet", "ansi-figlet"),
                ("TOIlet-compatible", "toilet-font"),
                ("All bundled FIGlet", "figlet-font"),
                ("Minimal", "minimal"),
                ("Custom", "custom"),
                ("None", "none"),
            ),
            entries=tuple(entry for _, entry in result),
            applied=self.session.preferences,
            current=self.session.current,
            is_previewing=self.session.is_previewing,
        )

    def preview(
        self,
        *,
        preset_id: str,
        visible: bool,
        size: str,
        color_behavior: str,
        alignment: str,
        custom_ascii: str,
        target_width: int | None,
        target_height: int | None,
        terminal_width: int,
        max_height: int,
        capability: ColorCapability = ColorCapability.TRUECOLOR,
    ) -> BrandingPreviewView:
        try:
            changes = {
                "preset_id": preset_id,
                "visible": visible and preset_id != "none",
                "size": _parse_size(size),
                "color_behavior": BrandColorBehavior(color_behavior.replace("_", "-")),
                "alignment": BrandAlignment(alignment),
                "custom_ascii": sanitize_custom_ascii(custom_ascii),
                "target_width": target_width,
                "target_height": target_height,
            }
        except ValueError as exc:
            raise BrandingValidationError("Invalid branding option") from exc
        preferences = self.session.preview(**changes)
        result = self._render(
            preferences,
            terminal_width=terminal_width,
            max_height=max_height,
            capability=capability,
        )
        if self._on_preview is not None:
            self._on_preview(preferences)
        return result

    def render_current(
        self,
        *,
        terminal_width: int,
        max_height: int,
        capability: ColorCapability = ColorCapability.TRUECOLOR,
    ) -> BrandingPreviewView:
        return self._render(
            self.session.current,
            terminal_width=terminal_width,
            max_height=max_height,
            capability=capability,
        )

    def apply(self) -> BrandingPreviewView:
        preferences = self.session.apply()
        width, height, capability = self._last_geometry
        result = self._render(
            preferences,
            terminal_width=width,
            max_height=height,
            capability=capability,
        )
        callback = self._on_apply or self._on_preview
        if callback is not None:
            callback(preferences)
        return result

    def cancel(self) -> BrandingPreviewView:
        preferences = self.session.cancel()
        width, height, capability = self._last_geometry
        result = self._render(
            preferences,
            terminal_width=width,
            max_height=height,
            capability=capability,
        )
        callback = self._on_cancel or self._on_preview
        if callback is not None:
            callback(preferences)
        return result

    def reset(self, *, terminal_width: int, max_height: int) -> BrandingPreviewView:
        preferences = self.session.reset()
        result = self._render(
            preferences,
            terminal_width=terminal_width,
            max_height=max_height,
            capability=ColorCapability.TRUECOLOR,
        )
        if self._on_preview is not None:
            self._on_preview(preferences)
        return result

    def _render(
        self,
        preferences: BrandingPreferences,
        *,
        terminal_width: int,
        max_height: int,
        capability: ColorCapability,
    ) -> BrandingPreviewView:
        self._last_geometry = (terminal_width, max_height, capability)
        theme = self.theme_provider()
        rendered = render_branding(
            preferences,
            theme,
            terminal_width=terminal_width,
            max_height=max_height,
            capability=capability,
            catalog=self.catalog,
        )
        warnings = list(validate_theme_branding(theme))
        if rendered.accessible_color_adjustment:
            warnings.append("Brand colors were adjusted for contrast against the header.")
        if rendered.fallback_used and rendered.fallback_reason:
            warnings.append(rendered.fallback_reason)
        return BrandingPreviewView(preferences, rendered, tuple(warnings))


def _parse_size(value: str) -> BrandSize:
    return BrandSize(value)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _branding_score(query: str, entry: BrandingEntryView) -> float:
    if not query:
        return 100.0
    name = _normalize(entry.name)
    corpus = _normalize(
        " ".join(
            (
                entry.name,
                entry.category,
                entry.description,
                entry.provenance,
                entry.preset_id,
            )
        )
    )
    score = max(float(fuzz.WRatio(query, name)), float(fuzz.WRatio(query, corpus)))
    if query in name:
        score += 18.0
    return min(score, 120.0)


def branding_renderable(preview: BrandingPreviewView) -> Text:
    """Convert semantic branding spans into a Rich renderable without ANSI input."""

    result = Text()
    for line_index, line in enumerate(preview.rendered.lines):
        if line_index:
            result.append("\n")
        for span in line.spans:
            style_parts: list[str] = []
            if span.foreground:
                style_parts.append(span.foreground)
            if span.background:
                style_parts.extend(("on", span.background))
            result.append(span.text, style=" ".join(style_parts) or None)
    if not preview.rendered.lines:
        result.append("Expanded ASCII branding is hidden.", style="dim")
    return result


class BrandingPreviewed(Message):
    def __init__(self, preview: BrandingPreviewView) -> None:
        super().__init__()
        self.preview = preview


class BrandingApplied(Message):
    def __init__(self, preview: BrandingPreviewView) -> None:
        super().__init__()
        self.preview = preview


class BrandingBrowserScreen(OperationalScreen):
    """Search, configure, resize, color, preview, and apply ASCII branding."""

    DEFAULT_CSS = """
    BrandingBrowserScreen { layout: vertical; }
    #branding-root { width: 100%; height: 100%; padding: 1 2; }
    #branding-title { height: 2; text-style: bold; }
    #branding-filter { height: 3; }
    #branding-search { width: 2fr; margin-right: 1; }
    #branding-category { width: 1fr; }
    #branding-table { height: 1fr; min-height: 6; }
    #branding-preview { height: 11; border-top: solid $panel; padding: 1; }
    #branding-controls, #branding-dimensions { height: 3; }
    #branding-controls Select { width: 1fr; margin-right: 1; }
    #branding-dimensions Input { width: 1fr; margin-right: 1; }
    #branding-visible { width: auto; }
    #branding-custom { display: none; height: 7; }
    #branding-actions { height: 3; }
    #branding-actions Button { width: 1fr; min-width: 0; margin-right: 1; }
    #branding-state { height: auto; min-height: 1; }
    BrandingBrowserScreen.compact-height #branding-root { padding: 0 1; }
    BrandingBrowserScreen.compact-height #branding-title { height: 1; }
    BrandingBrowserScreen.compact-height #branding-table { min-height: 4; }
    BrandingBrowserScreen.compact-height #branding-preview { height: 5; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+f", "focus_search", "Search", show=False),
        Binding("enter", "preview", "Preview", show=False),
        Binding("ctrl+enter", "apply", "Apply", show=False),
    ]

    def __init__(self, backend: BrandingBrowserBackendProtocol) -> None:
        super().__init__(area_name="branding")
        self.backend = backend
        self._entries: dict[str, BrandingEntryView] = {}
        self._selected_preset_id: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="branding-root"):
            yield Label("Appearance · ASCII branding", id="branding-title")
            with Horizontal(id="branding-filter"):
                yield Input(
                    placeholder="Search logo, wordmark, banner, or typo variants",
                    id="branding-search",
                )
                yield Select(
                    (("All branding", "all"),),
                    value="all",
                    allow_blank=False,
                    id="branding-category",
                )
            yield DataTable(id="branding-table", cursor_type="row", zebra_stripes=True)
            yield Static("Select a branding preset.", id="branding-preview")
            with Horizontal(id="branding-controls"):
                yield Select(
                    (
                        ("Auto", "auto"),
                        ("Full", "full"),
                        ("Compact", "compact"),
                        ("Minimal", "minimal"),
                    ),
                    value="auto",
                    allow_blank=False,
                    id="branding-size",
                )
                yield Select(
                    (
                        ("Theme colors", "theme"),
                        ("German flag", "german-flag"),
                        ("Monochrome", "monochrome"),
                    ),
                    value="theme",
                    allow_blank=False,
                    id="branding-colors",
                )
                yield Select(
                    (("Left", "left"), ("Center", "center"), ("Right", "right")),
                    value="left",
                    allow_blank=False,
                    id="branding-alignment",
                )
            with Horizontal(id="branding-dimensions"):
                yield Input(placeholder="Target width (auto)", id="branding-width", type="integer")
                yield Input(
                    placeholder="Target height (auto)",
                    id="branding-height",
                    type="integer",
                )
                yield Checkbox("Show expanded banner", value=True, id="branding-visible")
            with Horizontal(id="branding-actions"):
                yield Button("Preview", id="branding-preview-action")
                yield Button("Apply", id="branding-apply", variant="primary")
                yield Button("Cancel preview", id="branding-cancel")
                yield Button("Reset", id="branding-reset")
                yield Button("Close", id="branding-close")
            yield Static("", id="branding-state", markup=False)
            yield TextArea(
                "",
                id="branding-custom",
                tooltip="Custom terminal-safe ASCII; controls and bidi overrides are removed.",
            )

    def on_mount(self) -> None:
        self.set_class(self.size.height < 32, "compact-height")
        table = self.query_one("#branding-table", DataTable)
        table.add_columns("Name", "Kind", "Description", "Source")
        snapshot = self.backend.browse()
        self.query_one("#branding-category", Select).set_options(snapshot.categories)
        self._load_preferences(snapshot.current)
        self._refresh_rows(preserve=snapshot.current.preset_id)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.height < 32, "compact-height")

    def _load_preferences(self, preferences: BrandingPreferences) -> None:
        self.query_one("#branding-size", Select).value = preferences.size.value
        self.query_one("#branding-colors", Select).value = preferences.color_behavior.value
        self.query_one("#branding-alignment", Select).value = preferences.alignment.value
        self.query_one("#branding-width", Input).value = (
            "" if preferences.target_width is None else str(preferences.target_width)
        )
        self.query_one("#branding-height", Input).value = (
            "" if preferences.target_height is None else str(preferences.target_height)
        )
        self.query_one("#branding-visible", Checkbox).value = preferences.visible
        self.query_one("#branding-custom", TextArea).load_text(preferences.custom_ascii)

    def _refresh_rows(self, *, preserve: str | None = None) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        query = self.query_one("#branding-search", Input).value
        selected_category = self.query_one("#branding-category", Select).value
        category = "all" if selected_category is Select.BLANK else str(selected_category)
        snapshot = self.backend.browse(query=query, category=category)
        self._entries = {entry.preset_id: entry for entry in snapshot.entries}
        table = self.query_one("#branding-table", DataTable)
        table.clear()
        for entry in snapshot.entries:
            table.add_row(
                sanitize_terminal(entry.name, max_length=80),
                entry.category.title(),
                sanitize_terminal(entry.description, max_length=120),
                sanitize_terminal(entry.provenance, max_length=80),
                key=entry.preset_id,
            )
        visible = [entry.preset_id for entry in snapshot.entries]
        selected = preserve or self._selected_preset_id
        if selected not in self._entries:
            selected = visible[0] if visible else None
        self._selected_preset_id = selected
        if selected is not None:
            table.move_cursor(row=visible.index(selected))
        self.query_one("#branding-state", Static).update(
            f"{len(snapshot.entries)} branding choices · "
            f"{'staged preview' if snapshot.is_previewing else 'no staged preview'}"
        )

    @staticmethod
    def _optional_int(value: str, *, label: str) -> int | None:
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(stripped)
        except ValueError as exc:
            raise BrandingValidationError(f"{label} must be a whole number") from exc
        if parsed <= 0:
            raise BrandingValidationError(f"{label} must be positive")
        return parsed

    def _geometry(self) -> tuple[int, int]:
        preview = self.query_one("#branding-preview", Static)
        return max(1, preview.content_size.width), max(1, preview.content_size.height)

    def _preview_from_controls(self) -> BrandingPreviewView:
        preset_id = self._selected_preset_id
        if preset_id is None:
            raise BrandingValidationError("Select a branding preset")
        size = self.query_one("#branding-size", Select).value
        colors = self.query_one("#branding-colors", Select).value
        alignment = self.query_one("#branding-alignment", Select).value
        if Select.BLANK in {size, colors, alignment}:
            raise BrandingValidationError("Choose size, color, and alignment")
        width, height = self._geometry()
        return self.backend.preview(
            preset_id=preset_id,
            visible=self.query_one("#branding-visible", Checkbox).value,
            size=str(size),
            color_behavior=str(colors),
            alignment=str(alignment),
            custom_ascii=self.query_one("#branding-custom", TextArea).text,
            target_width=self._optional_int(
                self.query_one("#branding-width", Input).value,
                label="Target width",
            ),
            target_height=self._optional_int(
                self.query_one("#branding-height", Input).value,
                label="Target height",
            ),
            terminal_width=width,
            max_height=height,
        )

    def _show(self, preview: BrandingPreviewView, message: str) -> None:
        self.query_one("#branding-preview", Static).update(branding_renderable(preview))
        suffix = ""
        if preview.warnings:
            suffix = " · " + "; ".join(preview.warnings)
        self.query_one("#branding-state", Static).update(
            sanitize_terminal(message + suffix, max_length=1_000)
        )

    def _show_branding_error(self, error: BaseException) -> None:
        self.query_one("#branding-state", Static).update(
            f"Branding action failed: {sanitize_terminal(str(error), max_length=500)}"
        )

    def _guard_pending_mutation(self, *, shared: bool = True) -> bool:
        indicator = getattr(self.app, "_branding_change_pending", None)
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
        status = self.query("#branding-state")
        if status:
            status.first(Static).update(sanitize_terminal(message, max_length=1_000))

    def _show_error(self, action: str, error: BaseException) -> None:
        if isinstance(error, BrandingValidationError):
            self.view_state = ViewState.ERROR
            self._show_branding_error(error)
        else:
            super()._show_error(action, error)

    def action_refresh(self) -> None:
        self._refresh_rows(preserve=self._selected_preset_id)

    def action_focus_search(self) -> None:
        self.query_one("#branding-search", Input).focus()

    def action_preview(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        try:
            preview = self._preview_from_controls()
        except BrandingValidationError as exc:
            self._show_branding_error(exc)
            return
        self._show(preview, "Previewing branding · apply or cancel explicitly.")
        self.post_message(BrandingPreviewed(preview))

    def action_apply(self) -> None:
        if self._guard_pending_mutation():
            return
        try:
            if not self.backend.browse().is_previewing:
                self._preview_from_controls()
        except BrandingValidationError as exc:
            self._show_branding_error(exc)
            return
        indicator = getattr(self.app, "_branding_change_pending", None)

        def execute(_cancel: Event, _report: object) -> object:
            if isinstance(indicator, Event):
                indicator.set()
            try:
                return self.backend.apply()
            finally:
                if isinstance(indicator, Event):
                    indicator.clear()

        def complete(value: object) -> None:
            self.view_state = ViewState.READY
            preview = cast(BrandingPreviewView, value)
            self._show(preview, "Branding applied and persisted.")
            self.post_message(BrandingApplied(preview))

        self._begin_operation("Applying the title style", execute, complete)

    def action_cancel_preview(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        try:
            preview = self.backend.cancel()
        except BrandingValidationError as exc:
            self._show_branding_error(exc)
            return
        self._load_preferences(preview.preferences)
        self._refresh_rows(preserve=preview.preferences.preset_id)
        self._show(preview, "Preview cancelled · restored the exact applied branding.")
        self.post_message(BrandingPreviewed(preview))

    def action_reset(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        width, height = self._geometry()
        try:
            preview = self.backend.reset(terminal_width=width, max_height=height)
        except BrandingValidationError as exc:
            self._show_branding_error(exc)
            return
        self._load_preferences(preview.preferences)
        self._refresh_rows(preserve=preview.preferences.preset_id)
        self._show(preview, "Default branding staged · apply or cancel explicitly.")
        self.post_message(BrandingPreviewed(preview))

    def action_close(self) -> None:
        if self._guard_pending_mutation(shared=False):
            return
        if self.backend.browse().is_previewing:
            self.backend.cancel()
        self.dismiss(None)

    @on(DataTable.RowHighlighted, "#branding-table")
    def _branding_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_preset_id = str(event.row_key.value)
        custom = self._selected_preset_id == "custom"
        custom_editor = self.query_one("#branding-custom", TextArea)
        custom_editor.disabled = not custom
        custom_editor.display = custom
        if self._selected_preset_id == "none":
            self.query_one("#branding-visible", Checkbox).value = False

    @on(Input.Changed, "#branding-search")
    def _search_changed(self) -> None:
        self._selected_preset_id = None
        self._refresh_rows()

    @on(Select.Changed, "#branding-category")
    def _category_changed(self) -> None:
        self._selected_preset_id = None
        self._refresh_rows()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions: Mapping[str, Callable[[], None]] = {
            "branding-preview-action": self.action_preview,
            "branding-apply": self.action_apply,
            "branding-cancel": self.action_cancel_preview,
            "branding-reset": self.action_reset,
            "branding-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


__all__ = [
    "BrandingApplied",
    "BrandingBrowserBackend",
    "BrandingBrowserBackendProtocol",
    "BrandingBrowserScreen",
    "BrandingEntryView",
    "BrandingPreviewView",
    "BrandingPreviewed",
    "BrandingSnapshot",
    "branding_renderable",
]
