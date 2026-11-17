"""A three-step tour shown once, right after the first sign-in."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

STEPS: tuple[tuple[str, str], ...] = (
    (
        "Move around",
        "↑ and ↓ move through your messages.\nTab moves between folders, messages and the reader.",
    ),
    (
        "Open and tidy up",
        "Enter opens a message and Esc goes back.\n"
        "d deletes, e archives, ! flags. Ctrl+Z undoes.",
    ),
    (
        "Everything else is Ctrl+K",
        'Type what you want, like "compose", "calendar" or "search from Mona".\n'
        "Press ? any time for help.",
    ),
)


class WelcomeTourScreen(ModalScreen[None]):
    """Next / Skip through three short steps. Esc skips."""

    DEFAULT_CSS = """
    WelcomeTourScreen { align: center middle; }
    #tour-box {
        width: 64; max-width: 96%; height: auto; padding: 1 3;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #tour-step { color: $text-muted; }
    #tour-title { text-style: bold; margin-top: 1; }
    #tour-body { height: auto; margin-top: 1; }
    #tour-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #tour-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "skip", "Skip", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.step = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="tour-box"):
            yield Label("", id="tour-step")
            yield Label("", id="tour-title")
            yield Static("", id="tour-body", markup=False)
            with Horizontal(id="tour-actions"):
                yield Button("Skip tour", id="tour-skip")
                yield Button("Next", id="tour-next", variant="primary")

    def on_mount(self) -> None:
        self._show()
        self.query_one("#tour-next", Button).focus()

    def _show(self) -> None:
        title, body = STEPS[self.step]
        self.query_one("#tour-step", Label).update(f"Welcome to gucmail · {self.step + 1} of 3")
        self.query_one("#tour-title", Label).update(title)
        self.query_one("#tour-body", Static).update(body)
        last = self.step == len(STEPS) - 1
        self.query_one("#tour-next", Button).label = "Start" if last else "Next"
        self.query_one("#tour-skip", Button).display = not last

    @on(Button.Pressed, "#tour-next")
    def next_pressed(self) -> None:
        if self.step == len(STEPS) - 1:
            self.dismiss(None)
            return
        self.step += 1
        self._show()

    @on(Button.Pressed, "#tour-skip")
    def skip_pressed(self) -> None:
        self.dismiss(None)

    def action_skip(self) -> None:
        self.dismiss(None)
