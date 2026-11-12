"""Retain a composition until its acknowledged, rejected or uncertain outcome."""

from collections.abc import Callable
from typing import Any, ClassVar, Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Label, Static, TextArea

from gucmail.security import sanitize_terminal
from gucmail.services.send_attempts import CompositionSendStatus, SendAttemptStore
from gucmail.tui.area_common import OperationalScreen

SubmissionOutcome = Literal["saved", "sent", "queued", "failed", "uncertain", "not_started"]


def attempt_message(state: str | None) -> str:
    return {
        "sent": "This send was accepted. Do not send this draft again. "
        "Use a separately reviewed duplicate only if another send is intentional.",
        "sending": "A send was started, but its final outcome is not confirmed. "
        "Do not send this draft again. Check Sent Items and delivery first.",
        "uncertain": "The send may have completed. It was not retried. "
        "Check Sent Items and delivery before another send. Your recovery copy is retained.",
        "failed": "The last send was rejected before acceptance. "
        "Review the message and attachments before trying again.",
        "queued": "This draft already has a Scheduled Mail entry. "
        "Review, edit or cancel that job before another send.",
    }.get(state or "", "No send attempt is recorded for this draft.")


class ComposerSendStatus(OperationalScreen):
    """Read-only local attempt details; opening them can never repeat a send."""

    SHARED_WORKSPACE_LAYOUT: ClassVar = False

    DEFAULT_CSS = """
    ComposerSendStatus { align: center middle; background: $background 75%; }
    #composer-status-root {
        width: 92%; max-width: 96; height: 22; max-height: 92%; padding: 1 2;
        background: $gucmail-modal-background;
    }
    #composer-status-heading { height: 2; border-bottom: solid $gucmail-divider; }
    #composer-status-brand { width: 13; color: $gucmail-logo-primary; }
    #composer-status-title { width: 1fr; text-style: bold; }
    #composer-status-body { height: 1fr; }
    #composer-status-outcome { height: auto; margin: 1 0; text-style: bold; }
    #composer-status-summary { height: auto; margin-bottom: 1; }
    #composer-status-technical { padding: 0; border: none; height: auto; }
    #composer-status-text { height: auto; }
    #composer-status-actions {
        height: 2; align-horizontal: right; border-top: solid $gucmail-divider;
    }
    #composer-status-actions Button { min-width: 10; width: auto; }
    """

    def __init__(
        self, reference: str, attempt: CompositionSendStatus | None, error: str = ""
    ) -> None:
        super().__init__(area_name="compose", offline=True)
        self.reference, self.attempt, self.error = reference, attempt, error

    def compose(self) -> ComposeResult:
        attempt = self.attempt
        summary = self.error or attempt_message(attempt.state if attempt else None)
        detail = f"Draft: {self.reference}"
        if attempt:
            label = "Job" if attempt.source == "scheduled" else "Attempt"
            detail += (
                f"\nState: {attempt.state}\nUpdated: {attempt.updated_at}"
                f"\n{label}: {attempt.attempt_id}"
            )
            if attempt.queue_state:
                detail += f"\nScheduled Mail state: {attempt.queue_state}"
        with Vertical(id="composer-status-root"):
            with Horizontal(id="composer-status-heading"):
                yield Label("GUC Mail", id="composer-status-brand")
                yield Label("Send status", id="composer-status-title")
            with VerticalScroll(id="composer-status-body"):
                title = "Status unavailable" if self.error else {
                    "sent": "Send accepted",
                    "sending": "Outcome unconfirmed",
                    "uncertain": "Outcome unconfirmed",
                    "failed": "Send rejected",
                    "queued": "Already in Scheduled Mail",
                }.get(attempt.state if attempt else "", "No send recorded")
                yield Label(title, id="composer-status-outcome")
                yield Static(sanitize_terminal(summary), id="composer-status-summary", markup=False)
                with Collapsible(title="Local record details", id="composer-status-technical"):
                    yield Static(sanitize_terminal(detail), id="composer-status-text", markup=False)
            with Horizontal(id="composer-status-actions"):
                yield Button("Close", classes="area-close")


class ComposerSubmissionSession(ModalScreen[dict[str, Any] | None]):
    """Standalone result dialogs keep their contract; app submissions stay mounted."""

    def __init__(self) -> None:
        super().__init__()
        self.submit_handler: Callable[[dict[str, Any]], None] | None = None
        self._submission_pending = False
        self._submission_complete = False
        self._submission_uncertain = False
        self._submission_controls: dict[Widget, bool] = {}
        self._submission_focus: Widget | None = None
        self._attempt_pending = False
        self._attempt_blocked = False
        self._review_pending = False
        self._attempt_state: str | None = None
        self._initial_review_requested = False
        self._attempt_generation = 0
        self._attempt_controls: dict[Widget, bool] = {}
        self._attempt_body_read_only: bool | None = None
        self._attempt_focus: Widget | None = None
        self._attempt_previous_note = ""
        self._save_preflight_pending = False
        self._send_review_open = False
        self._status_checked = False
        self._status_ready_action: Callable[[], None] | None = None

    def defer_status_action(self, action: Callable[[], None]) -> bool:
        """Keep one explicitly requested handoff until its local status is safe."""
        if getattr(self.app, "context", None) is None:
            return False
        if self.is_mounted and self._status_checked and not self._attempt_pending:
            return False
        if self._status_ready_action is None:
            self._status_ready_action = action
            self._initial_review_requested = False
        if self.is_mounted:
            self._submission_note("Checking local send status before the requested action.")
        return True

    def _run_status_ready_action(self) -> None:
        if self._attempt_blocked or self._submission_uncertain or self._submission_complete:
            self._status_ready_action = None
            return
        if not self.is_active or self._attempt_pending or self._submission_pending:
            return
        action, self._status_ready_action = self._status_ready_action, None
        if action is not None:
            action()

    def on_unmount(self) -> None:
        self._status_ready_action = None

    def composition_reference(self) -> str:
        raise NotImplementedError

    def action_review_send(self) -> None:
        raise NotImplementedError

    def request_initial_send_review(self) -> None:
        """Retain a Drafts Send handoff until readiness; never dispatch a send."""
        self._initial_review_requested = True
        self._schedule_initial_review()

    def _schedule_initial_review(self) -> None:
        if self._initial_review_requested and self.is_mounted:
            self.call_after_refresh(self._review_initial_when_ready)

    def _review_initial_when_ready(self) -> None:
        if not self._initial_review_requested or not self.is_mounted or not self.is_active:
            return
        if self._attempt_pending or self._review_pending or self._submission_pending:
            return
        if self._attempt_blocked or self._submission_uncertain or self._submission_complete:
            self._initial_review_requested = False
            return
        if not self.attachments_ready():
            return
        self._initial_review_requested = False
        self.action_review_send()

    def attachments_ready(self) -> bool:
        return True

    def _submission_note(self, text: str) -> None:
        self.query_one("#compose-note", Static).update(sanitize_terminal(text, max_length=1500))

    def refresh_submission_actions(self) -> None:
        self.query_one("#compose-save", Button).disabled = (
            self._submission_pending
            or self._submission_complete
            or self._submission_uncertain
            or self._attempt_pending
            or self._attempt_blocked
            or self._save_preflight_pending
        )
        self.query_one("#compose-send", Button).disabled = (
            self._submission_pending
            or self._submission_complete
            or self._submission_uncertain
            or self._attempt_pending
            or self._attempt_blocked
            or self._review_pending
            or not self.attachments_ready()
        )

    def set_save_preflight_pending(self, pending: bool) -> None:
        self._save_preflight_pending = pending
        self.refresh_submission_actions()

    def _submit_value(self, payload: dict[str, Any]) -> None:
        if self._submission_pending or self._submission_complete or self._submission_uncertain:
            return
        if self._attempt_pending or self._attempt_blocked:
            self._submission_note(
                "Local send status must be checked before changing this draft. Use Send status."
            )
            return
        if self.submit_handler is None:
            self.dismiss(payload)
            return
        self._submission_pending = True
        self._submission_focus = self.focused
        for control in self.query("Input, Select, Checkbox, TextArea, Button"):
            self._submission_controls[control] = control.disabled
            control.disabled = True
        self._submission_note(
            "Waiting for the result. Your composition remains here; do not submit it again."
        )
        self.submit_handler(payload)

    def submission_finished(self, outcome: SubmissionOutcome, message: str) -> None:
        self._submission_pending = False
        self._attempt_pending = False
        self._submission_uncertain = outcome == "uncertain"
        if not self.is_mounted:
            return
        for control, disabled in self._submission_controls.items():
            if control.is_mounted:
                control.disabled = disabled
        self._submission_controls.clear()
        self._submission_note(message)
        if outcome in {"saved", "sent", "queued"}:
            self._submission_complete = True
            if self.is_active:
                self.dismiss(None)
            return
        if self._submission_uncertain:
            self._lock_uncertain_input()
        else:
            self._restore_submission_focus()
        self.refresh_submission_actions()
        if not self._submission_uncertain:
            self.request_send_status()

    def _restore_submission_focus(self) -> None:
        control = self._submission_focus
        if self.is_active and control is not None and control.is_mounted and not control.disabled:
            self.set_focus(control, from_app_focus=True)
            self._submission_focus = None

    def _lock_uncertain_input(self) -> None:
        # Keep the exact attempted composition/recovery intact. The body stays
        # selectable and scrollable, but cannot be edited into a different send.
        for control in self.query("Input, Select, Checkbox, TextArea, Button"):
            control.disabled = control.id not in {"compose-cancel", "compose-send-status"}
        body = self.query_one("#compose-body", TextArea)
        body.disabled = False
        body.read_only = True
        self.query_one("#compose-cancel", Button).label = "Close"
        self.query_one("#compose-send-status", Button).display = True

    def _pause_for_send_status(self) -> None:
        if not self._attempt_controls:
            self._attempt_focus = self.focused
            self._attempt_previous_note = str(self.query_one("#compose-note", Static).content)
            for control in self.query("Input, Select, Checkbox, TextArea, Button"):
                self._attempt_controls[control] = control.disabled
                control.disabled = control.id not in {"compose-cancel", "compose-send-status"}
            body = self.query_one("#compose-body", TextArea)
            self._attempt_body_read_only = body.read_only
            body.disabled = False
            body.read_only = True
        self._submission_note("Checking local send status. Editing is paused until it is known.")

    def _restore_after_send_status(self) -> None:
        for control, disabled in self._attempt_controls.items():
            if control.is_mounted:
                control.disabled = disabled
        self._attempt_controls.clear()
        if self._attempt_body_read_only is not None:
            self.query_one("#compose-body", TextArea).read_only = self._attempt_body_read_only
            self._attempt_body_read_only = None
            self._submission_note(self._attempt_previous_note)
        focus, self._attempt_focus = self._attempt_focus, None
        if focus is not None and focus.is_mounted and not focus.disabled and self.is_active:
            self.set_focus(focus, from_app_focus=True)
        self.query_one("#compose-cancel", Button).label = "Cancel"

    def read_send_attempt(self) -> CompositionSendStatus | None:
        context = getattr(self.app, "context", None)
        return (
            SendAttemptStore(context.state).composition_status(self.composition_reference())
            if context
            else None
        )

    @work(thread=True, exclusive=True, group="compose-attempt-status", exit_on_error=False)
    def check_send_attempt(self, generation: int, show_details: bool = False) -> None:
        attempt, error = None, ""
        try:
            attempt = self.read_send_attempt()
        except Exception:
            error = "Local send status could not be checked. Your text is retained. "
            error += "Use Send status to check again before sending."
        self.app.call_from_thread(
            self.send_attempt_checked, attempt, error, show_details, generation
        )

    def send_attempt_checked(
        self, attempt: CompositionSendStatus | None, error: str = "", show_details: bool = False,
        generation: int | None = None,
    ) -> None:
        if not self.is_mounted or self._submission_pending or self._submission_complete:
            return
        if generation is not None and generation != self._attempt_generation:
            return
        self._attempt_pending = False
        self._status_checked = True
        self._attempt_state = attempt.state if attempt else None
        self._attempt_blocked = bool(error) or bool(attempt and attempt.state != "failed")
        if self._submission_uncertain and attempt is None and not error:
            error = "The previous outcome is still unconfirmed. Do not resend. "
            error += "Review the retained recovery and Sent Items."
            self._attempt_blocked = True
        self.query_one("#compose-send-status", Button).display = bool(
            attempt or error or self._submission_uncertain
        )
        if attempt and attempt.state != "failed":
            self._submission_uncertain = True
        if self._attempt_blocked or self._submission_uncertain:
            self._lock_uncertain_input()
            self._submission_note(error or attempt_message(attempt.state if attempt else None))
        else:
            self._restore_after_send_status()
        self.refresh_submission_actions()
        self.call_after_refresh(self._run_status_ready_action)
        if show_details:
            self.app.push_screen(ComposerSendStatus(self.composition_reference(), attempt, error))
        else:
            self._schedule_initial_review()

    def request_send_status(self, *, show_details: bool = False) -> None:
        if self._submission_pending or self._submission_complete:
            return
        self._attempt_generation += 1
        self._attempt_pending = True
        self._pause_for_send_status()
        self.refresh_submission_actions()
        self.check_send_attempt(self._attempt_generation, show_details)

    def on_screen_resume(self) -> None:
        if self._submission_complete:
            self.call_after_refresh(self._dismiss_completed)
        elif not self._submission_pending:
            self._restore_submission_focus()
            if (
                self.is_mounted and getattr(self.app, "context", None) is not None
                and not self._send_review_open
            ):
                self.request_send_status()

    def _dismiss_completed(self) -> None:
        # Returning AwaitComplete would make Textual await dismissal from this
        # screen's own message handler. Invoke it and return None.
        if self.is_active and self._submission_complete:
            self.dismiss(None)
