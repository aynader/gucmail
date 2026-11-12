"""Explicit task deletion scope with a typed challenge before final confirmation."""

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from gucmail.security import sanitize_terminal
from gucmail.services.tasks import TaskDeletionMode
from gucmail.tui.literal_select import LiteralSelect as Select


class TaskDeleteOptionsScreen(ModalScreen[TaskDeletionMode | None]):
    DEFAULT_CSS = """
    TaskDeleteOptionsScreen { align: center middle; }
    #task-delete-dialog {
        width: 76; max-width: 94%; height: 85%; max-height: 28;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    #task-delete-body { height: 1fr; min-height: 4; }
    #task-delete-summary, #task-delete-explanation, #task-delete-error {
        height: auto; margin-bottom: 1;
    }
    #task-delete-title { height: auto; text-style: bold; margin-bottom: 1; }
    #task-delete-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #task-delete-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, references: tuple[str, ...]) -> None:
        super().__init__()
        self.references = references

    def compose(self) -> ComposeResult:
        with Vertical(id="task-delete-dialog"):
            yield Label("Task deletion options", id="task-delete-title")
            with VerticalScroll(id="task-delete-body"):
                yield Static(
                    f"Selected tasks: {len(self.references)}\n"
                    + "\n".join(sanitize_terminal(ref, max_length=160) for ref in self.references),
                    id="task-delete-summary",
                    markup=False,
                )
                yield Select(
                    [
                        ("Move stored task to Deleted Items", "move"),
                        ("Permanently delete this occurrence", "occurrence"),
                        ("Permanently delete the entire recurring series", "series"),
                    ],
                    value="move",
                    allow_blank=False,
                    id="task-delete-mode",
                )
                yield Static("", id="task-delete-explanation", markup=False)
                yield Input(
                    placeholder="Type the required phrase for permanent deletion",
                    id="task-delete-challenge",
                )
                yield Static("", id="task-delete-error", markup=False)
            with Horizontal(id="task-delete-actions"):
                yield Button("Cancel", id="task-delete-cancel")
                yield Button("Review deletion", id="task-delete-review", variant="warning")

    def on_mount(self) -> None:
        self._describe_mode()

    @on(Select.Changed, "#task-delete-mode")
    def _mode_changed(self, event: Select.Changed) -> None:
        self.query_one("#task-delete-challenge", Input).value = ""
        self._describe_mode()

    def _mode(self) -> TaskDeletionMode:
        return TaskDeletionMode(str(self.query_one("#task-delete-mode", Select).value))

    @on(Input.Changed, "#task-delete-challenge")
    def _challenge_changed(self, event: Input.Changed) -> None:
        expected = "DELETE SERIES" if self._mode().delete_series else "DELETE OCCURRENCE"
        if event.value == expected:
            self.query_one("#task-delete-error", Static).update("")

    def _describe_mode(self) -> None:
        mode = self._mode()
        detail = {
            TaskDeletionMode.MOVE: (
                "Move the stored task with its recurrence to Deleted Items. "
                "This does not skip a recurring occurrence."
            ),
            TaskDeletionMode.OCCURRENCE: (
                "Permanently delete the specified occurrence. Exchange decides the next "
                "recurring task. There is no Deleted Items copy. Type DELETE OCCURRENCE."
            ),
            TaskDeletionMode.SERIES: (
                "Permanently delete the active recurring series and its associated occurrences. "
                "Each series is verified before deletion. There is no Deleted Items copy. "
                "Type DELETE SERIES."
            ),
        }[mode]
        self.query_one("#task-delete-explanation", Static).update(detail)
        self.query_one("#task-delete-challenge", Input).display = mode.hard_delete
        self.query_one("#task-delete-error", Static).update("")

    @on(Button.Pressed, "#task-delete-review")
    def _review(self) -> None:
        mode = self._mode()
        expected = "DELETE SERIES" if mode.delete_series else "DELETE OCCURRENCE"
        if mode.hard_delete and self.query_one("#task-delete-challenge", Input).value != expected:
            self.query_one("#task-delete-error", Static).update(
                f"Type {expected} exactly to review."
            )
            self.query_one("#task-delete-challenge", Input).focus()
            return
        self.dismiss(mode)

    @on(Button.Pressed, "#task-delete-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
