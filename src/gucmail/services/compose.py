"""Compose, validation, and local reply/forward preview services."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from email.utils import getaddresses
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from gucmail.constants import DEFAULT_MAX_ATTACHMENT_BYTES
from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ItemNotFoundError,
    SendOutcomeUncertainError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.models import Address, DraftData, MessageDetail
from gucmail.security import (
    html_to_safe_text,
    sanitize_terminal,
    sha256_file,
    validate_email_address,
)
from gucmail.services._base import make_mutation_plan, validate_reference
from gucmail.services.compose_preferences import QUOTED_SECTION_RE, ComposerPreferences
from gucmail.services.mail_preferences import MailResponsePreferences, ReplyMode
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.state import StateStore

MAX_RECIPIENTS = 500
MAX_BODY_CHARACTERS = 5_000_000
MAX_QUOTED_BODY_CHARACTERS = 500_000
ATTACHMENT_MENTION_RE = re.compile(
    r"\b(?:attach(?:ed|ing|ment|ments)?|enclos(?:ed|ing|ure))\b", re.IGNORECASE
)
LOCAL_PART_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)


class ComposeKind(StrEnum):
    NEW = "new"
    REPLY = "reply"
    REPLY_ALL = "reply_all"
    FORWARD = "forward"


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ComposeIssue:
    code: str
    message: str
    level: IssueLevel
    field: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    path: Path
    size: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ComposeValidation:
    draft: DraftData
    issues: tuple[ComposeIssue, ...]
    attachments: tuple[AttachmentSnapshot, ...]

    @property
    def errors(self) -> tuple[ComposeIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level is IssueLevel.ERROR)

    @property
    def warnings(self) -> tuple[ComposeIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level is IssueLevel.WARNING)

    @property
    def can_send(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ComposePreview:
    kind: ComposeKind
    draft: DraftData
    source: MessageDetail
    source_has_attachments: bool = False


@dataclass(frozen=True, slots=True)
class ComposePlan:
    kind: ComposeKind
    draft: DraftData
    validation: ComposeValidation
    confirmation_scope: str
    source_has_attachments: bool = False
    attempt_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class SendExecution:
    plan: ComposePlan
    dry_run: bool
    sent: bool
    result: Any = None


class ComposeValidationError(ConfigurationError):
    def __init__(self, validation: ComposeValidation) -> None:
        message = "; ".join(issue.message for issue in validation.errors)
        super().__init__(message or "The message is not ready to send.")
        self.validation = validation


class ComposeGateway(Protocol):
    """Injected gateway. No Exchange object is exposed to the service caller."""

    def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail: ...

    def send(self, draft: DraftData) -> Any: ...

    def save_draft(self, draft: DraftData) -> Any: ...

    def reply(self, reference: str, draft: DraftData) -> Any: ...

    def reply_all(self, reference: str, draft: DraftData) -> Any: ...

    def forward(self, reference: str, draft: DraftData) -> Any: ...


class ComposeService:
    def __init__(
        self,
        gateway: ComposeGateway | None = None,
        *,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_total_attachment_bytes: int | None = None,
        hash_attachments: bool = True,
        state: StateStore | None = None,
        preferences: ComposerPreferences | None = None,
        response_preferences: MailResponsePreferences | None = None,
    ) -> None:
        if max_attachment_bytes < 1:
            raise ConfigurationError("max_attachment_bytes must be positive.")
        total = max_total_attachment_bytes or max_attachment_bytes
        if total < 1:
            raise ConfigurationError("max_total_attachment_bytes must be positive.")
        self.gateway = gateway
        self.max_attachment_bytes = max_attachment_bytes
        self.max_total_attachment_bytes = total
        self.hash_attachments = hash_attachments
        self.preferences = preferences or ComposerPreferences()
        self.response_preferences = response_preferences or MailResponsePreferences()
        attempt_state = state if state is not None else getattr(gateway, "state", None)
        self._attempts = (
            SendAttemptStore(attempt_state) if isinstance(attempt_state, StateStore) else None
        )
        self._attempt_lock = threading.Lock()
        self._submitted: set[str] = set()

    def validate(
        self,
        draft: DraftData,
        *,
        allow_empty_subject: bool | None = None,
        allow_empty_body: bool = False,
        source_has_attachments: bool = False,
    ) -> ComposeValidation:
        """Return every actionable issue without mutating the caller's draft."""

        issues: list[ComposeIssue] = []
        if allow_empty_subject is None:
            allow_empty_subject = self.preferences.allow_empty_subject
        normalized = _copy_draft(draft)
        if type(normalized.save_sent_copy) is not bool:
            issues.append(
                ComposeIssue(
                    "invalid_sent_copy",
                    "Choose whether to save a Sent Items copy.",
                    IssueLevel.ERROR,
                    "sent-copy",
                )
            )
        elif not normalized.save_sent_copy:
            issues.append(
                ComposeIssue(
                    "no_sent_copy",
                    "No Sent Items copy will be saved. An uncertain send still requires "
                    "manual reconciliation before any resend.",
                    IssueLevel.WARNING,
                    "sent-copy",
                )
            )
        normalized.to, normalized.cc, normalized.bcc = self._normalize_recipients(
            normalized.to,
            normalized.cc,
            normalized.bcc,
            issues,
        )
        recipient_count = len(normalized.to) + len(normalized.cc) + len(normalized.bcc)
        if recipient_count == 0:
            issues.append(
                ComposeIssue(
                    "missing_recipient",
                    "Add at least one To, Cc, or Bcc recipient.",
                    IssueLevel.ERROR,
                    "recipients",
                )
            )
        if recipient_count > MAX_RECIPIENTS:
            issues.append(
                ComposeIssue(
                    "too_many_recipients",
                    f"A message may contain at most {MAX_RECIPIENTS} recipients.",
                    IssueLevel.ERROR,
                    "recipients",
                )
            )

        threshold = self.preferences.recipient_warning_threshold
        if threshold > 0 and recipient_count >= threshold:
            issues.append(
                ComposeIssue(
                    "recipient_count_warning",
                    f"Review all {recipient_count} recipients before sending; "
                    "consider Bcc when recipients should not see one another.",
                    IssueLevel.WARNING,
                    "recipients",
                )
            )

        subject = normalized.subject.strip()
        if "\r" in normalized.subject or "\n" in normalized.subject:
            issues.append(
                ComposeIssue(
                    "invalid_subject",
                    "The subject may not contain line breaks.",
                    IssueLevel.ERROR,
                    "subject",
                )
            )
        normalized.subject = subject
        if not subject:
            issues.append(
                ComposeIssue(
                    "empty_subject",
                    "The message has an empty subject."
                    if allow_empty_subject
                    else "Add a subject or explicitly allow an empty subject.",
                    IssueLevel.WARNING if allow_empty_subject else IssueLevel.ERROR,
                    "subject",
                )
            )

        body_text = normalized.body_text or ""
        html_text = html_to_safe_text(normalized.body_html) if normalized.body_html else ""
        if len(body_text) + len(normalized.body_html or "") > MAX_BODY_CHARACTERS:
            issues.append(
                ComposeIssue(
                    "body_too_large",
                    f"The composed body exceeds {MAX_BODY_CHARACTERS:,} characters.",
                    IssueLevel.ERROR,
                    "body",
                )
            )
        selected_body = html_text if normalized.body_html is not None else body_text
        if not selected_body.strip():
            issues.append(
                ComposeIssue(
                    "empty_body",
                    "The message has an empty body."
                    if allow_empty_body
                    else "Add a body or explicitly allow an empty body.",
                    IssueLevel.WARNING if allow_empty_body else IssueLevel.ERROR,
                    "body",
                )
            )

        attachments, paths = self._snapshot_attachments(normalized.attachment_paths, issues)
        normalized.attachment_paths = paths
        unquoted_body = QUOTED_SECTION_RE.split(selected_body, maxsplit=1)[0]
        mention_text = f"{subject}\n{unquoted_body}"
        if (
            self.preferences.warn_missing_attachment
            and ATTACHMENT_MENTION_RE.search(mention_text)
            and not attachments
            and not source_has_attachments
        ):
            issues.append(
                ComposeIssue(
                    "attachment_mentioned_but_missing",
                    "The message mentions an attachment, but no file is attached.",
                    IssueLevel.WARNING,
                    "attachments",
                )
            )
        return ComposeValidation(
            draft=normalized,
            issues=tuple(issues),
            attachments=attachments,
        )

    def plan_send(
        self,
        draft_or_preview: DraftData | ComposePreview,
        *,
        kind: ComposeKind | str | None = None,
        allow_empty_subject: bool | None = None,
        allow_empty_body: bool = False,
        source_has_attachments: bool = False,
    ) -> ComposePlan:
        if isinstance(draft_or_preview, ComposePreview):
            draft = draft_or_preview.draft
            compose_kind = draft_or_preview.kind
            source_has_attachments = draft_or_preview.source_has_attachments
        else:
            draft = draft_or_preview
            try:
                compose_kind = ComposeKind(kind or ComposeKind.NEW)
            except ValueError as exc:
                raise ConfigurationError(f"Unknown compose kind: {kind}") from exc
        if kind is not None:
            try:
                requested_kind = ComposeKind(kind)
            except ValueError as exc:
                raise ConfigurationError(f"Unknown compose kind: {kind}") from exc
            if requested_kind is not compose_kind:
                raise ConfigurationError("The requested compose kind does not match the preview.")
        self._validate_kind_reference(compose_kind, draft)
        validation = self.validate(
            draft,
            allow_empty_subject=allow_empty_subject,
            allow_empty_body=allow_empty_body,
            source_has_attachments=source_has_attachments,
        )
        if not validation.can_send:
            raise ComposeValidationError(validation)
        scope = _compose_scope(compose_kind, validation.draft, validation.attachments)
        return ComposePlan(
            kind=compose_kind,
            draft=validation.draft,
            validation=validation,
            confirmation_scope=scope,
            source_has_attachments=source_has_attachments,
        )

    def execute_send(
        self,
        plan: ComposePlan,
        *,
        dry_run: bool = False,
        confirmation_scope: str | None = None,
    ) -> SendExecution:
        """Re-check local files and execute one explicitly confirmed send."""

        if dry_run:
            return SendExecution(plan=plan, dry_run=True, sent=False)
        self._verify_attachment_snapshots(plan.validation.attachments)
        current_scope = _compose_scope(plan.kind, plan.draft, plan.validation.attachments)
        if not hmac.compare_digest(current_scope, plan.confirmation_scope):
            raise ConflictError("The composed message changed after its send preview was created.")
        if confirmation_scope is None or not hmac.compare_digest(
            confirmation_scope, plan.confirmation_scope
        ):
            raise ConfirmationRequiredError(
                "Sending requires confirmation for the exact recipients, content, and files "
                "shown in the preview."
            )

        self._require_gateway()
        reference = (
            plan.attempt_id if plan.draft.reference.startswith("preview:") else plan.draft.reference
        )
        with self._attempt_lock:
            if self._attempts:
                self._attempts.claim(reference, plan.attempt_id)
            elif reference in self._submitted:
                raise ConflictError(
                    "This composition has already been submitted; it was not sent again."
                )
            self._submitted.add(reference)
        try:
            result = self._deliver(plan)
        except BaseException as exc:
            definite_failure = isinstance(
                exc,
                (
                    ConfigurationError,
                    ConflictError,
                    AuthenticationError,
                    ItemNotFoundError,
                    UnsupportedFeatureError,
                    ThrottledError,
                ),
            ) and not (isinstance(exc, SendOutcomeUncertainError) or exc.context.mutation_uncertain)
            outcome = "failed" if definite_failure else "uncertain"
            if self._attempts:
                with suppress(Exception):
                    self._attempts.finish(reference, plan.attempt_id, outcome)
            if definite_failure:
                with self._attempt_lock:
                    self._submitted.discard(reference)
            elif isinstance(exc, Exception) and not isinstance(exc, SendOutcomeUncertainError):
                raise SendOutcomeUncertainError(
                    "The send outcome could not be established. It was not retried; "
                    + (
                        "check Sent Items before submitting another composition."
                        if plan.draft.save_sent_copy
                        else "No Sent Items copy was requested; establish delivery manually "
                        "before submitting another composition."
                    )
                ) from exc
            raise
        if self._attempts:
            try:
                self._attempts.finish(reference, plan.attempt_id, "sent")
            except Exception as exc:
                raise SendOutcomeUncertainError(
                    "Exchange accepted the send, but recording its outcome failed. "
                    "Do not send again."
                ) from exc
        return SendExecution(plan=plan, dry_run=False, sent=True, result=result)

    def _require_gateway(self) -> ComposeGateway:
        if self.gateway is None:
            raise ConfigurationError("An Exchange gateway is required to execute this operation.")
        return self.gateway

    def _deliver(self, plan: ComposePlan) -> Any:
        if plan.kind is ComposeKind.REPLY:
            reference = validate_reference(
                plan.draft.reply_to_reference or "", label="reply source"
            )
            result = self._require_gateway().reply(reference, _copy_draft(plan.draft))
        elif plan.kind is ComposeKind.REPLY_ALL:
            reference = validate_reference(
                plan.draft.reply_to_reference or "", label="reply-all source"
            )
            result = self._require_gateway().reply_all(reference, _copy_draft(plan.draft))
        elif plan.kind is ComposeKind.FORWARD:
            reference = validate_reference(
                plan.draft.forward_of_reference or "", label="forward source"
            )
            result = self._require_gateway().forward(reference, _copy_draft(plan.draft))
        else:
            result = self._require_gateway().send(_copy_draft(plan.draft))
        if result is False or (isinstance(result, dict) and result.get("accepted") is False):
            raise ConflictError("The send adapter rejected the message; it was not accepted.")
        return result

    def save_draft(self, draft: DraftData, *, dry_run: bool = False) -> Any:
        """Save without applying send-only empty-recipient/body/subject restrictions."""

        candidate = _copy_draft(draft)
        if type(candidate.save_sent_copy) is not bool:
            raise ConfigurationError("Choose whether to save a Sent Items copy.")
        issues: list[ComposeIssue] = []
        candidate.to, candidate.cc, candidate.bcc = self._normalize_recipients(
            candidate.to, candidate.cc, candidate.bcc, issues
        )
        snapshots, paths = self._snapshot_attachments(candidate.attachment_paths, issues)
        candidate.attachment_paths = paths
        errors = tuple(issue for issue in issues if issue.level is IssueLevel.ERROR)
        if errors:
            raise ComposeValidationError(ComposeValidation(candidate, tuple(issues), snapshots))
        return candidate if dry_run else self._require_gateway().save_draft(candidate)

    def preview_reply(
        self,
        reference: str,
        *,
        reply_all: bool | None = None,
        account_addresses: Sequence[Address | str] = (),
        include_history: bool | None = None,
    ) -> ComposePreview:
        if reply_all is None:
            reply_all = self.response_preferences.reply_mode is ReplyMode.ALL
        if include_history is None:
            include_history = (
                self.response_preferences.reply_all_include_history
                if reply_all
                else self.response_preferences.reply_include_history
            )
        original = self._require_gateway().show_message(
            validate_reference(reference, label="message reference"), mark_read=False
        )
        own_addresses = {_address_key(_coerce_address(item)) for item in account_addresses}
        primary = list(original.reply_to)
        if not primary and original.summary.sender:
            primary = [original.summary.sender]
        if not primary:
            raise ConfigurationError("The original message has no replyable sender address.")

        if reply_all:
            to_candidates = [*primary, *original.to]
            cc_candidates = list(original.cc)
            to = _unique_addresses(to_candidates, excluded=own_addresses)
            to_keys = {_address_key(item) for item in to}
            cc = _unique_addresses(cc_candidates, excluded=own_addresses | to_keys)
            kind = ComposeKind.REPLY_ALL
        else:
            to = _unique_addresses(primary)
            cc = []
            kind = ComposeKind.REPLY

        body = _quoted_reply(original) if include_history else ""
        draft = DraftData(
            reference=f"preview:{kind.value}:{original.summary.reference}",
            to=to,
            cc=cc,
            subject=_prefixed_subject(original.summary.subject, "Re"),
            body_text=body,
            reply_to_reference=original.summary.reference,
        )
        return ComposePreview(
            kind=kind,
            draft=draft,
            source=original,
            source_has_attachments=False,
        )

    def preview_forward(
        self,
        reference: str,
        *,
        recipients: Sequence[Address | str] = (),
        include_history: bool | None = None,
    ) -> ComposePreview:
        if include_history is None:
            include_history = self.response_preferences.forward_include_history
        original = self._require_gateway().show_message(
            validate_reference(reference, label="message reference"), mark_read=False
        )
        to = _unique_addresses(_coerce_address(item) for item in recipients)
        body = _quoted_forward(original) if include_history else ""
        draft = DraftData(
            reference=f"preview:forward:{original.summary.reference}",
            to=to,
            subject=_prefixed_subject(original.summary.subject, "Fwd"),
            body_text=body,
            forward_of_reference=original.summary.reference,
        )
        return ComposePreview(
            kind=ComposeKind.FORWARD,
            draft=draft,
            source=original,
            source_has_attachments=bool(original.attachments or original.summary.has_attachments),
        )

    def _normalize_recipients(
        self,
        to: Sequence[Address | str],
        cc: Sequence[Address | str],
        bcc: Sequence[Address | str],
        issues: list[ComposeIssue],
    ) -> tuple[list[Address], list[Address], list[Address]]:
        seen: set[str] = set()
        result: list[list[Address]] = [[], [], []]
        for field_name, values, destination in zip(
            ("to", "cc", "bcc"), (to, cc, bcc), result, strict=True
        ):
            for raw in values:
                try:
                    address = _coerce_address(raw)
                except ConfigurationError as exc:
                    issues.append(
                        ComposeIssue(
                            "invalid_recipient",
                            str(exc),
                            IssueLevel.ERROR,
                            field_name,
                        )
                    )
                    continue
                key = _address_key(address)
                if key in seen:
                    issues.append(
                        ComposeIssue(
                            "duplicate_recipient",
                            f"Duplicate recipient removed from {field_name.title()}: "
                            f"{address.email}",
                            IssueLevel.WARNING,
                            field_name,
                        )
                    )
                    continue
                seen.add(key)
                destination.append(address)
        return result[0], result[1], result[2]

    def _snapshot_attachments(
        self,
        paths: Sequence[Path],
        issues: list[ComposeIssue],
    ) -> tuple[tuple[AttachmentSnapshot, ...], list[Path]]:
        snapshots: list[AttachmentSnapshot] = []
        normalized_paths: list[Path] = []
        seen: set[Path] = set()
        total = 0
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve(strict=True)
                if not path.is_file():
                    raise OSError("not a regular file")
                if path in seen:
                    issues.append(
                        ComposeIssue(
                            "duplicate_attachment",
                            f"Duplicate attachment removed: {path.name}",
                            IssueLevel.WARNING,
                            "attachments",
                        )
                    )
                    continue
                seen.add(path)
                initial_stat = path.stat()
                total += initial_stat.st_size
                if initial_stat.st_size > self.max_attachment_bytes:
                    issues.append(
                        ComposeIssue(
                            "attachment_too_large",
                            f"Attachment {path.name!r} exceeds the configured per-file limit.",
                            IssueLevel.ERROR,
                            "attachments",
                        )
                    )
                    continue
                if total > self.max_total_attachment_bytes:
                    continue
                digest = sha256_file(path) if self.hash_attachments else ""
                stat = path.stat()
                if (
                    stat.st_size != initial_stat.st_size
                    or stat.st_mtime_ns != initial_stat.st_mtime_ns
                ):
                    raise OSError("file changed while its attachment preview was built")
                snapshots.append(
                    AttachmentSnapshot(
                        path=path,
                        size=stat.st_size,
                        modified_ns=stat.st_mtime_ns,
                        sha256=digest,
                    )
                )
                normalized_paths.append(path)
            except (OSError, TypeError, ValueError) as exc:
                raw_name = getattr(raw, "name", None) or str(raw) or "<unnamed>"
                issues.append(
                    ComposeIssue(
                        "attachment_unavailable",
                        f"Attachment is unavailable: {Path(raw_name).name or '<unnamed>'} "
                        f"({type(exc).__name__}).",
                        IssueLevel.ERROR,
                        "attachments",
                    )
                )
        if total > self.max_total_attachment_bytes:
            issues.append(
                ComposeIssue(
                    "attachments_too_large",
                    "The combined attachment size exceeds the configured message limit.",
                    IssueLevel.ERROR,
                    "attachments",
                )
            )
        return tuple(snapshots), normalized_paths

    @staticmethod
    def _validate_kind_reference(kind: ComposeKind, draft: DraftData) -> None:
        if kind in {ComposeKind.REPLY, ComposeKind.REPLY_ALL}:
            validate_reference(draft.reply_to_reference or "", label="reply source")
        elif kind is ComposeKind.FORWARD:
            validate_reference(draft.forward_of_reference or "", label="forward source")

    @staticmethod
    def _verify_attachment_snapshots(snapshots: Sequence[AttachmentSnapshot]) -> None:
        for expected in snapshots:
            try:
                stat = expected.path.stat()
            except OSError as exc:
                raise ConflictError(
                    f"Attachment {expected.path.name!r} is no longer available. Rebuild the "
                    "send preview."
                ) from exc
            changed = stat.st_size != expected.size or stat.st_mtime_ns != expected.modified_ns
            if expected.sha256:
                changed = changed or sha256_file(expected.path) != expected.sha256
            if changed:
                raise ConflictError(
                    f"Attachment {expected.path.name!r} changed after confirmation. Rebuild "
                    "the send preview."
                )


def _copy_draft(draft: DraftData) -> DraftData:
    return DraftData(
        reference=draft.reference,
        to=list(draft.to),
        cc=list(draft.cc),
        bcc=list(draft.bcc),
        subject=draft.subject,
        body_text=draft.body_text,
        body_html=draft.body_html,
        attachment_paths=list(draft.attachment_paths),
        importance=draft.importance,
        request_read_receipt=draft.request_read_receipt,
        request_delivery_receipt=draft.request_delivery_receipt,
        location=draft.location,
        revision=draft.revision,
        updated_at=draft.updated_at,
        server_id=draft.server_id,
        server_change_key=draft.server_change_key,
        reply_to_reference=draft.reply_to_reference,
        forward_of_reference=draft.forward_of_reference,
        save_sent_copy=draft.save_sent_copy,
    )


def normalize_recipient(value: Address | str) -> Address:
    """Validate one recipient without resolving the directory or transmitting content."""
    return _coerce_address(value)


def _coerce_address(value: Address | str) -> Address:
    if isinstance(value, Address):
        raw_email = value.email
        name = value.name
        mailbox_type = value.mailbox_type
    elif isinstance(value, str):
        if "\r" in value or "\n" in value:
            raise ConfigurationError("A recipient may not contain line breaks.")
        candidates = getaddresses([value])
        if len(candidates) != 1 or not candidates[0][1]:
            raise ConfigurationError("Enter exactly one explicit email address per recipient.")
        name, raw_email = candidates[0]
        mailbox_type = None
    else:
        raise ConfigurationError("Recipients must be email addresses or Address values.")
    if any(character in raw_email for character in "\r\n") or (
        name and any(character in name for character in "\r\n")
    ):
        raise ConfigurationError("A recipient may not contain line breaks.")
    email = raw_email.strip()
    if not validate_email_address(email):
        raise ConfigurationError(f"Invalid recipient email address: {email or '<empty>'}")
    local, domain = email.rsplit("@", 1)
    if not LOCAL_PART_RE.fullmatch(local):
        raise ConfigurationError(f"Invalid recipient email address: {email}")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConfigurationError(f"Invalid recipient email domain: {domain}") from exc
    labels = ascii_domain.split(".")
    if any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        raise ConfigurationError(f"Invalid recipient email domain: {domain}")
    normalized_email = f"{local}@{ascii_domain}"
    display_name = sanitize_terminal(name).strip() if name else None
    return Address(email=normalized_email, name=display_name or None, mailbox_type=mailbox_type)


def _address_key(address: Address) -> str:
    return address.email.casefold()


def _unique_addresses(
    values: Sequence[Address] | Any,
    *,
    excluded: set[str] | None = None,
) -> list[Address]:
    blocked = excluded or set()
    result: list[Address] = []
    seen: set[str] = set()
    for address in values:
        key = _address_key(address)
        if key not in blocked and key not in seen:
            seen.add(key)
            result.append(address)
    return result


def _prefixed_subject(subject: str, prefix: str) -> str:
    clean = sanitize_terminal(subject).strip()
    if prefix == "Re" and re.match(r"(?i)^\s*re\s*:", clean):
        return clean
    if prefix == "Fwd" and re.match(r"(?i)^\s*(?:fw|fwd)\s*:", clean):
        return clean
    return f"{prefix}: {clean}" if clean else f"{prefix}:"


def _quoted_reply(message: MessageDetail) -> str:
    sender = message.summary.sender
    sender_text = sender.name or sender.email if sender else "the sender"
    date = message.summary.sent_at or message.summary.received_at
    when = date.isoformat() if date else "an unknown date"
    body = sanitize_terminal(message.body_text, max_length=MAX_QUOTED_BODY_CHARACTERS)
    quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    return f"\n\nOn {when}, {sender_text} wrote:\n{quoted}".rstrip()


def _quoted_forward(message: MessageDetail) -> str:
    sender = message.summary.sender
    sender_text = sender.email if sender else "unknown"
    date = message.summary.sent_at or message.summary.received_at
    when = date.isoformat() if date else "unknown"
    recipients = ", ".join(address.email for address in message.to) or "unknown"
    body = sanitize_terminal(message.body_text, max_length=MAX_QUOTED_BODY_CHARACTERS)
    return (
        "\n\n--- Forwarded message ---\n"
        f"From: {sender_text}\n"
        f"Date: {when}\n"
        f"Subject: {sanitize_terminal(message.summary.subject)}\n"
        f"To: {recipients}\n\n"
        f"{body}"
    ).rstrip()


def _compose_scope(
    kind: ComposeKind,
    draft: DraftData,
    attachments: Sequence[AttachmentSnapshot],
) -> str:
    body_digest = hashlib.sha256(
        (draft.body_text + "\0" + (draft.body_html or "")).encode("utf-8")
    ).hexdigest()
    attachment_manifest = tuple(
        (
            str(item.path),
            item.size,
            item.modified_ns,
            item.sha256,
        )
        for item in attachments
    )
    plan = make_mutation_plan(
        operation=f"compose_{kind.value}",
        targets=(validate_reference(draft.reference, label="draft reference"),),
        summary="Send the previewed message.",
        arguments={
            "to": tuple((item.email, item.name, item.mailbox_type) for item in draft.to),
            "cc": tuple((item.email, item.name, item.mailbox_type) for item in draft.cc),
            "bcc": tuple((item.email, item.name, item.mailbox_type) for item in draft.bcc),
            "subject": draft.subject,
            "body_sha256": body_digest,
            "body_format": "html" if draft.body_html is not None else "plain",
            "attachments": attachment_manifest,
            "attachment_paths": tuple(str(path) for path in draft.attachment_paths),
            "importance": draft.importance.value,
            "read_receipt": draft.request_read_receipt,
            "delivery_receipt": draft.request_delivery_receipt,
            "save_sent_copy": draft.save_sent_copy,
            "reply_source": draft.reply_to_reference,
            "forward_source": draft.forward_of_reference,
        },
        requires_confirmation=True,
    )
    return plan.confirmation_scope


__all__ = [
    "AttachmentSnapshot",
    "ComposeGateway",
    "ComposeIssue",
    "ComposeKind",
    "ComposePlan",
    "ComposePreview",
    "ComposeService",
    "ComposeValidation",
    "ComposeValidationError",
    "IssueLevel",
    "SendExecution",
]
