"""Reading a message: the preview pane, the floating reader and reader mode."""

from __future__ import annotations

from typing import Any, ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Label,
    Static,
)

from gucmail.security import sanitize_terminal
from gucmail.tui.shell_widgets import CompactHeader
from gucmail.tui.stable_footer import StableFooter as Footer


class MessagePreviewScroll(VerticalScroll):
    """Reader scroll surface with deterministic Home/End behavior after content swaps."""

    CONTEXT_BINDINGS: ClassVar = (
        Binding("end", "scroll_end", "End of message preview"),
        Binding("home", "scroll_home", "Start of message preview"),
    )

    def on_key(self, event: events.Key) -> None:
        if event.key == self.CONTEXT_BINDINGS[0].key:
            self.call_after_refresh(self.scroll_end, animate=False)
            event.prevent_default()
            event.stop()
        elif event.key == self.CONTEXT_BINDINGS[1].key:
            self.call_after_refresh(self.scroll_home, animate=False)
            event.prevent_default()
            event.stop()


class FloatingReader(Vertical):
    """A movable, modeless reader that leaves the message list usable."""

    DEFAULT_CSS = """
    FloatingReader {
        overlay: screen;
        layer: overlay;
        width: 62%;
        max-width: 110;
        min-width: 36;
        height: 78%;
        min-height: 12;
        offset: 30 2;
        border: thick $accent;
        background: $gucmail-elevated-surface;
    }
    FloatingReader #floating-reader-titlebar {
        height: 3;
        width: 100%;
        layout: horizontal;
        background: $panel;
    }
    FloatingReader #floating-reader-title {
        width: 1fr;
        padding: 1;
        text-style: bold;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }
    FloatingReader #floating-reader-close {
        width: 7;
        min-width: 7;
    }
    FloatingReader #floating-reader-scroll {
        width: 100%;
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, content: Any) -> None:
        super().__init__(id="floating-reader")
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content
        self._dragging = False
        self._grab = (0, 0)

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-reader-titlebar"):
            yield Static(self.reader_title, id="floating-reader-title", markup=False)
            yield Button("Close", id="floating-reader-close")
        with VerticalScroll(id="floating-reader-scroll"):
            yield Static(self.reader_content, id="floating-reader-content", markup=False)

    def set_message(self, title: str, content: Any) -> None:
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content
        if self.is_mounted:
            self.query_one("#floating-reader-title", Static).update(self.reader_title)
            self.query_one("#floating-reader-content", Static).update(content)

    @on(Button.Pressed, "#floating-reader-close")
    def close_reader(self, _event: Button.Pressed) -> None:
        self.remove()

    @on(events.MouseDown, "#floating-reader-titlebar")
    def start_drag(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._grab = (event.screen_x - self.region.x, event.screen_y - self.region.y)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        maximum_x = max(0, self.app.size.width - self.region.width)
        maximum_y = max(0, self.app.size.height - self.region.height)
        new_x = min(max(0, event.screen_x - self._grab[0]), maximum_x)
        new_y = min(max(0, event.screen_y - self._grab[1]), maximum_y)
        self.styles.offset = (new_x, new_y)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()


class ReaderScreen(Screen[None]):
    """Full-screen reader fallback for terminals where the preview pane is hidden."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("backspace", "dismiss", "Close", show=False),
    ]

    def __init__(self, title: str, content: Any) -> None:
        super().__init__()
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content

    def compose(self) -> ComposeResult:
        yield CompactHeader(f"Reading · {self.reader_title}")
        with VerticalScroll(id="reader-screen-scroll"):
            yield Static(self.reader_content, id="reader-screen-content", markup=False)
        yield Footer()


class ReaderModeScreen(ModalScreen[str | None]):
    """Explicit per-message reader choice used by the persisted ``ask`` mode."""

    DEFAULT_CSS = """
    ReaderModeScreen { align: center middle; background: $background 70%; }
    #reader-mode-box {
        width: 92%; max-width: 72; height: auto; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #reader-mode-title { height: 2; text-style: bold; }
    #reader-mode-detail { height: auto; min-height: 2; color: $text-muted; }
    #reader-mode-actions { height: auto; min-height: 3; align-horizontal: center; }
    #reader-mode-actions Button { margin: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="reader-mode-box"):
            yield Label("Open selected message", id="reader-mode-title")
            yield Static(
                "Choose where to read this message. External open exports one private "
                "temporary EML and still asks for confirmation.",
                id="reader-mode-detail",
                markup=False,
            )
            with Horizontal(id="reader-mode-actions"):
                yield Button("Preview", id="reader-mode-preview")
                yield Button("Full screen", id="reader-mode-full", variant="primary")
                yield Button("Floating", id="reader-mode-floating")
                yield Button("External", id="reader-mode-external")

    @on(Button.Pressed, "#reader-mode-actions Button")
    def _choice(self, event: Button.Pressed) -> None:
        choices = {
            "reader-mode-preview": "pane",
            "reader-mode-full": "full",
            "reader-mode-floating": "floating",
            "reader-mode-external": "external",
        }
        choice = choices.get(event.button.id or "")
        if choice is not None:
            self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)
