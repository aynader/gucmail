"""Keep service-backed editor input until the write has a known outcome."""

from collections.abc import Callable
from typing import Any, TypeVar

from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    GUCmailError,
    ItemNotFoundError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.tui.area_common import (
    BlockingOperation,
    OperationalScreen,
    OperationSuccess,
    PublicOperationError,
    clean_cell,
)

Value = TypeVar("Value")


class SubmissionEditor(ModalScreen[Value]):
    """Standalone result dialogs remain compatible; service submissions stay open."""

    def __init__(self) -> None:
        super().__init__()
        self.submit_handler: Callable[[Value], None] | None = None
        self._save_pending = False
        self._save_uncertain = False
        self._save_complete = False
        self._submission_controls: dict[Widget, bool] = {}
        self._submission_focus: Widget | None = None

    def _submission_status(self, message: str) -> None:
        for status in self.query(Static):
            if status.id and status.id.endswith("-editor-error"):
                status.update(clean_cell(message, max_length=1_000))
                status.scroll_visible(animate=False)

    def _restore_submission_controls(self) -> None:
        for widget, disabled in self._submission_controls.items():
            if widget.is_mounted:
                widget.disabled = disabled
        self._submission_controls.clear()
        self._restore_submission_focus()

    def _restore_submission_focus(self) -> None:
        widget = self._submission_focus
        if self.is_active and widget is not None and widget.is_mounted and not widget.disabled:
            # Restoring the same editing session must not trigger Input's
            # ordinary select-all-on-focus behavior or move its cursor.
            self.set_focus(widget, from_app_focus=True)
            self._submission_focus = None

    def _submit_value(self, value: Value) -> None:
        if self._save_pending or self._save_uncertain or self._save_complete:
            return
        if self.submit_handler is None:
            self.dismiss(value)
            return
        self._save_pending = True
        self._submission_focus = self.focused
        for widget in self.query("Input, Select, TextArea, Checkbox, Button"):
            self._submission_controls[widget] = widget.disabled
            widget.disabled = True
        self._submission_status("Saving… Your edits remain here until the result is known.")
        self.submit_handler(value)

    def submission_failed(self, message: str, *, uncertain: bool) -> None:
        self._save_pending = False
        self._save_uncertain = uncertain
        self._restore_submission_controls()
        if uncertain:
            for button in self.query(Button):
                if button.id and button.id.endswith("-edit-save"):
                    button.disabled = True
                elif button.id and button.id.endswith("-edit-cancel"):
                    button.label = "Close"
            message += " The change may have completed; check it before repeating it."
        self._submission_status(message + " Your edits are retained in this window.")

    def submission_cancelled(self) -> None:
        self._save_pending = False
        self._restore_submission_controls()
        self._submission_status("Save cancelled. Your edits are retained.")

    def submission_completed(self) -> None:
        self._save_pending = False
        self._save_complete = True
        self._restore_submission_controls()
        self._submission_status("Saved.")
        if self.is_active:
            self.dismiss(None)

    def on_screen_resume(self) -> None:
        if self._save_complete:
            self.call_after_refresh(self._dismiss_completed)
        elif not self._save_pending:
            self.call_after_refresh(self._restore_submission_focus)

    def _cancel_edit(self) -> None:
        if self._save_pending:
            self._submission_status("Waiting for the save result. Closing cannot undo the write.")
            return
        self.dismiss(None)

    def _dismiss_completed(self) -> None:
        if self.is_active and self._save_complete:
            self.dismiss(None)


class SubmissionWorkspace(OperationalScreen):
    """Connect the existing worker/confirmation pipeline to its visible editor."""

    _submission_editor: SubmissionEditor[Any] | None = None

    def _open_submission_editor(
        self, editor: SubmissionEditor[Value], submit: Callable[[Value], None]
    ) -> None:
        def submitted(value: Value) -> None:
            self._submission_editor = editor
            generation = self._operation_generation
            try:
                submit(value)
            except Exception as error:
                self._show_error("Save", error)
            else:
                if generation == self._operation_generation and self.app.screen is editor:
                    # A shared write lease may reject the action before dispatch.
                    editor.submission_failed(
                        "Save did not start. Check pending operations.", uncertain=False
                    )
                    self._submission_editor = None

        def closed(_value: Value | None) -> None:
            if self._submission_editor is editor:
                self._submission_editor = None

        editor.submit_handler = submitted
        self.app.push_screen(editor, closed)

    def _begin_operation(
        self,
        action: str,
        operation: BlockingOperation,
        on_success: OperationSuccess,
        *,
        timeout_seconds: float | None = None,
        remember: bool = True,
        retry_safe: bool = False,
    ) -> None:
        editor = self._submission_editor if not retry_safe else None

        def acknowledged(result: object) -> None:
            on_success(result)  # Keep input if result validation itself fails.
            if editor is not None:
                editor.submission_completed()
                if self._submission_editor is editor:
                    self._submission_editor = None

        super()._begin_operation(
            action,
            operation,
            acknowledged if editor else on_success,
            timeout_seconds=timeout_seconds,
            remember=remember,
            retry_safe=retry_safe,
        )

    def _show_error(self, action: str, error: BaseException) -> None:
        super()._show_error(action, error)
        editor = self._submission_editor
        if editor is not None and editor.is_mounted:
            definite = isinstance(
                error,
                (
                    AuthenticationError,
                    ConfigurationError,
                    ConflictError,
                    ItemNotFoundError,
                    ThrottledError,
                    UnsupportedFeatureError,
                    PublicOperationError,
                ),
            ) or (isinstance(error, GUCmailError) and error.context.retry_safe is True)
            uncertain = not definite or (
                isinstance(error, GUCmailError) and error.context.mutation_uncertain
            )
            status = self.query_one("#area-state", Static)
            editor.submission_failed(str(status.content), uncertain=uncertain)
            self._submission_editor = None

    def _operation_timed_out(self, generation: int, action: str) -> None:
        current = generation == self._operation_generation and self._operation_active
        super()._operation_timed_out(generation, action)
        if current and self._submission_editor is not None:
            self._submission_editor.submission_failed("Save timed out.", uncertain=True)
            self._submission_editor = None

    def action_cancel_operation(self) -> None:
        pending = self._operation_active
        super().action_cancel_operation()
        if pending and self._submission_editor is not None:
            self._submission_editor.submission_failed(
                "Stopped waiting for the save.", uncertain=True
            )
            self._submission_editor = None

    def _confirm(
        self,
        title: str,
        detail: str,
        confirmed: Callable[[], None],
        *,
        confirm_label: str = "Confirm",
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        editor = self._submission_editor

        def cancelled() -> None:
            if editor is not None:
                editor.submission_cancelled()
                self._submission_editor = None
            if on_cancel is not None:
                on_cancel()

        super()._confirm(title, detail, confirmed, confirm_label=confirm_label, on_cancel=cancelled)
