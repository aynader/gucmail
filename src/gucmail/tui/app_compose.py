"""Main window: composing, replying, forwarding and drafts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from textual import work

from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    ItemNotFoundError,
    SendOutcomeUncertainError,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    DraftData,
    DraftLocation,
    Importance,
    MessageDetail,
)
from gucmail.security import sanitize_terminal
from gucmail.services.compose import ComposeKind
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _compose_attachment_paths, _safe_failure
from gucmail.tui.write_coordinator import (
    coordinated_write,
)

if TYPE_CHECKING:
    from gucmail.commands import CommandContext, CommandInvocation
    from gucmail.tui.compose_screen import ComposeScreen
    from gucmail.tui.composer_submission import SubmissionOutcome
    from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend
    from gucmail.tui.palette_routes import PaletteRoute


class ComposeMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _draft_attachment_backend(self) -> ServiceDraftAttachmentBackend:
        from gucmail.services.attachments import AttachmentService
        from gucmail.services.drafts import DraftService, DraftSyncCoordinator
        from gucmail.services.gateway import DeferredGateway as _DeferredGateway
        from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend

        drafts = DraftService(
            self.context.state,
            self.context.paths.recovery_dir,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        return ServiceDraftAttachmentBackend(
            drafts,
            synchronizer=DraftSyncCoordinator(drafts, deferred_gateway),
            attachments=AttachmentService(
                deferred_gateway,
                max_download_bytes=self.context.config.max_attachment_bytes,
            ),
            online=not self.offline,
            maximum_local_copy_bytes=self.context.config.max_attachment_bytes,
        )

    def _open_draft_attachment_workspace(
        self,
        owner_reference: str | None = None,
        *,
        attachment_action: str | None = None,
    ) -> None:
        from gucmail.services.attachment_preferences import AttachmentPreferences
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.palette_routes import RouteTarget

        current = self._route_screen(RouteTarget.DRAFTS)
        if isinstance(current, DraftAttachmentScreen):
            if attachment_action:
                current.queue_attachment_action(attachment_action)
            if owner_reference:
                current.show_attachments_for(owner_reference)
            return
        screen = DraftAttachmentScreen(
            self._draft_attachment_backend(),
            page_size=min(max(self.context.config.page_size, 50), 1_000),
            download_directory=self.context.config.downloads,
            collision_policy=AttachmentPreferences.from_settings(
                self._settings_service()
            ).collision,
            initial_owner_reference=owner_reference,
            offline=self.offline,
        )
        if attachment_action:
            screen.queue_attachment_action(attachment_action)
        self.push_screen(screen)

    @work(thread=True, exclusive=False, group="schedule-draft")
    @coordinated_write()
    def _schedule_workspace_draft(self, reference: str, expression: str) -> None:
        try:
            scheduled = self._scheduler_service().schedule(
                reference,
                expression,
                timezone=self.context.config.timezone,
                require_final_confirmation=True,
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't schedule the draft"),
            )
            return

        def scheduled_ready() -> None:
            self._set_status(
                "Draft scheduled for "
                f"{scheduled.scheduled_for.isoformat()} with final confirmation."
            )
            self.set_timer(0.08, lambda: self._open_area("scheduled"))

        self.call_from_thread(scheduled_ready)

    def _palette_compose_new(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        recipient = self._palette_scalar(invocation.get("recipient"))
        if recipient:
            self._open_compose(
                DraftData(reference=f"dft-{uuid4().hex}", to=[Address(recipient)]), new=True
            )
            return
        self.action_compose()

    def _palette_recover_draft(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        from gucmail.services.drafts import DraftService

        service = DraftService(
            self.context.state,
            self.context.paths.recovery_dir,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )
        references = service.recovery_references()
        if not references:
            self._set_status("No autosaved draft recovery is available.")
            self.notify("No autosaved draft recovery is available.")
            return
        requested = self._palette_scalar(invocation.get("reference"))
        if requested and requested not in references:
            self._set_status(f"No recovery snapshot exists for {sanitize_terminal(requested)}.")
            return
        draft = service.read_recovery(requested or references[-1])
        self._open_compose(draft)

    def _palette_save_current_draft(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        from gucmail.tui.compose_screen import ComposeScreen

        composer = self.screen if isinstance(self.screen, ComposeScreen) else None
        if composer is None:
            self._set_status("Open a composer before saving the current draft.")
            return
        if composer._guard_running_change():
            return
        payload = composer._payload(action="save")
        payload["save_mode"] = "local_only"
        composer.set_save_preflight_pending(True)
        composer._set_note("Checking attachment files…")
        self.prepare_draft_save(composer, payload)

    def _palette_schedule_current_draft(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        from gucmail.tui.compose_screen import ComposeScreen
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen

        composer = self.screen if isinstance(self.screen, ComposeScreen) else None
        draft_workspace = self.screen if isinstance(self.screen, DraftAttachmentScreen) else None
        if draft_workspace is not None and draft_workspace.selected_draft_reference:
            expression = self._palette_scalar(invocation.get("when")) or "tomorrow 09:00"
            draft_workspace.action_schedule_draft(expression=expression)
            return
        if composer is None:
            self._set_status("Open a composer before scheduling the current draft.")
            return
        expression = self._palette_scalar(invocation.get("when")) or "tomorrow 09:00"
        composer.action_schedule_draft(expression=expression)

    def _compose_from_route(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.pim_screens import ContactsScreen, CorrespondentsScreen

        explicit = self._palette_scalar(invocation.get("person"))
        if route.intent == "prefill_selected_contact":
            contacts = self._route_screen(RouteTarget.CONTACTS)
            if explicit:
                self._open_compose(
                    DraftData(reference=f"dft-{uuid4().hex}", to=[Address(explicit)]), new=True
                )
            elif isinstance(contacts, ContactsScreen | CorrespondentsScreen):
                contacts.action_compose()
            else:
                self._set_status("Choose a contact or person before composing.")
            return
        if route.intent == "prefill_selected_sender":
            sender = self._selected_message_sender()
            if sender is None and explicit:
                sender = Address(explicit)
            if sender is None:
                self._set_status("Select a message with a sender before composing.")
                return
            self._open_compose(DraftData(reference=f"dft-{uuid4().hex}", to=[sender]), new=True)
            return
        if route.intent == "prefill_selected_recipients":
            if explicit:
                self._open_compose(
                    DraftData(reference=f"dft-{uuid4().hex}", to=[Address(explicit)]), new=True
                )
                return
            reference = self.ui.selected_reference
            if not reference:
                self._set_status("Select a message before composing to its recipients.")
                return
            if (
                isinstance(self._message_detail, MessageDetail)
                and self._message_detail_reference == reference
            ):
                self._open_compose_for_message_recipients(self._message_detail)
            else:
                self._prepare_compose_for_message_recipients(reference)
            return
        self.action_compose()

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_compose_for_message_recipients(self, reference: str) -> None:
        from gucmail.services.messages import MessageService

        try:
            detail = MessageService(self._ensure_gateway(), state=self.context.state).show_message(
                reference
            )
            self.call_from_thread(self._open_compose_for_message_recipients, detail)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't load the recipients"),
            )

    def _open_compose_for_message_recipients(self, detail: MessageDetail) -> None:
        own = self.context.config.email.casefold() if self.context.config.email else ""
        recipients: list[Address] = []
        seen: set[str] = set()
        for address in (*detail.to, *detail.cc, *detail.bcc):
            key = address.email.casefold()
            if not key or key == own or key in seen:
                continue
            seen.add(key)
            recipients.append(address)
        if not recipients:
            self._set_status("The selected message has no other usable recipients.")
            return
        self._open_compose(DraftData(reference=f"dft-{uuid4().hex}", to=recipients), new=True)

    @work(thread=True, exclusive=True, group="draft-send-outcome")
    def _palette_draft_send_status(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        from gucmail.services.send_attempts import SendAttemptStore

        reference = self._palette_scalar(invocation.get("reference"))
        try:
            attempt = SendAttemptStore(self.context.state).get(reference or "")
            outcome = attempt.state if attempt else "no recorded attempt"
            message = f"Draft send outcome: {outcome}."
            if outcome in {"sending", "uncertain"}:
                message += " Check Sent Items before composing another send; this draft is blocked."
            self.call_from_thread(self._set_status, message)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Not sure it was sent. Check Sent first"),
            )

    @staticmethod
    def _parse_addresses(value: object) -> list[Address]:
        raw = str(value or "").replace(";", ",")
        return [Address(part.strip()) for part in raw.split(",") if part.strip()]

    def _open_compose(
        self,
        draft: DraftData | None = None,
        *,
        kind: ComposeKind = ComposeKind.NEW,
        inherited_attachments: tuple[AttachmentInfo, ...] | None = None,
        new: bool = False,
        review_send: bool = False,
    ) -> None:
        from gucmail.services.compose_preferences import ComposerPreferences
        from gucmail.tui.compose_screen import ComposeScreen

        if draft is None or new or draft.reference.startswith("preview:"):
            preferences = ComposerPreferences.from_settings(self._settings_service())
            draft = preferences.seed_new(draft or DraftData(reference=f"dft-{uuid4().hex}"))

        def finished(payload: dict[str, Any] | None) -> None:
            if payload:
                self._set_status(
                    "Sending… waiting for Exchange. Do not resend while the outcome is pending."
                    if payload.get("action") == "send"
                    else "Saving draft…"
                )
                self.submit_composer(payload)

        composer = ComposeScreen(draft, kind=kind, inherited_attachments=inherited_attachments)
        if review_send:
            composer.request_initial_send_review()

        def submitted(payload: dict[str, Any]) -> None:
            self._set_status(
                "Waiting for the send result…"
                if payload.get("action") == "send"
                else "Saving draft…"
            )
            self.submit_composer(payload, composer=composer)

        composer.submit_handler = submitted
        self.push_screen(composer, finished)

    @work(thread=True, exclusive=True, group="compose-save-preflight")
    def prepare_draft_save(self, composer: ComposeScreen, payload: dict[str, Any]) -> None:
        from gucmail.services.compose import ComposeService

        try:
            validation = ComposeService(
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                hash_attachments=False,
            ).validate(
                self._draft_from_payload(payload), allow_empty_subject=True, allow_empty_body=True
            )
            errors = [issue.message for issue in validation.errors if issue.field == "attachments"]
            message = sanitize_terminal("; ".join(errors))
        except Exception:
            message = (
                "Could not check attachments. Check their paths and permissions, then try again."
            )
        self.call_from_thread(self._draft_save_checked, composer, payload, message)

    def _draft_save_checked(
        self, composer: ComposeScreen, payload: dict[str, Any], error: str
    ) -> None:
        if not composer.is_mounted:
            return
        composer.set_save_preflight_pending(False)
        if composer._guard_running_change():
            return
        current = {
            key: value
            for key, value in composer._payload(action="save").items()
            if key != "recovery_sequence"
        }
        if current != {
            key: value
            for key, value in payload.items()
            if key not in {"save_mode", "recovery_sequence"}
        }:
            error = "The draft changed during validation. Review your edits and save again."
        if error:
            composer._set_note(error)
            composer.query_one("#compose-note").scroll_visible()
        else:
            composer._submit_value(payload)

    @work(thread=True, exclusive=True, group="forward-attachment-metadata")
    def load_forward_attachments(self, composer: ComposeScreen) -> None:
        try:
            detail = self._ensure_gateway().show_message(
                composer.draft.forward_of_reference or "", mark_read=False
            )
            if composer.is_mounted:
                self.call_from_thread(composer.show_forward_attachments, tuple(detail.attachments))
        except Exception:
            if composer.is_mounted:
                self.call_from_thread(
                    composer._set_note,
                    "Could not check inherited attachments. Your draft is retained; "
                    "reopen it online before sending.",
                )

    @staticmethod
    def _duplicate_draft(draft: DraftData) -> DraftData:
        return replace(
            draft,
            reference=f"dft-{uuid4().hex}",
            to=list(draft.to),
            cc=list(draft.cc),
            bcc=list(draft.bcc),
            attachment_paths=list(draft.attachment_paths),
            location=DraftLocation.LOCAL,
            revision=1,
            server_id=None,
            server_change_key=None,
        )

    def action_compose(self) -> None:
        self._open_compose()

    @staticmethod
    def _draft_from_payload(payload: dict[str, Any]) -> DraftData:
        save_sent_copy = payload.get("save_sent_copy", True)
        if type(save_sent_copy) is not bool:
            raise ConfigurationError("Choose whether to save a Sent Items copy.")
        raw_importance = str(payload.get("importance") or "normal").casefold()
        importance = (
            Importance(raw_importance)
            if raw_importance in {item.value for item in Importance}
            else Importance.NORMAL
        )
        return DraftData(
            reference=str(payload.get("reference") or f"dft-{uuid4().hex}"),
            to=ComposeMixin._parse_addresses(payload.get("to")),
            cc=ComposeMixin._parse_addresses(payload.get("cc")),
            bcc=ComposeMixin._parse_addresses(payload.get("bcc")),
            subject=str(payload.get("subject") or ""),
            body_text=str(payload.get("body") or ""),
            body_html=(str(payload["body_html"]) if payload.get("body_html") is not None else None),
            attachment_paths=[
                Path(value).expanduser()
                for value in _compose_attachment_paths(str(payload.get("attachment_paths") or ""))
            ],
            importance=importance,
            request_read_receipt=bool(payload.get("request_read_receipt")),
            request_delivery_receipt=bool(payload.get("request_delivery_receipt")),
            save_sent_copy=save_sent_copy,
            reply_to_reference=(
                str(payload["reply_to_reference"]) if payload.get("reply_to_reference") else None
            ),
            forward_of_reference=(
                str(payload["forward_of_reference"])
                if payload.get("forward_of_reference")
                else None
            ),
            revision=int(payload.get("revision", 1)),
            location=DraftLocation(str(payload.get("location") or DraftLocation.LOCAL.value)),
            server_id=str(payload["server_id"]) if payload.get("server_id") else None,
            server_change_key=(
                str(payload["server_change_key"]) if payload.get("server_change_key") else None
            ),
        )

    def open_composer_external_editor(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
    ) -> None:
        command = self.context.config.external_editor
        if not command:
            composer._set_note(
                "No external editor is configured. Set Composer & Drafts → External editor."
            )
            return
        composer._set_note("Saving recovery state and opening the configured editor…")
        self._edit_composer_externally(composer, payload, command)

    @work(thread=True, exclusive=True, group="compose-external-editor")
    def _edit_composer_externally(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        command: str,
    ) -> None:
        from gucmail.services.drafts import DraftService
        from gucmail.services.editor import ExternalDraftEditor

        try:
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            candidate = self._draft_from_payload(payload)
            # The external editor is deliberately plain-text. Preserve the HTML source in
            # recovery, then make the returned editor text the active canonical body.
            candidate.body_html = None
            self._assert_composer_change_allowed(candidate.reference)
            try:
                existing = service.get(candidate.reference)
            except ItemNotFoundError:
                stored = service.create(candidate)
            else:
                if "revision" not in payload:
                    candidate.revision = existing.revision
                stored = service.save(candidate, expected_revision=candidate.revision)
            result = ExternalDraftEditor(service, command).edit(stored.reference)
            self.call_from_thread(self._external_editor_finished, composer, result.draft)
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="Couldn't open your editor"),
            )

    def attach_selected_message_to_composer(
        self, composer: ComposeScreen, *, reference: str | None = None
    ) -> None:
        reference = reference or self.ui.selected_reference
        if composer._guard_running_change():
            return
        if not reference:
            composer._set_note("Select a message before forwarding it as an attachment.")
            return
        composer._set_note("Retrieving a MIME copy for attachment…")
        self._attach_message_mime(composer, reference)

    def schedule_composer_draft(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        *,
        expression: str,
    ) -> None:
        composer._set_note("Saving a recoverable local draft before scheduling…")
        self._prepare_composer_schedule(composer, payload, expression)

    @work(thread=True, exclusive=False, group="compose-schedule")
    @coordinated_write()
    def _prepare_composer_schedule(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        expression: str,
    ) -> None:
        from gucmail.services.drafts import DraftService

        try:
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            candidate = self._draft_from_payload(payload)
            self._assert_composer_change_allowed(candidate.reference)
            try:
                existing = service.get(candidate.reference)
            except ItemNotFoundError:
                stored = service.create(candidate)
            else:
                if "revision" not in payload:
                    candidate.revision = existing.revision
                stored = service.save(candidate, expected_revision=candidate.revision)
            self.call_from_thread(
                self._open_composer_schedule_review,
                composer,
                stored.reference,
                expression,
            )
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="Couldn't schedule the draft"),
            )

    def _open_composer_schedule_review(
        self,
        composer: ComposeScreen,
        reference: str,
        expression: str,
    ) -> None:
        from gucmail.tui.drafts_attachments_screen import DraftScheduleScreen

        if not composer.is_mounted:
            return

        def reviewed(value: str | None) -> None:
            if not value:
                composer._set_note("Scheduling cancelled; the recoverable local draft remains.")
                return
            if composer.is_mounted:
                composer.dismiss(None)
            self._schedule_workspace_draft(reference, value)

        self.push_screen(
            DraftScheduleScreen(reference, expression=expression),
            reviewed,
        )

    @work(thread=True, exclusive=True, group="compose-message-attachment")
    def _attach_message_mime(self, composer: ComposeScreen, reference: str) -> None:
        try:
            raw = self._ensure_gateway().raw_message(reference)
            directory = self.context.paths.recovery_dir / "forward-attachments"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"forwarded-{uuid4().hex}.eml"
            with target.open("xb") as handle:
                handle.write(raw)
            self.call_from_thread(composer.add_attachment_path, target)
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="Couldn't forward as an attachment"),
            )

    def _assert_composer_change_allowed(self, reference: str) -> None:
        from gucmail.services.send_attempts import SendAttemptStore

        attempt = SendAttemptStore(self.context.state).composition_status(reference)
        if attempt is not None and attempt.state != "failed":
            raise ConflictError(
                "This draft has a send or Scheduled Mail record. Its recovery was preserved. "
                "Review Send status before changing it."
            )

    def _write_composer_recovery(self, payload: dict[str, Any]) -> None:
        from gucmail.services.drafts import DraftService

        with self._composer_recovery_lock:
            reference = str(payload.get("reference"))
            sequence = payload.get("recovery_sequence")
            if reference in self._finished_compositions:
                return
            if isinstance(sequence, int) and sequence < self._composer_recovery_sequences.get(
                reference, -1
            ):
                return
            self._assert_composer_change_allowed(reference)
            DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            ).write_recovery(self._draft_from_payload(payload))
            if isinstance(sequence, int):
                self._composer_recovery_sequences[reference] = sequence

    def recover_composer_before_close(self, payload: dict[str, Any]) -> bool:
        """Durably recover edited content before claiming that it is safe to close."""

        try:
            self._write_composer_recovery(payload)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Couldn't recover the draft"))
            return False
        return True

    @work(thread=True, exclusive=True, group="compose-autosave")
    def autosave_composer(self, payload: dict[str, Any]) -> None:
        try:
            self._write_composer_recovery(payload)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't recover the draft"),
            )

    def _preview_discard_composer(self, composer: ComposeScreen) -> None:
        from gucmail.tui.dialogs import ConfirmationScreen

        if composer._guard_running_change():
            return
        draft = self._draft_from_payload(composer._payload(action="discard"))
        detail = (
            f"Draft reference: {draft.reference}\n"
            "Local draft and recovery state will be removed. If this draft has a synchronized "
            "Exchange copy, that copy will be moved to Deleted Items first."
        )

        def confirmed(value: bool | None) -> None:
            if value:
                self._discard_composer(composer, draft)

        self.push_screen(ConfirmationScreen("Discard this draft?", detail), confirmed)

    @work(thread=True, exclusive=False, group="compose-discard")
    @coordinated_write()
    def _discard_composer(self, composer: ComposeScreen, draft: DraftData) -> None:
        from gucmail.services.drafts import DraftService

        try:
            self._assert_composer_change_allowed(draft.reference)
            if draft.server_id and self.offline:
                raise ConfigurationError(
                    "Reconnect before discarding a synchronized draft. Its local copy is retained."
                )
            if draft.server_id and not self.offline:
                self._ensure_gateway().delete_message(draft.server_id, permanent=False)
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            service.delete(draft.reference)
            service.remove_recovery(draft.reference)
            self.call_from_thread(composer.dismiss, None)
            self.call_from_thread(self._set_status, f"Discarded draft {draft.reference}")
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't discard the draft"),
            )

    @work(thread=True, exclusive=False, group="compose-submit")
    def submit_composer(
        self, payload: dict[str, Any], *, composer: ComposeScreen | None = None
    ) -> None:
        try:
            # A rejected overlapping submission must remain recoverable even
            # when its composer has already dismissed after confirmation.
            self._write_composer_recovery(payload)
        except Exception as exc:
            self.call_from_thread(
                self._set_status, _safe_failure(exc, action="Couldn't recover the draft")
            )
            if composer is not None:
                self.call_from_thread(
                    composer.submission_finished,
                    "not_started",
                    "Recovery could not be updated, so submission did not start. "
                    "Your input remains here. Review Send status or local storage before retrying.",
                )
            return
        self._submit_composer(payload, composer=composer)
        if composer is not None and composer._submission_pending:
            self._composer_outcome(
                composer,
                "not_started",
                "Another change is still pending. Your input and recovery copy are retained. "
                "Wait for it to finish before another submission.",
            )

    def _composer_outcome(
        self, composer: ComposeScreen | None, outcome: SubmissionOutcome, message: str
    ) -> None:
        self.call_from_thread(self._set_status, message)
        if outcome == "uncertain" and composer is not None:
            with self._composer_recovery_lock:
                self._finished_compositions.add(composer.draft.reference)
        if composer is not None:
            self.call_from_thread(composer.submission_finished, outcome, message)

    @coordinated_write(blocked_hint="The composer recovery copy is retained.")
    def _submit_composer(
        self, payload: dict[str, Any], *, composer: ComposeScreen | None = None
    ) -> None:
        from gucmail.services.compose import ComposeService
        from gucmail.services.compose_preferences import ComposerPreferences
        from gucmail.services.drafts import DraftService, DraftSyncCoordinator
        from gucmail.tui.composer_submission import attempt_message

        queue_started = False
        try:
            draft = self._draft_from_payload(payload)
            if payload.get("action") == "save":
                self._assert_composer_change_allowed(draft.reference)
                save_mode = str(
                    payload.get("save_mode")
                    or (
                        "local_then_server"
                        if bool(self._setting_value("composer.server_draft_sync", True))
                        else "local_only"
                    )
                )
                draft_service = DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                )
                if draft_service.exists(draft.reference):
                    if "revision" not in payload:
                        draft.revision = draft_service.get(draft.reference).revision
                    stored = draft_service.save(draft)
                else:
                    stored = draft_service.create(draft)
                draft_service.remove_recovery(draft.reference)
                if save_mode == "local_only":
                    self._composer_outcome(
                        composer,
                        "saved",
                        f"Draft saved locally: {stored.reference} · server synchronization skipped",
                    )
                    return
                if self.offline:
                    self._composer_outcome(
                        composer,
                        "saved",
                        f"Draft saved locally: {stored.reference} · offline, server save deferred",
                    )
                    return
                try:
                    synchronized = DraftSyncCoordinator(
                        draft_service, self._ensure_gateway()
                    ).sync_to_server(stored.reference)
                except Exception as exc:
                    self._composer_outcome(
                        composer,
                        "saved",
                        "Draft saved on this computer only. "
                        + _safe_failure(exc, action="Couldn't save it to the server"),
                    )
                    return
                self._composer_outcome(
                    composer,
                    "saved",
                    f"Draft saved locally and synchronized to Exchange: {synchronized.reference}",
                )
                return
            kind = ComposeKind(str(payload.get("kind") or ComposeKind.NEW.value))
            compose_service = ComposeService(
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                state=self.context.state,
                preferences=ComposerPreferences.from_settings(self._settings_service()),
            )
            plan = compose_service.plan_send(
                draft, kind=kind, allow_empty_subject=payload.get("allow_empty_subject") is True
            )
            if payload.get("confirmation_scope") != plan.confirmation_scope:
                self._composer_outcome(
                    composer,
                    "not_started",
                    "The reviewed message or attachment files changed. Review send again. "
                    "Nothing was queued or sent.",
                )
                return
            delay = int(str(self._setting_value("composer.undo_send_seconds", 0)))
            if delay:
                service = DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                )
                stored = (
                    service.save(plan.draft)
                    if service.exists(draft.reference)
                    else service.create(plan.draft)
                )
                queue_started = True
                job = self._scheduler_service().queue_undo_send(
                    stored.reference,
                    delay,
                    allow_empty_subject=payload.get("allow_empty_subject") is True,
                )
                with self._composer_recovery_lock:
                    self._finished_compositions.add(draft.reference)
                self.call_from_thread(self._queued_undo_send, job.reference, delay)
                self._composer_outcome(
                    composer,
                    "queued",
                    f"Queued with a {delay}-second Undo Send delay. Cancel in Scheduled Mail. "
                    "Closing the app leaves the job queued for a running scheduler.",
                )
                return
            compose_service.gateway = self._ensure_gateway()
            compose_service.execute_send(
                plan, confirmation_scope=str(payload["confirmation_scope"])
            )
            with self._composer_recovery_lock:
                self._finished_compositions.add(draft.reference)
                DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                ).finish_sent(draft.reference, expected_revision=draft.revision)
            remembered = self._remember_contacted_people(plan.draft)
            self._composer_outcome(
                composer,
                "sent",
                "Send accepted by the Exchange request path"
                + (
                    "; local recipient history could not be saved. Do not resend."
                    if not remembered
                    else ""
                ),
            )
        except SendOutcomeUncertainError:
            self._composer_outcome(
                composer,
                "uncertain",
                "The send may have completed. It was not retried. "
                + (
                    "Check Sent Items before sending again; "
                    if payload.get("save_sent_copy", True) is True
                    else "No Sent Items copy was requested; establish delivery manually "
                    "before sending again. "
                )
                + "The local recovery copy remains available.",
            )
        except Exception:
            if queue_started:
                self._composer_outcome(
                    composer,
                    "uncertain",
                    "The local queue result could not be confirmed. Your composition is retained. "
                    "Do not repeat the submission; review Scheduled Mail before another send.",
                )
                return
            if payload.get("action") == "save":
                self._composer_outcome(
                    composer,
                    "failed",
                    "The draft save could not be confirmed. Your input is retained. "
                    "Refresh Drafts to check the saved revision before trying again.",
                )
                return
            from gucmail.services.send_attempts import SendAttemptStore

            try:
                attempt = SendAttemptStore(self.context.state).get(str(payload.get("reference")))
            except Exception:
                self._composer_outcome(
                    composer,
                    "uncertain",
                    "The submission outcome could not be checked. Your recovery copy is retained. "
                    "Do not repeat it; review Send status and Scheduled Mail first.",
                )
                return
            if attempt and attempt.state == "sent":
                self._composer_outcome(
                    composer,
                    "sent",
                    "Send was accepted, but local cleanup could not be completed. Do not resend. "
                    "Review the retained draft/recovery after checking Sent Items.",
                )
            elif attempt and attempt.state in {"sending", "uncertain"}:
                self._composer_outcome(composer, "uncertain", attempt_message(attempt.state))
            elif attempt and attempt.state == "failed":
                self._composer_outcome(
                    composer, "failed", attempt_message("failed") + " Your input is retained here."
                )
            else:
                self._composer_outcome(
                    composer,
                    "not_started",
                    "Submission did not reach a confirmed send. "
                    "Your input and recovery are retained. "
                    "Check the connection, message and files. "
                    "Review Scheduled Mail before queuing again.",
                )

    def _queued_undo_send(self, reference: str, delay: int) -> None:
        self._set_status(
            f"Queued with a {delay}-second Undo Send delay. Cancel in Scheduled Mail. "
            "Closing the app leaves the job queued for a running scheduler."
        )
        self._open_area("scheduled")
        self.set_timer(delay, lambda: self._finish_undo_delay(reference))

    @work(thread=True, exclusive=False, group="undo-send-countdown")
    @coordinated_write(blocked_hint="The delayed job remains queued. Review it in Scheduled Mail.")
    def _finish_undo_delay(self, reference: str) -> None:
        if self.offline:
            return
        try:
            scheduler = self._scheduler_service()
            result = scheduler.run_reference_if_due(reference)
            if result is not None:
                self.call_from_thread(self._set_status, f"Delayed send: {result.state.value}")
            else:
                # Do not leave the "Queued with ... Undo Send delay" message standing
                # after the job was cancelled, rescheduled or paused elsewhere.
                state = str(getattr(scheduler.get(reference).message.state, "value", "")).casefold()
                if state == "cancelled":
                    message = "Undo Send: the delayed message was cancelled. Nothing was sent."
                else:
                    message = (
                        f"Delayed send was not run now (state: {state or 'unknown'}). "
                        "Review it in Scheduled Mail."
                    )
                self.call_from_thread(self._set_status, message)
        except Exception as exc:
            self.call_from_thread(
                self._set_status, _safe_failure(exc, action="Couldn't queue the message")
            )

    def _remember_contacted_people(self, draft: DraftData) -> bool:
        """Persist only explicit successful-send recipients, most recent first."""

        from gucmail.services.recipient_assistance import remember_sent_recipients

        try:
            remember_sent_recipients(
                self._settings_service(), draft, own_address=self.context.config.email
            )
            return True
        except Exception:
            # An ancillary local write must not obscure the accepted send or cause a retry.
            return False

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_reply(self, reference: str, *, reply_all: bool | None) -> None:
        from gucmail.services.compose import ComposeService
        from gucmail.services.mail_preferences import MailResponsePreferences

        try:
            service = ComposeService(
                self._ensure_gateway(),
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                response_preferences=MailResponsePreferences.from_settings(
                    self._settings_service()
                ),
            )
            own = (self.context.config.email,) if self.context.config.email else ()
            preview = service.preview_reply(
                reference,
                reply_all=reply_all,
                account_addresses=own,
            )
            self.call_from_thread(
                self._open_compose,
                preview.draft,
                kind=preview.kind,
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_forward(self, reference: str) -> None:
        from gucmail.services.compose import ComposeService
        from gucmail.services.mail_preferences import MailResponsePreferences

        try:
            service = ComposeService(
                self._ensure_gateway(),
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                response_preferences=MailResponsePreferences.from_settings(
                    self._settings_service()
                ),
            )
            preview = service.preview_forward(reference)
            self.call_from_thread(
                self._open_compose,
                preview.draft,
                kind=preview.kind,
                inherited_attachments=tuple(preview.source.attachments),
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    def _needs_message(self, action: str) -> bool:
        if self.ui.selected_reference:
            return True
        self._set_status(f"Select a message before choosing {action}.")
        return False

    def action_reply(self) -> None:
        if self._needs_message("Reply"):
            self._prepare_reply(self.ui.selected_reference or "", reply_all=None)

    def action_reply_all(self) -> None:
        if self._needs_message("Reply all"):
            self._prepare_reply(self.ui.selected_reference or "", reply_all=True)

    def action_forward(self) -> None:
        if self._needs_message("Forward"):
            self._prepare_forward(self.ui.selected_reference or "")
