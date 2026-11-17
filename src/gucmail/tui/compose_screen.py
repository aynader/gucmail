"""The message composer."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Static,
    TextArea,
)

from gucmail.errors import (
    ConfigurationError,
)
from gucmail.formatting import human_bytes
from gucmail.models import (
    Address,
    AttachmentInfo,
    DraftData,
    Importance,
)
from gucmail.security import html_to_safe_text, sanitize_terminal
from gucmail.services.compose import (
    ComposeKind,
    ComposeService,
    ComposeValidation,
    ComposeValidationError,
)
from gucmail.services.compose_preferences import ComposerPreferences
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _compose_attachment_paths
from gucmail.tui.composer_submission import (
    ComposerSubmissionSession,
)
from gucmail.tui.dialogs import ConfirmationScreen
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.pim_screens import (
    FilePathScreen,
)
from gucmail.tui.write_coordinator import (
    PENDING_CHANGE_MESSAGE,
    app_write_coordinator,
)

if TYPE_CHECKING:
    pass


class ComposerInput(Input):
    """Input that treats Backspace on an empty field as modal navigation."""

    def action_delete_left(self) -> None:
        if not self.value and isinstance(self.screen, ComposeScreen):
            self.screen.action_close()
            return
        super().action_delete_left()


class ComposerTextArea(TextArea):
    """Composer body with the same empty-Backspace navigation convention."""

    def action_delete_left(self) -> None:
        if not self.text and isinstance(self.screen, ComposeScreen):
            self.screen.action_close()
            return
        super().action_delete_left()


def _encode_compose_attachment_paths(values: Sequence[str]) -> str:
    if any(";" in value or "\n" in value or value.startswith("[") for value in values):
        return json.dumps(list(values), ensure_ascii=False)
    return "; ".join(values)


class ComposeScreen(ComposerSubmissionSession):
    USES_GLOBAL_PALETTE_SHORTCUTS: ClassVar = True
    DEFAULT_CSS = """
    ComposeScreen { align: center middle; }
    #composer {
        width: 96%; max-width: 120; height: 96%; border: round $border;
        background: $gucmail-modal-background; padding: 1 2;
    }
    #compose-fields { height: 1fr; }
    #composer TextArea { height: 1fr; min-height: 8; }
    #compose-note { height: auto; min-height: 1; color: $text-muted; }
    #compose-inherited { height: auto; margin-bottom: 1; }
    #compose-actions { height: 3; align-horizontal: right; }
    #compose-actions Button {
        width: auto; min-width: 8; height: 3; margin-left: 1; padding: 0 1;
    }
    #compose-actions #compose-save { width: 14; }
    #compose-actions #compose-send-later { width: 14; }
    #compose-actions #compose-send { width: 16; }
    #compose-importance { width: 14; }
    #compose-options { height: 3; }
    #compose-receipts { height: 3; width: 1fr; }
    #compose-options Select { width: 22; margin-right: 1; }
    #compose-options Checkbox { width: auto; margin-right: 2; }
    #compose-sent-copy { height: 3; width: auto; }
    .compose-label { width: 8; padding-top: 1; }
    #compose-extra-addresses {
        height: 1; min-height: 1; border: none; width: auto; margin-left: 8;
    }
    #compose-attachment-controls { height: 3; }
    #compose-attachment-controls Select { width: 1fr; }
    #compose-attachment-controls Button { width: 14; min-width: 0; }
    #compose-attachments { display: none; }
    .compose-row { height: 3; }
    .compose-row Input { width: 1fr; }
    ComposeScreen.compact-compose #compose-options { layout: vertical; height: 6; }
    ComposeScreen.compact-compose #compose-body-mode { width: 100%; }
    ComposeScreen.compact-compose #compose-importance { width: 14; }
    ComposeScreen.compact-compose #compose-actions Button { min-width: 6; margin-left: 1; }
    ComposeScreen.compact-compose #compose-actions #compose-save { width: 12; }
    ComposeScreen.compact-compose #compose-actions #compose-send-later { width: 10; }
    ComposeScreen.compact-compose #compose-actions #compose-send { width: 13; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("backspace", "close", "Close", show=False),
    ]

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        event.widget.scroll_visible(animate=False, force=True, immediate=True)

    def on_resize(self, event: events.Resize) -> None:
        self._set_compact(event.size.width < 85)
        self.call_after_refresh(self._reveal_focus)

    def _reveal_focus(self) -> None:
        if self.focused is not None:
            self.focused.scroll_visible(animate=False, force=True, immediate=True)

    def __init__(
        self,
        draft: DraftData | None = None,
        *,
        kind: ComposeKind = ComposeKind.NEW,
        inherited_attachments: tuple[AttachmentInfo, ...] | None = None,
    ) -> None:
        super().__init__()
        candidate = draft or DraftData(reference=f"dft-{uuid4().hex}")
        self.draft = (
            replace(candidate, reference=f"dft-{uuid4().hex}")
            if candidate.reference.startswith("preview:")
            else candidate
        )
        self.kind = (
            ComposeKind.FORWARD
            if self.draft.forward_of_reference
            else ComposeKind.REPLY
            if self.draft.reply_to_reference and kind is ComposeKind.NEW
            else kind
        )
        self.inherited_attachments = (
            inherited_attachments if self.draft.forward_of_reference else ()
        )

    @staticmethod
    def _addresses(values: list[Address]) -> str:
        return ", ".join(value.email for value in values)

    def composition_reference(self) -> str:
        return self.draft.reference

    def attachments_ready(self) -> bool:
        return self.inherited_attachments is not None

    def compose(self) -> ComposeResult:
        with Vertical(id="composer"):
            with Horizontal(id="compose-heading"):
                yield Label("GUC Mail", id="compose-brand")
                yield Label("Compose message", classes="dialog-title")
                yield Button("Options", id="compose-options-toggle")
                yield Button("Send status", id="compose-send-status")
            with VerticalScroll(id="compose-fields"):
                with Horizontal(classes="compose-row"):
                    yield Label("To", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.to), id="compose-to")
                yield Button("Show Cc/Bcc", id="compose-extra-addresses")
                with Horizontal(classes="compose-row", id="compose-cc-row"):
                    yield Label("Cc", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.cc), id="compose-cc")
                with Horizontal(classes="compose-row", id="compose-bcc-row"):
                    yield Label("Bcc", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.bcc), id="compose-bcc")
                with Horizontal(classes="compose-row"):
                    yield Label("Subject", classes="compose-label")
                    yield ComposerInput(value=self.draft.subject, id="compose-subject")
                yield ComposerInput(
                    value=_encode_compose_attachment_paths(
                        [str(path) for path in self.draft.attachment_paths]
                    ),
                    id="compose-attachments",
                )
                with Horizontal(id="compose-attachment-controls"):
                    yield Select(
                        (("No local files attached", ""),),
                        value="",
                        allow_blank=False,
                        id="compose-attachment-list",
                    )
                    yield Button("Attach file", id="compose-attachment-add")
                    yield Button("Remove", id="compose-attachment-remove", disabled=True)
                yield Static(
                    self._inherited_attachment_text(), id="compose-inherited", markup=False
                )
                with Vertical(id="compose-options-area"):
                    with Horizontal(id="compose-options"):
                        yield Select(
                            (("Plain text", "plain"), ("HTML source", "html")),
                            value="html" if self.draft.body_html is not None else "plain",
                            allow_blank=False,
                            id="compose-body-mode",
                        )
                        with Horizontal(id="compose-receipts"):
                            yield Checkbox(
                                self._receipt_label("Read", self.draft.request_read_receipt),
                                value=self.draft.request_read_receipt,
                                id="compose-read-receipt",
                            )
                            yield Checkbox(
                                self._receipt_label(
                                    "Delivery", self.draft.request_delivery_receipt
                                ),
                                value=self.draft.request_delivery_receipt,
                                id="compose-delivery-receipt",
                            )
                    with Horizontal(id="compose-delivery-options"):
                        yield Select(
                            [
                                ("Normal importance", "normal"),
                                ("High importance", "high"),
                                ("Low importance", "low"),
                            ],
                            value=self.draft.importance.value,
                            allow_blank=False,
                            id="compose-importance",
                        )
                        yield Checkbox(
                            f"Sent copy: {'on' if self.draft.save_sent_copy else 'off'}",
                            value=self.draft.save_sent_copy,
                            id="compose-sent-copy",
                            tooltip="Save a copy in Exchange Sent Items after sending. "
                            "Your choice is retained in local drafts, recovery and scheduled jobs.",
                        )
                yield Label("Message", classes="compose-body-label")
                yield ComposerTextArea(
                    self.draft.body_html
                    if self.draft.body_html is not None
                    else self.draft.body_text,
                    id="compose-body",
                )
            yield Static("", id="compose-note", markup=False)
            with Horizontal(id="compose-actions"):
                yield Button("Cancel", id="compose-cancel")
                yield Button("Editor", id="compose-external-editor")
                yield Button("Save draft", id="compose-save")
                yield Button("Send later", id="compose-send-later")
                yield Button("Review send", variant="primary", id="compose-send")

    def _set_compact(self, compact: bool) -> None:
        self.set_class(compact, "compact-compose")
        self.query_one("#compose-send-later", Button).label = "Later" if compact else "Send later"

    def on_mount(self) -> None:
        self._set_compact(self.size.width < 85)
        self._show_extra_addresses(bool(self.draft.cc or self.draft.bcc))
        self._show_options(
            self.draft.body_html is not None
            or self.draft.importance is not Importance.NORMAL
            or self.draft.request_read_receipt
            or self.draft.request_delivery_receipt
            or not self.draft.save_sent_copy
        )
        self._refresh_attachment_list()
        self.query_one("#compose-inherited").display = bool(self.draft.forward_of_reference)
        self.query_one("#compose-send-status", Button).display = False
        self.refresh_submission_actions()
        if self.inherited_attachments is None and isinstance(self.app, AppCore):
            self.app.load_forward_attachments(self)
        if isinstance(self.app, AppCore):
            self.request_send_status()
            for shortcut in (
                self.app._palette_bound_shortcuts if self.USES_GLOBAL_PALETTE_SHORTCUTS else ()
            ):
                self._bindings.bind(shortcut, "app.palette", "Commands", show=False, priority=True)
            self._refresh_recipient_suggestions(refresh_values=False)
            self.set_interval(
                self.app.context.config.autosave_seconds,
                self._request_autosave,
            )

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            self._refresh_recipient_suggestions()

    def _refresh_recipient_suggestions(self, *, refresh_values: bool = True) -> None:
        from gucmail.services.recipient_assistance import RecipientPreferences
        from gucmail.tui.recipient_suggester import RecipientSuggester

        if not isinstance(self.app, AppCore):
            return
        try:
            policy = RecipientPreferences.from_settings(self.app._settings_service())
        except (ConfigurationError, OSError, ValueError):
            policy = RecipientPreferences(autocomplete=False)
        for name in ("to", "cc", "bcc"):
            field = self.query_one(f"#compose-{name}", Input)
            previous = field.suggester
            enabled = policy.autocomplete and policy.recent_recipients
            if (isinstance(previous, RecipientSuggester) and previous.preferences == policy) or (
                previous is None and not enabled
            ):
                continue
            field.suggester = RecipientSuggester(policy) if enabled else None
            if refresh_values:
                field.mutate_reactive(Input.value)

    def _payload(self, *, action: str) -> dict[str, Any]:
        body = self.query_one("#compose-body", TextArea).text
        body_mode = str(self.query_one("#compose-body-mode", Select).value)
        return {
            "recovery_sequence": perf_counter_ns(),
            "reference": self.draft.reference,
            "revision": self.draft.revision,
            "location": self.draft.location.value,
            "server_id": self.draft.server_id,
            "server_change_key": self.draft.server_change_key,
            "to": self.query_one("#compose-to", Input).value,
            "cc": self.query_one("#compose-cc", Input).value,
            "bcc": self.query_one("#compose-bcc", Input).value,
            "subject": self.query_one("#compose-subject", Input).value,
            "body": html_to_safe_text(body) if body_mode == "html" else body,
            "body_html": body if body_mode == "html" else None,
            "body_mode": body_mode,
            "attachment_paths": self.query_one("#compose-attachments", Input).value,
            "request_read_receipt": self.query_one("#compose-read-receipt", Checkbox).value,
            "request_delivery_receipt": self.query_one("#compose-delivery-receipt", Checkbox).value,
            "save_sent_copy": self.query_one("#compose-sent-copy", Checkbox).value,
            "importance": self.query_one("#compose-importance", Select).value,
            "action": action,
            "kind": self.kind.value,
            "reply_to_reference": self.draft.reply_to_reference,
            "forward_of_reference": self.draft.forward_of_reference,
        }

    def _request_autosave(self) -> None:
        if (
            self._submission_pending
            or self._submission_uncertain
            or self._submission_complete
            or self._attempt_pending
            or self._attempt_blocked
        ):
            return
        payload = self._payload(action="autosave")
        content_fields = ("to", "cc", "bcc", "subject", "body")
        if (
            any(str(payload[field]).strip() for field in content_fields)
            and isinstance(self.app, AppCore)
            and bool(self.app._setting_value("composer.local_recovery", True))
        ):
            self.app.autosave_composer(payload)

    def _has_unsaved_changes(self) -> bool:
        """Compare editor values with the draft that opened the composer."""

        payload = self._payload(action="compare")
        original = {
            "to": self._addresses(self.draft.to),
            "cc": self._addresses(self.draft.cc),
            "bcc": self._addresses(self.draft.bcc),
            "subject": self.draft.subject,
            "body": self.draft.body_text,
            "importance": self.draft.importance.value,
            "body_html": self.draft.body_html,
            "attachment_paths": _encode_compose_attachment_paths(
                [str(path) for path in self.draft.attachment_paths]
            ),
            "request_read_receipt": self.draft.request_read_receipt,
            "request_delivery_receipt": self.draft.request_delivery_receipt,
            "save_sent_copy": self.draft.save_sent_copy,
        }
        return any(str(payload[key]) != str(value) for key, value in original.items())

    def _set_note(self, value: str) -> None:
        safe = sanitize_terminal(value, max_length=500)
        if self._attempt_controls:
            # A newer action/validation warning must survive the pending status
            # placeholder. Status failures and uncertain outcomes still take priority.
            self._attempt_previous_note = safe
        self.query_one("#compose-note", Static).update(safe)

    def _show_extra_addresses(self, visible: bool) -> None:
        self.query_one("#compose-cc-row").display = visible
        self.query_one("#compose-bcc-row").display = visible
        self.query_one("#compose-extra-addresses", Button).label = (
            "Hide Cc/Bcc" if visible else "Show Cc/Bcc"
        )

    @on(Input.Changed, "#compose-attachments")
    def _attachments_changed(self) -> None:
        self._refresh_attachment_list()

    def _refresh_attachment_list(self) -> None:
        values = _compose_attachment_paths(self.query_one("#compose-attachments", Input).value)
        listing = self.query_one("#compose-attachment-list", Select)
        previous = listing.value
        listing.set_options(
            [(sanitize_terminal(Path(value).name), value) for value in values]
            or [("No local files attached", "")]
        )
        listing.value = previous if previous in values else values[-1] if values else ""
        self.query_one("#compose-attachment-remove", Button).disabled = not values
        listing.display = bool(values)
        self.query_one("#compose-attachment-remove", Button).display = bool(values)
        self.query_one("#compose-attachment-controls").set_class(bool(values), "has-files")

    def _show_options(self, visible: bool) -> None:
        self.query_one("#compose-options-area").display = visible
        self.query_one("#compose-options-toggle", Button).label = (
            "Hide options" if visible else "Options"
        )

    def action_toggle_options(self) -> None:
        self._show_options(not self.query_one("#compose-options-area").display)

    def action_add_attachment(self) -> None:
        if self._guard_running_change():
            return

        def selected(value: str | None) -> None:
            if value:
                self.add_attachment_path(Path(value).expanduser())

        self.app.push_screen(FilePathScreen("Add attachment", "Path to a local file"), selected)

    def action_remove_attachment(self) -> None:
        if self._guard_running_change():
            return
        field = self.query_one("#compose-attachments", Input)
        selected = self.query_one("#compose-attachment-list", Select).value
        values = _compose_attachment_paths(field.value)
        if selected in values:
            values.remove(str(selected))
            field.value = _encode_compose_attachment_paths(values)
            self._set_note(
                f"Removed {sanitize_terminal(Path(str(selected)).name)} from this draft."
            )
        else:
            self._set_note("This draft has no local file attachment to remove.")

    def add_attachment_path(self, path: Path) -> None:
        if self.defer_status_action(lambda: self.add_attachment_path(path)):
            return
        if self._guard_running_change():
            return
        field = self.query_one("#compose-attachments", Input)
        values = _compose_attachment_paths(field.value)
        resolved = str(path)
        if resolved not in values:
            values.append(resolved)
        field.value = _encode_compose_attachment_paths(values)
        self._set_note(f"Attached {path.name}.")

    def action_toggle_html_plain_text(self) -> None:
        if self._guard_running_change():
            return
        selector = self.query_one("#compose-body-mode", Select)
        body = self.query_one("#compose-body", TextArea)
        if selector.value == "html":
            body.text = html_to_safe_text(body.text)
            selector.value = "plain"
            self._set_note("Converted HTML to safe plain text without fetching resources.")
        else:
            body.text = f"<pre>{html.escape(body.text)}</pre>"
            selector.value = "html"
            self._set_note("Converted plain text to escaped HTML.")

    def action_request_read_receipt(self) -> None:
        if self._guard_running_change():
            return
        control = self.query_one("#compose-read-receipt", Checkbox)
        control.value = not control.value

    @staticmethod
    def _receipt_label(kind: str, enabled: bool) -> str:
        return f"{kind} receipt: {'on' if enabled else 'off'}"

    @on(Checkbox.Changed, "#compose-read-receipt, #compose-delivery-receipt")
    def _receipt_changed(self, event: Checkbox.Changed) -> None:
        kind = "Read" if event.checkbox.id == "compose-read-receipt" else "Delivery"
        event.checkbox.label = self._receipt_label(kind, event.value)

    @on(Checkbox.Changed, "#compose-sent-copy")
    def _sent_copy_changed(self, event: Checkbox.Changed) -> None:
        event.checkbox.label = f"Sent copy: {'on' if event.value else 'off'}"

    def action_request_delivery_receipt(self) -> None:
        if self._guard_running_change():
            return
        control = self.query_one("#compose-delivery-receipt", Checkbox)
        control.value = not control.value

    def action_open_external_editor(self) -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, AppCore):
            self.app.open_composer_external_editor(self, self._payload(action="external-editor"))

    def action_forward_as_attachment(self) -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, AppCore):
            self.app.attach_selected_message_to_composer(self)

    def action_send_later(self) -> None:
        """Ask when, then schedule this message. The exact time is shown before saving."""

        from gucmail.tui.drafts_attachments_screen import DraftScheduleScreen

        def chosen(expression: str | None) -> None:
            if expression:
                self.action_schedule_draft(expression=expression)

        self.app.push_screen(
            DraftScheduleScreen(
                "",
                title="Send later",
                help_text=(
                    "When should it go? For example 'tomorrow 09:00', 'in 2 hours' or "
                    "'2026-08-06 09:00'. You'll see the exact time before it's saved."
                ),
            ),
            chosen,
        )

    def action_schedule_draft(self, *, expression: str = "tomorrow 09:00") -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, AppCore):
            self.app.schedule_composer_draft(
                self,
                self._payload(action="schedule"),
                expression=expression,
            )

    def apply_external_draft(self, draft: DraftData) -> None:
        if self.defer_status_action(lambda: self.apply_external_draft(draft)):
            return
        if self._guard_running_change():
            return
        self.draft = draft
        self.query_one("#compose-body-mode", Select).value = "plain"
        self.query_one("#compose-body", TextArea).text = draft.body_text
        self._set_note("External editor changes were saved to the recoverable local draft.")

    def _inherited_attachment_text(self) -> str:
        if self.inherited_attachments is None:
            return "Checking original message attachments before send review…"
        if not self.inherited_attachments:
            return "Original message: no inherited attachments."
        rows = [f"Included from original ({len(self.inherited_attachments)}):"]
        rows.extend(
            f"  {sanitize_terminal(item.name)} · {human_bytes(item.size)}"
            for item in self.inherited_attachments
        )
        return "\n".join(rows)

    def show_forward_attachments(self, attachments: tuple[AttachmentInfo, ...]) -> None:
        self.inherited_attachments = attachments
        self.query_one("#compose-inherited", Static).update(self._inherited_attachment_text())
        self.refresh_submission_actions()
        self._schedule_initial_review()

    def _guard_running_change(self) -> bool:
        if self._submission_pending:
            self._set_note("Waiting for this submission's result. Your composition remains here.")
            return True
        if self._attempt_pending:
            self._submission_note("Checking local send status. Wait before changing this draft.")
            return True
        if self._submission_uncertain or self._attempt_blocked:
            self._set_note(
                "This composition is read-only while its send outcome needs review. "
                "Use Send status."
            )
            return True
        if isinstance(self.app, AppCore) and app_write_coordinator(self.app).pending:
            self._set_note(PENDING_CHANGE_MESSAGE + " The composer remains open.")
            return True
        return False

    def action_review_send(self) -> None:
        """Show the same final send review used by the composer button."""

        if self._guard_running_change():
            return
        if self._attempt_pending or self._review_pending:
            return
        if self.inherited_attachments is None:
            self._set_note(
                "Original attachment details are unavailable. "
                "Reopen the draft online before sending."
            )
            return
        payload = self._payload(action="send")
        self._review_pending = True
        self.refresh_submission_actions()
        self._set_note("Checking recipients, message and attachment files…")
        self._validate_send(payload)

    @work(thread=True, exclusive=True, group="compose-review", exit_on_error=False)
    def _validate_send(self, payload: dict[str, Any]) -> None:
        validation = None
        error = ""
        try:
            attempt = self.read_send_attempt()
        except Exception:
            self.app.call_from_thread(self._review_status_unavailable)
            return
        if attempt and attempt.state != "failed":
            self.app.call_from_thread(self._review_blocked_by_attempt, attempt)
            return
        try:
            service = ComposeService()
            if isinstance(self.app, AppCore):
                service = ComposeService(
                    max_attachment_bytes=self.app.context.config.max_attachment_bytes,
                    preferences=ComposerPreferences.from_settings(self.app._settings_service()),
                )
            from gucmail.tui.app_compose import ComposeMixin  # imports this module

            plan = service.plan_send(
                ComposeMixin._draft_from_payload(payload),
                kind=self.kind,
                source_has_attachments=bool(self.inherited_attachments),
            )
            validation = plan.validation
            payload["confirmation_scope"] = plan.confirmation_scope
            payload["allow_empty_subject"] = service.preferences.allow_empty_subject
        except ComposeValidationError as exc:
            validation = exc.validation
        except Exception:
            error = "Could not validate the message. Check addresses and attachment paths."
        self.app.call_from_thread(self._send_checked, payload, validation, error)

    def _review_blocked_by_attempt(self, attempt: Any) -> None:
        self._review_pending = False
        self._attempt_generation += 1
        self.send_attempt_checked(attempt)

    def _review_status_unavailable(self) -> None:
        self._review_pending = False
        self._attempt_generation += 1
        self.send_attempt_checked(
            None,
            "Local send status could not be checked. "
            "Your input is retained. Use Send status to check again.",
        )

    def _reviewed_content(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._payload(action="send").items()
            if key != "recovery_sequence"
        }

    def _send_checked(
        self, payload: dict[str, Any], validation: ComposeValidation | None, error: str
    ) -> None:
        if not self.is_mounted:
            return
        self._review_pending = False
        self.refresh_submission_actions()
        reviewed_input = {
            key: value
            for key, value in payload.items()
            if key not in {"confirmation_scope", "allow_empty_subject", "recovery_sequence"}
        }
        if reviewed_input != self._reviewed_content():
            self._set_note("The message changed during validation. Review send again when ready.")
            return
        if error or validation is None:
            self._set_note(error)
            return
        if validation.errors:
            self._set_note("\n".join(issue.message for issue in validation.errors))
            field = validation.errors[0].field or "to"
            target = {"recipients": "to", "attachments": "attachment-add"}.get(field, field)
            matches = self.query(f"#compose-{target}")
            if matches:
                matches.first().focus()
            return
        if self._guard_running_change():
            return
        self._set_note("")
        recipient_count = sum(
            len(values)
            for values in (validation.draft.to, validation.draft.cc, validation.draft.bcc)
        )
        detail = (
            f"Recipients: {recipient_count}\nSubject: "
            f"{sanitize_terminal(payload['subject']) or '(empty)'}\n\n"
            f"Format: {'HTML' if validation.draft.body_html is not None else 'Plain text'}\n"
            f"Importance: {validation.draft.importance.value}\n"
            f"Read receipt: {'requested' if validation.draft.request_read_receipt else 'off'}\n"
            "Delivery receipt: "
            f"{'requested' if validation.draft.request_delivery_receipt else 'off'}\n\n"
            f"Sent Items copy: {'save' if validation.draft.save_sent_copy else 'none'}\n\n"
            "The message cannot be recalled by the local Undo Send feature after Exchange "
            "accepts it."
        )
        for label, values in (
            ("To", validation.draft.to),
            ("Cc", validation.draft.cc),
            ("Bcc", validation.draft.bcc),
        ):
            if values:
                detail += f"\n{label}: " + ", ".join(
                    sanitize_terminal(value.email) for value in values
                )
        local_paths = [
            Path(value).name
            for value in _compose_attachment_paths(str(payload["attachment_paths"]))
        ]
        detail += f"\n\nLocal attachments ({len(local_paths)}): " + (
            ", ".join(sanitize_terminal(name) for name in local_paths) or "none"
        )
        if self.draft.forward_of_reference:
            detail += "\n" + self._inherited_attachment_text()
        if validation.warnings:
            detail += "\n\n" + "\n".join(issue.message for issue in validation.warnings)
        if isinstance(self.app, AppCore):
            delay = int(str(self.app._setting_value("composer.undo_send_seconds", 0)))
            if delay:
                detail += (
                    f"\nLocal Undo Send delay: {delay} seconds. Cancel from Scheduled Mail. "
                    "If you close the app, the queued message needs a running scheduler."
                )

        def confirmed(value: bool | None) -> None:
            self._send_review_open = False
            if value:
                if self._guard_running_change():
                    return
                if reviewed_input != self._reviewed_content():
                    self._set_note(
                        "The message changed after review. Review send again when ready."
                    )
                    return
                self._submit_value(payload)
            elif isinstance(self.app, AppCore):
                self.request_send_status()

        self._send_review_open = True
        self.app.push_screen(
            ConfirmationScreen("Send this message?", detail, confirm_label="Send message"),
            confirmed,
        )

    def action_close(self) -> None:
        """Close safely, preserving and confirming edited content."""

        if self._submission_pending:
            self._set_note(
                "Waiting for the result. Closing cannot cancel an already started write."
            )
            return
        if self._submission_uncertain:

            def close_review(value: bool | None) -> None:
                if value:
                    self.dismiss(None)

            self.app.push_screen(
                ConfirmationScreen(
                    "Close this send review?",
                    (
                        "This draft has a Scheduled Mail entry. Closing does not cancel that job. "
                        "Review Scheduled Mail to edit or cancel it."
                        if self._attempt_state == "queued"
                        else "The prior send may have completed. Closing does not cancel it. "
                        "Your exact recovery copy remains available. Review Send status and "
                        "Sent Items before another send."
                    ),
                    confirm_label="Close review",
                ),
                close_review,
            )
            return
        if not self._has_unsaved_changes():
            self.dismiss(None)
            return
        payload = self._payload(action="close-recovery")
        if not isinstance(self.app, AppCore) or not self.app.recover_composer_before_close(payload):
            return

        def confirmed(value: bool | None) -> None:
            if value:
                self.dismiss(None)

        self.app.push_screen(
            ConfirmationScreen(
                "Close composer?",
                "Your unsent changes were saved to local recovery. Close this composer?",
            ),
            confirmed,
        )

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "compose-send-status":
            self.request_send_status(show_details=True)
            return
        if event.button.id == "compose-extra-addresses":
            self._show_extra_addresses(not self.query_one("#compose-cc-row").display)
            return
        if event.button.id == "compose-attachment-add":
            self.action_add_attachment()
            return
        if event.button.id == "compose-options-toggle":
            self.action_toggle_options()
            return
        if event.button.id == "compose-attachment-remove":
            self.action_remove_attachment()
            return
        if event.button.id == "compose-cancel":
            self.action_close()
            return
        if event.button.id == "compose-send":
            self.action_review_send()
            return
        if event.button.id == "compose-external-editor":
            self.action_open_external_editor()
            return
        if event.button.id == "compose-send-later":
            self.action_send_later()
            return
        if event.button.id == "compose-save":
            if self._guard_running_change():
                return
            payload = self._payload(action="save")
            if isinstance(self.app, AppCore):
                self.set_save_preflight_pending(True)
                self._set_note("Checking attachment files…")
                self.app.prepare_draft_save(self, payload)
            else:
                self.dismiss(payload)
