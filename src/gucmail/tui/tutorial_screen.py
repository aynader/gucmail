"""Optional, keyboard-driven offline practice lessons."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Static, TextArea

from gucmail.help.tutorials import (
    LESSONS,
    ExerciseResult,
    LessonProgress,
    TutorialProgress,
    check_exercise,
)
from gucmail.security import sanitize_terminal
from gucmail.tui.events import TutorialHelpRequested
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class TutorialScreen(Screen[None]):
    DEFAULT_CSS = """
    TutorialScreen { background: $surface; }
    TutorialScreen #tutorial-heading {
        height: 1; padding: 0 1; background: $panel; text-style: bold;
    }
    TutorialScreen #tutorial-lesson { margin: 0 1; width: 1fr; }
    TutorialScreen #tutorial-progress { height: 1; padding: 0 2; color: $accent; }
    TutorialScreen #tutorial-scroll { height: 1fr; padding: 0 2; }
    TutorialScreen #tutorial-title { height: auto; text-style: bold; margin-bottom: 1; }
    TutorialScreen #tutorial-instruction { height: auto; margin-bottom: 1; }
    TutorialScreen #tutorial-input { width: 100%; }
    TutorialScreen #tutorial-typed { height: auto; }
    TutorialScreen #tutorial-body { height: 6; min-height: 4; }
    TutorialScreen #tutorial-navigation { height: 5; }
    TutorialScreen #tutorial-feedback { height: auto; min-height: 2; margin-top: 1; }
    TutorialScreen #tutorial-actions { height: 3; padding: 0 1; background: $panel; }
    TutorialScreen #tutorial-actions Button { min-width: 6; width: auto; margin-right: 1; }
    TutorialScreen #tutorial-next { min-width: 8; }
    TutorialScreen #tutorial-storage { height: 1; padding: 0 1; color: $text-muted; }
    TutorialScreen #tutorial-keys { height: 1; padding: 0 1; background: $panel; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close / skip"),
        Binding("ctrl+enter", "check", "Check"),
        Binding("alt+left", "back", "Back", show=False),
        Binding("alt+right", "next", "Next", show=False),
    ]

    def __init__(
        self,
        progress: TutorialProgress | None = None,
        *,
        initial_lesson: str = "navigation",
        timezone: str = "Africa/Cairo",
    ) -> None:
        super().__init__()
        self.progress = progress or TutorialProgress()
        self.timezone = timezone
        self._lesson = next(
            (lesson for lesson in LESSONS if lesson.lesson_id == initial_lesson), LESSONS[0]
        )
        self._position = 0
        self._completed = False
        self._passed = False

    def compose(self) -> ComposeResult:
        yield Static("Guided tutorials · synthetic offline practice", id="tutorial-heading")
        yield Select(
            [(lesson.title, lesson.lesson_id) for lesson in LESSONS],
            value=self._lesson.lesson_id,
            allow_blank=False,
            id="tutorial-lesson",
        )
        yield Static("", id="tutorial-progress", markup=False)
        with VerticalScroll(id="tutorial-scroll"):
            yield Static("", id="tutorial-title", markup=False)
            yield Static("", id="tutorial-instruction", markup=False)
            yield DataTable(id="tutorial-navigation", cursor_type="row")
            yield Select([], id="tutorial-choice", prompt="Choose an answer")
            with Vertical(id="tutorial-typed"):
                yield Input(id="tutorial-input", max_length=500)
                yield TextArea("", id="tutorial-body")
            yield Static("", id="tutorial-feedback", markup=False)
        with Horizontal(id="tutorial-actions"):
            yield Button("Back", id="tutorial-back")
            yield Button("Check", id="tutorial-check")
            yield Button("Next", variant="primary", id="tutorial-next", disabled=True)
            yield Button("Restart", id="tutorial-restart")
            yield Button("Guide", id="tutorial-guide")
            yield Button("Close", id="tutorial-close")
        yield Static("", id="tutorial-storage", markup=False)
        yield Static("Tab Focus · Enter Select · Ctrl+Enter Check · Esc Close", id="tutorial-keys")

    def on_mount(self) -> None:
        table = self.query_one("#tutorial-navigation", DataTable)
        table.add_columns("Sender", "Subject")
        table.add_row("Guide", "Welcome", key="welcome")
        table.add_row("Learner", "Practice note", key="practice")
        self._load_lesson()

    def _load_lesson(self) -> None:
        saved = self.progress.load(self._lesson)
        self._position, self._completed = saved.step, saved.completed
        self._render_step()

    def _render_step(self) -> None:
        step = self._lesson.steps[self._position]
        self._passed = False
        self.query_one("#tutorial-progress", Static).update(
            f"{self._lesson.title} · Step {self._position + 1} of {len(self._lesson.steps)}"
            + (" · Completed" if self._completed else "")
        )
        self.query_one("#tutorial-title", Static).update(step.title)
        self.query_one("#tutorial-instruction", Static).update(step.instruction)
        table = self.query_one("#tutorial-navigation", DataTable)
        table.display = step.exercise == "navigation" and not self._completed
        choice = self.query_one("#tutorial-choice", Select)
        choice.display = step.exercise == "choice" and not self._completed
        choice.set_options([(text, index) for index, text in enumerate(step.choices)])
        choice.value = Select.BLANK
        typed = self.query_one("#tutorial-typed")
        typed.display = (
            step.exercise in {"compose", "palette", "natural-language", "time"}
            and not self._completed
        )
        field = self.query_one("#tutorial-input", Input)
        field.value = ""
        field.placeholder = {
            "compose": "Synthetic subject",
            "palette": "open help",
            "natural-language": "show unread mail from learner@example.invalid",
            "time": "tomorrow 10 AM",
        }.get(step.exercise, "")
        body = self.query_one("#tutorial-body", TextArea)
        body.load_text("")
        body.display = step.exercise == "compose"
        self.query_one("#tutorial-feedback", Static).update(
            "Lesson complete. Choose another lesson, open its Guide or Restart to practice again."
            if self._completed
            else "Check your practice before continuing. You can close at any time."
        )
        self.query_one("#tutorial-back", Button).disabled = self._position == 0 or self._completed
        self.query_one("#tutorial-check", Button).disabled = self._completed
        self.query_one("#tutorial-next", Button).disabled = True
        self.query_one("#tutorial-next", Button).label = (
            "Finish" if self._position == len(self._lesson.steps) - 1 else "Next"
        )
        self._storage_notice()
        self.query_one("#tutorial-scroll", VerticalScroll).scroll_home(animate=False)

    def _storage_notice(self) -> None:
        self.query_one("#tutorial-storage", Static).update(
            self.progress.notice
            or "Only lesson position/completion is saved; practice text is discarded."
        )

    @on(Select.Changed, "#tutorial-lesson")
    def lesson_changed(self, event: Select.Changed) -> None:
        lesson = next((item for item in LESSONS if item.lesson_id == event.value), None)
        if lesson is not None and lesson is not self._lesson:
            self._lesson = lesson
            self._load_lesson()

    @on(Input.Changed, "#tutorial-input")
    @on(TextArea.Changed, "#tutorial-body")
    @on(Select.Changed, "#tutorial-choice")
    def exercise_changed(self) -> None:
        if self._passed:
            self._passed = False
            self.query_one("#tutorial-next", Button).disabled = True
            self.query_one("#tutorial-feedback", Static).update("Example changed. Check it again.")

    @on(Input.Submitted, "#tutorial-input")
    def input_submitted(self) -> None:
        self.action_check()

    @on(DataTable.RowSelected, "#tutorial-navigation")
    def practice_selected(self, event: DataTable.RowSelected) -> None:
        if self._lesson.steps[self._position].exercise == "navigation":
            self._show_result(
                check_exercise(self._lesson.steps[self._position], value=str(event.row_key.value))
            )

    def _show_result(self, result: ExerciseResult) -> None:
        self._passed = result.passed
        self.query_one("#tutorial-next", Button).disabled = not result.passed
        feedback = self.query_one("#tutorial-feedback", Static)
        feedback.update(sanitize_terminal(result.detail, max_length=1_000))
        feedback.scroll_visible(animate=False, force=True, immediate=True)

    def action_check(self) -> None:
        if self._completed:
            return
        step = self._lesson.steps[self._position]
        choice = self.query_one("#tutorial-choice", Select).value
        self._show_result(
            check_exercise(
                step,
                value=self.query_one("#tutorial-input", Input).value,
                body=self.query_one("#tutorial-body", TextArea).text,
                choice=choice if isinstance(choice, int) else None,
                timezone=self.timezone,
            )
        )

    def action_next(self) -> None:
        if not self._passed or self._completed:
            return
        if self._position == len(self._lesson.steps) - 1:
            self._completed = True
        else:
            self._position += 1
        self.progress.save(self._lesson, LessonProgress(self._position, self._completed))
        self._render_step()

    def action_back(self) -> None:
        if self._position > 0 and not self._completed:
            self._position -= 1
            self.progress.save(self._lesson, LessonProgress(self._position))
            self._render_step()

    def action_restart(self) -> None:
        self._position, self._completed = 0, False
        self.progress.save(self._lesson, LessonProgress())
        self._render_step()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "tutorial-check": self.action_check,
            "tutorial-next": self.action_next,
            "tutorial-back": self.action_back,
            "tutorial-restart": self.action_restart,
            "tutorial-close": self.action_close,
        }
        if event.button.id == "tutorial-guide":
            self.post_message(TutorialHelpRequested(self._lesson.help_page))
        elif event.button.id in actions:
            actions[event.button.id]()

    def action_close(self) -> None:
        self.dismiss(None)
