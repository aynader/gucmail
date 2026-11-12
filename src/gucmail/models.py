"""Exchange-independent domain models shared by every presentation surface."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Importance(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    PERSONAL = "personal"
    PRIVATE = "private"
    CONFIDENTIAL = "confidential"


class ItemSource(StrEnum):
    LIVE = "live_server"
    CACHE = "cache"
    INDEX = "search_index"
    BACKUP = "backup"
    LOCAL = "local"


class DraftLocation(StrEnum):
    LOCAL = "local"
    SERVER = "server"
    SYNCHRONIZED = "synchronized"
    CONFLICTED = "conflicted"


class SendState(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    UNDO_DELAY = "undo_delay"
    DUE = "due"
    WAITING_CONNECTIVITY = "waiting_connectivity"
    WAITING_AUTHENTICATION = "waiting_authentication"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCELLED = "cancelled"
    UNCERTAIN = "send_state_uncertain"
    MANUAL_REVIEW = "manual_review_required"


class CapabilityStatus(StrEnum):
    IMPLEMENTED = "implemented_tested"
    LIVE_VERIFIED = "live_verified"
    PARTIAL = "partial"
    SERVER_UNSUPPORTED = "unsupported_server_version"
    CONFIG_DISABLED = "disabled_by_server_configuration"
    UNAUTHORIZED = "unauthorized_for_account"
    ADMIN_ONLY = "administrative_only"
    NOT_APPLICABLE = "not_applicable"
    LIBRARY_UNSUPPORTED = "unsupported_by_library"
    NOT_IMPLEMENTED = "not_implemented_by_client"
    NOT_LIVE_VERIFIED = "implemented_not_live_verified"


class SafetyClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


class ImplementationPath(StrEnum):
    EXCHANGELIB = "exchangelib"
    SOAP_TYPED = "soap_typed"
    EXTENDED_PROPERTY = "extended_property"
    LOCAL = "local"
    REGISTRY_ONLY = "registry_only"


class ImplementationStatus(StrEnum):
    IMPLEMENTED = "implemented"
    PARTIAL = "partial"
    NOT_IMPLEMENTED = "not_implemented"


class ServerSupport(StrEnum):
    NOT_PROBED = "not_probed"
    VERSION_ELIGIBLE = "version_eligible"
    UNSUPPORTED_VERSION = "unsupported_version"
    SERVER_REJECTED = "server_rejected"


class AccountAccess(StrEnum):
    NOT_PROBED = "not_probed"
    ALLOWED = "allowed"
    UNAUTHORIZED = "unauthorized"
    ADMIN_ONLY = "administrative_only"


class VerificationLevel(StrEnum):
    STATIC = "static_documentation"
    FIXTURE_TESTED = "fixture_tested"
    LIVE_READ = "live_read"
    LIVE_MUTATION = "live_mutation"


class QuotaSource(StrEnum):
    """Provenance for one mailbox-usage or Exchange-threshold value."""

    SERVER_REPORTED = "server_reported"
    SERVER_NOTIFICATION = "server_notification"
    AUTOMATICALLY_DETECTED = "automatically_detected"
    KNOWN_GUC_FALLBACK = "known_guc_fallback"
    USER_CONFIGURED = "user_configured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class QuotaConfidence(StrEnum):
    """How confidently a quota value describes the current mailbox configuration."""

    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class MailboxQuotaStatus(StrEnum):
    """User-facing state derived only from known thresholds or observed behavior."""

    NORMAL = "normal"
    WARNING = "warning"
    NEAR_SEND_LIMIT = "near_send_limit"
    SEND_BLOCKED = "send_blocked"
    SEND_RECEIVE_BLOCKED = "send_receive_blocked"
    UNKNOWN = "quota_status_unknown"


class MailboxOperationStatus(StrEnum):
    """Whether a mailbox operation is known to be available or blocked."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    NOT_KNOWN_TO_BE_BLOCKED = "not_known_to_be_blocked"
    UNKNOWN = "unknown"


class QuotaThresholdKind(StrEnum):
    ISSUE_WARNING = "issue_warning"
    PROHIBIT_SEND = "prohibit_send"
    PROHIBIT_SEND_RECEIVE = "prohibit_send_receive"


class CalendarResponse(StrEnum):
    ACCEPT = "accept"
    TENTATIVE = "tentative"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class Address:
    email: str
    name: str | None = None
    mailbox_type: str | None = None


@dataclass(frozen=True, slots=True)
class FolderInfo:
    reference: str
    name: str
    path: str
    parent_reference: str | None
    total_count: int | None = None
    unread_count: int | None = None
    child_count: int | None = None
    folder_class: str | None = None
    distinguished_role: str | None = None
    ews_id: str | None = None
    change_key: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    reference: str
    name: str
    size: int | None
    content_type: str | None = None
    is_inline: bool = False
    is_item_attachment: bool = False
    content_id: str | None = None
    dangerous: bool = False


@dataclass(frozen=True, slots=True)
class MessageSummary:
    reference: str
    subject: str
    sender: Address | None
    received_at: datetime | None
    sent_at: datetime | None = None
    is_read: bool = False
    has_attachments: bool = False
    importance: Importance = Importance.NORMAL
    flagged: bool = False
    categories: tuple[str, ...] = ()
    size: int | None = None
    folder_path: str | None = None
    conversation_id: str | None = None
    preview: str | None = None
    source: ItemSource = ItemSource.LIVE


@dataclass(frozen=True, slots=True)
class MessageDetail:
    summary: MessageSummary
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    bcc: tuple[Address, ...] = ()
    reply_to: tuple[Address, ...] = ()
    body_text: str = ""
    body_html: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    internet_message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    raw_headers: Mapping[str, str] = field(default_factory=dict)
    attachments: tuple[AttachmentInfo, ...] = ()
    links: tuple[str, ...] = ()
    ews_id: str | None = None
    change_key: str | None = None
    extended_properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DraftData:
    reference: str
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    bcc: list[Address] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str | None = None
    attachment_paths: list[Path] = field(default_factory=list)
    importance: Importance = Importance.NORMAL
    request_read_receipt: bool = False
    request_delivery_receipt: bool = False
    location: DraftLocation = DraftLocation.LOCAL
    revision: int = 1
    updated_at: datetime | None = None
    server_id: str | None = None
    server_change_key: str | None = None
    reply_to_reference: str | None = None
    forward_of_reference: str | None = None
    save_sent_copy: bool = True


@dataclass(frozen=True, slots=True)
class ScheduledMessage:
    reference: str
    draft_reference: str
    scheduled_for: datetime
    timezone: str
    original_expression: str
    state: SendState
    created_at: datetime
    updated_at: datetime
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    idempotency_key: str = ""
    require_final_confirmation: bool = False
    outcome_reference: str | None = None
    recurrence_id: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    reference: str
    subject: str
    start: datetime
    end: datetime
    timezone: str
    location: str | None = None
    body: str = ""
    organizer: Address | None = None
    required_attendees: tuple[Address, ...] = ()
    optional_attendees: tuple[Address, ...] = ()
    resources: tuple[Address, ...] = ()
    is_all_day: bool = False
    is_meeting: bool = False
    recurrence: Mapping[str, Any] | None = None
    categories: tuple[str, ...] = ()
    importance: Importance = Importance.NORMAL
    sensitivity: Sensitivity = Sensitivity.NORMAL
    reminder_minutes: int | None = None
    availability: str = "busy"
    attachments: tuple[AttachmentInfo, ...] = ()
    response_status: str | None = None
    item_type: str | None = None
    recurrence_id: datetime | None = None
    uid: str | None = None
    source: ItemSource = ItemSource.LIVE


@dataclass(frozen=True, slots=True)
class ContactInfo:
    reference: str
    display_name: str
    given_name: str | None = None
    middle_name: str | None = None
    family_name: str | None = None
    nickname: str | None = None
    emails: tuple[Address, ...] = ()
    phones: Mapping[str, str] = field(default_factory=dict)
    addresses: Mapping[str, str] = field(default_factory=dict)
    company: str | None = None
    department: str | None = None
    job_title: str | None = None
    birthday: date | None = None
    notes: str | None = None
    categories: tuple[str, ...] = ()
    has_photo: bool = False
    photo_size: int | None = None
    photo_content_type: str | None = None
    source: ItemSource = ItemSource.LIVE


@dataclass(frozen=True, slots=True)
class TaskInfo:
    reference: str
    subject: str
    status: str
    percent_complete: int = 0
    start_date: date | None = None
    due_date: date | None = None
    completed_at: datetime | None = None
    body: str = ""
    categories: tuple[str, ...] = ()
    importance: Importance = Importance.NORMAL
    reminder_at: datetime | None = None
    recurrence: Mapping[str, Any] | None = None
    source: ItemSource = ItemSource.LIVE


@dataclass(frozen=True, slots=True)
class QuotaValue:
    """One byte value together with the evidence supporting it."""

    value_bytes: int | None
    source: QuotaSource = QuotaSource.UNKNOWN
    confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    observed_at: datetime | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.value_bytes is not None and self.value_bytes < 0:
            raise ValueError("Quota values may not be negative.")

    @property
    def known(self) -> bool:
        return self.value_bytes is not None


@dataclass(frozen=True, slots=True)
class QuotaInfo:
    """Independent Exchange quota thresholds and current mailbox usage.

    ``quota_bytes`` is retained for compatibility and means an explicitly reported total
    capacity only. It is never populated from the known GUC 92 MiB sending threshold.
    ``warning_bytes`` is the Exchange ``IssueWarningQuota`` value.
    """

    used_bytes: int | None = None
    quota_bytes: int | None = None
    warning_bytes: int | None = None
    prohibit_send_bytes: int | None = None
    prohibit_send_receive_bytes: int | None = None
    deleted_bytes: int | None = None
    recoverable_bytes: int | None = None
    archive_used_bytes: int | None = None
    archive_quota_bytes: int | None = None
    source: str | None = None
    usage_source: QuotaSource = QuotaSource.UNKNOWN
    usage_confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    total_capacity_source: QuotaSource = QuotaSource.UNKNOWN
    total_capacity_confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    issue_warning_source: QuotaSource = QuotaSource.UNKNOWN
    issue_warning_confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    prohibit_send_source: QuotaSource = QuotaSource.UNKNOWN
    prohibit_send_confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    prohibit_send_receive_source: QuotaSource = QuotaSource.UNKNOWN
    prohibit_send_receive_confidence: QuotaConfidence = QuotaConfidence.UNKNOWN
    observed_send_blocked: bool = False
    observed_receive_blocked: bool = False
    observed_at: datetime | None = None
    limitations: tuple[str, ...] = ()

    @property
    def issue_warning_bytes(self) -> int | None:
        """The independent Exchange ``IssueWarningQuota`` threshold."""

        return self.warning_bytes

    @property
    def current_usage(self) -> QuotaValue:
        return QuotaValue(
            self.used_bytes,
            _effective_quota_source(self.usage_source, self.source),
            _effective_quota_confidence(
                self.usage_confidence,
                _effective_quota_source(self.usage_source, self.source),
            ),
            self.observed_at,
        )

    @property
    def total_capacity(self) -> QuotaValue:
        return QuotaValue(
            self.quota_bytes,
            _effective_quota_source(self.total_capacity_source, self.source),
            _effective_quota_confidence(
                self.total_capacity_confidence,
                _effective_quota_source(self.total_capacity_source, self.source),
            ),
            self.observed_at,
            "Only a server-reported total is a mailbox capacity.",
        )

    @property
    def issue_warning(self) -> QuotaValue:
        return QuotaValue(
            self.warning_bytes,
            _effective_quota_source(self.issue_warning_source, self.source),
            _effective_quota_confidence(
                self.issue_warning_confidence,
                _effective_quota_source(self.issue_warning_source, self.source),
            ),
            self.observed_at,
        )

    @property
    def prohibit_send(self) -> QuotaValue:
        return QuotaValue(
            self.prohibit_send_bytes,
            _effective_quota_source(self.prohibit_send_source, self.source),
            _effective_quota_confidence(
                self.prohibit_send_confidence,
                _effective_quota_source(self.prohibit_send_source, self.source),
            ),
            self.observed_at,
            "Exchange may block outgoing mail at this threshold.",
        )

    @property
    def prohibit_send_receive(self) -> QuotaValue:
        return QuotaValue(
            self.prohibit_send_receive_bytes,
            _effective_quota_source(self.prohibit_send_receive_source, self.source),
            _effective_quota_confidence(
                self.prohibit_send_receive_confidence,
                _effective_quota_source(self.prohibit_send_receive_source, self.source),
            ),
            self.observed_at,
            "Exchange may block both outgoing and incoming mail at this threshold.",
        )

    @property
    def remaining_bytes(self) -> int | None:
        """Remaining bytes to a genuinely reported total capacity, when one is known."""

        if self.used_bytes is None or self.quota_bytes is None:
            return None
        return max(self.quota_bytes - self.used_bytes, 0)

    @property
    def percent_used(self) -> float | None:
        """Usage against a reported total capacity; the numeric value is never capped."""

        if self.used_bytes is None or not self.quota_bytes:
            return None
        return max((self.used_bytes / self.quota_bytes) * 100, 0.0)

    @property
    def send_threshold_percent(self) -> float | None:
        """Usage relative to ``ProhibitSendQuota``; values above 100% are meaningful."""

        if self.used_bytes is None or not self.prohibit_send_bytes:
            return None
        return max((self.used_bytes / self.prohibit_send_bytes) * 100, 0.0)

    @property
    def send_headroom_bytes(self) -> int | None:
        """Signed distance from the send-block threshold."""

        if self.used_bytes is None or self.prohibit_send_bytes is None:
            return None
        return self.prohibit_send_bytes - self.used_bytes

    @property
    def remaining_to_send_limit_bytes(self) -> int | None:
        headroom = self.send_headroom_bytes
        return None if headroom is None else max(headroom, 0)

    @property
    def over_send_limit_bytes(self) -> int | None:
        headroom = self.send_headroom_bytes
        return None if headroom is None else max(-headroom, 0)

    @property
    def remaining_to_warning_bytes(self) -> int | None:
        if self.used_bytes is None or self.warning_bytes is None:
            return None
        return max(self.warning_bytes - self.used_bytes, 0)

    @property
    def remaining_to_receive_limit_bytes(self) -> int | None:
        if self.used_bytes is None or self.prohibit_send_receive_bytes is None:
            return None
        return max(self.prohibit_send_receive_bytes - self.used_bytes, 0)

    @property
    def send_status(self) -> MailboxOperationStatus:
        if self.observed_receive_blocked or self.observed_send_blocked:
            return MailboxOperationStatus.BLOCKED
        if (
            self.used_bytes is not None
            and self.prohibit_send_receive_bytes is not None
            and self.used_bytes >= self.prohibit_send_receive_bytes
        ):
            return MailboxOperationStatus.BLOCKED
        if (
            self.used_bytes is not None
            and self.prohibit_send_bytes is not None
            and self.used_bytes >= self.prohibit_send_bytes
        ):
            return MailboxOperationStatus.BLOCKED
        if self.used_bytes is not None and self.prohibit_send_bytes is not None:
            return MailboxOperationStatus.ALLOWED
        return MailboxOperationStatus.UNKNOWN

    @property
    def receive_status(self) -> MailboxOperationStatus:
        if self.observed_receive_blocked:
            return MailboxOperationStatus.BLOCKED
        if self.used_bytes is not None and self.prohibit_send_receive_bytes is not None:
            if self.used_bytes >= self.prohibit_send_receive_bytes:
                return MailboxOperationStatus.BLOCKED
            return MailboxOperationStatus.ALLOWED
        if self.send_status is MailboxOperationStatus.BLOCKED:
            return MailboxOperationStatus.NOT_KNOWN_TO_BE_BLOCKED
        return MailboxOperationStatus.UNKNOWN

    @property
    def mailbox_status(self) -> MailboxQuotaStatus:
        if self.receive_status is MailboxOperationStatus.BLOCKED:
            return MailboxQuotaStatus.SEND_RECEIVE_BLOCKED
        if self.send_status is MailboxOperationStatus.BLOCKED:
            return MailboxQuotaStatus.SEND_BLOCKED
        if self.used_bytes is None:
            return MailboxQuotaStatus.UNKNOWN
        send_percent = self.send_threshold_percent
        if send_percent is not None and send_percent >= 90.0:
            return MailboxQuotaStatus.NEAR_SEND_LIMIT
        if self.warning_bytes is not None and self.used_bytes >= self.warning_bytes:
            return MailboxQuotaStatus.WARNING
        if self.prohibit_send_bytes is not None or self.warning_bytes is not None:
            return MailboxQuotaStatus.NORMAL
        return MailboxQuotaStatus.UNKNOWN

    @property
    def status_message(self) -> str:
        if self.mailbox_status is MailboxQuotaStatus.SEND_RECEIVE_BLOCKED:
            return "Exchange is blocking both sending and receiving for this mailbox."
        if self.mailbox_status is MailboxQuotaStatus.SEND_BLOCKED:
            if self.prohibit_send_receive_bytes is None and not self.observed_receive_blocked:
                return (
                    "Exchange is blocking sending. No receive block has been detected; "
                    "the receive-block threshold is unknown."
                )
            return "Exchange is blocking sending for this mailbox."
        if self.mailbox_status is MailboxQuotaStatus.NEAR_SEND_LIMIT:
            return "Mailbox usage is near the known Exchange send-block threshold."
        if self.mailbox_status is MailboxQuotaStatus.WARNING:
            return "Mailbox usage has reached the known Exchange warning threshold."
        if self.mailbox_status is MailboxQuotaStatus.NORMAL:
            return "No quota block is indicated by the available Exchange values."
        return "The available data is insufficient to determine the mailbox quota status."


def _effective_quota_source(explicit: QuotaSource, legacy: str | None) -> QuotaSource:
    if explicit is not QuotaSource.UNKNOWN:
        return explicit
    normalized = (legacy or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("folder_sum", "estimate", "approx")):
        return QuotaSource.ESTIMATED
    if "notification" in normalized:
        return QuotaSource.SERVER_NOTIFICATION
    if "fallback" in normalized:
        return QuotaSource.KNOWN_GUC_FALLBACK
    if any(token in normalized for token in ("user", "configured", "override")):
        return QuotaSource.USER_CONFIGURED
    if any(token in normalized for token in ("detect", "mapi", "soap")):
        return QuotaSource.AUTOMATICALLY_DETECTED
    if any(token in normalized for token in ("ews", "server", "exact")):
        return QuotaSource.SERVER_REPORTED
    return QuotaSource.UNKNOWN


def _effective_quota_confidence(
    explicit: QuotaConfidence,
    source: QuotaSource,
) -> QuotaConfidence:
    if explicit is not QuotaConfidence.UNKNOWN:
        return explicit
    if source is QuotaSource.SERVER_REPORTED:
        return QuotaConfidence.EXACT
    if source in {QuotaSource.SERVER_NOTIFICATION, QuotaSource.USER_CONFIGURED}:
        return QuotaConfidence.HIGH
    if source in {QuotaSource.AUTOMATICALLY_DETECTED, QuotaSource.KNOWN_GUC_FALLBACK}:
        return QuotaConfidence.MEDIUM
    if source is QuotaSource.ESTIMATED:
        return QuotaConfidence.LOW
    return QuotaConfidence.UNKNOWN


@dataclass(frozen=True, slots=True)
class SearchExclusion:
    """One explicit negative clause retained by every search front end."""

    field: str
    value: str
    expression: str = ""

    def __post_init__(self) -> None:
        if self.field not in {
            "attachment_type",
            "folder",
            "has_attachments",
            "item_type",
            "sender",
            "text",
        }:
            raise ValueError(f"Unsupported search exclusion field: {self.field}")
        if not self.value.strip():
            raise ValueError("A search exclusion value cannot be empty.")


@dataclass(frozen=True, slots=True)
class SearchRanking:
    """Deterministic ordering and an optional result cap for a mailbox search."""

    field: str
    direction: str
    limit: int | None = None
    expression: str = ""

    def __post_init__(self) -> None:
        if self.field not in {
            "attachment_type_frequency",
            "received_at",
            "sender_frequency",
            "size",
            "thread_message_count",
        }:
            raise ValueError(f"Unsupported search ranking field: {self.field}")
        if self.direction not in {"ascending", "descending"}:
            raise ValueError(f"Unsupported search ranking direction: {self.direction}")
        if self.limit is not None and not 1 <= self.limit <= 1_000:
            raise ValueError("A search ranking limit must be between 1 and 1000.")


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str | None = None
    sender: str | None = None
    recipient: str | None = None
    to: str | None = None
    cc: str | None = None
    bcc: str | None = None
    subject: str | None = None
    body: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    has_attachments: bool | None = None
    attachment_name: str | None = None
    is_read: bool | None = None
    flagged: bool | None = None
    importance: Importance | None = None
    folder: str | None = None
    categories: tuple[str, ...] = ()
    minimum_size: int | None = None
    maximum_size: int | None = None
    internet_message_id: str | None = None
    fuzzy: bool = False
    fuzzy_threshold: int = 82
    exclusions: tuple[SearchExclusion, ...] = ()
    ranking: SearchRanking | None = None
    thread_response: str | None = None
    mailbox_scope: bool = False


@dataclass(frozen=True, slots=True)
class Capability:
    operation: str
    family: str
    safety: SafetyClass
    status: CapabilityStatus
    minimum_version: str | None = None
    implementation_path: ImplementationPath = ImplementationPath.EXCHANGELIB
    implementation_status: ImplementationStatus = ImplementationStatus.IMPLEMENTED
    server_support: ServerSupport = ServerSupport.NOT_PROBED
    account_access: AccountAccess = AccountAccess.NOT_PROBED
    verification: VerificationLevel = VerificationLevel.STATIC
    retry_class: str = "read_only"
    dry_run: bool = False
    fallback: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    account_id: str
    folder: str | None = None
    message_reference: str | None = None
    sender: Address | None = None
    subject: str | None = None
    importance: Importance = Importance.NORMAL
    has_attachments: bool = False
    preview: str | None = None
    open_hint: str | None = None


def to_primitive(value: Any) -> Any:
    """Convert domain values to deterministic JSON-compatible values."""

    safe_serializer = getattr(value, "to_safe_primitive", None)
    if callable(safe_serializer):
        return to_primitive(safe_serializer())
    if is_dataclass(value) and not isinstance(value, type):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (to_primitive(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_primitive(item) for item in value]
    return value
