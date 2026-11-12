"""Offline guided lessons, shared validators and non-sensitive progress storage.

This module deliberately has no gateway, command executor, job queue or provider.
Exercises can interpret and validate examples, but cannot dispatch their actions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from gucmail.dates import parse_datetime_expression
from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.models import Address, DraftData
from gucmail.palette import PaletteEngine
from gucmail.security import sanitize_terminal
from gucmail.services.compose import ComposeService
from gucmail.state import StateStore


@dataclass(frozen=True)
class LessonStep:
    title: str
    instruction: str
    exercise: str = "choice"
    choices: tuple[str, ...] = ()
    correct: int = 0
    explanation: str = ""


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    title: str
    help_page: str
    steps: tuple[LessonStep, ...]


LESSONS = (
    Lesson(
        "navigation",
        "Find your way around",
        "start.interface",
        (
            LessonStep(
                "Open a practice message",
                "The main screen has folders, messages and a reader. Tab and Shift+Tab move "
                "focus; arrow keys move within a list. In this synthetic list, choose Practice "
                "note and press Enter. A selected summary alone does not mark a message read.",
                exercise="navigation",
            ),
            LessonStep(
                "Return and find help",
                "Escape returns from a reader or dialog. Page Up/Down scroll lists and readers. "
                "The footer shows actions for the focused view. Which key opens Help from Mail?",
                choices=("Delete", "?", "Ctrl+C"),
                correct=1,
                explanation="? opens contextual Help. You can restart these lessons from its "
                "Tutorials button. Use Tab to reach another pane without closing the current one.",
            ),
        ),
    ),
    Lesson(
        "first-message",
        "Prepare your first email",
        "tutorial.first-message",
        (
            LessonStep(
                "Validate a practice message",
                "In Mail, C opens Compose. Add To, subject and body; use Show Cc/Bcc when needed. "
                "Here, supply a synthetic subject and body for learner@example.invalid. "
                "Check uses the same message validator as Compose; this exercise cannot send.",
                exercise="compose",
            ),
            LessonStep(
                "Review before sending",
                "For a real message, Review send shows the actual recipients and content. "
                "Inspect Cc/Bcc, attached files and any warnings. What should you do if an "
                "address in that confirmation is wrong?",
                choices=(
                    "Confirm, then send a correction",
                    "Cancel and correct the draft",
                    "Retry",
                ),
                correct=1,
                explanation="Cancel returns control before transmission. Save draft and Send "
                "are separate actions. After a timeout, inspect send status before retrying: "
                "Exchange may have accepted the message.",
            ),
        ),
    ),
    Lesson(
        "palette",
        "Use the command palette",
        "tutorial.palette",
        (
            LessonStep(
                "Discover a command",
                "Ctrl+K opens All Commands with the default shortcuts. Type open help below "
                "and choose Check to see the real local interpreter's result. In the normal "
                "palette, arrows choose a suggestion and Enter opens it.",
                exercise="palette",
            ),
            LessonStep(
                "Read the proposed action",
                "The normal palette shows availability, parameters and confirmation requirements. "
                "An incomplete move request still needs selected messages and a destination. "
                "How do you leave the palette without running a suggestion?",
                choices=("Press Enter", "Press Escape", "Choose the first result"),
                correct=1,
                explanation="Escape closes the palette. Your configured shortcut can differ "
                "from Ctrl+K; its current binding appears in the Mail footer.",
            ),
        ),
    ),
    Lesson(
        "natural-language",
        "Describe a mail search",
        "tutorial.palette",
        (
            LessonStep(
                "Inspect extracted search fields",
                "Type show unread mail from learner@example.invalid below. Check interprets "
                "it locally and shows the extracted sender and read state. It performs no search.",
                exercise="natural-language",
            ),
            LessonStep(
                "Resolve uncertainty",
                "The interpreter uses a supported local grammar. It can leave uncertain names "
                "or dates unresolved. What should you do when its proposed sender is wrong?",
                choices=(
                    "Run it anyway",
                    "Use a precise address and review again",
                    "Repeat faster",
                ),
                correct=1,
                explanation="Use an explicit email address, folder or ISO date. Review the "
                "proposed fields before execution; text that resembles a command is not proof "
                "that every detail was understood.",
            ),
        ),
    ),
    Lesson(
        "scheduling",
        "Schedule a message",
        "tutorial.scheduling",
        (
            LessonStep(
                "Resolve the delivery time",
                "Save a local draft before scheduling real mail. In the schedule review, inspect "
                "the absolute time, zone and UTC offset. Try tomorrow 10 AM or in 45 minutes "
                "below. This practice resolves a time without creating a draft or queued job.",
                exercise="time",
            ),
            LessonStep(
                "Arrange the worker",
                "Real scheduled jobs need a running, authenticated worker. Closing the TUI "
                "does not keep a worker running. What makes unattended delivery possible?",
                choices=(
                    "The local job record alone",
                    "A configured supervisor and available computer/network/credentials",
                    "Leaving Help open",
                ),
                correct=1,
                explanation="Configure and verify Start with Windows or an OS supervisor. "
                "In Scheduled Mail, inspect the job and cancellation state. Sleep, logged-out "
                "keyring access and provider acceptance need separate verification.",
            ),
        ),
    ),
    Lesson(
        "notifications",
        "Set up Windows notifications",
        "tutorial.notifications",
        (
            LessonStep(
                "Choose a private test",
                "Open Notifications from All Commands. Review provider availability and privacy "
                "fields before enabling a desktop provider. Which first CLI test previews a "
                "synthetic event without contacting a mailbox or external destination?",
                choices=("gucmail notifications test", "Send a real message", "Enable a webhook"),
                explanation="gucmail notifications test previews a synthetic event by default. "
                "An explicit desktop delivery test can then check OS delivery when the optional "
                "desktop dependency is installed. This lesson does not deliver notifications.",
            ),
            LessonStep(
                "Separate delivery from detection",
                "A desktop test can succeed while mailbox monitoring is stopped. What else "
                "must you check before relying on new-mail notifications?",
                choices=(
                    "Only the notification sound",
                    "Monitoring, quiet baseline/checkpoint and delivery history",
                    "Send the password to the provider",
                ),
                correct=1,
                explanation="Start a configured watcher and check its status and history. "
                "Windows grouping, custom sounds and click actions are not implemented by the "
                "current desktop adapter. OS notification settings may also suppress delivery.",
            ),
        ),
    ),
    Lesson(
        "backup",
        "Create your first backup",
        "tutorial.backup",
        (
            LessonStep(
                "Review scope and destination",
                "Open Backups, choose a protected destination and a small explicit scope. "
                "Before Create, which action helps review expected item count and storage?",
                choices=("Restore", "Estimate", "Delete"),
                correct=1,
                explanation="Estimate reads bounded metadata. Creating the archive also reads "
                "bodies and attachment content. Review scope, free space and encryption, and "
                "keep the passphrase separately; the application cannot recover a lost one.",
            ),
            LessonStep(
                "Verify and extend the archive",
                "After creation, inspect completion, Verify the archive and browse it offline. "
                "For an incremental backup, select a complete parent. What scope does it use?",
                choices=(
                    "All mail regardless of the parent",
                    "The parent's exact scope and exclusions",
                    "Only whatever is highlighted in Mail now",
                ),
                correct=1,
                explanation="Incrementals inherit their selected parent. Search/cluster archives "
                "retain fixed resolved references. Restore is a separate confirmed operation "
                "with documented fidelity limits; a verified archive is not a proven restore.",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class ExerciseResult:
    passed: bool
    detail: str


def check_exercise(
    step: LessonStep,
    *,
    value: str = "",
    body: str = "",
    choice: int | None = None,
    timezone: str = "Africa/Cairo",
    now: datetime | None = None,
) -> ExerciseResult:
    """Check an exercise through pure validators; never save its input or dispatch it."""
    if len(value) > 500 or len(body) > 2_000:
        return ExerciseResult(False, "Use a short synthetic example (500 / 2,000 characters).")
    if step.exercise == "choice":
        passed = choice == step.correct
        return ExerciseResult(
            passed, step.explanation if passed else "Review the step and try again."
        )
    if step.exercise == "navigation":
        passed = value == "practice"
        return ExerciseResult(
            passed,
            "Practice note opened. Tab changes focus; Escape returns."
            if passed
            else "Select Practice note with the arrows and press Enter.",
        )
    try:
        if step.exercise == "compose":
            validation = ComposeService().validate(
                DraftData(
                    reference="tutorial-practice",
                    to=[Address(email="learner@example.invalid")],
                    subject=value,
                    body_text=body,
                )
            )
            return ExerciseResult(
                validation.can_send,
                "Valid synthetic message. Nothing was saved or "
                "sent. Next, review the final-send decision."
                if validation.can_send
                else "; ".join(i.message for i in validation.errors),
            )
        if step.exercise in {"palette", "natural-language"}:
            result = PaletteEngine(timezone=timezone, now=now).interpret(value)
            invocation = result.invocation
            expected = "help.open" if step.exercise == "palette" else "search.mail"
            passed = invocation is not None and invocation.command_id == expected
            if passed and step.exercise == "natural-language":
                assert invocation is not None
                sender = invocation.get("sender")
                passed = (
                    getattr(sender, "email", "") == "learner@example.invalid"
                    and invocation.get("read_state") == "unread"
                )
            detail = result.explanation
            if passed and step.exercise == "natural-language":
                detail += " Sender: learner@example.invalid; read state: unread."
            return ExerciseResult(
                passed,
                sanitize_terminal(detail, max_length=500)
                + (" Preview only; no action ran." if passed else " Try the example in this step."),
            )
        if step.exercise == "time":
            resolved = parse_datetime_expression(
                value, timezone=timezone, now=now, future_required=True
            )
            return ExerciseResult(
                True,
                f"Local: {resolved.local.isoformat()} ({resolved.timezone})"
                f"\nUTC: {resolved.utc.isoformat()}\nNo job was created.",
            )
    except (ConfigurationError, ValueError, OverflowError) as exc:
        return ExerciseResult(False, sanitize_terminal(str(exc), max_length=500))
    raise ValueError(f"Unknown tutorial exercise: {step.exercise}")


@dataclass(frozen=True)
class LessonProgress:
    step: int = 0
    completed: bool = False


class TutorialProgress:
    """One small record per lesson; no mailbox content or practice text is retained."""

    def __init__(self, state: StateStore | None = None) -> None:
        self.state = state
        self.notice = "" if state is not None else "Progress stays in this session."
        self._memory: dict[str, LessonProgress] = {}

    def load(self, lesson: Lesson) -> LessonProgress:
        if lesson.lesson_id in self._memory:
            return self._memory[lesson.lesson_id]
        progress = LessonProgress()
        if self.state is not None:
            try:
                record = self.state.get_ui_preference(lesson.lesson_id, scope="help.tutorials")
                value = record.value if record else None
                if isinstance(value, dict) and value.get("version") == 1:
                    step = value.get("step")
                    if type(step) is int and 0 <= step < len(lesson.steps):
                        progress = LessonProgress(
                            step, value.get("completed") is True and step == len(lesson.steps) - 1
                        )
            except (LocalStateError, OSError, ValueError, sqlite3.DatabaseError):
                self.notice = (
                    "Progress is available for this session; local storage is unavailable."
                )
        self._memory[lesson.lesson_id] = progress
        return progress

    def save(self, lesson: Lesson, progress: LessonProgress) -> None:
        if not 0 <= progress.step < len(lesson.steps):
            raise ValueError("Invalid tutorial step.")
        if progress.completed and progress.step != len(lesson.steps) - 1:
            raise ValueError("Only the last step can complete a tutorial.")
        self._memory[lesson.lesson_id] = progress
        if self.state is not None:
            try:
                self.state.put_ui_preference(
                    lesson.lesson_id,
                    {
                        "version": 1,
                        "step": progress.step,
                        "completed": progress.completed,
                    },
                    scope="help.tutorials",
                )
            except (LocalStateError, OSError, sqlite3.DatabaseError):
                self.notice = (
                    "Progress is available for this session; local storage is unavailable."
                )
