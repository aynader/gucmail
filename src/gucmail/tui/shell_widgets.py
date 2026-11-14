"""Small widgets that make up the main window: header, status, divider, mail table."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Label,
    Static,
)

from gucmail.security import sanitize_terminal
from gucmail.services.shell_preferences import (
    HEADER_DEFAULT,
    fit_header,
    header_variants,
)
from gucmail.tui.app_helpers import _safe_failure
from gucmail.tui.branding import (
    GERMAN_BLACK,
    GERMAN_GOLD,
    GERMAN_RED,
    SLIM_TITLE,
    RenderedBranding,
)
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(slots=True)
class UIState:
    folder: str = "inbox"
    selected_reference: str | None = None
    search: str = ""
    online: bool = False
    busy: str | None = None


@dataclass(frozen=True, slots=True)
class _AutomaticReadRequest:
    generation: int
    reference: str
    view: Widget
    screen_updates: int
    cancel: Event


class AccountStatus(Static):
    """Keep connection and qualified storage facts readable as the header shrinks."""

    _variants: tuple[str, ...] = ()

    def set_mailbox_status(
        self,
        *,
        quota: str,
        watcher: str,
        folder: str,
        detail: str,
        connection: str = "Connected",
        fields: tuple[str, ...] = HEADER_DEFAULT,
        compactness: str = "adaptive",
    ) -> None:
        self.tooltip = Text(sanitize_terminal(detail, max_length=1000))
        self._variants = header_variants(
            {
                "account": connection,
                "storage": quota,
                "monitoring": watcher,
                "folder": folder,
            },
            fields,
            compactness=compactness,
        )
        self._fit_status()

    def on_resize(self, event: events.Resize) -> None:
        self._fit_status()

    def _fit_status(self) -> None:
        if self._variants:
            self.update(fit_header(self._variants, self.content_size.width))


class CompactHeader(Widget):
    """A permanently one-row header; unlike Textual's stock Header it never expands."""

    DEFAULT_CSS = (
        """
    CompactHeader {
        height: 1;
        width: 100%;
        layout: horizontal;
        background: $gucmail-header;
        color: $foreground;
    }
    CompactHeader #compact-title {
        width: auto;
        min-width: 14;
        padding: 0 0 0 1;
        text-wrap: nowrap;
    }
    CompactHeader .brand-letter {
        width: 1;
        height: 1;
        text-style: bold;
        text-align: center;
    }
    CompactHeader #brand-g { color: $foreground; background: transparent; }
    CompactHeader #brand-u { color: __GERMAN_RED__; }
    CompactHeader #brand-c { color: __GERMAN_GOLD__; }
    CompactHeader.monochrome-brand #brand-g,
    CompactHeader.monochrome-brand #brand-u,
    CompactHeader.monochrome-brand #brand-c {
        color: $foreground;
        background: transparent;
    }
    CompactHeader #brand-name {
        width: 11;
        height: 1;
        text-style: bold;
        padding: 0 1 0 1;
    }
    CompactHeader .compact-header-status {
        width: 1fr;
        padding: 0 1;
        text-align: right;
        text-overflow: ellipsis;
        text-wrap: nowrap;
        color: $text-muted;
    }
    CompactHeader #palette-launcher {
        width: auto;
        min-width: 3;
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    """.replace("__GERMAN_BLACK__", GERMAN_BLACK)
        .replace("__GERMAN_RED__", GERMAN_RED)
        .replace("__GERMAN_GOLD__", GERMAN_GOLD)
    )

    def __init__(
        self,
        status: str = "",
        *,
        status_id: str | None = None,
        branding: RenderedBranding | None = None,
        launcher_label: str | None = None,
        launcher_first: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._initial_status = sanitize_terminal(status, max_length=500)
        self._status_id = status_id
        self._branding = branding
        self._launcher_label = launcher_label
        self._launcher_first = launcher_first

    def compose(self) -> ComposeResult:
        if self._launcher_label and self._launcher_first:
            yield Static(self._launcher_label, id="palette-launcher", markup=False)
        with Horizontal(id="compact-title"):
            yield Static(SLIM_TITLE[0], id="brand-g", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[1], id="brand-u", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[2], id="brand-c", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[3:], id="brand-name", markup=False)
        status_widget = AccountStatus if self._status_id == "account-bar" else Static
        yield status_widget(
            self._initial_status,
            id=self._status_id,
            classes="compact-header-status",
            markup=False,
        )
        if self._launcher_label and not self._launcher_first:
            yield Static(self._launcher_label, id="palette-launcher", markup=False)

    def on_mount(self) -> None:
        self._apply_branding()

    def set_branding(self, branding: RenderedBranding) -> None:
        """Update exact semantic G/U/C colors and any accessible swatches."""

        self._branding = branding
        if self.is_mounted:
            self._apply_branding()

    def _apply_branding(self) -> None:
        if self._branding is None or not self._branding.lines:
            return
        spans = self._branding.lines[0].spans
        if len(spans) < 3:
            return
        # Colourless spans mean monochrome output: fall back to the stylesheet's
        # foreground rule instead of keeping the fixed German-flag CSS colours.
        self.set_class(all(span.foreground is None for span in spans[:3]), "monochrome-brand")
        for selector, span in zip(
            ("#brand-g", "#brand-u", "#brand-c"),
            spans[:3],
            strict=True,
        ):
            widget = self.query_one(selector, Static)
            if span.foreground is not None:
                widget.styles.color = span.foreground
            widget.styles.background = span.background or "transparent"


class PaneDivider(Static):
    """Mouse-draggable divider that reports cell deltas to the application."""

    class Moved(Message):
        def __init__(self, delta: int, *, final: bool = False) -> None:
            super().__init__()
            self.delta = delta
            self.final = final

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._dragging = False
        self._last_coordinate = 0

    def _coordinate(self, event: events.MouseEvent) -> int:
        app = self.app
        orientation = getattr(app, "_split_orientation", "vertical")
        return event.screen_y if orientation == "horizontal" else event.screen_x

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._last_coordinate = self._coordinate(event)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        coordinate = self._coordinate(event)
        delta = coordinate - self._last_coordinate
        self._last_coordinate = coordinate
        if delta:
            self.post_message(self.Moved(delta))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            self.post_message(self.Moved(0, final=True))
            event.stop()


class AreaScreen(ModalScreen[None]):
    """Bounded read-only product-area view loaded outside the UI thread."""

    DEFAULT_CSS = """
    AreaScreen { align: center middle; }
    #area-dialog {
        width: 90%; height: 85%; border: thick $accent;
        background: $gucmail-modal-background; padding: 1 2;
    }
    #area-title { text-style: bold; height: 2; }
    #area-scroll { height: 1fr; }
    #area-content { width: 100%; }
    #area-actions { height: 3; align-horizontal: right; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, title: str, loader: Callable[[], str]) -> None:
        super().__init__()
        self.area_title = sanitize_terminal(title, max_length=100)
        self.loader = loader
        self._load_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="area-dialog"):
            yield Label(self.area_title, id="area-title", markup=False)
            with VerticalScroll(id="area-scroll"):
                yield Static("Loading…", id="area-content", markup=False)
            with Horizontal(id="area-actions"):
                yield Button("Refresh", id="area-refresh")
                yield Button("Close", id="area-close", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(self._start_load)

    def on_unmount(self) -> None:
        self._load_generation += 1

    def _start_load(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        if self.is_mounted:
            try:
                self.query_one("#area-content", Static).update("Loading…")
            except NoMatches:
                return
        self.load_content(generation)

    @work(thread=True, exclusive=True, group="area-load")
    def load_content(self, generation: int) -> None:
        try:
            content = self.loader() or "No items in this bounded view."
        except Exception as exc:
            content = _safe_failure(exc)
        self.app.call_from_thread(self._show_content, generation, content)

    def _show_content(self, generation: int, content: str) -> None:
        if generation != self._load_generation or not self.is_mounted:
            return
        try:
            self.query_one("#area-content", Static).update(
                sanitize_terminal(content, max_length=1_000_000)
            )
        except NoMatches:
            return

    def _close(self) -> None:
        self._load_generation += 1
        self.dismiss(None)

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "area-refresh":
            self._start_load()
        elif event.button.id == "area-close":
            self._close()

    def action_refresh(self) -> None:
        self._start_load()

    def action_close(self) -> None:
        self._close()


class MailMessageTable(DataTable[str]):
    class MoreRequested(Message):
        """The user tried to move beyond the currently loaded messages."""

    def action_cursor_down(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_cursor_down()

    def action_page_down(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_page_down()

    # A row-cursor DataTable maps Home/End to horizontal column scrolling. In the
    # message list the documented meaning is first/last message, so route them to
    # the vertical cursor actions. End on the last loaded row asks for the next
    # bounded page, exactly like Down and Page Down.
    def action_scroll_home(self) -> None:
        super().action_scroll_top()

    def action_scroll_end(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_scroll_bottom()
