"""Production Exchange Web Services gateway.

The gateway owns one lazily-created :mod:`exchangelib` ``Account``.  Importing this
module never resolves credentials and never touches the network.  Presentation and
service layers see only the domain models from :mod:`gucmail.models`.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Protocol, TypeVar

from gucmail.config import AppConfig
from gucmail.constants import MAX_PAGE_SIZE, USER_AGENT
from gucmail.credentials import CredentialData
from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    ErrorContext,
    GUCmailError,
    ItemNotFoundError,
    LocalStateError,
    MutationResultUnavailableError,
    NetworkError,
    SendOutcomeUncertainError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.ews.delegates import delegate_reader, map_delegate_collection
from gucmail.ews.inbox_rules import NativeInboxRules
from gucmail.ews.mapping import (
    body_parts,
    domain_datetime,
    make_local_reference,
    map_addresses,
    map_attachments,
    map_calendar_event,
    map_calendar_history,
    map_contact,
    map_folder,
    map_message_detail,
    map_message_summary,
    map_task,
)
from gucmail.ews.native_items import (
    NATIVE_ITEM_MEDIA_TYPE,
    NATIVE_ITEM_ROLE,
    NATIVE_ITEM_SCHEMA,
    NativeItemStreams,
    validate_item_identifier,
    validate_native_item,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    DraftData,
    DraftLocation,
    FolderInfo,
    Importance,
    MessageDetail,
    MessageSummary,
    QuotaInfo,
    SearchQuery,
    Sensitivity,
    TaskInfo,
    to_primitive,
)
from gucmail.security import sanitize_terminal
from gucmail.services.attachment_uploads import AttachmentUploadStore
from gucmail.services.automatic_replies import AutomaticReplySettings
from gucmail.services.calendar_history import CalendarSeriesHistory, history_limit
from gucmail.services.contact_groups import (
    MAX_GROUP_MEMBERS,
    ContactGroup,
    ContactGroupVerificationError,
    GroupSource,
    validate_group,
)
from gucmail.services.contacts import (
    CorrespondenceDirection,
    CorrespondenceEvent,
    contact_search_text,
)
from gucmail.services.delegates import DelegateInfo
from gucmail.services.folders import parse_folder_path
from gucmail.services.inbox_rules import InboxRule, SimpleInboxRule
from gucmail.services.pim import PimPage
from gucmail.services.search import (
    QueryExpression,
    SearchPlan,
    build_search_plan,
    evaluate_residual,
)
from gucmail.services.task_history import TaskHistory, TaskHistoryPage
from gucmail.state import StateStore

SUMMARY_FIELDS = (
    "subject",
    "sender",
    "author",
    "datetime_received",
    "datetime_sent",
    "is_read",
    "has_attachments",
    "importance",
    "categories",
    "size",
    "conversation_id",
    "gucmail_flag_status",
)
DETAIL_FIELDS = (
    *SUMMARY_FIELDS,
    "to_recipients",
    "cc_recipients",
    "bcc_recipients",
    "reply_to",
    "body",
    "sensitivity",
    "message_id",
    "in_reply_to",
    "references",
    "headers",
    "attachments",
)
RAW_FIELDS = ("mime_content",)
# Reply-all needs the original envelope to calculate recipients. Reply and forward
# do not need the original body, headers, MIME payload, or attachment content: EWS
# carries the referenced message (and its attachments for forwards) server-side.
REPLY_FIELDS = ("author", "to_recipients", "cc_recipients", "bcc_recipients")
DRAFT_FIELDS = (
    "is_draft",
    "subject",
    "body",
    "to_recipients",
    "cc_recipients",
    "bcc_recipients",
    "importance",
    "is_read_receipt_requested",
    "is_delivery_receipt_requested",
    "attachments",
    "last_modified_time",
)
FOLDER_FIELDS = (
    "parent_folder_id",
    "total_count",
    "unread_count",
    "child_folder_count",
)
CALENDAR_FIELDS = (
    "_start_timezone",
    "_end_timezone",
    "subject",
    "start",
    "end",
    "location",
    "body",
    "organizer",
    "required_attendees",
    "optional_attendees",
    "resources",
    "is_all_day",
    "is_meeting",
    "recurrence",
    "categories",
    "importance",
    "sensitivity",
    "reminder_is_set",
    "reminder_minutes_before_start",
    "legacy_free_busy_status",
    "attachments",
    "my_response_type",
    "type",
    "recurrence_id",
    "uid",
)
CALENDAR_METADATA_FIELDS = (
    "_start_timezone",
    "_end_timezone",
    "subject",
    "start",
    "end",
    "location",
    "is_all_day",
    "is_meeting",
    "categories",
    "importance",
    "sensitivity",
    "my_response_type",
    "type",
    "recurrence_id",
    "uid",
)
CALENDAR_HISTORY_FIELDS = (
    "type",
    "first_occurrence",
    "last_occurrence",
    "modified_occurrences",
    "deleted_occurrences",
)
CONTACT_FIELDS = (
    "item_class",
    "display_name",
    "given_name",
    "middle_name",
    "surname",
    "nickname",
    "email_addresses",
    "phone_numbers",
    "physical_addresses",
    "company_name",
    "department",
    "job_title",
    "birthday",
    "body",
    "categories",
    "has_picture",
    "photo",
)
CONTACT_METADATA_FIELDS = (
    "item_class",
    "display_name",
    "email_addresses",
    "company_name",
    "job_title",
    "categories",
    "has_picture",
)
CONTACT_SEARCH_FIELDS = tuple(field for field in CONTACT_FIELDS if field != "photo")
CORRESPONDENCE_FIELDS = (
    "sender",
    "author",
    "to_recipients",
    "cc_recipients",
    "bcc_recipients",
    "datetime_received",
    "datetime_sent",
)
TASK_FIELDS = (
    "subject",
    "status",
    "percent_complete",
    "start_date",
    "due_date",
    "complete_date",
    "body",
    "categories",
    "importance",
    "reminder_is_set",
    "reminder_due_by",
    "recurrence",
)
TASK_METADATA_FIELDS = (
    "subject",
    "status",
    "percent_complete",
    "start_date",
    "due_date",
    "complete_date",
    "categories",
    "importance",
    "reminder_is_set",
    "reminder_due_by",
)

_ORDER_FIELDS = {
    "received_at": "datetime_received",
    "datetime_received": "datetime_received",
    "sent_at": "datetime_sent",
    "datetime_sent": "datetime_sent",
    "subject": "subject",
    "sender": "sender",
    "size": "size",
    "importance": "importance",
}
_STANDARD_FOLDERS = {
    "archive": "archive_inbox",
    "calendar": "calendar",
    "contacts": "contacts",
    "deleted": "trash",
    "deleteditems": "trash",
    "deleted_items": "trash",
    "drafts": "drafts",
    "inbox": "inbox",
    "junk": "junk",
    "junkemail": "junk",
    "outbox": "outbox",
    "sent": "sent",
    "sentitems": "sent",
    "sent_items": "sent",
    "tasks": "tasks",
    "trash": "trash",
}
_REFERENCE_RE = re.compile(r"^[a-z]{3}_[a-z2-7]{16}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MAX_FOLDER_DEPTH = 64
_STALE_CHANGE_KEY_ERRORS = frozenset(
    {"ErrorIrresolvableConflict", "ErrorChangeKeyRequiredForWriteOperations"}
)
_MAIL_FOLDER_ROLES = frozenset(
    {"inbox", "sentitems", "drafts", "deleteditems", "junkemail", "outbox"}
)
_MAX_FOLDER_COMPONENT = 255
_MAX_ATTACHMENTS = 100
_ATTACHMENT_CHUNK_SIZE = 64 * 1024

T = TypeVar("T")


class AccountFactory(Protocol):
    def __call__(self) -> Any: ...


class CredentialProvider(Protocol):
    def __call__(self) -> CredentialData: ...


@dataclass(frozen=True, slots=True)
class AttachmentDownload:
    path: Path
    size: int
    sha256: str | None = None


class _AttachmentStream:
    """A closeable, non-seekable size-limited wrapper around FileAttachment.fp."""

    def __init__(self, raw: Any, maximum: int) -> None:
        self._raw = raw
        self._reader: BinaryIO = raw.__enter__()
        self._maximum = maximum
        self._read = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed attachment stream")
        remaining = self._maximum - self._read
        if remaining < 0:
            raise ConfigurationError("Attachment exceeds the configured download limit.")
        requested = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        data = self._reader.read(requested)
        self._read += len(data)
        if self._read > self._maximum:
            raise ConfigurationError("Attachment exceeds the configured download limit.")
        return data

    def readinto(self, buffer: bytearray | memoryview) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.read(_ATTACHMENT_CHUNK_SIZE):
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._reader.close()
        finally:
            self._raw.__exit__(None, None, None)

    def __enter__(self) -> _AttachmentStream:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _class_name(exc: BaseException) -> str:
    return type(exc).__name__


def translate_ews_error(
    exc: BaseException,
    *,
    operation: str,
    send_may_have_completed: bool = False,
) -> GUCmailError:
    """Translate transport-specific failures without echoing server or credential text."""

    if isinstance(exc, GUCmailError):
        return exc
    name = _class_name(exc)
    lowered = name.casefold()
    if name == "RateLimitError" or "serverbusy" in lowered or "throttl" in lowered:
        wait = getattr(exc, "wait", None) or getattr(exc, "back_off", None)
        return ThrottledError("Exchange requested a back-off before retrying.", retry_after=wait)
    if name in {
        "UnauthorizedError",
        "ErrorInvalidCredentials",
        "ErrorInvalidLogonType",
        "ErrorInvalidClientSecurityContext",
        "ErrorInvalidAuthorizationContext",
        "ErrorMailboxLogonFailed",
        "ErrorPasswordChangeRequired",
        "ErrorPasswordExpired",
        "ErrorAccountDisabled",
    }:
        return AuthenticationError(
            "Exchange rejected the account credentials or the account cannot sign in."
        )
    if name in {
        "DoesNotExist",
        "ErrorItemNotFound",
        "ErrorFolderNotFound",
        "ErrorParentFolderNotFound",
        "ErrorToFolderNotFound",
        "ErrorInvalidAttachmentId",
    }:
        return ItemNotFoundError(f"The requested Exchange object was not found during {operation}.")
    if name in {
        "ErrorInvalidChangeKey",
        "ErrorChangeKeyRequired",
        "ErrorChangeKeyRequiredForWriteOperations",
        "ErrorIrresolvableConflict",
        "ErrorStaleObject",
        "MultipleObjectsReturned",
    }:
        return ConflictError(
            f"Exchange reported that the object changed or was ambiguous during {operation}."
        )
    if name in {
        "ErrorQuotaExceeded",
        "ErrorSubmissionQuotaExceeded",
        "ErrorSendMeetingInvitationsOrCancellationsOverQuota",
    }:
        return ConflictError(
            "Exchange rejected the operation because a mailbox quota threshold was reached.",
            context=ErrorContext(
                retry_safe=False,
                suggestion=(
                    "Keep the local draft, reduce mailbox usage or attachments, and retry only "
                    "after Exchange reports that sending is available."
                ),
            ),
        )
    if name in {
        "ErrorInvalidRecipient",
        "ErrorInvalidRecipients",
        "ErrorNameResolutionNoResults",
        "ErrorRecipientNotFound",
        "ErrorUnresolvableRecipient",
    }:
        return ConfigurationError(
            "Exchange could not resolve one or more recipients.",
            context=ErrorContext(
                retry_safe=False,
                suggestion="Review every To, Cc, and Bcc address before trying again.",
            ),
        )
    if name in {
        "ErrorAttachmentSizeLimitExceeded",
        "ErrorMaxSubmissionExceeded",
        "ErrorMessageSizeExceeded",
    }:
        return ConfigurationError(
            "Exchange rejected the message because its submitted size is too large.",
            context=ErrorContext(
                retry_safe=False,
                suggestion="Remove or reduce attachments, then create a new send attempt.",
            ),
        )
    if name in {"ErrorSendAsDenied", "ErrorSendOnBehalfOfDenied"}:
        return UnsupportedFeatureError(
            "Exchange does not permit sending as the selected mailbox for this account."
        )
    if "unsupported" in lowered or name in {
        "ErrorAccessDenied",
        "ErrorNotDelegate",
        "ErrorUserNotAllowedByPolicy",
    }:
        return UnsupportedFeatureError(
            f"Exchange does not support or permit {operation} for this account."
        )
    is_transport = (
        isinstance(exc, (ConnectionError, TimeoutError, OSError))
        or "transport" in lowered
        or "connection" in lowered
        or "timeout" in lowered
        or name in {"SOAPError", "MalformedResponseError", "CASError"}
        or type(exc).__module__.startswith(("requests", "urllib3"))
    )
    if is_transport and send_may_have_completed:
        return SendOutcomeUncertainError(
            "The connection failed after Exchange may have accepted the message."
        )
    if is_transport:
        return NetworkError(
            f"Exchange could not complete {operation} because the connection failed."
        )
    if isinstance(exc, (TypeError, ValueError)):
        return ConfigurationError(f"Invalid data supplied for {operation} ({name}).")
    return GUCmailError(f"Exchange could not complete {operation} ({name}).")


def _load_exchangelib() -> SimpleNamespace:
    """Import and register the tiny exchangelib surface only when the account is used."""

    try:
        from exchangelib import (
            DELEGATE,
            Account,
            Body,
            CalendarItem,
            Configuration,
            Contact,
            Credentials,
            DistributionList,
            EWSDateTime,
            FailFast,
            FileAttachment,
            HTMLBody,
            Mailbox,
            Message,
            Q,
            Task,
        )
        from exchangelib.folders import Calendar as CalendarFolder
        from exchangelib.folders import Contacts as ContactsFolder
        from exchangelib.folders import Folder
        from exchangelib.folders import Tasks as TasksFolder
        from exchangelib.folders.queryset import SHALLOW, SingleFolderQuerySet
        from exchangelib.indexed_properties import (
            EmailAddress,
            PhoneNumber,
            PhysicalAddress,
        )
        from exchangelib.items import (
            HARD_DELETE,
            MOVE_TO_DELETED_ITEMS,
            SAVE_ONLY,
            SEND_AND_SAVE_COPY,
            SEND_ONLY,
            SEND_ONLY_TO_CHANGED,
            SEND_TO_ALL_AND_SAVE_COPY,
            SEND_TO_CHANGED_AND_SAVE_COPY,
            SEND_TO_NONE,
            SOFT_DELETE,
        )
        from exchangelib.properties import Attendee, Member
        from exchangelib.settings import OofSettings
    except ImportError as exc:
        raise ConfigurationError(
            "Exchange support is not installed. Install the project with its required dependencies."
        ) from exc

    try:
        Message.get_field_by_fieldname("gucmail_flag_status")
    except Exception:
        from exchangelib.extended_properties import Flag

        with contextlib.suppress(ValueError):
            Message.register("gucmail_flag_status", Flag)

    return SimpleNamespace(
        Account=Account,
        Attendee=Attendee,
        Body=Body,
        CalendarItem=CalendarItem,
        CalendarFolder=CalendarFolder,
        Configuration=Configuration,
        Contact=Contact,
        ContactsFolder=ContactsFolder,
        Credentials=Credentials,
        DistributionList=DistributionList,
        DELEGATE=DELEGATE,
        EmailAddress=EmailAddress,
        EWSDateTime=EWSDateTime,
        FailFast=FailFast,
        FileAttachment=FileAttachment,
        Folder=Folder,
        HARD_DELETE=HARD_DELETE,
        HTMLBody=HTMLBody,
        GetDelegate=delegate_reader,
        MOVE_TO_DELETED_ITEMS=MOVE_TO_DELETED_ITEMS,
        Mailbox=Mailbox,
        Member=Member,
        Message=Message,
        OofSettings=OofSettings,
        PhoneNumber=PhoneNumber,
        PhysicalAddress=PhysicalAddress,
        Q=Q,
        SAVE_ONLY=SAVE_ONLY,
        SEND_AND_SAVE_COPY=SEND_AND_SAVE_COPY,
        SEND_ONLY=SEND_ONLY,
        SEND_ONLY_TO_CHANGED=SEND_ONLY_TO_CHANGED,
        SEND_TO_ALL_AND_SAVE_COPY=SEND_TO_ALL_AND_SAVE_COPY,
        SEND_TO_CHANGED_AND_SAVE_COPY=SEND_TO_CHANGED_AND_SAVE_COPY,
        SEND_TO_NONE=SEND_TO_NONE,
        SHALLOW=SHALLOW,
        SOFT_DELETE=SOFT_DELETE,
        SingleFolderQuerySet=SingleFolderQuerySet,
        Task=Task,
        TasksFolder=TasksFolder,
    )


class EWSGateway:
    """Single-account, fake-friendly implementation of all core service protocols."""

    # This adapter currently implements bounded timestamp polling. Declaring the
    # strategy prevents presentation layers from mistaking the generic
    # ``poll_new_messages`` method name for EWS push or SyncFolderItems support.
    monitor_strategy = "bounded_poll"
    object_cache_limit = 512
    memory_reference_limit = 10_000

    def __init__(
        self,
        config: AppConfig,
        credentials: CredentialData | CredentialProvider | None = None,
        state: StateStore | None = None,
        *,
        account_factory: AccountFactory | None = None,
        bindings: object | None = None,
        factory_overrides: Mapping[str, Callable[..., Any]] | None = None,
        allow_raw_ews_ids: bool = False,
    ) -> None:
        self.config = config.validate()
        self._credentials = credentials
        self.state = state
        self._attachment_uploads = AttachmentUploadStore(state)
        self._account_factory = account_factory
        self._account: Any | None = None
        self._bindings_value = bindings
        self._factory_overrides = dict(factory_overrides or {})
        self.allow_raw_ews_ids = allow_raw_ews_ids
        self._account_lock = threading.RLock()
        self._session_reset_lock = threading.RLock()
        self._reference_lock = threading.RLock()
        self._objects: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._memory_references: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._parents: dict[tuple[str, str], Any] = {}
        self._folder_paths: dict[str, str] = {}
        self._attachment_parents: dict[str, str] = {}
        self._session_generation = 0
        self._session_healthy = False
        self._closed = False

    @property
    def connected(self) -> bool:
        """Whether an Account has been constructed; this never initiates a connection."""

        return self._account is not None and not self._closed

    @property
    def account(self) -> Any:
        if self._closed:
            raise NetworkError("The Exchange gateway is closed.")
        if self._account is not None:
            return self._account
        with self._account_lock:
            if self._account is None:
                self._account = self._create_account()
            return self._account

    def _bindings(self) -> Any:
        if self._bindings_value is None:
            with self._account_lock:
                if self._bindings_value is None:
                    self._bindings_value = _load_exchangelib()
        return self._bindings_value

    def _create_account(self) -> Any:
        if self._account_factory is not None:
            return self._account_factory()
        if self._credentials is None:
            raise AuthenticationError(
                "No credential provider was supplied for the Exchange account."
            )
        data = self._credentials() if callable(self._credentials) else self._credentials
        if not isinstance(data, CredentialData):
            raise AuthenticationError("The credential provider returned invalid account data.")
        lib = self._bindings()
        ews_credentials = lib.Credentials(username=data.username, password=data.password)
        configuration = lib.Configuration(
            service_endpoint=self.config.endpoint,
            credentials=ews_credentials,
            retry_policy=lib.FailFast(),
            max_connections=1,
        )
        try:
            account = lib.Account(
                primary_smtp_address=data.email,
                config=configuration,
                autodiscover=False,
                access_type=lib.DELEGATE,
            )
        except BaseException as exc:
            raise translate_ews_error(exc, operation="account setup") from exc
        account.protocol.TIMEOUT = self.config.request_timeout_seconds
        account.protocol.USERAGENT = USER_AGENT
        return account

    def _construct(self, class_name: str, *args: Any, **kwargs: Any) -> Any:
        factory = self._factory_overrides.get(class_name)
        if factory is not None:
            return factory(*args, **kwargs)
        return getattr(self._bindings(), class_name)(*args, **kwargs)

    def _call(
        self,
        operation: str,
        function: Callable[[], T],
        *,
        send_may_have_completed: bool = False,
        read_only: bool = False,
    ) -> T:
        """Run an EWS operation with deliberately narrow reconnect semantics.

        A read may be repeated once only when this gateway previously completed a
        read successfully and the current failure looks like a stale HTTP session.
        Initial authentication, throttling, and every mailbox mutation are never
        retried here.  This keeps a lost send from becoming a duplicate send.
        """

        with self._session_reset_lock:
            generation = self._session_generation
        for attempt in range(2):
            try:
                result = function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                retry_stale_read = (
                    read_only
                    and attempt == 0
                    and self._has_healthy_session()
                    and self._is_stale_session_error(exc)
                )
                if retry_stale_read:
                    time.sleep(max(2.0, self.config.retry_base_seconds))
                    self._renew_sessions(expected_generation=generation)
                    continue
                raise translate_ews_error(
                    exc,
                    operation=operation,
                    send_may_have_completed=send_may_have_completed,
                ) from exc
            if read_only:
                with self._session_reset_lock:
                    self._session_healthy = True
            return result
        raise AssertionError("unreachable EWS retry state")

    def _has_healthy_session(self) -> bool:
        with self._session_reset_lock:
            return self._session_healthy

    @staticmethod
    def _is_stale_session_error(exc: BaseException) -> bool:
        """Return whether one idempotent retry can safely replace the HTTP session."""

        if isinstance(exc, GUCmailError):
            return False
        name = _class_name(exc)
        lowered = name.casefold()
        if name == "RateLimitError" or "serverbusy" in lowered or "throttl" in lowered:
            return False
        if name in {
            "UnauthorizedError",
            "ErrorInvalidClientSecurityContext",
            "ErrorInvalidAuthorizationContext",
        }:
            return True
        return (
            isinstance(exc, (ConnectionError, TimeoutError, OSError))
            or "transport" in lowered
            or "connection" in lowered
            or "timeout" in lowered
            or name in {"SOAPError", "MalformedResponseError", "CASError"}
            or type(exc).__module__.startswith(("requests", "urllib3"))
        )

    @property
    def session_generation(self) -> int:
        """Generation of the active account/session lifecycle (diagnostic only)."""

        with self._session_reset_lock:
            return self._session_generation

    @staticmethod
    def _close_protocol(account: Any | None) -> None:
        if account is None:
            return
        protocol = getattr(account, "protocol", None)
        close = getattr(protocol, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    def _renew_sessions(self, *, expected_generation: int) -> None:
        """Drain stale HTTP sessions while retaining the authenticated Account object."""

        with self._session_reset_lock:
            if expected_generation != self._session_generation:
                return
            with self._account_lock:
                account = self._account
            self._close_protocol(account)
            self._session_generation += 1
            self._session_healthy = False

    def reconnect(self) -> None:
        """Reset the lazy Account lifecycle without authenticating immediately.

        This is an explicit recovery action. Automatic recovery uses
        :meth:`_renew_sessions` instead, so normal reads reuse the same Account and
        credentials and never create an authentication loop.
        """

        with self._session_reset_lock:
            with self._account_lock:
                account, self._account = self._account, None
                self._closed = False
            self._close_protocol(account)
            # Provider objects retain their Account, including its sessions and
            # credentials. Resolve subsequent actions from persisted identities.
            with self._reference_lock:
                self._objects.clear()
                self._parents.clear()
                self._folder_paths.clear()
                self._attachment_parents.clear()
            self._session_generation += 1
            self._session_healthy = False

    @staticmethod
    def _page(limit: int, offset: int = 0) -> tuple[int, int]:
        if not isinstance(limit, int) or not isinstance(offset, int):
            raise ConfigurationError("Pagination values must be integers.")
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise ConfigurationError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
        if offset < 0:
            raise ConfigurationError("offset may not be negative.")
        return limit, offset

    @staticmethod
    def _id(value: object) -> str | None:
        raw = getattr(value, "id", None)
        return str(raw) if raw else None

    @staticmethod
    def _changekey(value: object) -> str | None:
        raw = getattr(value, "changekey", None)
        return str(raw) if raw else None

    def _cache_projection(self, value: Any) -> Any:
        """Keep provider identities, never a history of read message bodies."""
        module = type(value).__module__
        if module.startswith("exchangelib.items.message") and self._id(value):
            return self._construct(
                "Message",
                account=value.account,
                id=value.id,
                changekey=value.changekey,
            )
        if module.startswith("exchangelib.attachments"):
            attachment = copy.copy(value)
            parent_item = getattr(value, "parent_item", None)
            if parent_item is not None:
                attachment.parent_item = self._cache_projection(parent_item)
            return attachment
        return value

    def _remember(
        self,
        kind: str,
        value: Any,
        *,
        reference: str | None = None,
        parent: Any | None = None,
        folder_path: str | None = None,
    ) -> str:
        remote_id = self._id(value)
        requested_reference = reference or make_local_reference(kind, remote_id or repr(value))
        canonical_reference = requested_reference
        cache_value = True
        if self.state is not None and remote_id:
            # StateStore owns identity reconciliation.  In particular, an EWS move can
            # replace the remote ID while the local reference must remain stable.  The
            # store returns that canonical reference; older stores returned None, which
            # remains supported during migration.
            stored_reference = self.state.put_reference(
                requested_reference,
                kind,
                remote_id,
                change_key=self._changekey(value),
                folder_path=folder_path,
            )
            if isinstance(stored_reference, str) and stored_reference:
                canonical_reference = stored_reference
            # ``StateStore`` intentionally refuses to roll a canonical reference
            # back to an inactive pre-move EWS ID when a stale folder page arrives.
            # Do not undo that protection by caching the stale provider object in
            # memory under the canonical reference.
            resolve_reference = getattr(self.state, "resolve_reference", None)
            if callable(resolve_reference):
                current = resolve_reference(canonical_reference, kind)
                if isinstance(current, Mapping):
                    kind = str(current.get("item_kind", kind))
                    current_remote_id = current.get("ews_id")
                    if current_remote_id and str(current_remote_id) != remote_id:
                        cache_value = False
        with self._reference_lock:
            if canonical_reference != requested_reference:
                stale_key = (kind, requested_reference)
                old_parent = self._parents.pop(stale_key, None)
                self._objects.pop(stale_key, None)
                if parent is None:
                    parent = old_parent
            if not cache_value:
                return canonical_reference
            canonical_key = (kind, canonical_reference)
            if self.state is None and remote_id:
                previous = self._memory_references.get(canonical_key, {})
                self._memory_references[canonical_key] = {
                    "ews_id": remote_id,
                    "change_key": self._changekey(value),
                    "item_kind": kind,
                    "canonical_ref": canonical_reference,
                    "folder_path": folder_path or previous.get("folder_path"),
                }
                self._memory_references.move_to_end(canonical_key)
                while len(self._memory_references) > self.memory_reference_limit:
                    self._memory_references.popitem(last=False)
            self._objects[canonical_key] = self._cache_projection(value)
            self._objects.move_to_end(canonical_key)
            if parent is not None:
                self._parents[canonical_key] = self._cache_projection(parent)
            if kind == "folder" and remote_id and folder_path:
                self._folder_paths[remote_id] = folder_path
            while len(self._objects) > self.object_cache_limit:
                expired_key, expired = self._objects.popitem(last=False)
                self._parents.pop(expired_key, None)
                if expired_key[0] == "folder":
                    self._folder_paths.pop(self._id(expired) or "", None)
                if expired_key[0] == "attachment":
                    self._attachment_parents.pop(expired_key[1], None)
        return canonical_reference

    def _memory_object(self, reference: str, *kinds: str) -> tuple[str, Any] | None:
        with self._reference_lock:
            for kind in kinds:
                value = self._objects.get((kind, reference))
                if value is not None:
                    self._objects.move_to_end((kind, reference))
                    return kind, value
        return None

    def _state_reference(self, reference: str, *kinds: str) -> tuple[str, dict[str, Any]] | None:
        if self.state is None:
            with self._reference_lock:
                for kind in kinds:
                    snapshot = self._memory_references.get((kind, reference))
                    if snapshot is not None:
                        self._memory_references.move_to_end((kind, reference))
                        return kind, dict(snapshot)
            return None
        for kind in kinds:
            row = self.state.resolve_reference(reference, kind)
            if row:
                return str(row.get("item_kind", kind)), row
        return None

    def _is_current_object(self, kind: str, reference: str, value: Any) -> bool:
        """Return false when ``value`` is an inactive pre-move EWS identity."""

        remote_id = self._id(value)
        if self.state is None or remote_id is None:
            return True
        resolve_reference = getattr(self.state, "resolve_reference", None)
        if not callable(resolve_reference):
            return True
        resolved = resolve_reference(reference, kind)
        if not isinstance(resolved, Mapping) or not resolved.get("ews_id"):
            return True
        return str(resolved["ews_id"]) == remote_id

    @classmethod
    def _folder_parts(cls, value: str) -> tuple[str, ...]:
        text = value.strip()
        if not text or text == "/":
            return ()
        if _WINDOWS_DRIVE_RE.match(text):
            raise ConfigurationError(
                "Folder selectors are mailbox paths, not local filesystem paths."
            )
        return parse_folder_path(text)

    def _standard_folder(self, selector: str) -> Any | None:
        attribute = _STANDARD_FOLDERS.get(selector.casefold().replace(" ", ""))
        if attribute is None:
            return None
        return self._call(
            "standard folder resolution",
            lambda: getattr(self.account, attribute, None),
            read_only=True,
        )

    def _message_folder_root(self) -> Any:
        return self._call(
            "message folder root resolution",
            lambda: self.account.msg_folder_root,
            read_only=True,
        )

    def _account_root(self) -> Any:
        return self._call(
            "account root resolution",
            lambda: self.account.root,
            read_only=True,
        )

    def _resolve_folder(self, selector: str | None) -> Any:
        if selector is None:
            return self._message_folder_root()
        memory = self._memory_object(selector, "folder")
        if memory and self._is_current_object("folder", selector, memory[1]):
            return memory[1]
        standard = self._standard_folder(selector)
        if standard is not None:
            return standard
        state = self._state_reference(selector, "folder")
        if state:
            row = state[1]
            path = row.get("folder_path")
            if path:
                resolved = None
                with contextlib.suppress(ItemNotFoundError):
                    resolved = self._resolve_folder_path(str(path))
                if resolved is not None:
                    if self._id(resolved) != str(row["ews_id"]):
                        raise ConflictError(
                            "The saved folder path now identifies a different folder. "
                            "No mailbox change was attempted. Refresh the folder tree "
                            "and select the folder again."
                        )
                    return resolved
            return self._recover_folder_metadata(selector, str(row["ews_id"]))
        if _REFERENCE_RE.fullmatch(selector):
            raise ItemNotFoundError(f"Unknown local folder reference: {selector}")
        if self.allow_raw_ews_ids and selector.startswith("ews:"):
            return self._construct("Folder", root=self._account_root(), id=selector[4:])
        return self._resolve_folder_path(selector)

    def _recover_folder_metadata(self, reference: str, remote_id: str) -> Any:
        """Read one saved identity and bounded ancestry, never a lazy full hierarchy."""

        from exchangelib.services import GetFolder

        root = self._message_folder_root()
        root_id = self._id(root)
        if not root_id:
            raise ConfigurationError(
                "The mailbox root identity is unavailable for folder recovery."
            )
        current_id = remote_id
        seen: set[str] = set()
        chain: list[Any] = []
        for _depth in range(_MAX_FOLDER_DEPTH):
            if current_id == root_id:
                break
            if current_id in seen:
                raise ConfigurationError(
                    "Saved folder ancestry is cyclic. Refresh the folder tree."
                )
            seen.add(current_id)
            stub = self._construct("Folder", root=self._account_root(), id=current_id)
            fields = set(
                self._bindings()
                .SingleFolderQuerySet(account=self.account, folder=stub)
                .only("name", "folder_class", *FOLDER_FIELDS)
                .only_fields
            )

            def fetch(
                request_folder: Any = stub,
                additional_fields: Any = fields,
                expected_id: str = current_id,
            ) -> Any:
                values = tuple(
                    islice(
                        GetFolder(account=self.account).call(
                            folders=[request_folder],
                            additional_fields=additional_fields,
                            shape="IdOnly",
                        ),
                        2,
                    )
                )
                for value in values:
                    if isinstance(value, BaseException):
                        raise value
                if not values:
                    raise ItemNotFoundError(
                        "No folder metadata was returned for the saved reference. "
                        "Refresh the folder tree."
                    )
                if len(values) != 1:
                    raise ConfigurationError("Folder metadata response was ambiguous.")
                value = values[0]
                if self._id(value) != expected_id:
                    raise ConflictError(
                        "Folder metadata returned a different identity. "
                        "No mailbox change was attempted. Refresh the folder tree."
                    )
                name = getattr(value, "name", None)
                if not isinstance(name, str) or not name:
                    raise ConfigurationError("Folder metadata has no valid display name.")
                return value

            folder = self._call("saved folder metadata recovery", fetch, read_only=True)
            parent_id = self._id(getattr(folder, "parent_folder_id", None))
            if not parent_id:
                raise ConfigurationError("Folder metadata has no parent in the mailbox hierarchy.")
            chain.append(folder)
            current_id = parent_id
        else:
            raise ConfigurationError("Saved folder ancestry exceeds the safe depth limit.")

        # Validate the complete reconstructed path before recording any part of it.
        path = self._folder_path(root)
        paths: list[tuple[Any, str]] = []
        for folder in reversed(chain):
            path = self._folder_path(folder, parent_path=path)
            self._folder_parts(path)
            paths.append((folder, path))
        parent = root
        self._remember("folder", root, folder_path=self._folder_path(root))
        for folder, path in paths:
            self._remember(
                "folder",
                folder,
                reference=reference if self._id(folder) == remote_id else None,
                parent=parent,
                folder_path=path,
            )
            parent = folder
        return parent

    def _resolve_folder_path(self, selector: str) -> Any:
        parts = self._folder_parts(selector)
        if not parts:
            return self._message_folder_root()
        if len(parts) == 1:
            standard = self._standard_folder(parts[0])
            if standard is not None:
                return standard
        current = self._message_folder_root()
        root_names = {
            str(value).casefold()
            for value in (
                getattr(current, "name", None),
                getattr(current, "DISTINGUISHED_FOLDER_ID", None),
            )
            if value
        }
        if selector.strip().startswith("/") and parts[0].casefold() in root_names:
            # Discovery emits the root prefix. It is a namespace, not another child.
            parts = parts[1:]
        elif not selector.strip().startswith("/"):
            # Relative role prefixes use the same distinguished folder as a bare alias.
            # An absolute discovery path remains available for literal colliding names.
            standard = self._standard_folder(parts[0])
            if standard is not None:
                current = standard
                parts = parts[1:]
        current_path = self._folder_path(current)
        self._remember("folder", current, folder_path=current_path)
        for component in parts:
            parent = current
            current = self._find_child(parent, component)
            current_path = self._folder_path(current, parent_path=current_path)
            self._remember("folder", current, parent=parent, folder_path=current_path)
        return current

    def _find_child(self, parent: Any, name: str) -> Any:
        def find() -> Any:
            if hasattr(parent, "child_by_name"):
                return parent.child_by_name(name)
            if type(parent).__module__.startswith("exchangelib"):
                query = (
                    self._bindings()
                    .SingleFolderQuerySet(account=self.account, folder=parent)
                    .only("name", "folder_class", *FOLDER_FIELDS)
                    .depth(self._bindings().SHALLOW)
                    .filter(name=name)
                )
                values = tuple(
                    query.folder_collection.find_folders(
                        q=query.q,
                        additional_fields=set(query.only_fields),
                        depth=self._bindings().SHALLOW,
                        page_size=2,
                        max_items=2,
                        offset=0,
                    )
                )
                for value in values:
                    if isinstance(value, BaseException):
                        raise value
                if not values:
                    raise ItemNotFoundError(f"Folder not found below the selected parent: {name}")
                if len(values) != 1:
                    raise ConflictError("Folder name is ambiguous below the selected parent.")
                return values[0]
            children = getattr(parent, "children", ())
            matches = [
                item
                for item in children
                if str(getattr(item, "name", "")).casefold() == name.casefold()
            ]
            if not matches:
                raise ItemNotFoundError(
                    f"Folder not found below {getattr(parent, 'name', 'root')}: {name}"
                )
            if len(matches) > 1:
                raise ConflictError(f"Folder name is ambiguous below the selected parent: {name}")
            return matches[0]

        return self._call("folder resolution", find, read_only=True)

    def _folder_path(
        self,
        folder: Any,
        *,
        parent_path: str | None = None,
        _seen: frozenset[str] = frozenset(),
    ) -> str:
        remote_id = self._id(folder)
        if parent_path is None:
            identity = remote_id or str(id(folder))
            if identity in _seen or len(_seen) >= _MAX_FOLDER_DEPTH:
                raise ConfigurationError("Known folder parent links are cyclic or too deep.")
            with self._reference_lock:
                if remote_id and remote_id in self._folder_paths:
                    return self._folder_paths[remote_id]
                parent = next(
                    (
                        self._parents[key]
                        for key, value in self._objects.items()
                        if key[0] == "folder"
                        and self._id(value) == remote_id
                        and key in self._parents
                    ),
                    None,
                )
            if parent is not None:
                # Only use metadata already retained by a bounded lookup/listing.
                # exchangelib's lazy Folder.parent can enumerate the whole hierarchy.
                parent_path = self._folder_path(parent, _seen=_seen | {identity})
        name = str(getattr(folder, "name", "folder"))
        # Folder display names are opaque Exchange labels. Preserve literal slash
        # and backslash characters in the user-facing path grammar so discovery
        # remains round-trippable and duplicate names stay scoped by parent.
        encoded_name = name.replace("\\", "\\\\").replace("/", "\\/")
        if parent_path:
            return f"{'' if parent_path == '/' else parent_path}/{encoded_name}"
        role = getattr(folder, "DISTINGUISHED_FOLDER_ID", None)
        return f"/{role or encoded_name}"

    def _relocate_known_folder_paths(self, old_path: str, new_path: str) -> None:
        """Update retained subtree metadata after a successful local folder mutation."""

        if self.state is not None:
            self.state.relocate_reference_folder_paths(old_path, new_path)
        with self._reference_lock:
            for snapshot in self._memory_references.values():
                path = snapshot.get("folder_path")
                if isinstance(path, str) and (path == old_path or path.startswith(old_path + "/")):
                    snapshot["folder_path"] = new_path + path[len(old_path) :]
            self._folder_paths.clear()

    def _folder_change_result(self, publish: Callable[[], FolderInfo]) -> FolderInfo:
        """Report accepted changes accurately if local reconciliation cannot complete."""

        try:
            return publish()
        except Exception as exc:
            with self._reference_lock:
                self._folder_paths.clear()
                for key in tuple(self._objects):
                    if key[0] == "folder":
                        self._objects.pop(key, None)
                        self._parents.pop(key, None)
            raise MutationResultUnavailableError(
                "Exchange accepted the folder change, but its local result could not be recorded. "
                "Do not repeat the change automatically.",
                suggestion="Refresh the folder tree and inspect the folder before retrying.",
            ) from exc

    def list_folders(
        self,
        parent_reference: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[FolderInfo, ...]:
        limit, offset = self._page(limit, offset)
        parent = self._resolve_folder(parent_reference)
        parent_path = self._folder_path(parent)

        def load() -> tuple[FolderInfo, ...]:
            if type(parent).__module__.startswith("exchangelib"):
                query = (
                    self._bindings()
                    .SingleFolderQuerySet(account=self.account, folder=parent)
                    .only(*FOLDER_FIELDS)
                    .depth(self._bindings().SHALLOW)
                )
                children = query.folder_collection.find_folders(
                    additional_fields=set(query.only_fields),
                    depth=self._bindings().SHALLOW,
                    page_size=limit,
                    max_items=limit,
                    offset=offset,
                )
            else:
                raw = getattr(parent, "children", ())
                raw = raw() if callable(raw) else raw
                children = islice(iter(raw), offset, offset + limit)
            result: list[FolderInfo] = []
            parent_ref = self._remember(
                "folder",
                parent,
                reference=parent_reference,
                folder_path=parent_path,
            )
            for child in children:
                if isinstance(child, BaseException):
                    raise child
                path = self._folder_path(child, parent_path=parent_path)
                reference = self._remember("folder", child, parent=parent, folder_path=path)
                result.append(
                    map_folder(
                        child,
                        reference=reference,
                        parent_reference=parent_ref,
                        path=path,
                    )
                )
            return tuple(result)

        return self._call("folder listing", load, read_only=True)

    def get_folder(self, reference: str) -> FolderInfo:
        folder = self._resolve_folder(reference)
        path = self._folder_path(folder)
        local = self._remember("folder", folder, reference=reference, folder_path=path)
        return map_folder(folder, reference=local, path=path)

    resolve_folder = get_folder
    resolve_folder_path = get_folder

    def _versioned_protocol(self) -> Any:
        """Return the protocol with its EWS version set for protocol-level services.

        exchangelib's ResolveNames and GetUserAvailability take their version hint from
        the protocol configuration, which stays unset when the account carries the
        negotiated version. Without it they fail with an AttributeError.
        """

        protocol = self.account.protocol
        config = getattr(protocol, "config", None)
        if config is not None and getattr(config, "version", None) is None:
            version = getattr(self.account, "version", None)
            if version is not None:
                config.version = version
        return protocol

    def search_directory(self, query: str, *, limit: int = 25) -> tuple[Address, ...]:
        """Resolve one partial name against Active Directory through ResolveNames."""

        value = " ".join(query.split())
        if not value or any(character in value for character in "\r\n\x00"):
            raise ConfigurationError("Directory search text is empty or invalid.")
        limit, _ = self._page(limit, 0)
        limit = min(limit, 100)

        def resolve() -> tuple[Address, ...]:
            raw = self._versioned_protocol().resolve_names(
                [value],
                return_full_contact_data=False,
                search_scope="ActiveDirectory",
            )
            results: list[Address] = []
            seen: set[str] = set()
            first_error: BaseException | None = None
            for entry in raw:
                if isinstance(entry, BaseException):
                    if type(entry).__name__ != "ErrorNameResolutionNoResults":
                        first_error = first_error or entry
                    continue
                mailbox = entry[0] if isinstance(entry, tuple) else entry
                email = str(getattr(mailbox, "email_address", "") or "").strip()
                if not email or email.casefold() in seen:
                    continue
                seen.add(email.casefold())
                results.append(
                    Address(
                        email=email,
                        name=str(getattr(mailbox, "name", "") or "").strip() or None,
                        mailbox_type=(
                            str(getattr(mailbox, "mailbox_type", "") or "").strip() or None
                        ),
                    )
                )
                if len(results) >= limit:
                    break
            if not results and first_error is not None:
                raise first_error
            return tuple(results)

        return self._call("directory name resolution", resolve, read_only=True)

    @classmethod
    def _folder_name(cls, name: str) -> str:
        # This argument is an already-separated display name, not a path selector.
        # EWS permits unusual labels; escaping is applied later when a path is built.
        value = str(name).strip()
        if not value or value in {".", ".."}:
            raise ConfigurationError("Folder names may not be empty or traversal tokens.")
        if len(value) > _MAX_FOLDER_COMPONENT or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ConfigurationError("Folder name is invalid or too long.")
        return value

    def _save_new_folder(self, folder: Any, parent: Any) -> None:
        """Create under the known parent without hydrating the whole folder tree."""
        if not type(folder).__module__.startswith("exchangelib"):
            folder.save()
            return
        from exchangelib.services import CreateFolder

        result = CreateFolder(account=self.account).get(parent_folder=parent, folders=[folder])
        folder.id, folder.changekey = result.id, result.changekey
        cached = getattr(folder.root, "_subfolders", None)
        if cached is not None:
            cached[folder.id] = folder

    def create_folder(self, parent_reference: str | None, name: str) -> FolderInfo:
        parent = self._resolve_folder(parent_reference)
        value = self._folder_name(name)

        def create() -> FolderInfo:
            folder = self._construct("Folder", parent=parent, name=value)
            self._save_new_folder(folder, parent)
            path = self._folder_path(folder, parent_path=self._folder_path(parent))
            parent_local = self._remember(
                "folder",
                parent,
                reference=parent_reference,
                folder_path=self._folder_path(parent),
            )
            reference = self._remember("folder", folder, parent=parent, folder_path=path)
            return map_folder(
                folder,
                reference=reference,
                parent_reference=parent_local,
                path=path,
            )

        return self._call("folder creation", create)

    def rename_folder(self, reference: str, new_name: str) -> FolderInfo:
        folder = self._resolve_folder(reference)
        value = self._folder_name(new_name)

        def rename() -> FolderInfo:
            old_path = self._folder_path(folder)
            provider_folder = type(folder).__module__.startswith("exchangelib")
            # Folder.parent and Folder.save() both consult exchangelib's lazy
            # hierarchy map. Use already discovered paths and the narrow service.
            with self._reference_lock:
                parent = self._parents.get(("folder", reference))
                known_path = self._folder_paths.get(self._id(folder) or "")
            if parent is None and not provider_folder:
                parent = getattr(folder, "parent", None)
            parent_path = self._folder_path(parent) if parent is not None else None
            if parent_path is None and known_path:
                parts = self._folder_parts(known_path)
                parent_path = "/" + "/".join(
                    part.replace("\\", "\\\\").replace("/", "\\/") for part in parts[:-1]
                )
            folder.name = value
            if provider_folder:
                from exchangelib.services import UpdateFolder

                result = UpdateFolder(account=self.account).get(folders=[(folder, ["name"])])
                folder.id, folder.changekey = result.id, result.changekey
                cached = getattr(folder.root, "_subfolders", None)
                if cached is not None:
                    cached[folder.id] = folder
            else:
                folder.save(update_fields=["name"])

            def publish() -> FolderInfo:
                # Rebase retained descendant metadata without enumerating the subtree.
                path = self._folder_path(folder, parent_path=parent_path)
                self._relocate_known_folder_paths(old_path, path)
                canonical = self._remember(
                    "folder", folder, reference=reference, parent=parent, folder_path=path
                )
                return map_folder(folder, reference=canonical, path=path)

            return self._folder_change_result(publish)

        return self._call("folder rename", rename)

    def move_folder(
        self,
        reference: str,
        destination_reference: str | None,
    ) -> FolderInfo:
        """Move one custom folder and preserve its stable local reference."""

        folder = self._resolve_folder(reference)
        destination = self._resolve_folder(destination_reference)
        if not bool(getattr(folder, "is_deletable", True)):
            raise UnsupportedFeatureError("Distinguished Exchange folders cannot be moved.")
        folder_id = self._id(folder)
        destination_id = self._id(destination)
        if folder is destination or (folder_id and folder_id == destination_id):
            raise ConfigurationError("A folder cannot be moved into itself.")
        parent_id = self._id(getattr(folder, "parent_folder_id", None))
        if destination_id and parent_id == destination_id:
            raise ConfigurationError("The folder is already below that destination.")
        move = getattr(folder, "move", None)
        if not callable(move):
            raise UnsupportedFeatureError("This Exchange folder type cannot be moved.")
        destination_path = self._folder_path(destination)
        old_path = self._folder_path(folder)

        def execute() -> FolderInfo:
            if type(folder).__module__.startswith("exchangelib"):
                from exchangelib.properties import ParentFolderId
                from exchangelib.services import MoveFolder

                result = MoveFolder(account=self.account).get(
                    folders=[folder], to_folder=destination
                )
                folder.id, folder.changekey = result.id, result.changekey
                folder.parent_folder_id = ParentFolderId(
                    id=destination.id, changekey=destination.changekey
                )
                cached = getattr(folder.root, "_subfolders", None)
                if cached is not None:
                    cached.pop(folder_id, None)
                    cached[folder.id] = folder
            else:
                move(destination)

            def publish() -> FolderInfo:
                path = self._folder_path(folder, parent_path=destination_path)
                self._relocate_known_folder_paths(old_path, path)
                destination_local = self._remember(
                    "folder",
                    destination,
                    reference=destination_reference,
                    folder_path=destination_path,
                )
                canonical = self._remember(
                    "folder",
                    folder,
                    reference=reference,
                    parent=destination,
                    folder_path=path,
                )
                return map_folder(
                    folder,
                    reference=canonical,
                    parent_reference=destination_local,
                    path=path,
                )

            return self._folder_change_result(publish)

        return self._call("folder move", execute)

    def delete_folder(self, reference: str, permanent: bool = False) -> None:
        folder = self._resolve_folder(reference)
        if not bool(getattr(folder, "is_deletable", True)):
            raise UnsupportedFeatureError("Distinguished Exchange folders cannot be deleted.")
        delete_type = (
            self._bindings().HARD_DELETE if permanent else self._bindings().MOVE_TO_DELETED_ITEMS
        )

        def delete() -> None:
            if not type(folder).__module__.startswith("exchangelib"):
                folder.delete(delete_type=delete_type)
                return
            from exchangelib.services import DeleteFolder

            DeleteFolder(account=self.account).get(folders=[folder], delete_type=delete_type)
            cached = getattr(folder.root, "_subfolders", None)
            if cached is not None:
                cached.pop(folder.id, None)
            folder._id = None

        self._call("folder deletion", delete)

    def empty_folder(self, reference: str, permanent: bool = False) -> None:
        folder = self._resolve_folder(reference)
        delete_type = (
            self._bindings().HARD_DELETE if permanent else self._bindings().MOVE_TO_DELETED_ITEMS
        )
        self._call(
            "empty folder",
            lambda: folder.empty(delete_type=delete_type, delete_sub_folders=False),
        )

    # ------------------------------------------------------------------
    # Session and bounded mailbox metadata

    def close(self) -> None:
        """Close the shared protocol session without constructing one just to close it."""

        with self._session_reset_lock:
            with self._account_lock:
                account, self._account = self._account, None
                self._closed = True
            self._close_protocol(account)
            self._session_generation += 1
            self._session_healthy = False

    def status(self) -> dict[str, Any]:
        """Perform the deliberately small online status probe used by ``login``/``status``."""

        def load() -> dict[str, Any]:
            account = self.account
            inbox = account.inbox
            version = getattr(getattr(account, "version", None), "build", None)
            return {
                "connected": True,
                "endpoint": self.config.endpoint,
                "exchange_version": str(version or "detected"),
                "inbox_count": getattr(inbox, "total_count", None),
                "inbox_unread": getattr(inbox, "unread_count", None),
                "quota": "unavailable",
            }

        return self._call("bounded account status", load, read_only=True)

    # ------------------------------------------------------------------
    # Shared item resolution and bounded queries

    def _resolve_item(self, reference: str, *kinds: str) -> tuple[str, Any]:
        memory = self._memory_object(reference, *kinds)
        stored = self._state_reference(reference, *kinds)
        if memory is not None:
            if stored is None:
                return memory
            kind, row = stored
            cached = memory[1]
            if self._id(cached) == str(row["ews_id"]) and self._changekey(cached) == row.get(
                "change_key"
            ):
                return memory
            # A foreground command or watcher can update the shared identity
            # while this gateway stays alive. Never reuse the pre-move object or
            # its parent, and do not write its old ChangeKey back to the store.
            with self._reference_lock:
                self._objects.pop((memory[0], reference), None)
                self._parents.pop((memory[0], reference), None)
        if stored is not None:
            kind, row = stored
            class_name = {
                "calendar": "CalendarItem",
                "event": "CalendarItem",
                "contact": "Contact",
                "contact_group": "DistributionList",
                "task": "Task",
            }.get(kind, "Message")
            item = self._construct(
                class_name,
                account=self.account,
                id=str(row["ews_id"]),
                changekey=row.get("change_key"),
            )
            self._remember(kind, item, reference=reference)
            return kind, item
        if _REFERENCE_RE.fullmatch(reference):
            raise ItemNotFoundError(f"Unknown local item reference: {reference}")
        if self.allow_raw_ews_ids and reference.startswith("ews:"):
            kind = kinds[0] if kinds else "message"
            class_name = {
                "calendar": "CalendarItem",
                "event": "CalendarItem",
                "contact": "Contact",
                "contact_group": "DistributionList",
                "task": "Task",
            }.get(kind, "Message")
            return kind, self._construct(class_name, account=self.account, id=reference[4:])
        raise ItemNotFoundError(f"Unknown local item reference: {reference}")

    def _fetch_item(self, reference: str, kinds: tuple[str, ...], fields: tuple[str, ...]) -> Any:
        kind, item = self._resolve_item(reference, *kinds)
        if not type(item).__module__.startswith("exchangelib") or not self._id(item):
            return item

        def fetch() -> Any:
            identities = [(self._id(item), self._changekey(item))]
            results = self.account.fetch(ids=identities, only_fields=fields)
            pim_class = {
                "calendar": "CalendarItem",
                "event": "CalendarItem",
                "contact": "Contact",
                "task": "Task",
            }.get(kind)
            label = "Task" if kind == "task" else kind.title()
            if pim_class is not None:
                values = list(islice(results, 2))
                if len(values) > 1:
                    raise ConfigurationError(f"{label} detail retrieval returned multiple items.")
                value = values[0] if values else None
            else:
                value = next(iter(results), None)
            if value is None:
                raise ItemNotFoundError(f"The Exchange item no longer exists: {reference}")
            if isinstance(value, BaseException):
                raise value
            if pim_class is not None:
                expected = getattr(self._bindings(), pim_class)
                if (
                    kind == "contact"
                    and self._id(value) == self._id(item)
                    and self._is_contact_group(value)
                ):
                    raise UnsupportedFeatureError("Use the contact-group workflow for this item.")
                if not isinstance(value, expected) or self._id(value) != self._id(item):
                    raise ConfigurationError(
                        f"{label} detail retrieval did not return the requested {label}. "
                        f"The local identity was preserved; refresh the bounded {label}s list."
                    )
            self._remember(kind, value, reference=reference)
            return value

        return self._call("item detail retrieval", fetch, read_only=True)

    @staticmethod
    def _query_source(folder: Any) -> Any:
        source = getattr(folder, "all", None)
        if callable(source):
            return source()
        for attribute in ("messages", "items", "events", "contacts", "tasks"):
            value = getattr(folder, attribute, None)
            if value is not None:
                return value() if callable(value) else value
        return ()

    def _bounded_items(
        self,
        folder: Any,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> tuple[Any, ...]:
        limit, offset = self._page(limit, offset)

        def load() -> tuple[Any, ...]:
            query = self._query_source(folder)
            if type(query).__module__.startswith("exchangelib"):
                if filters:
                    query = query.filter(**dict(filters))
                query = query.only(*fields)
                if order_by:
                    query = query.order_by(order_by)
                # QuerySet slicing pushes both the offset and page size into
                # FindItem. islice would download preceding pages and fetch a
                # default-sized server page even for a three-message request.
                values = query[offset : offset + limit]
            else:
                raw = list(query)
                if order_by:
                    reverse = order_by.startswith("-")
                    name = order_by.lstrip("-")
                    raw.sort(
                        key=lambda item: (
                            getattr(item, name, None) is not None,
                            getattr(item, name, None),
                        ),
                        reverse=reverse,
                    )
                values = islice(raw, offset, offset + limit)
            result: list[Any] = []
            for value in values:
                if isinstance(value, BaseException):
                    raise value
                result.append(value)
            return tuple(result)

        return self._call("bounded item listing", load, read_only=True)

    # ------------------------------------------------------------------
    # Messages and attachments

    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 50,
        offset: int = 0,
        order_by: str = "-datetime_received",
    ) -> tuple[MessageSummary, ...]:
        virtual = folder.casefold().strip()
        actual_folder = "inbox" if virtual in {"unread", "flagged"} else folder
        resolved = self._resolve_folder(actual_folder)
        normalized = order_by.strip()
        descending = normalized.startswith("-")
        field_name = _ORDER_FIELDS.get(normalized.lstrip("-"))
        if field_name is None:
            raise ConfigurationError(f"Unsupported message order: {order_by}")
        ews_order = f"-{field_name}" if descending else field_name
        filters: dict[str, Any] = {}
        if virtual == "unread":
            filters["is_read"] = False
        values = self._bounded_items(
            resolved,
            fields=SUMMARY_FIELDS,
            limit=limit,
            offset=offset,
            order_by=ews_order,
            filters=filters,
        )
        path = self._folder_path(resolved)
        summaries: list[MessageSummary] = []
        for item in values:
            reference = self._remember("message", item, parent=resolved, folder_path=path)
            if not self._is_current_object("message", reference, item):
                continue
            summary = map_message_summary(item, reference=reference, folder_path=path)
            if virtual == "flagged" and not summary.flagged:
                continue
            summaries.append(summary)
        return tuple(summaries)

    def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail:
        kind, _ = self._resolve_item(reference, "message", "draft")
        item = self._fetch_item(reference, (kind,), DETAIL_FIELDS)
        if mark_read and not bool(getattr(item, "is_read", False)):
            item.is_read = True
            self._call("mark message read", lambda: item.save(update_fields=["is_read"]))
        parent = self._parents.get((kind, reference))
        stored_path = None
        if parent is None:
            stored = self._state_reference(reference, "message", "draft")
            if stored is not None:
                canonical_hint = str(stored[1].get("canonical_ref") or reference)
                parent = self._parents.get((stored[0], canonical_hint))
                stored_path = stored[1].get("folder_path")
        if parent is None:
            parent = getattr(item, "folder", None)
        path = self._folder_path(parent) if parent is not None else stored_path
        canonical = self._remember(kind, item, reference=reference, parent=parent, folder_path=path)
        detail = map_message_detail(item, reference=canonical, folder_path=path)
        for attachment, metadata in zip(
            tuple(getattr(item, "attachments", ()) or ()), detail.attachments, strict=False
        ):
            self._remember("attachment", attachment, reference=metadata.reference, parent=item)
            self._attachment_parents[metadata.reference] = canonical
        return detail

    def raw_message(self, reference: str) -> bytes:
        """Return bounded MIME bytes for an intentional file export."""

        item = self._fetch_item(reference, ("message", "draft"), RAW_FIELDS)
        value = getattr(item, "mime_content", None)
        if value is None:
            raise UnsupportedFeatureError("Exchange did not provide MIME for this item.")
        data = bytes(value)
        maximum = max(self.config.max_attachment_bytes * 2, 10 * 1024 * 1024)
        if len(data) > maximum:
            raise ConfigurationError("The MIME message exceeds the configured export limit.")
        return data

    # Compatibility with the first TUI prototype and older integrations.
    get_message = show_message

    @staticmethod
    def _summary_matches(summary: MessageSummary, query: SearchQuery) -> bool:
        sender = summary.sender.email if summary.sender else ""
        subject = summary.subject.casefold()
        if query.sender and query.sender.casefold() not in sender.casefold():
            return False
        if query.subject and query.subject.casefold() not in subject:
            return False
        if query.after or query.before:
            if summary.received_at is None:
                return False
            received = summary.received_at
            received_utc = (
                received.replace(tzinfo=UTC)
                if received.tzinfo is None
                else received.astimezone(UTC)
            )
            if query.after:
                after = (
                    query.after.replace(tzinfo=UTC)
                    if query.after.tzinfo is None
                    else query.after.astimezone(UTC)
                )
                if received_utc < after:
                    return False
            if query.before:
                before = (
                    query.before.replace(tzinfo=UTC)
                    if query.before.tzinfo is None
                    else query.before.astimezone(UTC)
                )
                if received_utc >= before:
                    return False
        if query.has_attachments is not None and summary.has_attachments != query.has_attachments:
            return False
        if query.is_read is not None and summary.is_read != query.is_read:
            return False
        if query.flagged is not None and summary.flagged != query.flagged:
            return False
        if query.importance is not None and summary.importance != query.importance:
            return False
        if query.categories and not {value.casefold() for value in query.categories}.issubset(
            {value.casefold() for value in summary.categories}
        ):
            return False
        if query.minimum_size is not None and (
            summary.size is None or summary.size < query.minimum_size
        ):
            return False
        return not (
            query.maximum_size is not None
            and (summary.size is None or summary.size > query.maximum_size)
        )

    def search_messages(
        self,
        query: SearchQuery,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MessageSummary, ...]:
        plan = build_search_plan(query, residual_candidate_limit=MAX_PAGE_SIZE)
        return self.search_messages_planned(query, plan, limit=limit, offset=offset)

    def search_messages_planned(
        self,
        query: SearchQuery,
        plan: SearchPlan,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MessageSummary, ...]:
        """Execute server predicates plus a bounded, body-safe residual plan."""

        expected_plan = build_search_plan(
            query,
            residual_candidate_limit=plan.residual_candidate_limit,
        )
        return self._execute_message_search(query, plan, expected_plan, limit=limit, offset=offset)

    def search_messages_expression_planned(
        self,
        query: QueryExpression,
        plan: SearchPlan,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MessageSummary, ...]:
        """Execute parsed expert syntax without degrading it to free text."""

        expected_plan = build_search_plan(
            query,
            residual_candidate_limit=plan.residual_candidate_limit,
        )
        return self._execute_message_search(None, plan, expected_plan, limit=limit, offset=offset)

    def _execute_message_search(
        self,
        query: SearchQuery | None,
        plan: SearchPlan,
        expected_plan: SearchPlan,
        *,
        limit: int,
        offset: int,
    ) -> tuple[MessageSummary, ...]:
        """Shared exact executor for structured values and parsed expressions."""

        limit, offset = self._page(limit, offset)
        if plan != expected_plan:
            raise ConfigurationError("Search plan does not correspond to the supplied query.")
        if plan.mailbox_scope and limit + offset > MAX_PAGE_SIZE:
            raise ConfigurationError(
                "Mailbox search exposes a bounded 200-result window. Narrow the folder "
                "or server-side criteria before requesting this offset."
            )
        folder = plan.folder_scope or self.config.default_folder
        filters: dict[str, Any] = {}
        lookup_aliases = {"internet_message_id": "message_id"}
        for server_filter in plan.server_filters:
            lookup = lookup_aliases.get(server_filter.lookup, server_filter.lookup)
            value = server_filter.value
            if server_filter.field == "importance":
                value = str(getattr(value, "value", value)).title()
            if lookup in filters:
                if lookup != "categories__contains":
                    raise ConfigurationError(
                        f"The EWS search plan contains a duplicate {lookup!r} filter."
                    )
                previous = filters[lookup]
                category_values = (
                    list(previous) if isinstance(previous, (list, tuple)) else [previous]
                )
                category_values.append(value)
                filters[lookup] = category_values
            else:
                filters[lookup] = value
        if query is not None and query.attachment_name:
            filters.setdefault("has_attachments", True)

        projected_fields = list(SUMMARY_FIELDS)
        if plan.required_fields & {"body", "links"}:
            projected_fields.append("body")
        if plan.required_fields & {"recipients", "to"}:
            projected_fields.append("to_recipients")
        if plan.required_fields & {"recipients", "cc"}:
            projected_fields.append("cc_recipients")
        if plan.required_fields & {"recipients", "bcc"}:
            projected_fields.append("bcc_recipients")
        if "attachment_names" in plan.required_fields:
            projected_fields.append("attachments")
        if "internet_message_id" in plan.required_fields:
            projected_fields.append("message_id")
        if "headers" in plan.required_fields:
            projected_fields.append("headers")
        needs_detail = len(projected_fields) > len(SUMMARY_FIELDS)
        advanced_postprocess = bool(
            query is not None
            and (
                query.thread_response
                or (
                    query.ranking is not None
                    and query.ranking.field in {"sender_frequency", "thread_message_count"}
                )
            )
        )
        if (plan.uses_residual or advanced_postprocess) and (
            limit + offset > plan.residual_candidate_limit
        ):
            raise ConfigurationError(
                "This bounded local search stage only exposes the first "
                f"{plan.residual_candidate_limit} candidates. Narrow the server-side "
                "criteria or use the local search index before requesting this offset."
            )
        candidate_limit = (
            min(MAX_PAGE_SIZE, plan.residual_candidate_limit)
            if plan.uses_residual or advanced_postprocess
            else limit
        )
        server_offset = 0 if plan.uses_residual or advanced_postprocess else offset
        order_by = "-datetime_received"
        if query is not None and query.ranking is not None:
            prefix = "-" if query.ranking.direction == "descending" else ""
            if query.ranking.field == "received_at":
                order_by = f"{prefix}datetime_received"
            elif query.ranking.field == "size":
                order_by = f"{prefix}size"
        fields = tuple(dict.fromkeys(projected_fields))
        if plan.mailbox_scope:
            candidates = self._mailbox_search_candidates(
                fields=fields,
                limit=candidate_limit + server_offset,
                order_by=order_by,
                filters=filters,
            )[server_offset:]
        else:
            resolved = self._resolve_folder(folder)
            values = self._bounded_items(
                resolved,
                fields=fields,
                limit=candidate_limit,
                offset=server_offset,
                order_by=order_by,
                filters=filters,
            )
            path = self._folder_path(resolved)
            candidates = tuple((item, resolved, path) for item in values)
        matches: list[MessageSummary] = []
        for item, resolved, path in candidates:
            reference = self._remember("message", item, parent=resolved, folder_path=path)
            if not self._is_current_object("message", reference, item):
                continue
            summary = map_message_summary(item, reference=reference, folder_path=path)
            if query is not None and not self._summary_matches(summary, query):
                continue
            if plan.residual_predicate is not None:
                candidate: MessageSummary | Mapping[str, Any] = summary
                if needs_detail:
                    body_text, body_html = (
                        body_parts(item) if plan.required_fields & {"body", "links"} else ("", None)
                    )
                    candidate = {
                        "summary": to_primitive(summary),
                        "body_text": body_text,
                        "body_html": body_html,
                        "to": to_primitive(map_addresses(getattr(item, "to_recipients", ()) or ())),
                        "cc": to_primitive(map_addresses(getattr(item, "cc_recipients", ()) or ())),
                        "bcc": to_primitive(
                            map_addresses(getattr(item, "bcc_recipients", ()) or ())
                        ),
                        "attachments": to_primitive(
                            map_attachments(getattr(item, "attachments", ()) or ())
                        ),
                        "internet_message_id": getattr(item, "message_id", None),
                        "raw_headers": map_message_detail(
                            item,
                            reference=reference,
                            folder_path=path,
                        ).raw_headers,
                    }
                if not evaluate_residual(candidate, plan.residual_predicate):
                    continue
            matches.append(summary)
        if query is not None:
            matches = self._apply_advanced_search_semantics(matches, query)
        effective_limit = limit
        if query is not None and query.ranking is not None and query.ranking.limit is not None:
            effective_limit = min(effective_limit, query.ranking.limit)
        if plan.uses_residual or advanced_postprocess:
            return tuple(matches[offset : offset + effective_limit])
        return tuple(matches[:effective_limit])

    def _mailbox_search_folders(self) -> tuple[tuple[Any, str], ...]:
        """Discover a complete, capped hierarchy without bodies or root.walk()."""

        root = self._message_folder_root()
        maximum = 50

        def discover() -> tuple[Any, ...]:
            if type(root).__module__.startswith("exchangelib"):
                query = (
                    self._bindings()
                    .SingleFolderQuerySet(account=self.account, folder=root)
                    .only("name", "folder_class", "parent_folder_id")
                    .depth("Deep")
                )
                children = query.folder_collection.find_folders(
                    additional_fields=set(query.only_fields),
                    depth="Deep",
                    page_size=maximum + 1,
                    max_items=maximum + 1,
                    offset=0,
                )
            else:

                def descend(parent: Any) -> Iterator[Any]:
                    for child in getattr(parent, "children", ()):
                        yield child
                        yield from descend(child)

                children = descend(root)
            values = tuple(islice(children, maximum + 1))
            for value in values:
                if isinstance(value, BaseException):
                    raise value
            if len(values) > maximum:
                raise ConfigurationError(
                    "Mailbox search is limited to 50 descendant folders; no partial "
                    "mailbox was searched. Select a folder or use the local index."
                )
            return values

        descendants = self._call("mailbox search folder discovery", discover, read_only=True)
        root_id = self._id(root)
        if root_id is None:
            raise ConfigurationError("Mailbox search root has no usable folder identity.")
        by_id = {root_id: root}
        paths = {root_id: self._folder_path(root)}
        for child in descendants:
            identity = self._id(child)
            if identity is None or identity in by_id:
                raise ConfigurationError(
                    "Mailbox search returned missing or duplicate folder identities."
                )
            by_id[identity] = child

        def path_for(identity: str, visiting: frozenset[str] = frozenset()) -> str:
            if identity in paths:
                return paths[identity]
            if identity in visiting or len(visiting) >= _MAX_FOLDER_DEPTH:
                raise ConfigurationError("Mailbox search hierarchy is cyclic or too deep.")
            child = by_id[identity]
            parent_id = self._id(getattr(child, "parent_folder_id", None))
            if parent_id not in by_id:
                raise ConfigurationError("Mailbox search hierarchy has an unknown parent folder.")
            parent_path = path_for(parent_id, visiting | {identity})
            paths[identity] = self._folder_path(child, parent_path=parent_path)
            return paths[identity]

        inbox_id = self._id(getattr(self.account, "inbox", None))
        selected = []
        for identity, child in by_id.items():
            path = path_for(identity)
            # PIM folders and search-folder aliases are not ordinary mail sources.
            # Real GUC mailboxes contain classless system folders (Files, Yammer
            # Root, Conversation Action Settings); they never hold ordinary mail.
            # A classless *standard* mail folder is still an error: skipping it would
            # silently omit real mail from a "whole mailbox" search.
            folder_class = str(getattr(child, "folder_class", "") or "").casefold()
            if identity != root_id and not folder_class:
                role = str(getattr(child, "DISTINGUISHED_FOLDER_ID", "") or "").casefold()
                if role in _MAIL_FOLDER_ROLES or identity == inbox_id:
                    raise ConfigurationError("Mailbox search returned a folder without its class.")
                continue
            if (
                identity == root_id
                or folder_class == "ipf.note"
                or (
                    folder_class.startswith("ipf.note.")
                    and not folder_class.startswith("ipf.note.search")
                )
            ):
                self._remember("folder", child, folder_path=path)
                selected.append((child, path))
        return tuple(selected)

    def _mailbox_search_candidates(
        self,
        *,
        fields: tuple[str, ...],
        limit: int,
        order_by: str,
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Any, Any, str], ...]:
        """Merge per-folder prefixes, then fetch complex fields only for global candidates."""

        folders = self._mailbox_search_folders()
        candidates = []
        seen: set[str] = set()
        for folder, path in folders:
            for item in self._bounded_items(
                folder,
                fields=SUMMARY_FIELDS,
                limit=limit,
                offset=0,
                order_by=order_by,
                filters=filters,
            ):
                identity = self._id(item)
                if identity is None:
                    raise ConfigurationError("Mailbox search returned an item without an identity.")
                if identity not in seen:
                    seen.add(identity)
                    candidates.append((item, folder, path))
        field = order_by.lstrip("-")
        candidates.sort(
            key=lambda entry: (
                getattr(entry[0], field, None) is not None,
                getattr(entry[0], field, None),
            ),
            reverse=order_by.startswith("-"),
        )
        selected = tuple(candidates[:limit])
        if fields == SUMMARY_FIELDS or not selected:
            return selected
        if not type(selected[0][0]).__module__.startswith("exchangelib"):
            return selected
        identities: list[tuple[str, str | None]] = []
        for item, _folder, _path in selected:
            identity = self._id(item)
            if identity is None:
                raise ConfigurationError(
                    "Mailbox search candidate identity changed during retrieval."
                )
            identities.append((identity, self._changekey(item)))

        def fetch() -> dict[str, Any]:
            values = self.account.fetch(
                ids=identities, only_fields=fields, chunk_size=MAX_PAGE_SIZE
            )
            result = {}
            expected = {identity for identity, _key in identities}
            for item in islice(values, len(identities) + 1):
                if isinstance(item, BaseException):
                    raise item
                identity = self._id(item)
                if identity is None or identity not in expected or identity in result:
                    raise ConfigurationError(
                        "Mailbox search details returned an unexpected item identity."
                    )
                result[identity] = item
            if len(result) != len(identities):
                raise ItemNotFoundError(
                    "Mailbox search items changed during retrieval; retry the read-only search."
                )
            return result

        details = self._call("bounded mailbox search details", fetch, read_only=True)
        return tuple(
            (details[identity], folder, path)
            for (identity, _key), (_item, folder, path) in zip(identities, selected, strict=True)
        )

    def _apply_advanced_search_semantics(
        self,
        matches: list[MessageSummary],
        query: SearchQuery,
    ) -> list[MessageSummary]:
        """Apply bounded conversation ownership and aggregate ranking without extra I/O."""

        sender_counts = Counter(
            (item.sender.email.casefold() if item.sender else "") for item in matches
        )
        thread_counts = Counter(item.conversation_id or item.reference for item in matches)
        if query.thread_response:
            configured_email = (self.config.email or "").strip().casefold()
            if not configured_email and isinstance(self._credentials, CredentialData):
                configured_email = self._credentials.email.strip().casefold()
            if not configured_email:
                raise ConfigurationError(
                    "Conversation response ownership needs the configured account email."
                )
            latest: dict[str, MessageSummary] = {}
            minimum_time = datetime.min.replace(tzinfo=UTC)
            for item in matches:
                key = item.conversation_id or item.reference
                value_time = item.received_at or item.sent_at or minimum_time
                if value_time.tzinfo is None:
                    value_time = value_time.replace(tzinfo=UTC)
                else:
                    value_time = value_time.astimezone(UTC)
                current = latest.get(key)
                current_time = None
                if current is not None:
                    current_time = current.received_at or current.sent_at or minimum_time
                if current_time is not None:
                    current_time = (
                        current_time.replace(tzinfo=UTC)
                        if current_time.tzinfo is None
                        else current_time.astimezone(UTC)
                    )
                if current is None or current_time is None or value_time > current_time:
                    latest[key] = item
            waiting_on_me = query.thread_response == "waiting_on_me"
            matches = [
                item
                for item in latest.values()
                if ((item.sender.email.casefold() if item.sender else "") != configured_email)
                is waiting_on_me
            ]

        ranking = query.ranking
        if ranking is None:
            return matches

        descending = ranking.direction == "descending"
        minimum_time = datetime.min.replace(tzinfo=UTC)

        def received(item: MessageSummary) -> float:
            value = item.received_at or item.sent_at or minimum_time
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC).timestamp()

        def ranking_key(item: MessageSummary) -> tuple[bool, float, str]:
            missing = False
            if ranking.field == "received_at":
                missing = item.received_at is None and item.sent_at is None
                score = received(item)
            elif ranking.field == "size":
                missing = item.size is None
                score = float(item.size if item.size is not None else -1)
            elif ranking.field == "sender_frequency":
                sender = item.sender.email.casefold() if item.sender else ""
                score = float(sender_counts[sender])
            elif ranking.field == "thread_message_count":
                score = float(thread_counts[item.conversation_id or item.reference])
            else:  # validated by the shared query planner before any provider call
                raise ConfigurationError(f"Unsupported live-mail ranking field: {ranking.field}.")
            return missing, -score if descending else score, item.reference

        return sorted(matches, key=ranking_key)

    def _message_for_update(self, reference: str) -> Any:
        return self._resolve_item(reference, "message", "draft")[1]

    def _save_message_fields(
        self, reference: str, item: Any, fields: list[str], operation: str
    ) -> Any:
        def save() -> Any:
            try:
                item.save(update_fields=fields)
            except Exception as exc:
                # Exchange answers some single-field updates (notably IsRead) with a
                # placeholder change key; the next write with it is rejected as a
                # conflict. These writes set absolute values, so re-reading only the
                # current change key and retrying once cannot double-apply anything.
                if type(exc).__name__ not in _STALE_CHANGE_KEY_ERRORS or not self._id(item):
                    raise
                current = self.account.fetch(ids=[(self._id(item), None)], only_fields=("subject",))
                fresh = next(iter(current), None)
                if fresh is None or isinstance(fresh, BaseException) or not self._changekey(fresh):
                    raise
                item.changekey = self._changekey(fresh)
                item.save(update_fields=fields)
            stored = self._state_reference(reference, "message", "draft")
            memory = self._memory_object(reference, "message", "draft")
            kind = stored[0] if stored else memory[0] if memory else "message"
            self._remember(kind, item, reference=reference)
            return reference

        return self._call(operation, save)

    def mark_read(self, reference: str, read: bool = True) -> Any:
        item = self._message_for_update(reference)
        item.is_read = bool(read)
        return self._save_message_fields(reference, item, ["is_read"], "update message read state")

    def flag(self, reference: str, flagged: bool = True) -> Any:
        item = self._message_for_update(reference)
        item.gucmail_flag_status = 2 if flagged else None
        return self._save_message_fields(
            reference, item, ["gucmail_flag_status"], "update message flag"
        )

    def categorize(self, reference: str, categories: Any, mode: str = "set") -> Any:
        if mode not in {"set", "add", "remove"}:
            raise ConfigurationError("Category mode must be set, add, or remove.")
        item = (
            self._fetch_item(reference, ("message", "draft"), ("categories",))
            if mode != "set"
            else self._message_for_update(reference)
        )
        requested = tuple(
            dict.fromkeys(str(value).strip() for value in categories if str(value).strip())
        )
        current = list(getattr(item, "categories", None) or [])
        if mode == "set":
            updated = list(requested)
        elif mode == "add":
            updated = list(dict.fromkeys((*current, *requested)))
        elif mode == "remove":
            removed = {value.casefold() for value in requested}
            updated = [value for value in current if str(value).casefold() not in removed]
        else:
            raise ConfigurationError("Category mode must be set, add, or remove.")
        item.categories = updated
        return self._save_message_fields(
            reference, item, ["categories"], "update message categories"
        )

    def set_importance(self, reference: str, importance: Importance) -> Any:
        item = self._message_for_update(reference)
        item.importance = importance.value.title()
        return self._save_message_fields(
            reference, item, ["importance"], "update message importance"
        )

    def move_message(self, reference: str, destination: str = "trash") -> str | None:
        kind, item = self._resolve_item(reference, "message", "draft")
        target = self._resolve_folder(destination)
        return self._call(
            "move message", lambda: self._move_with_reference(kind, item, reference, target)
        )

    def _move_with_reference(self, kind: str, item: Any, reference: str, target: Any) -> str | None:
        """Persist replacement IDs for every recoverable item move."""

        target_path = self._folder_path(target)
        result = item.move(target)
        result_id, result_change_key = self._result_identity(result)
        if result_id:
            item.id = result_id
            if result_change_key:
                item.changekey = result_change_key
        if self._id(item):
            return self._remember(
                kind,
                item,
                reference=reference,
                parent=target,
                folder_path=target_path,
            )
        return None

    def copy_message(self, reference: str, destination: str) -> str | None:
        item = self._message_for_update(reference)
        target = self._resolve_folder(destination)

        def copy() -> str | None:
            result = item.copy(target)
            copied_id, copied_change_key = self._result_identity(result)
            if copied_id is None:
                return None
            copied = self._construct(
                "Message",
                account=self.account,
                id=copied_id,
                changekey=copied_change_key,
            )
            return self._remember(
                "message",
                copied,
                parent=target,
                folder_path=self._folder_path(target),
            )

        return self._call("copy message", copy)

    def delete_message(self, reference: str, permanent: bool = False) -> Any:
        if permanent:
            item = self._message_for_update(reference)
            return self._call(
                "permanently delete message",
                item.delete,
            )
        # A normal delete is a MoveItem operation so the replacement ID can remain recoverable.
        return self.move_message(reference, "trash")

    def list_attachments(
        self,
        message_reference: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AttachmentInfo, ...]:
        limit, offset = self._page(min(limit, _MAX_ATTACHMENTS), offset)
        # Listing attachment metadata must not take the message-detail path: that
        # projection includes bodies, recipients, and headers.  Exchange can return
        # the attachment collection alone, which is sufficient for names, sizes,
        # types, warning flags, and the stable references used by a later explicit
        # download.
        kind, _ = self._resolve_item(message_reference, "message", "draft")
        item = self._fetch_item(
            message_reference,
            ("message", "draft"),
            ("attachments",),
        )
        parent = self._parents.get(("message", message_reference)) or self._parents.get(
            ("draft", message_reference)
        )
        canonical = self._remember(
            kind,
            item,
            reference=message_reference,
            parent=parent,
        )
        raw_attachments = tuple(getattr(item, "attachments", ()) or ())
        metadata = map_attachments(
            raw_attachments,
            parent_remote_id=self._id(item),
            maximum=_MAX_ATTACHMENTS,
        )
        canonical_metadata: list[AttachmentInfo] = []
        for attachment, info in zip(raw_attachments, metadata, strict=False):
            attachment_reference = self._remember(
                "attachment",
                attachment,
                reference=info.reference,
                parent=item,
            )
            self._attachment_parents[attachment_reference] = canonical
            canonical_metadata.append(
                info
                if attachment_reference == info.reference
                else replace(info, reference=attachment_reference)
            )
        return tuple(canonical_metadata[offset : offset + limit])

    def open_attachment(self, message_reference: str, attachment_reference: str) -> BinaryIO:
        canonical_message_reference = message_reference
        stored = self._state_reference(message_reference, "message", "draft")
        if stored is not None:
            canonical_message_reference = str(stored[1].get("canonical_ref") or message_reference)
        memory = self._memory_object(attachment_reference, "attachment")
        if (
            memory is None
            or self._attachment_parents.get(attachment_reference) != canonical_message_reference
        ):
            # Recover an evicted or restarted attachment by metadata only. Opening
            # a file must not silently retrieve the containing message's body.
            self.list_attachments(message_reference, limit=_MAX_ATTACHMENTS)
            memory = self._memory_object(attachment_reference, "attachment")
        if (
            memory is None
            or self._attachment_parents.get(attachment_reference) != canonical_message_reference
        ):
            raise ItemNotFoundError("The attachment was not found on the selected message.")
        attachment = memory[1]
        stream = getattr(attachment, "fp", None)
        if stream is None:
            raise UnsupportedFeatureError("This Exchange attachment cannot be streamed safely.")
        return _AttachmentStream(stream, self.config.max_attachment_bytes)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Composing and sending

    def _mailboxes(self, values: Any) -> list[Any]:
        result = []
        for value in values:
            address = value if isinstance(value, Address) else Address(email=str(value))
            result.append(
                self._construct("Mailbox", email_address=address.email, name=address.name)
            )
        return result

    def _body(self, draft: DraftData) -> Any:
        if draft.body_html:
            return self._construct("HTMLBody", draft.body_html)
        return self._construct("Body", draft.body_text)

    def _message_kwargs(self, draft: DraftData) -> dict[str, Any]:
        return {
            "subject": draft.subject,
            "body": self._body(draft),
            "to_recipients": self._mailboxes(draft.to),
            "cc_recipients": self._mailboxes(draft.cc),
            "bcc_recipients": self._mailboxes(draft.bcc),
            "importance": draft.importance.value.title(),
            "is_read_receipt_requested": draft.request_read_receipt,
            "is_delivery_receipt_requested": draft.request_delivery_receipt,
        }

    def _file_attachments(self, paths: Any) -> list[Any]:
        attachments: list[Any] = []
        total = 0
        names: set[str] = set()
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve(strict=True)
                size = path.stat().st_size
            except OSError as exc:
                raise ConfigurationError(
                    "A selected attachment is missing or inaccessible."
                ) from exc
            if not path.is_file():
                raise ConfigurationError(f"Attachment is not a regular file: {path.name}")
            name_key = path.name.casefold()
            if name_key in names:
                raise ConfigurationError(
                    "Selected attachments have the same filename; rename one first."
                )
            names.add(name_key)
            remaining = self.config.max_attachment_bytes - total
            if size > remaining:
                raise ConfigurationError(
                    "The selected attachments exceed the configured size limit."
                )
            try:
                with path.open("rb") as handle:
                    content = handle.read(remaining + 1)
            except OSError as exc:
                raise ConfigurationError("A selected attachment could not be read.") from exc
            if len(content) > remaining:
                raise ConfigurationError("An attachment grew beyond the configured size limit.")
            total += len(content)
            attachments.append(self._construct("FileAttachment", name=path.name, content=content))
        return attachments

    @staticmethod
    def _attachment_identity(attachment: Any) -> tuple[str, int | None]:
        name = str(getattr(attachment, "name", "") or "").casefold()
        size = getattr(attachment, "size", None)
        # FileAttachment.content is a network-backed property. Only local,
        # unsaved attachments can be inspected to infer a missing size.
        if size is None and getattr(attachment, "attachment_id", None) is None:
            content = getattr(attachment, "content", None)
            if isinstance(content, (bytes, bytearray, memoryview)):
                size = len(content)
        try:
            normalized_size = int(size) if size is not None else None
        except (TypeError, ValueError):
            normalized_size = None
        return name, normalized_size

    @classmethod
    def _result_identity(cls, value: Any) -> tuple[str | None, str | None]:
        """Read EWS identity results from exchangelib objects or legacy tuples."""

        if isinstance(value, (tuple, list)):
            remote_id = str(value[0]) if value and value[0] else None
            change_key = str(value[1]) if len(value) > 1 and value[1] else None
            return remote_id, change_key
        return cls._id(value), cls._changekey(value)

    def _pending_attachments(self, item: Any, attachments: list[Any]) -> list[Any]:
        # Draft autosave may present the same local paths repeatedly. Do not create
        # duplicate server attachments, and never delete an attachment that exists
        # only on the server (for example, one inherited by a forward).
        current = list(getattr(item, "attachments", None) or ())
        existing = {self._attachment_identity(value)[0]: value for value in current}
        pending: list[Any] = []
        for value in attachments:
            identity = self._attachment_identity(value)
            if identity[0] in existing:
                previous = existing[identity[0]]
                digest = hashlib.sha256(value.content).hexdigest()
                previous_id = str(getattr(getattr(previous, "attachment_id", None), "id", "") or "")
                if self._attachment_uploads.matches(
                    self._id(item) or "", identity[0], previous_id, digest
                ):
                    continue
                raise ConflictError(
                    "A server draft already contains an attachment with the same name; "
                    "its content differs or no verified upload record is available.",
                    context=ErrorContext(
                        retry_safe=False,
                        suggestion=(
                            "Rename the local attachment or reopen the server draft before "
                            "saving it again."
                        ),
                    ),
                )
            pending.append(value)
        return pending

    def _record_attachment_uploads(self, item: Any, attachments: list[Any]) -> None:
        current = {
            self._attachment_identity(value)[0]: value
            for value in (getattr(item, "attachments", None) or ())
        }
        for value in attachments:
            name = self._attachment_identity(value)[0]
            uploaded = current.get(name)
            if uploaded is not None and self._id(item):
                attachment_id = str(
                    getattr(getattr(uploaded, "attachment_id", None), "id", "") or ""
                )
                self._attachment_uploads.record(
                    self._id(item) or "",
                    name,
                    attachment_id,
                    hashlib.sha256(value.content).hexdigest(),
                )

    def _attach(self, item: Any, draft: DraftData, *, prepared: list[Any] | None = None) -> None:
        attachments = (
            self._pending_attachments(item, self._file_attachments(draft.attachment_paths))
            if prepared is None
            else prepared
        )
        if not attachments:
            return
        attach = getattr(item, "attach", None)
        if callable(attach) and self._id(item):
            attach(attachments)
            self._record_attachment_uploads(item, attachments)
            return
        current_attachments = getattr(item, "attachments", None)
        if current_attachments is None:
            item.attachments = attachments
        else:
            current_attachments.extend(attachments)

    def _save_draft_item(
        self,
        draft: DraftData,
        *,
        remember: bool,
        response_kind: str | None = None,
    ) -> tuple[str, Any]:
        """Create or update one real Exchange draft and return its stable reference."""

        # Read and validate local attachments before the first server mutation.
        attachments = self._file_attachments(draft.attachment_paths)
        if draft.server_id:
            kind, _stored = self._resolve_item(draft.server_id, "draft", "message")
            message = self._fetch_item(draft.server_id, (kind,), DRAFT_FIELDS)
            self._require_server_draft(message)
            expected_change_key = draft.server_change_key
            actual_change_key = self._changekey(message)
            if expected_change_key and expected_change_key != actual_change_key:
                raise ConflictError(
                    "The Exchange draft changed since it was opened.",
                    context=ErrorContext(
                        retry_safe=False,
                        suggestion=(
                            "Reopen the server draft and merge the local changes before saving."
                        ),
                    ),
                )
            pending_attachments = self._pending_attachments(message, attachments)
            updates = self._message_kwargs(draft)
            for name, value in updates.items():
                setattr(message, name, value)
            message.save(update_fields=list(updates))
            self._attach(message, draft, prepared=pending_attachments)
            if remember:
                reference = self._remember(
                    kind,
                    message,
                    reference=draft.server_id,
                    parent=self.account.drafts,
                )
            else:
                reference = draft.server_id
            return reference, message

        source_reference = draft.forward_of_reference or draft.reply_to_reference
        if source_reference:
            kind = response_kind or ("forward" if draft.forward_of_reference else "reply")
            message = self._new_response_draft(kind, source_reference, draft)
            self._attach(message, draft, prepared=self._pending_attachments(message, attachments))
        else:
            message = self._construct(
                "Message",
                account=self.account,
                folder=self.account.drafts,
                attachments=attachments,
                **self._message_kwargs(draft),
            )
            message.save()
            self._record_attachment_uploads(message, attachments)
        if remember:
            return self._remember("draft", message, parent=self.account.drafts), message
        return draft.reference, message

    def save_draft(self, draft: DraftData) -> str:
        return self._call(
            "save server draft",
            lambda: self._save_draft_item(draft, remember=True)[0],
        )

    def save_server_draft(self, draft: DraftData) -> DraftData:
        """Synchronize a draft and return its stable server identity and ChangeKey."""

        def save() -> DraftData:
            reference, message = self._save_draft_item(draft, remember=True)
            return replace(
                draft,
                location=DraftLocation.SERVER,
                server_id=reference,
                server_change_key=self._changekey(message),
                updated_at=datetime.now(UTC),
            )

        return self._call("synchronize server draft", save)

    @staticmethod
    def _require_server_draft(message: Any) -> None:
        if getattr(message, "is_draft", None) is not True:
            raise ConflictError(
                "The selected Exchange item is not an unsent draft. "
                "It may already have been sent; reopen Drafts before continuing.",
                context=ErrorContext(retry_safe=False),
            )

    def get_server_draft(self, server_id: str) -> DraftData:
        """Retrieve one server draft on demand for conflict reconciliation."""

        kind, _stored = self._resolve_item(server_id, "draft", "message")
        message = self._fetch_item(server_id, (kind,), DRAFT_FIELDS)
        self._require_server_draft(message)
        reference = self._remember(
            kind,
            message,
            reference=server_id,
            parent=self.account.drafts,
        )
        body_text, body_html = body_parts(message)
        importance_text = str(getattr(message, "importance", "Normal") or "Normal").casefold()
        importance = {
            "low": Importance.LOW,
            "high": Importance.HIGH,
        }.get(importance_text, Importance.NORMAL)
        return DraftData(
            reference=reference,
            to=list(map_addresses(getattr(message, "to_recipients", None))),
            cc=list(map_addresses(getattr(message, "cc_recipients", None))),
            bcc=list(map_addresses(getattr(message, "bcc_recipients", None))),
            subject=str(getattr(message, "subject", "") or ""),
            body_text=body_text,
            body_html=body_html or None,
            importance=importance,
            request_read_receipt=bool(getattr(message, "is_read_receipt_requested", False)),
            request_delivery_receipt=bool(getattr(message, "is_delivery_receipt_requested", False)),
            location=DraftLocation.SERVER,
            updated_at=domain_datetime(getattr(message, "last_modified_time", None)),
            server_id=reference,
            server_change_key=self._changekey(message),
        )

    def send(self, draft: DraftData) -> dict[str, Any]:
        if type(draft.save_sent_copy) is not bool:
            raise ConfigurationError("The Sent Items copy choice must be a boolean.")

        def prepare() -> Any:
            _reference, message = self._save_draft_item(draft, remember=False)
            return message

        message = self._call("prepare message for send", prepare)

        def deliver() -> dict[str, Any]:
            message.send(save_copy=draft.save_sent_copy)
            return {
                "accepted": True,
                "sent_item_reference": None,
                "sent_copy_requested": draft.save_sent_copy,
            }

        return self._call("send message", deliver, send_may_have_completed=True)

    def _new_response_draft(self, kind: str, reference: str, draft: DraftData) -> Any:
        source = self._fetch_item(reference, ("message",), REPLY_FIELDS)
        if kind == "reply_all":
            response = source.create_reply_all(subject=draft.subject, body=self._body(draft))
            response.to_recipients = self._mailboxes(draft.to)
            response.cc_recipients = self._mailboxes(draft.cc)
            response.bcc_recipients = self._mailboxes(draft.bcc)
        else:
            create = source.create_forward if kind == "forward" else source.create_reply
            response = create(
                subject=draft.subject,
                body=self._body(draft),
                to_recipients=self._mailboxes(draft.to),
                cc_recipients=self._mailboxes(draft.cc),
                bcc_recipients=self._mailboxes(draft.bcc),
            )
        response.is_read_receipt_requested = draft.request_read_receipt
        response.is_delivery_receipt_requested = draft.request_delivery_receipt
        created = response.save(self.account.drafts)
        created_id, change_key = self._result_identity(created)
        if created_id is None:
            raise GUCmailError(
                "Exchange did not return an identifier for the saved response draft."
            )
        message = self._construct(
            "Message",
            account=self.account,
            folder=self.account.drafts,
            id=created_id,
            changekey=change_key,
        )
        local_reference = self._remember("draft", message, parent=self.account.drafts)
        if kind == "forward" and draft.attachment_paths:
            # Inherited attachments remain on the server; retrieve only their
            # metadata before checking names of newly selected local files.
            message = self._fetch_item(local_reference, ("draft",), ("attachments",))
        # EWS response Body is a prefix, and Exchange appends its own quote.
        # Save the exact reviewed body/envelope after creating the response so
        # quoted history is not duplicated and threading/forward attachments stay intact.
        updates = self._message_kwargs(draft)
        for name, value in updates.items():
            setattr(message, name, value)
        message.save(update_fields=list(updates))
        self._remember("draft", message, reference=local_reference, parent=self.account.drafts)
        return message

    def _reply_or_forward(self, kind: str, reference: str, draft: DraftData) -> dict[str, Any]:
        if type(draft.save_sent_copy) is not bool:
            raise ConfigurationError("The Sent Items copy choice must be a boolean.")
        response_draft = replace(
            draft,
            forward_of_reference=reference if kind == "forward" else None,
            reply_to_reference=reference if kind != "forward" else None,
        )

        message = self._call(
            f"prepare {kind.replace('_', ' ')} draft",
            lambda: self._save_draft_item(response_draft, remember=False, response_kind=kind)[1],
        )

        def deliver() -> dict[str, Any]:
            message.send(save_copy=draft.save_sent_copy)
            return {
                "accepted": True,
                "sent_item_reference": None,
                "kind": kind,
                "sent_copy_requested": draft.save_sent_copy,
            }

        return self._call(
            f"{kind.replace('_', ' ')} message", deliver, send_may_have_completed=True
        )

    def reply(self, reference: str, draft: DraftData) -> dict[str, Any]:
        return self._reply_or_forward("reply", reference, draft)

    def reply_all(self, reference: str, draft: DraftData) -> dict[str, Any]:
        return self._reply_or_forward("reply_all", reference, draft)

    def forward(self, reference: str, draft: DraftData) -> dict[str, Any]:
        return self._reply_or_forward("forward", reference, draft)

    def get_quota(self) -> QuotaInfo | None:
        # Standard user-mailbox EWS exposes folder sizes and MailTips thresholds, not a
        # dependable exact used/quota pair. Returning None is deliberate capability honesty.
        return None

    # ------------------------------------------------------------------
    # Calendar

    def _calendar_folder(self, reference: str | None) -> Any:
        return self._resolve_folder(reference or "calendar")

    def _attendees(self, values: Any) -> list[Any]:
        return [
            self._construct(
                "Attendee",
                mailbox=self._construct("Mailbox", email_address=value.email, name=value.name),
            )
            for value in values
        ]

    def _map_calendar_item(self, item: Any, *, reference: str) -> CalendarEvent:
        event = map_calendar_event(item, reference=reference, timezone=self.config.timezone)
        for attachment, metadata in zip(
            tuple(getattr(item, "attachments", ()) or ()), event.attachments, strict=False
        ):
            self._remember("attachment", attachment, reference=metadata.reference, parent=item)
            self._attachment_parents[metadata.reference] = reference
        return event

    @staticmethod
    def _calendar_scope_item(item: Any, scope: object) -> Any:
        """Resolve an occurrence to its master only for an explicit series operation."""

        normalized_scope = str(getattr(scope, "value", scope)).casefold()
        item_type = str(getattr(item, "type", "")).casefold()
        if normalized_scope not in {"occurrence", "series"}:
            raise UnsupportedFeatureError("This calendar recurrence scope is unavailable.")
        if normalized_scope == "occurrence" and item_type == "recurringmaster":
            raise ConfigurationError("Choose entire series to change a recurring series master.")
        if normalized_scope != "series" or item_type not in {"occurrence", "exception"}:
            return item
        recurring_master = getattr(item, "recurring_master", None)
        if not callable(recurring_master):
            raise UnsupportedFeatureError(
                "This Exchange calendar item cannot resolve its recurring series master."
            )
        master = recurring_master()
        refresh = getattr(master, "refresh", None)
        if callable(refresh):
            refresh()
        return master

    @staticmethod
    def _invitation_value(value: object, bindings: Any, *, changed: bool = False) -> str:
        normalized = str(getattr(value, "value", value)).casefold()
        if normalized == "send_none":
            return str(bindings.SEND_TO_NONE)
        if changed or normalized == "send_changed":
            return str(bindings.SEND_TO_CHANGED_AND_SAVE_COPY)
        return str(bindings.SEND_TO_ALL_AND_SAVE_COPY)

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
        metadata_only: bool = True,
    ) -> tuple[CalendarEvent, ...]:
        """List events, optionally using a minimal body-free smoke-test projection."""

        limit, _ = self._page(limit)
        folder = self._calendar_folder(calendar_reference)

        def load() -> tuple[CalendarEvent, ...]:
            view = getattr(folder, "view", None)
            if callable(view):
                values = view(start=start, end=end, max_items=limit)
                if type(values).__module__.startswith("exchangelib"):
                    fields = CALENDAR_METADATA_FIELDS if metadata_only else CALENDAR_FIELDS
                    values = values.only(*fields)
            else:
                values = self._query_source(folder)
            result: list[CalendarEvent] = []
            for item in islice(iter(values), limit):
                if isinstance(item, BaseException):
                    raise item
                item_start = getattr(item, "start", None)
                item_end = getattr(item, "end", None)
                if item_start is not None:
                    if item_end is not None:
                        if not (item_start < end and item_end > start):
                            continue
                    elif not (start <= item_start < end):
                        continue
                reference = self._remember("event", item, parent=folder)
                result.append(self._map_calendar_item(item, reference=reference))
            return tuple(result)

        return self._call("calendar listing", load, read_only=True)

    def get_event(self, reference: str) -> CalendarEvent:
        item = self._fetch_item(reference, ("event", "calendar"), CALENDAR_FIELDS)
        self._remember("event", item, reference=reference)
        return self._map_calendar_item(item, reference=reference)

    def get_calendar_history(self, reference: str, *, limit: int = 100) -> CalendarSeriesHistory:
        """Explicit GetItem metadata read; never expand CalendarView or fetch content."""
        limit = history_limit(limit)

        def load() -> CalendarSeriesHistory:
            item = self._fetch_item(reference, ("event", "calendar"), CALENDAR_HISTORY_FIELDS)
            series_reference = reference
            if str(getattr(item, "type", "")).casefold() in {"occurrence", "exception"}:
                resolve = getattr(item, "recurring_master", None)
                if not callable(resolve):
                    raise UnsupportedFeatureError("This event cannot resolve its recurring master.")
                master = resolve()
                if type(master).__module__.startswith("exchangelib"):
                    # Pass the Item, preserving RecurringMasterItemId. A plain ID tuple
                    # would fetch the original occurrence instead of its master.
                    results = self.account.fetch(ids=[master], only_fields=CALENDAR_HISTORY_FIELDS)
                    item = next(iter(results), None)
                    if item is None:
                        raise ItemNotFoundError("The recurring Calendar master no longer exists.")
                    if isinstance(item, BaseException):
                        raise item
                else:
                    item = master
                series_reference = self._remember("event", item)
            else:
                self._remember("event", item, reference=reference)
            return map_calendar_history(
                item,
                requested_reference=reference,
                series_reference=series_reference,
                observed_at=datetime.now(UTC),
                limit=limit,
                reference_factory=lambda occurrence: self._remember("event", occurrence),
            )

        return self._call("read calendar occurrence history", load, read_only=True)

    def _inbox_rule_adapter(self) -> NativeInboxRules:
        return NativeInboxRules(lambda: self.account, self._call, self._resolve_folder)

    def list_inbox_rules(self) -> tuple[InboxRule, ...]:
        return self._inbox_rule_adapter().list()

    def create_inbox_rule(self, rule: SimpleInboxRule, expected_inventory: str) -> InboxRule:
        return self._inbox_rule_adapter().create(rule, expected_inventory)

    def set_inbox_rule_enabled(self, rule: InboxRule, enabled: bool) -> InboxRule:
        return self._inbox_rule_adapter().set_enabled(rule, enabled)

    def delete_inbox_rule(self, rule: InboxRule) -> None:
        self._inbox_rule_adapter().delete(rule)

    def list_delegates(self) -> tuple[DelegateInfo, ...]:
        def read() -> tuple[DelegateInfo, ...]:
            service = self._construct("GetDelegate", account=self.account)
            return map_delegate_collection(service.call(user_ids=None, include_permissions=True))

        return self._call("read mailbox delegates", read, read_only=True)

    def get_automatic_replies(self) -> AutomaticReplySettings:
        from gucmail.services.automatic_replies import (
            AutomaticReplySettings,
            AutomaticReplyState,
            ExternalReplyAudience,
        )

        def read() -> AutomaticReplySettings:
            settings = self.account.oof_settings
            return AutomaticReplySettings(
                state=AutomaticReplyState(str(settings.state).casefold()),
                external_audience=ExternalReplyAudience(str(settings.external_audience).casefold()),
                internal_reply=str(settings.internal_reply or ""),
                external_reply=str(settings.external_reply or ""),
                start=domain_datetime(settings.start),
                end=domain_datetime(settings.end),
            )

        return self._call("read automatic replies", read, read_only=True)

    def set_automatic_replies(self, settings: AutomaticReplySettings) -> None:
        from gucmail.services.automatic_replies import validate_automatic_replies

        settings = validate_automatic_replies(settings)
        value = self._construct(
            "OofSettings",
            state=settings.state.value.title(),
            external_audience=settings.external_audience.value.title(),
            internal_reply=settings.internal_reply,
            # The library requires nonempty messages even when this audience is disabled.
            # A harmless placeholder must never copy internal content to the external reply.
            external_reply=settings.external_reply or "External automatic replies are disabled.",
            start=settings.start,
            end=settings.end,
        )
        self._call("set automatic replies", lambda: setattr(self.account, "oof_settings", value))

    def get_event_series(self, reference: str) -> CalendarEvent:
        """Read the real series master without changing the occurrence's identity."""

        def load() -> CalendarEvent:
            original = self._fetch_item(reference, ("event", "calendar"), CALENDAR_FIELDS)
            item = self._calendar_scope_item(original, "series")
            master_reference = self._remember(
                "event", item, reference=reference if self._id(item) == self._id(original) else None
            )
            return self._map_calendar_item(item, reference=master_reference)

        return self._call("load calendar series", load, read_only=True)

    def get_availability(
        self,
        attendees: tuple[Address, ...] | list[Address],
        *,
        start: datetime,
        end: datetime,
        interval_minutes: int = 30,
    ) -> tuple[dict[str, Any], ...]:
        """Return bounded GetUserAvailability free/busy results as domain-neutral mappings."""

        values = tuple(attendees)
        if not values or len(values) > 100:
            raise ConfigurationError("Availability requires between 1 and 100 attendees.")
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ConfigurationError("Availability needs an aware, increasing time window.")
        if interval_minutes < 5 or interval_minutes > 1440:
            raise ConfigurationError("Availability interval must be between 5 and 1440 minutes.")

        def load() -> tuple[dict[str, Any], ...]:
            lib = self._bindings()
            ews_start = lib.EWSDateTime.from_datetime(start)
            ews_end = lib.EWSDateTime.from_datetime(end)
            raw = self._versioned_protocol().get_free_busy_info(
                [(address.email, "Required", False) for address in values],
                start=ews_start,
                end=ews_end,
                merged_free_busy_interval=interval_minutes,
                requested_view="DetailedMerged",
            )
            output: list[dict[str, Any]] = []
            for address, view in zip(values, raw, strict=False):
                if isinstance(view, BaseException):
                    output.append(
                        {
                            "attendee": address,
                            "available": False,
                            "error": type(view).__name__,
                        }
                    )
                    continue
                events = []
                for event in getattr(view, "calendar_events", None) or ():
                    events.append(
                        {
                            "start": getattr(event, "start", None),
                            "end": getattr(event, "end", None),
                            "busy_type": getattr(event, "busy_type", None),
                        }
                    )
                output.append(
                    {
                        "attendee": address,
                        "available": True,
                        "view_type": getattr(view, "view_type", None),
                        "merged": getattr(view, "merged", None),
                        "events": events,
                    }
                )
            return tuple(output)

        return self._call("calendar availability", load, read_only=True)

    def _event_kwargs(self, event: CalendarEvent) -> dict[str, Any]:
        return {
            "subject": event.subject,
            "start": event.start,
            "end": event.end,
            "location": event.location,
            "body": self._construct("Body", event.body),
            "required_attendees": self._attendees(event.required_attendees),
            "optional_attendees": self._attendees(event.optional_attendees),
            "resources": self._attendees(event.resources),
            "is_all_day": event.is_all_day,
            "categories": list(event.categories),
            "importance": event.importance.value.title(),
            "sensitivity": event.sensitivity.value.title(),
            "reminder_is_set": event.reminder_minutes is not None,
            "reminder_minutes_before_start": event.reminder_minutes,
            "legacy_free_busy_status": event.availability.title(),
            "recurrence": self._ews_recurrence(
                event.recurrence,
                target="calendar",
                start=event.start,
            )
            if event.recurrence
            else None,
        }

    def create_event(self, plan: Any) -> CalendarEvent:
        folder = self._calendar_folder(plan.calendar_reference)

        def create() -> CalendarEvent:
            attachments = self._file_attachments(plan.attachment_paths)
            item = self._construct(
                "CalendarItem",
                account=self.account,
                folder=folder,
                **self._event_kwargs(plan.event),
            )
            invitations = self._invitation_value(plan.invitations, self._bindings())
            item.save(send_meeting_invitations=invitations)
            if attachments:
                item.attach(attachments)
            reference = self._remember("event", item, parent=folder)
            return self._map_calendar_item(item, reference=reference)

        return self._call("create calendar event", create)

    def _event_change(
        self,
        name: str,
        value: Any,
        *,
        recurrence_start: date | datetime | None,
    ) -> tuple[str, Any]:
        aliases = {
            "reminder_minutes": "reminder_minutes_before_start",
            "availability": "legacy_free_busy_status",
        }
        field = aliases.get(name, name)
        if name == "body":
            value = self._construct("Body", value or "")
        elif name in {"required_attendees", "optional_attendees", "resources"}:
            value = self._attendees(value or ())
        elif name in {"importance", "sensitivity"} and value is not None:
            value = str(getattr(value, "value", value)).title()
        elif name == "availability" and value is not None:
            value = str(value).title()
        elif name == "categories" and value is not None:
            value = list(value)
        elif name == "recurrence" and value:
            value = self._ews_recurrence(
                value,
                target="calendar",
                start=recurrence_start,
            )
        return field, value

    def update_event(self, plan: Any) -> CalendarEvent:
        original = self._resolve_item(plan.reference, "event", "calendar")[1]
        item = self._calendar_scope_item(original, plan.scope)
        reference = (
            plan.reference
            if self._id(item) == self._id(original)
            else self._remember("event", item)
        )

        def update() -> CalendarEvent:
            # Resolve ownership and read all local files before any remote write.
            attachments = self._file_attachments(plan.attachment_paths)
            removals: list[Any] = []
            for attachment_reference in plan.remove_attachment_references:
                if self._attachment_parents.get(attachment_reference) != reference:
                    raise ConfigurationError(
                        "A removed attachment must belong to the selected calendar event."
                    )
                memory = self._memory_object(attachment_reference, "attachment")
                if memory is None:
                    raise ItemNotFoundError("Calendar attachment not found.")
                removals.append(memory[1])
            if (
                str(getattr(item, "type", "")).casefold() in {"occurrence", "exception"}
                and "recurrence" in plan.changes
            ):
                raise ConfigurationError("Change the repetition pattern with entire-series scope.")
            fields: list[str] = []
            recurrence_start = plan.changes.get("start", getattr(item, "start", None))
            for name, value in plan.changes.items():
                field, converted = self._event_change(
                    name,
                    value,
                    recurrence_start=recurrence_start,
                )
                setattr(item, field, converted)
                fields.append(field)
                if name == "reminder_minutes":
                    item.reminder_is_set = value is not None
                    fields.append("reminder_is_set")
            if fields:
                invitations = self._invitation_value(
                    plan.invitations, self._bindings(), changed=True
                )
                item.save(
                    update_fields=list(dict.fromkeys(fields)),
                    send_meeting_invitations=invitations,
                )
            if removals:
                item.detach(removals)
                for attachment_reference in plan.remove_attachment_references:
                    self._attachment_parents.pop(attachment_reference, None)
            if attachments:
                item.attach(attachments)
            if not fields and not removals and not attachments:
                raise ConfigurationError("No calendar fields or attachments were supplied.")
            canonical = self._remember("event", item, reference=reference)
            return self._map_calendar_item(item, reference=canonical)

        return self._call("update calendar event", update)

    def delete_event(self, plan: Any) -> None:
        kind, original = self._resolve_item(plan.reference, "event", "calendar")
        item = self._calendar_scope_item(original, plan.scope)
        if plan.send_cancellation:
            kwargs = {"body": self._construct("Body", plan.reason)} if plan.reason else {}
            self._call("cancel calendar event", lambda: item.cancel(**kwargs))
        else:
            # CalendarItem.delete() is an unconditional hard delete in exchangelib.
            # A series master has its own identity; never rebind an occurrence
            # reference to the master when resolving an explicit series action.
            reference = (
                plan.reference
                if self._id(item) == self._id(original)
                else self._remember(kind, item)
            )
            self._call(
                "move calendar event to Deleted Items",
                lambda: self._move_with_reference(kind, item, reference, self.account.trash),
            )

    def respond_to_meeting(self, plan: Any) -> CalendarEvent | None:
        item = self._resolve_item(plan.reference, "event", "calendar")[1]
        response_name = str(getattr(plan.response, "value", plan.response)).casefold()
        method = {
            CalendarResponse.ACCEPT.value: "accept",
            CalendarResponse.TENTATIVE.value: "tentatively_accept",
            CalendarResponse.DECLINE.value: "decline",
        }.get(response_name)
        if method is None:
            raise ConfigurationError(f"Unknown meeting response: {response_name}")
        disposition = (
            self._bindings().SEND_AND_SAVE_COPY
            if plan.send_response
            else self._bindings().SAVE_ONLY
        )
        kwargs: dict[str, Any] = {}
        if plan.comment:
            kwargs["body"] = self._construct("Body", plan.comment)
        if plan.proposed_start is not None:
            kwargs["proposed_start"] = plan.proposed_start
            kwargs["proposed_end"] = plan.proposed_end
        self._call(
            "respond to meeting",
            lambda: getattr(item, method)(message_disposition=disposition, **kwargs),
        )
        return None

    # ------------------------------------------------------------------
    # Contacts

    def _contacts_folder(self, reference: str | None) -> Any:
        return self._resolve_folder(reference or "contacts")

    @staticmethod
    def _is_contact_group(item: Any) -> bool:
        return type(item).__name__ == "DistributionList" or str(
            getattr(item, "item_class", "")
        ).casefold().startswith("ipm.distlist")

    def _map_contact_group(
        self, item: Any, reference: str, *, members_loaded: bool = True
    ) -> ContactGroup:
        if not self._is_contact_group(item):
            raise UnsupportedFeatureError("The selected item is not a personal contact group.")
        raw = tuple(islice(getattr(item, "members", ()) or (), MAX_GROUP_MEMBERS + 1))
        if len(raw) > MAX_GROUP_MEMBERS:
            raise UnsupportedFeatureError("The group exceeds the 200-member safety limit.")
        members: list[Address] = []
        unresolved = 0
        linked = False
        for member in raw:
            mailbox = getattr(member, "mailbox", None)
            if (
                mailbox is None
                or getattr(member, "status", "Normal") != "Normal"
                or (
                    getattr(mailbox, "routing_type", "SMTP") not in {None, "SMTP"}
                    or getattr(mailbox, "mailbox_type", None)
                    in {"PrivateDL", "PublicDL", "Unknown"}
                )
            ):
                unresolved += 1
                continue
            values = map_addresses((mailbox,))
            if not values or not values[0].email:
                unresolved += 1
                continue
            linked = linked or getattr(mailbox, "item_id", None) is not None
            members.extend(values)
        return ContactGroup(
            reference,
            str(getattr(item, "display_name", "")),
            tuple(members),
            GroupSource.MAILBOX,
            self._changekey(item) or "",
            members_loaded,
            unresolved,
            members_loaded
            and not unresolved
            and not linked
            and getattr(item, "contact_source", "Store") in {None, "Store"},
        )

    def list_contact_groups(self, *, limit: int = 50, offset: int = 0) -> tuple[ContactGroup, ...]:
        folder = self._contacts_folder(None)
        items = self._bounded_items(
            folder,
            fields=("display_name", "item_class"),
            limit=limit,
            offset=offset,
            filters={"item_class__startswith": "IPM.DistList"},
        )
        return tuple(
            self._map_contact_group(
                item, self._remember("contact_group", item, parent=folder), members_loaded=False
            )
            for item in items
            if self._is_contact_group(item)
        )

    def get_contact_group(self, reference: str) -> ContactGroup:
        item = self._fetch_item(
            reference,
            ("contact_group",),
            (
                "display_name",
                "item_class",
                "members",
                "contact_source",
            ),
        )
        value = self._map_contact_group(item, reference)
        self._remember("contact_group", item, reference=reference)
        return value

    def _group_members(self, members: tuple[Address, ...]) -> list[Any]:
        return [
            self._construct(
                "Member",
                mailbox=self._construct(
                    "Mailbox",
                    name=member.name,
                    email_address=member.email,
                    routing_type="SMTP",
                ),
            )
            for member in members
        ]

    def _group_write(self, operation: str, function: Callable[[], T]) -> T:
        try:
            return self._call(operation, function)
        except NetworkError as exc:
            if isinstance(exc, ThrottledError):
                raise
            raise ContactGroupVerificationError(
                "The group update lost its response and may have completed. Refresh before "
                "another change; the write was not retried.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            ) from exc

    def _group_after_write(self, function: Callable[[], T]) -> T:
        try:
            return function()
        except Exception as exc:
            raise ContactGroupVerificationError(
                "Exchange accepted the group change, but local tracking or readback failed. "
                "Refresh before "
                "another change; the write was not retried.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            ) from exc

    def create_contact_group(self, name: str, members: tuple[Address, ...]) -> ContactGroup:
        name, members = validate_group(name, members)
        folder = self._contacts_folder(None)
        item = self._construct(
            "DistributionList",
            account=self.account,
            folder=folder,
            display_name=name,
            members=self._group_members(members),
        )
        self._group_write("create personal contact group", item.save)
        return self._group_after_write(
            lambda: self.get_contact_group(self._remember("contact_group", item, parent=folder))
        )

    def update_contact_group(
        self, before: ContactGroup, name: str, members: tuple[Address, ...]
    ) -> ContactGroup:
        name, members = validate_group(name, members)
        current = self.get_contact_group(before.reference)
        if current.fingerprint != before.fingerprint:
            raise ConflictError("The mailbox group changed after preview. Refresh before editing.")
        if not current.editable:
            raise UnsupportedFeatureError("Inspect linked or unsupported members in Outlook first.")
        item = self._resolve_item(before.reference, "contact_group")[1]
        item.display_name = name
        item.members = self._group_members(members)
        self._group_write(
            "update personal contact group",
            lambda: item.save(
                update_fields=["display_name", "members"],
                conflict_resolution="NeverOverwrite",
            ),
        )

        def readback() -> ContactGroup:
            self._remember("contact_group", item, reference=before.reference)
            return self.get_contact_group(before.reference)

        return self._group_after_write(readback)

    def delete_contact_group(self, before: ContactGroup) -> None:
        current = self.get_contact_group(before.reference)
        if current.fingerprint != before.fingerprint:
            raise ConflictError("The mailbox group changed after preview. Refresh before deleting.")
        item = self._resolve_item(before.reference, "contact_group")[1]
        target = self.account.trash
        target_path = self._folder_path(target)
        result = self._group_write(
            "move personal group to Deleted Items", lambda: item.move(target)
        )

        def track_move() -> None:
            result_id, result_change_key = self._result_identity(result)
            if result_id:
                item.id = result_id
                if result_change_key:
                    item.changekey = result_change_key
            if not self._id(item):
                raise LocalStateError("The moved contact group's identity is unavailable.")
            self._remember(
                "contact_group",
                item,
                reference=before.reference,
                parent=target,
                folder_path=target_path,
            )

        self._group_after_write(track_move)

    def list_contacts(
        self,
        *,
        folder_reference: str | None,
        limit: int,
        offset: int = 0,
        metadata_only: bool = True,
    ) -> tuple[ContactInfo, ...]:
        """List contacts, optionally excluding notes and detailed personal fields."""

        folder = self._contacts_folder(folder_reference)
        values = self._bounded_items(
            folder,
            fields=CONTACT_METADATA_FIELDS if metadata_only else CONTACT_FIELDS,
            limit=limit,
            offset=offset,
            filters={"item_class__startswith": "IPM.Contact"},
        )
        result: list[ContactInfo] = []
        for item in values:
            if self._is_contact_group(item):
                continue
            reference = self._remember("contact", item, parent=folder)
            result.append(map_contact(item, reference=reference))
        return tuple(result)

    @staticmethod
    def _contact_matches(contact: ContactInfo, query: Any) -> bool:
        text = contact_search_text(contact).casefold()
        if query.text and query.text.casefold() not in text:
            return False
        if query.email and not any(
            query.email.casefold() in address.email.casefold() for address in contact.emails
        ):
            return False
        if query.company and query.company.casefold() not in (contact.company or "").casefold():
            return False
        return not (
            query.category
            and query.category.casefold() not in {value.casefold() for value in contact.categories}
        )

    def search_contacts(
        self,
        query: Any,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> tuple[ContactInfo, ...]:
        return self.search_contacts_page(
            query, folder_reference=folder_reference, limit=limit
        ).items

    def search_contacts_page(
        self,
        query: Any,
        *,
        folder_reference: str | None,
        limit: int,
        offset: int = 0,
    ) -> PimPage[ContactInfo]:
        from exchangelib import Q

        limit, offset = self._page(limit, offset)
        folder = self._contacts_folder(folder_reference)
        restriction = Q(item_class__startswith="IPM.Contact")
        if query.email:
            # EWS indexed fields require a concrete label in each restriction.
            restriction &= (
                Q(email_addresses__EmailAddress1__icontains=query.email)
                | Q(email_addresses__EmailAddress2__icontains=query.email)
                | Q(email_addresses__EmailAddress3__icontains=query.email)
            )
        if query.company:
            restriction &= Q(company_name__icontains=query.company)
        if query.category:
            restriction &= Q(categories__iexact=query.category)

        def load() -> PimPage[ContactInfo]:
            source = self._query_source(folder)
            scan_limit = min(MAX_PAGE_SIZE, limit * 4) if query.text else limit
            if type(source).__module__.startswith("exchangelib"):
                # Free text spans formatted dates, indexed labels, addresses and
                # notes; preserve its broader semantics as an explicit bounded
                # residual. Specific filters still run before that scan limit.
                fields = CONTACT_SEARCH_FIELDS if query.text else CONTACT_METADATA_FIELDS
                projected = source.filter(restriction).only(*fields)
                projected.page_size = scan_limit
                values = projected[offset : offset + scan_limit]
            else:
                values = islice(source, offset, offset + scan_limit)
            result: list[ContactInfo] = []
            scanned = 0
            for item in values:
                if isinstance(item, BaseException):
                    raise item
                scanned += 1
                if self._is_contact_group(item):
                    continue
                contact = map_contact(item)
                if not self._contact_matches(contact, query):
                    continue
                reference = self._remember("contact", item, parent=folder)
                result.append(map_contact(item, reference=reference))
                if len(result) >= limit:
                    break
            return PimPage(
                tuple(result),
                offset,
                scanned,
                offset + scanned if scanned == scan_limit or len(result) == limit else None,
                "Free text is matched locally against the projected contact fields."
                if query.text
                else None,
            )

        return self._call("search contacts", load, read_only=True)

    def list_correspondence(
        self,
        *,
        since: datetime | None,
        limit: int,
    ) -> tuple[CorrespondenceEvent, ...]:
        """Read a body-free, bounded inbox/sent history for contact navigation."""

        if since is not None and since.tzinfo is None:
            raise ConfigurationError("Correspondence history start must include a time zone.")
        bounded_limit, _ = self._page(limit, 0)
        observations: list[CorrespondenceEvent] = []
        sources = (
            ("inbox", CorrespondenceDirection.INBOUND),
            ("sent", CorrespondenceDirection.OUTBOUND),
        )
        for folder_reference, direction in sources:
            folder = self._resolve_folder(folder_reference)
            items = self._bounded_items(
                folder,
                fields=CORRESPONDENCE_FIELDS,
                limit=bounded_limit,
                order_by="-datetime_received"
                if direction is CorrespondenceDirection.INBOUND
                else "-datetime_sent",
                filters=(
                    {
                        (
                            "datetime_received__gte"
                            if direction is CorrespondenceDirection.INBOUND
                            else "datetime_sent__gte"
                        ): since
                    }
                    if since is not None
                    else None
                ),
            )
            for item in items:
                occurred_at = (
                    getattr(item, "datetime_received", None)
                    if direction is CorrespondenceDirection.INBOUND
                    else getattr(item, "datetime_sent", None)
                )
                if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
                    continue
                if since is not None and occurred_at < since:
                    continue
                if direction is CorrespondenceDirection.INBOUND:
                    raw_addresses = (
                        getattr(item, "sender", None) or getattr(item, "author", None),
                    )
                else:
                    raw_addresses = (
                        *(getattr(item, "to_recipients", None) or ()),
                        *(getattr(item, "cc_recipients", None) or ()),
                        *(getattr(item, "bcc_recipients", None) or ()),
                    )
                reference = self._remember("message", item, parent=folder)
                observations.extend(
                    CorrespondenceEvent(address, occurred_at, direction, reference)
                    for address in map_addresses(raw_addresses)
                )
        observations.sort(
            key=lambda event: (
                -event.occurred_at.astimezone(UTC).timestamp(),
                event.address.email.casefold(),
                event.message_reference or "",
            )
        )
        return tuple(observations[:bounded_limit])

    def get_contact(self, reference: str) -> ContactInfo:
        item = self._fetch_item(reference, ("contact",), CONTACT_FIELDS)
        if self._is_contact_group(item):
            raise UnsupportedFeatureError("Use the contact-group workflow for this item.")
        self._remember("contact", item, reference=reference)
        return map_contact(item, reference=reference)

    def _contact_indexed(self, contact: ContactInfo) -> dict[str, Any]:
        email_values = [
            self._construct("EmailAddress", label=f"EmailAddress{index}", email=address.email)
            for index, address in enumerate(contact.emails[:3], start=1)
        ]
        phone_labels: set[str] = set()
        phone_values = []
        for label, value in contact.phones.items():
            canonical = self._contact_index_label(
                label,
                aliases={
                    "business": "BusinessPhone",
                    "work": "BusinessPhone",
                    "home": "HomePhone",
                    "mobile": "MobilePhone",
                    "cell": "MobilePhone",
                    "primary": "PrimaryPhone",
                    "fax": "BusinessFax",
                    "other": "OtherTelephone",
                },
                available=(
                    "BusinessPhone",
                    "BusinessPhone2",
                    "HomePhone",
                    "HomePhone2",
                    "MobilePhone",
                    "PrimaryPhone",
                    "OtherTelephone",
                    "CompanyMainPhone",
                    "BusinessFax",
                    "HomeFax",
                    "OtherFax",
                    "AssistantPhone",
                    "Callback",
                    "CarPhone",
                    "Pager",
                    "RadioPhone",
                    "Telex",
                    "TtyTddPhone",
                    "Isdn",
                ),
                used=phone_labels,
                field="phone",
            )
            phone_values.append(self._construct("PhoneNumber", label=canonical, phone_number=value))
        address_labels: set[str] = set()
        physical_values = []
        for label, value in contact.addresses.items():
            canonical = self._contact_index_label(
                label,
                aliases={"business": "Business", "work": "Business", "home": "Home"},
                available=("Business", "Home", "Other"),
                used=address_labels,
                field="postal address",
            )
            physical_values.append(
                self._construct("PhysicalAddress", label=canonical, street=value)
            )
        return {
            "email_addresses": email_values,
            "phone_numbers": phone_values,
            "physical_addresses": physical_values,
        }

    @staticmethod
    def _contact_index_label(
        label: str,
        *,
        aliases: Mapping[str, str],
        available: tuple[str, ...],
        used: set[str],
        field: str,
    ) -> str:
        key = re.sub(r"[^a-z0-9]", "", label.casefold())
        canonical_by_key = {
            re.sub(r"[^a-z0-9]", "", value.casefold()): value for value in available
        }
        preferred = aliases.get(key) or canonical_by_key.get(key)
        candidates = (preferred, *available) if preferred else available
        for candidate in candidates:
            if candidate and candidate not in used:
                used.add(candidate)
                return candidate
        raise ConfigurationError(
            f"Exchange supports at most {len(available)} distinct {field} labels per contact."
        )

    def _contact_kwargs(self, contact: ContactInfo) -> dict[str, Any]:
        return {
            "display_name": contact.display_name,
            "given_name": contact.given_name,
            "middle_name": contact.middle_name,
            "surname": contact.family_name,
            "nickname": contact.nickname,
            "company_name": contact.company,
            "department": contact.department,
            "job_title": contact.job_title,
            "birthday": contact.birthday,
            "body": self._construct("Body", contact.notes or ""),
            "categories": list(contact.categories),
            **self._contact_indexed(contact),
        }

    def create_contact(self, plan: Any) -> ContactInfo:
        folder = self._contacts_folder(plan.folder_reference)

        def create() -> ContactInfo:
            item = self._construct(
                "Contact",
                account=self.account,
                folder=folder,
                **self._contact_kwargs(plan.contact),
            )
            item.save()
            reference = self._remember("contact", item, parent=folder)
            return map_contact(item, reference=reference)

        return self._call("create contact", create)

    def update_contact(self, plan: Any) -> ContactInfo:
        item = self._fetch_item(plan.reference, ("contact",), CONTACT_FIELDS)
        if self._is_contact_group(item):
            raise UnsupportedFeatureError("Use the contact-group workflow for this item.")
        aliases = {
            "family_name": "surname",
            "company": "company_name",
            "notes": "body",
            "emails": "email_addresses",
            "phones": "phone_numbers",
            "addresses": "physical_addresses",
        }

        def update() -> ContactInfo:
            fields: list[str] = []
            for name, value in plan.changes.items():
                field = aliases.get(name, name)
                if name == "notes":
                    value = self._construct("Body", value or "")
                elif name == "emails":
                    value = self._contact_indexed(
                        ContactInfo("temporary", "temporary", emails=tuple(value or ()))
                    )["email_addresses"]
                elif name == "phones":
                    value = self._contact_indexed(
                        ContactInfo("temporary", "temporary", phones=value or {})
                    )["phone_numbers"]
                elif name == "addresses":
                    value = self._contact_indexed(
                        ContactInfo("temporary", "temporary", addresses=value or {})
                    )["physical_addresses"]
                elif name == "categories" and value is not None:
                    value = list(value)
                setattr(item, field, value)
                fields.append(field)
            if not fields:
                raise ConfigurationError("No contact fields were supplied for update.")
            # Exchange can recompute/clear DisplayName when company/name fields
            # change. A partial patch must preserve the user's explicit name.
            if "display_name" not in fields and getattr(item, "display_name", None):
                fields.append("display_name")
            item.save(update_fields=fields)
            self._remember("contact", item, reference=plan.reference)
            return map_contact(item, reference=plan.reference)

        return self._call("update contact", update)

    def delete_contact(self, plan: Any) -> None:
        item = self._fetch_item(plan.reference, ("contact",), CONTACT_FIELDS)
        if self._is_contact_group(item):
            raise UnsupportedFeatureError("Use the contact-group workflow for this item.")
        if plan.hard_delete:
            self._call(
                "permanently delete contact",
                item.delete,
            )
        else:
            self._call(
                "move contact to Deleted Items",
                lambda: self._move_with_reference(
                    "contact", item, plan.reference, self.account.trash
                ),
            )

    # ------------------------------------------------------------------
    # Tasks

    def _tasks_folder(self, reference: str | None) -> Any:
        return self._resolve_folder(reference or "tasks")

    def list_tasks(
        self,
        *,
        folder_reference: str | None,
        limit: int,
        offset: int = 0,
        metadata_only: bool = True,
    ) -> tuple[TaskInfo, ...]:
        """List tasks, optionally excluding bodies and recurrence details."""

        folder = self._tasks_folder(folder_reference)
        values = self._bounded_items(
            folder,
            fields=TASK_METADATA_FIELDS if metadata_only else TASK_FIELDS,
            limit=limit,
            offset=offset,
        )
        result: list[TaskInfo] = []
        for item in values:
            reference = self._remember("task", item, parent=folder)
            result.append(map_task(item, reference=reference))
        return tuple(result)

    @staticmethod
    def _task_matches(task: TaskInfo, query: Any) -> bool:
        if getattr(query, "exclude_completed", False) and (
            task.status == "completed" or task.percent_complete >= 100
        ):
            return False
        if query.text and query.text.casefold() not in f"{task.subject} {task.body}".casefold():
            return False
        if query.status and task.status.casefold().replace("_", "") != str(
            getattr(query.status, "value", query.status)
        ).casefold().replace("_", ""):
            return False
        if query.due_before and (task.due_date is None or task.due_date >= query.due_before):
            return False
        if query.due_after and (task.due_date is None or task.due_date <= query.due_after):
            return False
        if query.category and query.category.casefold() not in {
            value.casefold() for value in task.categories
        }:
            return False
        return not (
            query.overdue_at and (task.due_date is None or task.due_date >= query.overdue_at)
        )

    def search_tasks(
        self,
        query: Any,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> tuple[TaskInfo, ...]:
        return self.search_tasks_page(query, folder_reference=folder_reference, limit=limit).items

    def search_tasks_page(
        self,
        query: Any,
        *,
        folder_reference: str | None,
        limit: int,
        offset: int = 0,
    ) -> PimPage[TaskInfo]:
        """Apply supported restrictions before limiting server results."""
        from exchangelib import Q

        limit, offset = self._page(limit, offset)
        folder = self._tasks_folder(folder_reference)
        restriction = Q()
        if query.text:
            restriction &= Q(subject__icontains=query.text) | Q(body__icontains=query.text)
        if query.status:
            restriction &= Q(is_complete=self._task_status(query.status) == "Completed")
        if query.due_before:
            restriction &= Q(due_date__lt=query.due_before)
        if query.due_after:
            restriction &= Q(due_date__gt=query.due_after)
        if query.overdue_at:
            restriction &= Q(due_date__lt=query.overdue_at)
        if query.category:
            restriction &= Q(categories__iexact=query.category)
        if getattr(query, "exclude_completed", False):
            restriction &= Q(is_complete=False)

        def load() -> PimPage[TaskInfo]:
            source = self._query_source(folder)
            residual_status = query.status and self._task_status(query.status) != "Completed"
            scan_limit = min(MAX_PAGE_SIZE, limit * 4) if residual_status else limit
            if type(source).__module__.startswith("exchangelib"):
                # Body matches stay server-side; result rows need metadata only.
                # EWS cannot restrict the individual incomplete TaskStatus values.
                # Keep that residual bounded and explicitly distinguish it from the
                # completion/date/text restrictions pushed to Exchange.
                projected = source.filter(restriction).only(*TASK_METADATA_FIELDS)
                projected.page_size = scan_limit
                values = projected[offset : offset + scan_limit]
            else:
                values = islice(source, offset, offset + scan_limit)
            result: list[TaskInfo] = []
            scanned = 0
            for item in values:
                if isinstance(item, BaseException):
                    raise item
                scanned += 1
                if not type(source).__module__.startswith("exchangelib") and not self._task_matches(
                    map_task(item), query
                ):
                    continue
                if query.status and self._task_status(map_task(item).status) != self._task_status(
                    query.status
                ):
                    continue
                reference = self._remember("task", item, parent=folder)
                result.append(map_task(item, reference=reference))
                if len(result) >= limit:
                    break
            return PimPage(
                tuple(result),
                offset,
                scanned,
                offset + scanned if scanned == scan_limit or len(result) == limit else None,
                "The individual incomplete status is matched locally." if residual_status else None,
            )

        return self._call("search tasks", load, read_only=True)

    def get_task(self, reference: str) -> TaskInfo:
        item = self._fetch_item(reference, ("task",), TASK_FIELDS)
        self._remember("task", item, reference=reference)
        return map_task(item, reference=reference)

    @staticmethod
    def _task_status(value: object) -> str:
        key = str(getattr(value, "value", value)).casefold().replace("_", "")
        return {
            "notstarted": "NotStarted",
            "inprogress": "InProgress",
            "completed": "Completed",
            "waitingonothers": "WaitingOnOthers",
            "deferred": "Deferred",
        }.get(key, str(value))

    def _task_kwargs(self, task: TaskInfo) -> dict[str, Any]:
        return {
            "subject": task.subject,
            "status": self._task_status(task.status),
            "percent_complete": Decimal(task.percent_complete),
            "start_date": task.start_date,
            "due_date": task.due_date,
            "complete_date": task.completed_at,
            "body": self._construct("Body", task.body),
            "categories": list(task.categories),
            "importance": task.importance.value.title(),
            "reminder_is_set": task.reminder_at is not None,
            "reminder_due_by": task.reminder_at,
            "recurrence": self._ews_recurrence(
                task.recurrence,
                target="task",
                start=task.start_date or task.due_date,
            )
            if task.recurrence
            else None,
        }

    def create_task(self, plan: Any) -> TaskInfo:
        folder = self._tasks_folder(plan.folder_reference)

        def create() -> TaskInfo:
            item = self._construct(
                "Task",
                account=self.account,
                folder=folder,
                **self._task_kwargs(plan.task),
            )
            item.save()
            try:
                if not self._id(item):
                    raise LocalStateError("The accepted Task creation returned no usable item ID.")
                reference = self._remember("task", item, parent=folder)
                result = map_task(item, reference=reference)
                from gucmail.services.tasks import normalize_task

                normalize_task(result)
                return result
            except Exception as exc:
                raise MutationResultUnavailableError(
                    "The Task creation was accepted, but its result could not be read or recorded. "
                    "Do not create it again automatically.",
                    suggestion="Refresh the bounded Tasks list and inspect newly created Tasks.",
                ) from exc

        return self._call("create task", create)

    def _local_task_history(self) -> TaskHistory:
        history = getattr(self, "_task_history_value", None)
        if not isinstance(history, TaskHistory) or history.state is not self.state:
            history = TaskHistory(self.state)
            self._task_history_value = history
        return history

    def get_task_history(
        self, reference: str, *, limit: int = 20, before: int | None = None
    ) -> TaskHistoryPage:
        """Inspect local observations without resolving credentials or an Account."""
        return self._local_task_history().page(reference, limit=limit, before=before)

    def update_task(self, plan: Any) -> TaskInfo:
        item = self._resolve_item(plan.reference, "task")[1]
        aliases = {
            "completed_at": "complete_date",
            "reminder_at": "reminder_due_by",
        }

        def update() -> TaskInfo:
            from gucmail.services.task_history import ACTIONS, history_reference

            original_id = self._id(item)
            original_reference = self._remember(
                "task",
                item,
                reference=None if plan.reference.startswith("ews:") else plan.reference,
            )
            history_reference(original_reference)
            action = str(getattr(plan, "action", "edit"))
            if action not in ACTIONS:
                raise ConfigurationError("Unknown Task lifecycle action. No update was attempted.")
            fields: list[str] = []
            recurrence_start = plan.changes.get(
                "start_date",
                getattr(item, "start_date", None) or getattr(item, "due_date", None),
            )
            for name, value in plan.changes.items():
                field = aliases.get(name, name)
                if name == "completed_at":
                    # EWS CompleteDate is read-only. Status drives its server value.
                    # Clear the cached value so exchangelib.clean() cannot force a
                    # reopened task back to Completed before serializing Status.
                    item.complete_date = None
                    continue
                if name == "status":
                    value = self._task_status(value)
                elif name == "percent_complete":
                    value = Decimal(value)
                elif name == "body":
                    value = self._construct("Body", value or "")
                elif name == "importance" and value is not None:
                    value = str(getattr(value, "value", value)).title()
                elif name == "categories" and value is not None:
                    value = list(value)
                elif name == "recurrence" and value:
                    value = self._ews_recurrence(
                        value,
                        target="task",
                        start=recurrence_start,
                    )
                setattr(item, field, value)
                fields.append(field)
                if name == "reminder_at":
                    item.reminder_is_set = value is not None
                    fields.append("reminder_is_set")
            if not fields:
                raise ConfigurationError("No task fields were supplied for update.")
            item.save(update_fields=list(dict.fromkeys(fields)))
            result_reference = original_reference
            observation = None
            history = None
            try:
                history = self._local_task_history()
                result_id = self._id(item)
                if not result_id:
                    raise LocalStateError("The accepted Task update returned no usable item ID.")
                distinct_instance = bool(original_id and result_id != original_id)
                if distinct_instance:
                    # Task UpdateItem can create a one-off instance while the
                    # original ID remains the active series/next task. This is
                    # a birth, not a move; never retire that original identity.
                    with self._reference_lock:
                        for reference in {original_reference, plan.reference}:
                            self._objects.pop(("task", reference), None)
                            self._parents.pop(("task", reference), None)
                    result_reference = self._remember("task", item)
                else:
                    result_reference = self._remember("task", item, reference=original_reference)
                observation = history.accepted(
                    original_reference,
                    result_reference,
                    action=action,
                    distinct_instance=distinct_instance,
                )
                readback = type(item).__module__.startswith("exchangelib") and (
                    distinct_instance or "completed_at" in plan.changes
                )
                result = (
                    self.get_task(result_reference)
                    if readback
                    else map_task(item, reference=result_reference)
                )
                from gucmail.services.tasks import normalize_task

                normalize_task(result)
                history.finish(observation.operation_id, verified=True if readback else None)
                return result
            except Exception as exc:
                if observation is not None and history is not None:
                    # Keep the original accepted-result failure if local journaling also fails.
                    with contextlib.suppress(Exception):
                        history.finish(observation.operation_id, verified=False)
                raise MutationResultUnavailableError(
                    "The task update was accepted, but its result could not be read or recorded. "
                    "Do not repeat the update automatically.",
                    suggestion=(
                        "Inspect the original task "
                        + sanitize_terminal(original_reference, max_length=200)
                        + " and result "
                        + sanitize_terminal(result_reference, max_length=200)
                        + "; refresh the bounded Tasks list before deciding what to do next."
                    ),
                ) from exc

        return self._call("update task", update)

    def _ews_recurrence(
        self,
        value: Mapping[str, object],
        *,
        target: str,
        start: date | datetime | None,
    ) -> Any:
        """Translate the validated recurrence schema to exchangelib elements.

        This helper imports only local type definitions and exchangelib's pure
        recurrence classes.  It does not construct an Account or perform any
        request, which keeps dry-run planning and unit tests fully offline.
        """

        from exchangelib import recurrence as ews_recurrence

        from gucmail.services.recurrence import (
            AbsoluteMonthlyPattern,
            AbsoluteYearlyPattern,
            DailyPattern,
            EndDateBoundary,
            NoEndBoundary,
            NumberedBoundary,
            RecurrenceTarget,
            RelativeMonthlyPattern,
            RelativeWeekday,
            RelativeYearlyPattern,
            TaskRegenerationPattern,
            WeekIndex,
            WeeklyPattern,
            normalize_recurrence,
        )

        recurrence_target = RecurrenceTarget(target)
        definition = normalize_recurrence(
            value,
            target=recurrence_target,
            start=start,
        )
        relative_weekday_numbers = {
            RelativeWeekday.MONDAY: 1,
            RelativeWeekday.TUESDAY: 2,
            RelativeWeekday.WEDNESDAY: 3,
            RelativeWeekday.THURSDAY: 4,
            RelativeWeekday.FRIDAY: 5,
            RelativeWeekday.SATURDAY: 6,
            RelativeWeekday.SUNDAY: 7,
            RelativeWeekday.DAY: 8,
            RelativeWeekday.WEEKDAY: 9,
            RelativeWeekday.WEEKEND_DAY: 10,
        }
        week_index_numbers = {
            WeekIndex.FIRST: 1,
            WeekIndex.SECOND: 2,
            WeekIndex.THIRD: 3,
            WeekIndex.FOURTH: 4,
            WeekIndex.LAST: 5,
        }
        pattern = definition.pattern
        if isinstance(pattern, TaskRegenerationPattern):
            regeneration_class = {
                "day": ews_recurrence.DailyRegeneration,
                "week": ews_recurrence.WeeklyRegeneration,
                "month": ews_recurrence.MonthlyRegeneration,
                "year": ews_recurrence.YearlyRegeneration,
            }[pattern.unit.value]
            provider_pattern = regeneration_class(interval=pattern.interval)
        elif isinstance(pattern, DailyPattern):
            provider_pattern = ews_recurrence.DailyPattern(interval=pattern.interval)
        elif isinstance(pattern, WeeklyPattern):
            provider_pattern = ews_recurrence.WeeklyPattern(
                interval=pattern.interval,
                weekdays=[item.iso_number for item in pattern.weekdays],
                first_day_of_week=pattern.first_day_of_week.iso_number,
            )
        elif isinstance(pattern, AbsoluteMonthlyPattern):
            provider_pattern = ews_recurrence.AbsoluteMonthlyPattern(
                interval=pattern.interval,
                day_of_month=pattern.day_of_month,
            )
        elif isinstance(pattern, RelativeMonthlyPattern):
            provider_pattern = ews_recurrence.RelativeMonthlyPattern(
                interval=pattern.interval,
                weekday=relative_weekday_numbers[pattern.weekday],
                week_number=week_index_numbers[pattern.week_index],
            )
        elif isinstance(pattern, AbsoluteYearlyPattern):
            provider_pattern = ews_recurrence.AbsoluteYearlyPattern(
                month=int(pattern.month),
                day_of_month=pattern.day_of_month,
            )
        elif isinstance(pattern, RelativeYearlyPattern):
            provider_pattern = ews_recurrence.RelativeYearlyPattern(
                month=int(pattern.month),
                weekday=relative_weekday_numbers[pattern.weekday],
                week_number=week_index_numbers[pattern.week_index],
            )
        else:  # pragma: no cover - normalize_recurrence exhausts the union
            raise UnsupportedFeatureError("The recurrence pattern is not supported by EWS.")

        boundary = definition.boundary
        if isinstance(boundary, NumberedBoundary):
            provider_boundary = ews_recurrence.NumberedPattern(
                start=boundary.start,
                number=boundary.occurrences,
            )
        elif isinstance(boundary, EndDateBoundary):
            provider_boundary = ews_recurrence.EndDatePattern(
                start=boundary.start,
                end=boundary.end,
            )
        elif isinstance(boundary, NoEndBoundary):
            provider_boundary = ews_recurrence.NoEndPattern(start=boundary.start)
        else:  # pragma: no cover - normalize_recurrence exhausts the union
            raise UnsupportedFeatureError("The recurrence boundary is not supported by EWS.")
        recurrence_class = (
            ews_recurrence.TaskRecurrence
            if recurrence_target is RecurrenceTarget.TASK
            else ews_recurrence.Recurrence
        )
        return recurrence_class(pattern=provider_pattern, boundary=provider_boundary)

    def delete_task(self, plan: Any) -> None:
        from gucmail.services.tasks import TaskDeletePlan

        plan = TaskDeletePlan(
            plan.reference, plan.hard_delete, getattr(plan, "delete_series", False)
        )
        item = self._resolve_item(plan.reference, "task")[1]
        if plan.delete_series:
            if type(item).__module__.startswith("exchangelib"):
                from exchangelib import Task

                def scope_item() -> Any:
                    values = list(
                        self.account.fetch(ids=[item], only_fields=("recurrence", "is_recurring"))
                    )
                    if len(values) != 1:
                        raise ConfigurationError("The task series could not be verified.")
                    result = values[0]
                    if isinstance(result, BaseException):
                        raise result
                    if not isinstance(result, Task):
                        raise ConfigurationError(
                            "The series preflight did not return a Task. No deletion was attempted."
                        )
                    if self._id(result) != self._id(item):
                        raise ConfigurationError("The returned task series identity changed.")
                    if getattr(result, "is_recurring", None) is not True:
                        raise ConfigurationError(
                            "Exchange did not confirm that this is a recurring Task. "
                            "No deletion was attempted. Refresh its active task reference "
                            "or choose occurrence deletion explicitly."
                        )
                    return result

                item = self._call("verify task series deletion scope", scope_item, read_only=True)
            if not (getattr(item, "is_recurring", False) or getattr(item, "recurrence", None)):
                raise ConfigurationError(
                    "The referenced task is not an active recurring series. "
                    "Use its active series reference or delete this occurrence explicitly."
                )
            self._remember("task", item, reference=plan.reference)
        if plan.hard_delete:
            self._call(
                "permanently delete task",
                lambda: item.delete(
                    affected_task_occurrences="AllOccurrences"
                    if plan.delete_series
                    else "SpecifiedOccurrenceOnly"
                ),
            )
        else:
            # Task.delete() is an unconditional hard delete in exchangelib.
            self._call(
                "move task to Deleted Items",
                lambda: self._move_with_reference("task", item, plan.reference, self.account.trash),
            )

    @staticmethod
    def _restore_mapping(value: object, *, label: str = "backup item") -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ConfigurationError(f"Restored {label} data is not a mapping.")
        return value

    @classmethod
    def _restore_addresses(cls, value: object) -> tuple[Address, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        output: list[Address] = []
        for raw in value:
            item = cls._restore_mapping(raw, label="address")
            email = str(item.get("email", "")).strip()
            if email:
                output.append(
                    Address(
                        email=email,
                        name=str(item.get("name") or "").strip() or None,
                        mailbox_type=str(item.get("mailbox_type") or "").strip() or None,
                    )
                )
        return tuple(output)

    @staticmethod
    def _restore_datetime(value: object, *, label: str, required: bool = False) -> datetime | None:
        if value in (None, ""):
            if required:
                raise ConfigurationError(f"Restored {label} is missing.")
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"Restored {label} is not ISO-8601.") from exc
        if parsed.tzinfo is None:
            raise ConfigurationError(f"Restored {label} does not contain a UTC offset.")
        return parsed

    @staticmethod
    def _restore_date(value: object, *, label: str) -> Any:
        if value in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError as exc:
            raise ConfigurationError(f"Restored {label} is not an ISO date.") from exc

    def _restore_folder(self, destination: str, kind: str) -> Any:
        normalized = kind.casefold()
        if normalized in {"calendar", "event"}:
            current = self.account.calendar
            folder_class = "CalendarFolder"
        elif normalized == "contact":
            current = self.account.contacts
            folder_class = "ContactsFolder"
        elif normalized == "task":
            current = self.account.tasks
            folder_class = "TasksFolder"
        else:
            current = self.account.msg_folder_root
            folder_class = "Folder"
        for component in self._folder_parts(destination):
            try:
                current = self._find_child(current, component)
            except ItemNotFoundError:
                parent = current
                created = self._construct(folder_class, parent=parent, name=component)
                self._call(
                    "create restore folder",
                    partial(self._save_new_folder, created, parent),
                )
                current = created
        return current

    def _restore_attachments(self, item: Any) -> list[Any]:
        """Validate every payload before creating the destination item."""
        attachments = []
        for raw in item.attachments:
            content = raw.get("content")
            if not isinstance(content, bytes):
                raise ConfigurationError("Restored attachment content is unavailable.")
            metadata = self._restore_mapping(raw.get("metadata") or {}, label="attachment")
            if metadata.get("role") == "message_mime":
                continue
            attachments.append(
                self._construct(
                    "FileAttachment",
                    name=str(
                        raw.get("original_filename")
                        or raw.get("normalized_filename")
                        or "attachment"
                    ),
                    content=content,
                    content_type=raw.get("content_type"),
                    is_inline=bool(metadata.get("inline", False)),
                    content_id=metadata.get("content_id"),
                    is_contact_photo=bool(metadata.get("is_contact_photo", False)),
                )
            )
        return attachments

    def _save_restored_item(
        self,
        remote: Any,
        kind: str,
        folder: Any,
        *,
        attachments: Any = (),
        save_options: Mapping[str, Any] | None = None,
    ) -> str:
        """Create once; missing results and later failures cannot imply rollback."""
        from exchangelib.errors import ResponseMessageError

        if getattr(remote, "id", None) is not None:
            raise ConfigurationError("Restore requires a new unsaved item.")

        def uncertain() -> MutationResultUnavailableError:
            return MutationResultUnavailableError(
                "Exchange may have created the restored item, but its result is unavailable.",
                suggestion=(
                    "Inspect the dedicated restore folder before another attempt. "
                    "No automatic retry was made."
                ),
            )

        try:
            remote.save(**(save_options or {}))
        except ResponseMessageError:
            # A structured CreateItem rejection is known only before an identity
            # was accepted. Any subsequent attachment/reference failure is uncertain.
            if getattr(remote, "id", None) is None:
                raise
            raise uncertain() from None
        except Exception:
            raise uncertain() from None
        try:
            for field in ("id", "changekey"):
                validate_item_identifier(getattr(remote, field, None), "returned " + field)
            if attachments:
                remote.attach(attachments)
            for field in ("id", "changekey"):
                validate_item_identifier(getattr(remote, field, None), "final " + field)
            return self._remember(kind, remote, parent=folder)
        except Exception:
            raise uncertain() from None

    def _restore_message(self, item: Any, folder: Any, data: Mapping[str, Any]) -> str:
        summary = self._restore_mapping(data.get("summary"), label="message summary")
        importance = Importance(str(summary.get("importance", "normal")).casefold())
        sensitivity = Sensitivity(str(data.get("sensitivity", "normal")).casefold())
        attachments = self._restore_attachments(item)
        mime_payloads = [
            raw["content"]
            for raw in item.attachments
            if (raw.get("metadata") or {}).get("role") == "message_mime"
        ]
        if len(mime_payloads) > 1:
            raise ConfigurationError(
                "The backup contains more than one MIME source for this message."
            )

        def create() -> str:
            if mime_payloads:
                # CreateItem/SaveOnly parses MIME and creates its attachments.
                # Attaching the separately archived files again would duplicate them.
                message = self._construct(
                    "Message",
                    account=self.account,
                    folder=folder,
                    mime_content=mime_payloads[0],
                    categories=[str(value) for value in summary.get("categories", ())],
                    importance=importance.value.title(),
                    sensitivity=sensitivity.value.title(),
                    is_read=bool(summary.get("is_read", False)),
                )
                return self._save_restored_item(message, "message", folder)
            message = self._construct(
                "Message",
                account=self.account,
                folder=folder,
                subject=str(summary.get("subject") or "(restored message)"),
                body=self._construct(
                    "HTMLBody" if data.get("body_html") else "Body",
                    str(data.get("body_html") or data.get("body_text") or ""),
                ),
                to_recipients=self._mailboxes(self._restore_addresses(data.get("to"))),
                cc_recipients=self._mailboxes(self._restore_addresses(data.get("cc"))),
                bcc_recipients=self._mailboxes(self._restore_addresses(data.get("bcc"))),
                reply_to=self._mailboxes(self._restore_addresses(data.get("reply_to"))),
                categories=[str(value) for value in summary.get("categories", ())],
                importance=importance.value.title(),
                sensitivity=sensitivity.value.title(),
                is_read=bool(summary.get("is_read", False)),
            )
            return self._save_restored_item(message, "message", folder, attachments=attachments)

        return self._call("restore message", create)

    def _restore_event(self, folder: Any, data: Mapping[str, Any], *, attachments: Any = ()) -> str:
        start = self._restore_datetime(data.get("start"), label="event start", required=True)
        end = self._restore_datetime(data.get("end"), label="event end", required=True)
        assert start is not None and end is not None
        event = CalendarEvent(
            reference="restore:event",
            subject=str(data.get("subject") or "(restored event)"),
            start=start,
            end=end,
            timezone=str(data.get("timezone") or self.config.timezone),
            location=str(data.get("location")) if data.get("location") is not None else None,
            body=str(data.get("body") or ""),
            required_attendees=self._restore_addresses(data.get("required_attendees")),
            optional_attendees=self._restore_addresses(data.get("optional_attendees")),
            resources=self._restore_addresses(data.get("resources")),
            is_all_day=bool(data.get("is_all_day", False)),
            is_meeting=bool(data.get("is_meeting", False)),
            categories=tuple(str(value) for value in data.get("categories", ())),
            importance=Importance(str(data.get("importance", "normal")).casefold()),
            sensitivity=Sensitivity(str(data.get("sensitivity", "normal")).casefold()),
            reminder_minutes=(
                int(data["reminder_minutes"]) if data.get("reminder_minutes") is not None else None
            ),
            availability=str(data.get("availability") or "busy"),
            recurrence=self._restore_mapping(data["recurrence"], label="recurrence")
            if data.get("recurrence")
            else None,
        )

        def create() -> str:
            remote = self._construct(
                "CalendarItem",
                account=self.account,
                folder=folder,
                **self._event_kwargs(event),
            )
            return self._save_restored_item(
                remote,
                "event",
                folder,
                attachments=attachments,
                save_options={"send_meeting_invitations": self._bindings().SEND_TO_NONE},
            )

        return self._call("restore calendar event", create)

    def _restore_contact(
        self, folder: Any, data: Mapping[str, Any], *, attachments: Any = ()
    ) -> str:
        phones = self._restore_mapping(data.get("phones") or {}, label="contact phones")
        addresses = self._restore_mapping(data.get("addresses") or {}, label="contact addresses")
        contact = ContactInfo(
            reference="restore:contact",
            display_name=str(data.get("display_name") or "(restored contact)"),
            given_name=str(data.get("given_name")) if data.get("given_name") is not None else None,
            middle_name=(
                str(data.get("middle_name")) if data.get("middle_name") is not None else None
            ),
            family_name=(
                str(data.get("family_name")) if data.get("family_name") is not None else None
            ),
            nickname=str(data.get("nickname")) if data.get("nickname") is not None else None,
            emails=self._restore_addresses(data.get("emails")),
            phones={str(key): str(value) for key, value in phones.items()},
            addresses={str(key): str(value) for key, value in addresses.items()},
            company=str(data.get("company")) if data.get("company") is not None else None,
            department=(
                str(data.get("department")) if data.get("department") is not None else None
            ),
            job_title=(str(data.get("job_title")) if data.get("job_title") is not None else None),
            birthday=self._restore_date(data.get("birthday"), label="contact birthday"),
            notes=str(data.get("notes")) if data.get("notes") is not None else None,
            categories=tuple(str(value) for value in data.get("categories", ())),
        )

        def create() -> str:
            remote = self._construct(
                "Contact",
                account=self.account,
                folder=folder,
                **self._contact_kwargs(contact),
            )
            return self._save_restored_item(remote, "contact", folder, attachments=attachments)

        return self._call("restore contact", create)

    def _restore_task(self, folder: Any, data: Mapping[str, Any], *, attachments: Any = ()) -> str:
        task = TaskInfo(
            reference="restore:task",
            subject=str(data.get("subject") or "(restored task)"),
            status=str(data.get("status") or "not_started"),
            percent_complete=int(data.get("percent_complete") or 0),
            start_date=self._restore_date(data.get("start_date"), label="task start date"),
            due_date=self._restore_date(data.get("due_date"), label="task due date"),
            completed_at=self._restore_datetime(data.get("completed_at"), label="task completion"),
            reminder_at=self._restore_datetime(data.get("reminder_at"), label="task reminder"),
            recurrence=self._restore_mapping(data["recurrence"], label="recurrence")
            if data.get("recurrence")
            else None,
            body=str(data.get("body") or ""),
            categories=tuple(str(value) for value in data.get("categories", ())),
            importance=Importance(str(data.get("importance", "normal")).casefold()),
        )

        def create() -> str:
            remote = self._construct(
                "Task",
                account=self.account,
                folder=folder,
                **self._task_kwargs(task),
            )
            return self._save_restored_item(remote, "task", folder, attachments=attachments)

        return self._call("restore task", create)

    def restore_item(self, item: Any, destination_folder: str) -> str:
        """Best-effort SaveOnly restore into a dedicated folder; never sends content."""

        kind = str(item.kind).casefold()
        data = self._restore_mapping(item.data)
        if kind not in {"message", "email", "draft", "calendar", "event", "contact", "task"}:
            raise UnsupportedFeatureError(f"Backup item kind cannot be restored: {kind}")
        native_content = self._native_restore_content(item, kind)
        if native_content is not None:
            folder = self._restore_folder(destination_folder, kind)
            return self._restore_native_pim(folder, kind, native_content)
        # Reject malformed recurrence and attachment data before creating folders.
        if kind in {"calendar", "event", "task"} and data.get("recurrence"):
            self._ews_recurrence(
                self._restore_mapping(data["recurrence"], label="recurrence"),
                target="task" if kind == "task" else "calendar",
                start=self._restore_date(data.get("start_date"), label="task start")
                if kind == "task"
                else self._restore_datetime(data.get("start"), label="event start", required=True),
            )
        attachments = self._restore_attachments(item)
        folder = self._restore_folder(destination_folder, kind)
        if kind in {"message", "email", "draft"}:
            return self._restore_message(item, folder, data)
        if kind in {"calendar", "event"}:
            return self._restore_event(folder, data, attachments=attachments)
        if kind == "contact":
            return self._restore_contact(folder, data, attachments=attachments)
        if kind == "task":
            return self._restore_task(folder, data, attachments=attachments)
        raise UnsupportedFeatureError(f"Backup item kind cannot be restored: {kind}")

    def _native_restore_content(self, item: Any, kind: str) -> bytes | None:
        native = []
        for attachment in item.attachments:
            metadata = self._restore_mapping(attachment.get("metadata") or {}, label="attachment")
            role = metadata.get("role")
            if (
                isinstance(role, str)
                and role.startswith(NATIVE_ITEM_ROLE)
                and role != NATIVE_ITEM_ROLE
            ):
                raise ConfigurationError("The native PIM backup format is not recognized.")
            if metadata.get("role") == NATIVE_ITEM_ROLE:
                native.append((attachment, metadata))
        if not native:
            return None
        if len(native) != 1 or len(item.attachments) != 1:
            raise ConfigurationError("A native PIM backup requires exactly one item stream.")
        attachment, metadata = native[0]
        normalized = "calendar" if kind == "event" else kind
        if (
            normalized not in {"calendar", "contact", "task"}
            or metadata.get("item_kind") != normalized
            or type(metadata.get("schema")) is not int
            or metadata["schema"] != NATIVE_ITEM_SCHEMA
            or attachment.get("content_type") != NATIVE_ITEM_MEDIA_TYPE
        ):
            raise ConfigurationError("The native PIM backup type or schema is invalid.")
        content = validate_native_item(
            attachment.get("content"), max_bytes=self.config.max_attachment_bytes
        )
        # Version checks and byte validation precede destination-folder creation.
        NativeItemStreams(
            self.account, max_bytes=self.config.max_attachment_bytes
        ).ensure_supported()
        return content

    def _restore_native_pim(self, folder: Any, kind: str, content: bytes) -> str:
        streams = NativeItemStreams(self.account, max_bytes=self.config.max_attachment_bytes)
        identity = self._call(
            "restore native PIM item", lambda: streams.upload_create(folder, content)
        )
        try:
            values = self._call(
                "verify native PIM restore",
                lambda: list(
                    islice(
                        self.account.fetch(
                            ids=[(identity.item_id, identity.change_key)],
                            only_fields=("item_class",),
                        ),
                        2,
                    )
                ),
            )
            expected = getattr(
                self._bindings(),
                {
                    "calendar": "CalendarItem",
                    "event": "CalendarItem",
                    "contact": "Contact",
                    "task": "Task",
                }[kind],
            )
            if (
                len(values) != 1
                or not isinstance(values[0], expected)
                or self._id(values[0]) != identity.item_id
                or not self._changekey(values[0])
            ):
                raise ConfigurationError(
                    "Native PIM restore did not return the expected item type and identity."
                )
            return self._remember("event" if kind == "calendar" else kind, values[0], parent=folder)
        except Exception:
            raise MutationResultUnavailableError(
                "Exchange created the native PIM item but its result could not be verified.",
                suggestion=(
                    "Inspect the dedicated restore folder before another attempt. "
                    "No automatic retry was made."
                ),
            ) from None

    # ------------------------------------------------------------------
    # Monitoring fallback

    _POLL_CURSOR_PREFIX = "gucmail-poll-v3:"
    _LEGACY_POLL_CURSOR_PREFIX = "gucmail-poll-v2:"

    @staticmethod
    def _monitor_datetime(value: str, *, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"The {label} monitoring checkpoint is invalid.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ConfigurationError(f"The {label} monitoring checkpoint has no UTC offset.")
        return parsed.astimezone(UTC)

    def _poll_cursor(
        self,
        since: str | None,
    ) -> tuple[datetime | None, int, datetime | None, str | None]:
        if since is None:
            return None, 0, None, None
        legacy = since.startswith(self._LEGACY_POLL_CURSOR_PREFIX)
        if not legacy and not since.startswith(self._POLL_CURSOR_PREFIX):
            threshold = self._monitor_datetime(since, label="mailbox")
            return threshold, 0, threshold, None
        try:
            prefix = self._LEGACY_POLL_CURSOR_PREFIX if legacy else self._POLL_CURSOR_PREFIX
            payload = json.loads(since[len(prefix) :])
            threshold_raw = payload["threshold"]
            offset = int(payload["offset"])
            newest_raw = payload.get("newest")
            anchor = payload.get("anchor")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError("The mailbox monitoring continuation is invalid.") from exc
        if not isinstance(threshold_raw, str) or offset < 1:
            raise ConfigurationError("The mailbox monitoring continuation is invalid.")
        threshold = self._monitor_datetime(threshold_raw, label="mailbox")
        # Older continuations lack a boundary identity. Restart from the original
        # watermark rather than trusting their offset; event IDs deduplicate repeats.
        if legacy:
            return threshold, 0, threshold, None
        if not isinstance(anchor, str) or not anchor:
            raise ConfigurationError("The mailbox monitoring continuation is invalid.")
        newest = (
            self._monitor_datetime(newest_raw, label="newest")
            if isinstance(newest_raw, str)
            else threshold
        )
        return threshold, offset, newest, anchor

    def _encode_poll_cursor(
        self,
        *,
        threshold: datetime,
        offset: int,
        newest: datetime,
        anchor: str,
    ) -> str:
        payload = json.dumps(
            {
                "anchor": anchor,
                "newest": newest.astimezone(UTC).isoformat(),
                "offset": offset,
                "threshold": threshold.astimezone(UTC).isoformat(),
                "version": 3,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return self._POLL_CURSOR_PREFIX + payload

    def poll_new_messages(self, folder: str, since: str | None, limit: int) -> dict[str, Any]:
        self._page(limit)
        threshold, offset, newest, anchor = self._poll_cursor(since)
        request_limit = min(limit + 1, MAX_PAGE_SIZE) if anchor is not None else limit
        page = self.list_messages(
            folder=folder,
            limit=request_limit,
            offset=offset - 1 if anchor is not None else 0,
            order_by="-datetime_received",
        )
        if anchor is not None:
            if not page or page[0].reference != anchor:
                raise ConfigurationError(
                    "The monitored folder changed while paging. "
                    "The checkpoint was not advanced; retry the poll."
                )
            messages = page[1:]
        else:
            messages = page

        def received_utc(item: MessageSummary) -> datetime | None:
            value = item.received_at
            if value is None:
                return None
            if value.tzinfo is None or value.utcoffset() is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

        new_items_list: list[MessageSummary] = []
        for item in messages:
            item_received = received_utc(item)
            # Deliberate overlap at the timestamp boundary prevents a
            # later-observed message with the same Exchange timestamp from being
            # skipped. Stable event IDs suppress repeated boundary items.
            if threshold is None or item_received is None or item_received >= threshold:
                new_items_list.append(item)
        new_items = tuple(new_items_list)
        received = [value for item in messages if (value := received_utc(item)) is not None]
        watermarks = [*received, *(value for value in (newest, threshold) if value is not None)]
        if watermarks:
            newest = max(watermarks)

        last_received = received_utc(messages[-1]) if messages else None
        more_available = bool(
            threshold is not None
            and len(page) == request_limit
            and (last_received is None or last_received >= threshold)
        )
        checkpoint: str | None
        if more_available:
            assert threshold is not None  # Narrowed by more_available.
            assert newest is not None  # The threshold is included in watermarks.
            checkpoint = self._encode_poll_cursor(
                threshold=threshold,
                offset=offset + len(messages),
                newest=newest,
                anchor=messages[-1].reference,
            )
        else:
            checkpoint = newest.astimezone(UTC).isoformat() if newest is not None else since
        return {
            "items": new_items,
            "checkpoint": checkpoint,
            "more_available": more_available,
        }

    def sync_folder_items(
        self, folder: str, sync_state: str | None = None, max_changes: int = 50
    ) -> Any:
        """Read one metadata-only change page for a mail or calendar folder.

        Initial synchronization is a quiet, resumable baseline. Its cursor is
        committed page by page by the monitoring service; no full-folder drain,
        body download, attachment retrieval or mailbox mutation occurs here.
        """
        from gucmail.ews.sync import SyncCursor, read_change_page, sync_scope
        from gucmail.services.monitoring import MonitorChange, PollBatch

        max_changes, _ = self._page(max_changes, 0)
        resolved = self._resolve_folder(folder)
        folder_class = str(getattr(resolved, "folder_class", "") or "IPF.Note")
        if folder_class not in {"IPF.Note", "IPF.Appointment"}:
            raise UnsupportedFeatureError(
                "Incremental monitoring supports mail and calendar folders."
            )
        calendar = folder_class == "IPF.Appointment"
        item_kind = "event" if calendar else "message"
        remote_folder_id = self._id(resolved)
        if not remote_folder_id:
            raise ConfigurationError("The monitoring folder has no Exchange identifier.")
        cursor = SyncCursor.read(
            sync_state,
            scope=sync_scope(
                self.config.email or "configured-mailbox", remote_folder_id, item_kind
            ),
        )
        fields = (
            ("subject", "organizer", "has_attachments", "importance")
            if calendar
            else SUMMARY_FIELDS
        )
        changes, next_cursor, more = self._call(
            "synchronize folder changes",
            lambda: read_change_page(
                self.account,
                resolved,
                fields=(*fields, "last_modified_time"),
                cursor=cursor,
                limit=max_changes,
            ),
            read_only=True,
        )
        path = self._folder_path(resolved)
        mapped: list[MonitorChange] = []
        kinds = {
            "create": "calendar_created" if calendar else "new_message",
            "update": "calendar_updated" if calendar else "changed_message",
            "delete": "calendar_deleted" if calendar else "deleted_message",
            "read_flag_change": "changed_message",
        }
        for index, (change_type, value) in enumerate(changes):
            if change_type not in kinds:
                raise ConfigurationError("Exchange returned an unknown synchronization change.")
            item = value[0] if change_type == "read_flag_change" else value
            reference = make_local_reference(item_kind, self._id(item) or "")
            if change_type in {"create", "update"}:
                reference = self._remember(item_kind, item, parent=resolved, folder_path=path)
            elif self.state is not None:
                stored = self.state.resolve_reference(reference, item_kind)
                if stored is not None:
                    reference = str(stored.get("canonical_ref") or stored["local_ref"])
            else:
                with self._reference_lock:
                    reference = next(
                        (
                            ref
                            for (kind, ref), record in self._memory_references.items()
                            if kind == item_kind and record["ews_id"] == self._id(item)
                        ),
                        reference,
                    )
            if change_type in {"delete", "read_flag_change"}:
                with self._reference_lock:
                    self._objects.pop((item_kind, reference), None)
            summary = (
                map_message_summary(item, reference=reference, folder_path=path)
                if change_type in {"create", "update"}
                else None
            )
            if calendar and summary:
                organizers = map_addresses([getattr(item, "organizer", None)])
                summary = replace(summary, sender=organizers[0] if organizers else None)
            # The input cursor and position make replays of a page idempotent,
            # while later changes to the same item retain their own identity.
            identity = "\0".join(
                (cursor.scope, cursor.token or "", str(index), change_type, self._id(item) or "")
            )
            event_id = (
                None
                if change_type == "create"
                else "ews-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            )
            command = "calendar show" if calendar else "messages show"
            mapped.append(
                MonitorChange(
                    kinds[change_type],
                    message=summary,
                    reference=reference,
                    occurred_at=domain_datetime(getattr(item, "last_modified_time", None)),
                    folder=path,
                    event_id=event_id,
                    open_hint=(
                        None if change_type == "delete" else f"gucmail {command} {reference}"
                    ),
                )
            )
        return PollBatch(tuple(mapped), next_cursor.encode(), more, baseline=cursor.baseline)

    # ------------------------------------------------------------------
    # Backup source

    @property
    def backup_source_identity(self) -> str:
        """Bind archive chains to the actual mailbox without storing its address."""
        address = str(self.account.primary_smtp_address).strip().casefold()
        endpoint = self.config.endpoint.rstrip("/").casefold()
        return "ews:" + hashlib.sha256(f"{endpoint}\n{address}".encode()).hexdigest()

    def _backup_folders(self, roots: tuple[str, ...]) -> list[FolderInfo]:
        """Resolve a complete, bounded folder snapshot or fail rather than truncate."""

        if roots:
            if len(roots) > 10_000:
                raise ConfigurationError("A backup may select at most 10000 folder roots.")
            return [self.get_folder(value) for value in roots]
        folders: list[FolderInfo] = []
        queue: list[str | None] = [None]
        seen: set[str] = set()
        while queue:
            if len(folders) >= 10_000:
                raise ConfigurationError(
                    "Mailbox folder discovery exceeded the 10000-folder safety bound; "
                    "select a narrower backup scope."
                )
            parent = queue.pop(0)
            offset = 0
            while True:
                page = self.list_folders(parent, limit=MAX_PAGE_SIZE, offset=offset)
                for folder in page:
                    if folder.reference in seen:
                        continue
                    seen.add(folder.reference)
                    folders.append(folder)
                    if len(folders) > 10_000:
                        raise ConfigurationError(
                            "Mailbox folder discovery exceeded the 10000-folder safety bound; "
                            "select a narrower backup scope."
                        )
                    if folder.child_count is None or folder.child_count > 0:
                        queue.append(folder.reference)
                if len(page) < MAX_PAGE_SIZE:
                    break
                offset += len(page)
        return folders

    def estimate_backup(self, request: Any) -> Any:
        """Estimate from body-free metadata projections only.

        This method deliberately never calls ``show_message``, ``raw_message``,
        ``_backup_message``, or ``open_attachment``. A message with attachments adds
        one lower-bound attachment count and one unknown-size marker because summary
        metadata cannot prove the exact attachment count or payload sizes.
        """

        from gucmail.services.backup import BackupEstimate

        selection = request.selection
        kinds = {value.casefold() for value in selection.item_kinds}
        item_count = 0
        attachment_count = 0
        estimated_bytes = 0
        unknown_size_count = 0

        def wanted(kind: str) -> bool:
            return not kinds or kind.casefold() in kinds

        def excluded(kind: str, reference: str, folder_path: str | None) -> bool:
            import fnmatch

            values = (kind, reference, folder_path or "")
            return any(
                fnmatch.fnmatchcase(value, pattern)
                for pattern in request.exclusions
                for value in values
            )

        def add_metadata(value: Any, *, declared_size: int | None = None) -> None:
            nonlocal item_count, estimated_bytes, unknown_size_count
            item_count += 1
            if declared_size is None:
                encoded = json.dumps(
                    to_primitive(value),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                estimated_bytes += len(encoded)
            else:
                estimated_bytes += max(0, int(declared_size))

        def add_message(summary: MessageSummary) -> None:
            nonlocal attachment_count, unknown_size_count
            if excluded("message", summary.reference, summary.folder_path):
                return
            add_metadata(summary, declared_size=summary.size)
            if summary.size is None:
                unknown_size_count += 1
            if summary.has_attachments:
                attachment_count += 1
                unknown_size_count += 1

        if selection.messages:
            if wanted("message"):
                for reference in selection.messages:
                    item = self._fetch_item(reference, ("message", "draft"), SUMMARY_FIELDS)
                    parent = self._parents.get(("message", reference)) or getattr(
                        item, "folder", None
                    )
                    path = self._folder_path(parent) if parent is not None else None
                    add_message(map_message_summary(item, reference=reference, folder_path=path))
            return BackupEstimate(
                item_count,
                attachment_count,
                estimated_bytes,
                unknown_size_count,
                False,
                note=(
                    "Metadata-only estimate; attachment counts are lower bounds and no "
                    "message body, MIME content, or attachment payload was retrieved."
                ),
            )

        roots = tuple(selection.folders)
        folders = self._backup_folders(roots)
        for folder in folders:
            if wanted("folder") and not excluded("folder", folder.reference, folder.path):
                add_metadata(folder)
            folder_class = (folder.folder_class or "IPF.Note").casefold()
            if not wanted("message") or not folder_class.startswith("ipf.note"):
                continue
            offset = 0
            while True:
                summaries = self.list_messages(
                    folder.reference,
                    limit=MAX_PAGE_SIZE,
                    offset=offset,
                    order_by="datetime_received",
                )
                for summary in summaries:
                    add_message(summary)
                if len(summaries) < MAX_PAGE_SIZE:
                    break
                offset += len(summaries)

        for kind, value, path in self._iter_backup_pim(folders, wanted, metadata_only=True):
            if not excluded(kind, value.reference, path):
                add_metadata(value)

        # Canonical record/descriptors and optional AEAD overhead are applied by the
        # service for iterable sources. Provider estimates include a conservative
        # record allowance here; encryption overhead is added by BackupService.
        estimated_bytes += item_count * 512 + attachment_count * 256
        return BackupEstimate(
            item_count,
            attachment_count,
            estimated_bytes,
            unknown_size_count,
            False,
            note=(
                "Metadata-only estimate; attachment counts are lower bounds and no "
                "message body, MIME content, or attachment payload was retrieved."
            ),
        )

    def _backup_message(self, reference: str, *, allow_missing: bool = False) -> Any:
        from gucmail.services.backup import BackupAttachment, BackupItem

        if allow_missing:
            # A missing local reference map is not evidence of a server deletion.
            self._resolve_item(reference, "message", "draft")
        try:
            detail = self.show_message(reference)
        except ItemNotFoundError:
            if allow_missing:
                return None
            raise
        attachments: list[BackupAttachment] = [
            BackupAttachment(
                original_filename="original-message.eml",
                data=self.raw_message(reference),
                content_type="message/rfc822",
                metadata={"role": "message_mime"},
            )
        ]
        for metadata in detail.attachments:
            with self.open_attachment(reference, metadata.reference) as stream:
                data = stream.read()
            attachments.append(
                BackupAttachment(
                    original_filename=metadata.name,
                    data=data,
                    content_type=metadata.content_type or "application/octet-stream",
                    metadata={
                        "reference": metadata.reference,
                        "inline": metadata.is_inline,
                        "content_id": metadata.content_id,
                    },
                )
            )
        return BackupItem(
            reference=reference,
            kind="message",
            data=to_primitive(detail),
            folder_path=detail.summary.folder_path,
            attachments=tuple(attachments),
            metadata={"mail_format": "mime-and-selected-ews-fields"},
        )

    def _backup_pim_attachments(self, reference: str, kind: str) -> tuple[Any, ...]:
        from exchangelib.attachments import ItemAttachment

        from gucmail.services.backup import BackupAttachment

        item = self._fetch_item(
            reference, ("event" if kind == "calendar" else kind,), ("attachments",)
        )
        attachments = tuple(getattr(item, "attachments", ()) or ())
        if any(isinstance(attachment, ItemAttachment) for attachment in attachments):
            # Export the containing item intact. Never dereference ItemAttachment.item,
            # recursively materialize embedded objects, or flatten them to files.
            native = self._fetch_item(
                reference, ("event" if kind == "calendar" else kind,), ("size",)
            )
            expected = getattr(
                self._bindings(),
                {"calendar": "CalendarItem", "contact": "Contact", "task": "Task"}[kind],
            )
            if not isinstance(native, expected) or self._id(native) != self._id(item):
                raise ConfigurationError("Native backup did not retrieve the requested PIM item.")
            streams = NativeItemStreams(self.account, max_bytes=self.config.max_attachment_bytes)
            content = self._call(
                "export native PIM item", lambda: streams.export_item(native), read_only=True
            )
            return (
                BackupAttachment(
                    original_filename="exchange-item.bin",
                    data=content,
                    content_type=NATIVE_ITEM_MEDIA_TYPE,
                    metadata={
                        "role": NATIVE_ITEM_ROLE,
                        "schema": NATIVE_ITEM_SCHEMA,
                        "item_kind": kind,
                    },
                ),
            )
        result = []
        for attachment in attachments:
            stream = getattr(attachment, "fp", None)
            if stream is None:
                raise UnsupportedFeatureError(
                    "A PIM item attachment cannot be exported as a file; this backup is incomplete."
                )
            with _AttachmentStream(stream, self.config.max_attachment_bytes) as source:
                content = source.read()
            result.append(
                BackupAttachment(
                    original_filename=str(getattr(attachment, "name", None) or "attachment"),
                    data=content,
                    content_type=str(
                        getattr(attachment, "content_type", None) or "application/octet-stream"
                    ),
                    metadata={
                        "inline": bool(getattr(attachment, "is_inline", False)),
                        "content_id": getattr(attachment, "content_id", None),
                        "is_contact_photo": bool(getattr(attachment, "is_contact_photo", False)),
                    },
                )
            )
        return tuple(result)

    def iter_backup_items(self, request: Any, completed: frozenset[str]) -> Iterator[Any]:
        """Yield a restartable mailbox snapshot for :class:`BackupService`.

        Exchange does not offer a globally atomic snapshot. The backup manifest records that
        limitation, while content-addressing and ``completed`` keys make local resumption safe.
        """

        from gucmail.services.backup import BackupItem, BackupService

        selection = request.selection
        kinds = {value.casefold() for value in selection.item_kinds}

        def wanted(kind: str) -> bool:
            return not kinds or kind.casefold() in kinds

        def pending(kind: str, reference: str) -> bool:
            return BackupService.item_key(kind, reference) not in completed

        if selection.messages:
            if wanted("message"):
                for reference in selection.messages:
                    if pending("message", reference):
                        item = self._backup_message(
                            reference,
                            allow_missing=request.sync_state.get("strategy")
                            == "full_snapshot_parent_diff",
                        )
                        if item is not None:
                            yield item
            return

        roots = tuple(selection.folders)
        folders = self._backup_folders(roots)

        for folder in folders:
            if wanted("folder") and pending("folder", folder.reference):
                yield BackupItem(
                    reference=folder.reference,
                    kind="folder",
                    data=to_primitive(folder),
                    folder_path=folder.path,
                )
            folder_class = (folder.folder_class or "IPF.Note").casefold()
            if not wanted("message") or not folder_class.startswith("ipf.note"):
                continue
            offset = 0
            while True:
                summaries = self.list_messages(
                    folder.reference,
                    limit=MAX_PAGE_SIZE,
                    offset=offset,
                    order_by="datetime_received",
                )
                for summary in summaries:
                    if pending("message", summary.reference):
                        yield self._backup_message(summary.reference)
                if len(summaries) < MAX_PAGE_SIZE:
                    break
                offset += len(summaries)

        for kind, value, path in self._iter_backup_pim(folders, wanted, metadata_only=False):
            if pending(kind, value.reference):
                yield BackupItem(
                    value.reference,
                    kind,
                    to_primitive(value),
                    folder_path=path,
                    attachments=self._backup_pim_attachments(value.reference, kind),
                )

    def _iter_backup_pim(
        self,
        folders: list[FolderInfo],
        wanted: Callable[[str], bool],
        *,
        metadata_only: bool,
    ) -> Iterator[tuple[str, Any, str]]:
        """Use the same actual folder scope for content exports and body-free estimates."""
        seen_folders: set[str] = set()
        for folder in folders:
            if folder.reference in seen_folders:
                continue
            seen_folders.add(folder.reference)
            folder_class = (folder.folder_class or "").casefold()
            if folder_class.startswith("ipf.appointment"):
                kind = "calendar"
            elif folder_class.startswith("ipf.contact"):
                kind = "contact"
            elif folder_class.startswith("ipf.task"):
                kind = "task"
            else:
                continue
            if not wanted(kind):
                continue
            if kind == "calendar":
                # FindItem returns finite stored items / recurring masters. A
                # CalendarView expands occurrences and silently imposes a date
                # horizon, neither of which is suitable for a full archive.
                provider_folder = self._calendar_folder(folder.reference)
                offset = 0
                while True:
                    items = self._bounded_items(
                        provider_folder,
                        fields=CALENDAR_METADATA_FIELDS if metadata_only else CALENDAR_FIELDS,
                        limit=MAX_PAGE_SIZE,
                        offset=offset,
                    )
                    for item in items:
                        reference = self._remember("event", item, parent=provider_folder)
                        yield kind, self._map_calendar_item(item, reference=reference), folder.path
                    if len(items) < MAX_PAGE_SIZE:
                        break
                    offset += len(items)
                continue
            listing = self.list_contacts if kind == "contact" else self.list_tasks
            offset = 0
            while True:
                values = listing(
                    folder_reference=folder.reference,
                    limit=MAX_PAGE_SIZE,
                    offset=offset,
                    metadata_only=metadata_only,
                )
                for value in values:
                    yield kind, value, folder.path
                if len(values) < MAX_PAGE_SIZE:
                    break
                offset += len(values)


# The composition root and public documentation use the more explicit Adapter name.
EWSAdapter = EWSGateway


__all__ = [
    "AttachmentDownload",
    "EWSAdapter",
    "EWSGateway",
    "translate_ews_error",
]
