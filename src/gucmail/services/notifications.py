"""Provider-neutral, durable notification dispatch.

Providers receive domain events, not Exchange objects.  The dispatcher isolates provider
failures and persists provider-specific outcomes so a retried monitoring batch does not
repeat notifications that were already delivered.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO, cast

from gucmail.constants import APP_NAME
from gucmail.models import NotificationEvent, to_primitive
from gucmail.security import redact_text, sanitize_terminal
from gucmail.state import StateStore, iso

if TYPE_CHECKING:
    from gucmail.notification_providers.windows import WindowsToastProvider

EVENT_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "account_id",
        "folder",
        "message_reference",
        "sender",
        "subject",
        "importance",
        "has_attachments",
        "preview",
        "open_hint",
    }
)


def notification_event_schema() -> dict[str, Any]:
    """Return the stable, copyable JSON Schema for outbound notification events."""

    nullable_string = {"type": ["string", "null"]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://gucmail.local/schemas/notification-event-v1.json",
        "title": "GUC Mail notification event",
        "type": "object",
        "additionalProperties": False,
        "required": ["event_id", "event_type", "occurred_at", "account_id"],
        "properties": {
            "event_id": {"type": "string", "minLength": 1},
            "event_type": {"type": "string", "minLength": 1},
            "occurred_at": {"type": "string", "format": "date-time"},
            "account_id": {"type": "string", "minLength": 1},
            "folder": nullable_string,
            "message_reference": nullable_string,
            "sender": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "email": {"type": "string"},
                    "name": nullable_string,
                    "mailbox_type": nullable_string,
                },
                "required": ["email"],
            },
            "subject": nullable_string,
            "importance": {"enum": ["low", "normal", "high"]},
            "has_attachments": {"type": "boolean"},
            "preview": nullable_string,
            "open_hint": nullable_string,
        },
    }


class DeliveryState(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"
    DEFERRED = "deferred"
    SKIPPED = "skipped"
    DISABLED = "disabled"


class _ClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    UNTRACKED = "untracked"
    ALREADY_DELIVERED = "already_delivered"
    IN_PROGRESS = "in_progress"


_CLAIM_LEASE_SECONDS = 900
_HISTORY_RETENTION = 10_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """A secret-safe outcome returned by every notification provider."""

    event_id: str
    provider: str
    state: DeliveryState
    detail: str | None = None
    attempts: int = 1
    status_code: int | None = None
    retry_after: str | float | int | None = None

    @property
    def succeeded(self) -> bool:
        return self.state in {
            DeliveryState.DELIVERED,
            DeliveryState.SKIPPED,
            DeliveryState.DISABLED,
        }


class NotificationProvider(Protocol):
    """Small plugin boundary implemented by local and outbound providers."""

    @property
    def name(self) -> str: ...

    def notify(self, event: NotificationEvent) -> DeliveryResult: ...


NotificationEventFilter = Callable[[NotificationEvent], bool]


def event_preference_result(
    event: NotificationEvent, event_allowed: NotificationEventFilter | None
) -> DeliveryResult | None:
    """Check current preferences before disclosing an event to any provider."""
    if event_allowed is None:
        return None
    try:
        allowed = event_allowed(event)
        if type(allowed) is not bool:
            raise TypeError("Notification preference must return a boolean.")
    except Exception as exc:
        return DeliveryResult(
            event.event_id,
            "notification-preferences",
            DeliveryState.FAILED,
            f"Notification preferences are unavailable ({type(exc).__name__}).",
            attempts=0,
        )
    if allowed:
        return None
    return DeliveryResult(
        event.event_id,
        "notification-preferences",
        DeliveryState.SKIPPED,
        "Event delivery is disabled by current account preferences.",
        attempts=0,
    )


def _safe_value(value: Any, *, redact: bool) -> Any:
    if isinstance(value, str):
        safe = sanitize_terminal(value, max_length=4096)
        return redact_text(safe, redact_emails=redact) if redact else safe
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item, redact=redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item, redact=redact) for item in value]
    return value


def event_payload(
    event: NotificationEvent,
    *,
    field_allowlist: frozenset[str] | set[str] | None = None,
    redact: bool = True,
) -> dict[str, Any]:
    """Create the stable JSON event representation after allowlisting and sanitization."""

    requested = EVENT_FIELDS if field_allowlist is None else frozenset(field_allowlist)
    # Unknown names are ignored rather than copied from future model fields.  This makes an
    # old allowlist fail closed when the event schema grows.
    allowed = requested & EVENT_FIELDS
    primitive = cast(dict[str, Any], to_primitive(event))
    payload = {key: primitive[key] for key in EVENT_FIELDS if key in allowed}
    return cast(dict[str, Any], _safe_value(payload, redact=redact))


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """Optional event filtering and disclosure policy for one provider."""

    event_types: frozenset[str] = field(default_factory=frozenset)
    sender_domains: frozenset[str] = field(default_factory=frozenset)
    subject_contains: tuple[str, ...] = ()

    def matches(self, event: NotificationEvent) -> bool:
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.sender_domains:
            if event.sender is None or "@" not in event.sender.email:
                return False
            domain = event.sender.email.rsplit("@", 1)[1].casefold()
            if domain not in {item.casefold().lstrip("@") for item in self.sender_domains}:
                return False
        if self.subject_contains:
            subject = (event.subject or "").casefold()
            if not any(needle.casefold() in subject for needle in self.subject_contains):
                return False
        return True


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    provider: NotificationProvider
    policy: NotificationPolicy = field(default_factory=NotificationPolicy)


class ConsoleNotificationProvider:
    """Write compact, terminal-safe human notifications."""

    name = "console"

    def __init__(self, stream: TextIO | None = None, *, redact: bool = True) -> None:
        self._stream = stream or sys.stdout
        self._redact = redact

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        sender = event.sender.name if event.sender and event.sender.name else None
        if sender is None and event.sender:
            sender = event.sender.email
        parts = [f"[{sanitize_terminal(event.event_type, max_length=40)}]"]
        if sender:
            safe_sender = sanitize_terminal(sender, max_length=200)
            parts.append(redact_text(safe_sender, redact_emails=self._redact))
        if event.subject:
            safe_subject = sanitize_terminal(event.subject, max_length=500)
            parts.append(redact_text(safe_subject, redact_emails=self._redact))
        if event.folder:
            parts.append(f"({sanitize_terminal(event.folder, max_length=100)})")
        self._stream.write(" — ".join(parts) + "\n")
        self._stream.flush()
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


class NDJSONNotificationProvider:
    """Append one sanitized JSON object per event for pipes or local automation."""

    name = "ndjson"

    def __init__(
        self,
        destination: TextIO | str | Path | None = None,
        *,
        field_allowlist: frozenset[str] | set[str] | None = None,
        redact: bool = True,
    ) -> None:
        self._stream = destination if hasattr(destination, "write") else None
        self._path = Path(destination) if isinstance(destination, (str, Path)) else None
        self._stream = self._stream or (sys.stdout if destination is None else None)
        self._allowlist = None if field_allowlist is None else frozenset(field_allowlist)
        self._redact = redact
        self._lock = threading.Lock()

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        payload = event_payload(
            event,
            field_allowlist=self._allowlist,
            redact=self._redact,
        )
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            else:
                stream = cast(TextIO, self._stream)
                stream.write(line + "\n")
                stream.flush()
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


DesktopSender = Callable[[str, str], None]


class DesktopNotificationProvider:
    """Shared optional desktop delivery, with native Windows capability reporting."""

    name = "desktop"

    def __init__(
        self,
        sender: DesktopSender | None = None,
        *,
        redact: bool = True,
        windows_provider: WindowsToastProvider | None = None,
        allow_sound: bool = True,
    ) -> None:
        self._sender = sender
        self._redact = redact
        self._windows_provider = windows_provider
        self._allow_sound = allow_sound

    def backend_status(self) -> Any:
        from gucmail.notification_providers.windows import WindowsBackendStatus

        if self._sender is not None:
            return WindowsBackendStatus(
                True,
                "injected",
                False,
                False,
                "Injected sender; native desktop behavior is unverified.",
            )
        if sys.platform == "win32":
            return self._windows_adapter().status
        available = self._optional_sender() is not None
        return WindowsBackendStatus(
            available,
            "plyer" if available else None,
            False,
            False,
            "Optional cross-platform desktop facade; visible delivery is unverified.",
        )

    def _windows_adapter(self) -> WindowsToastProvider:
        if self._windows_provider is None:
            from gucmail.notification_providers.core import PrivacyMode
            from gucmail.notification_providers.windows import (
                WindowsToastProvider,
                WindowsToastSettings,
            )

            self._windows_provider = WindowsToastProvider(
                settings=WindowsToastSettings(privacy=PrivacyMode.FULL)
            )
        return self._windows_provider

    @staticmethod
    def _optional_sender() -> DesktopSender | None:
        try:
            from plyer import notification  # type: ignore[import-not-found]
        except (ImportError, RuntimeError):
            return None

        def send(title: str, message: str) -> None:
            notification.notify(
                title=title,
                message=message,
                app_name=APP_NAME,
                timeout=8,
            )

        return send

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        if self._sender is None and sys.platform == "win32":
            private_event = event
            if self._redact:
                from gucmail.models import Address

                private_sender = (
                    Address(redact_text(event.sender.email), redact_text(event.sender.name or ""))
                    if event.sender is not None
                    else None
                )
                private_event = replace(
                    event,
                    sender=private_sender,
                    subject=redact_text(event.subject or ""),
                    preview=redact_text(event.preview or ""),
                )
            outcome = self._windows_adapter().notify(private_event)
            return replace(outcome, provider=self.name)
        if not self._allow_sound:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                "The portable desktop backend cannot guarantee silent delivery. "
                "The event remains available in notification history.",
                attempts=0,
            )
        sender = self._sender or self._optional_sender()
        if sender is None:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                "Desktop notifications are unavailable; install the desktop extra.",
            )
        title = sanitize_terminal(event.subject or "New mail", max_length=120)
        message_parts = []
        if event.sender:
            message_parts.append(event.sender.name or event.sender.email)
        if event.preview:
            message_parts.append(event.preview)
        message = " — ".join(message_parts) or "A mailbox event occurred."
        message = sanitize_terminal(message, max_length=300)
        if self._redact:
            title = redact_text(title)
            message = redact_text(message)
        sender(title, message)
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class CommandHookProvider:
    """Explicitly enabled local command hook; it never invokes a command shell."""

    name = "command"

    def __init__(
        self,
        command: Sequence[str],
        *,
        enabled: bool = False,
        timeout_seconds: float = 15.0,
        field_allowlist: frozenset[str] | set[str] | None = None,
        redact: bool = True,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty sequence of arguments")
        if any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in command
        ):
            raise ValueError("command arguments must be non-empty strings without NUL bytes")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("command hook timeout must be between 0 and 300 seconds")
        self._command = tuple(command)
        self._enabled = enabled
        self._timeout = timeout_seconds
        self._allowlist = None if field_allowlist is None else frozenset(field_allowlist)
        self._redact = redact
        self._runner = runner

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        if not self._enabled:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.DISABLED,
                "Command hooks require explicit configuration.",
            )
        payload = json.dumps(
            event_payload(event, field_allowlist=self._allowlist, redact=self._redact),
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            completed = self._runner(
                list(self._command),
                input=payload,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                "Command hook timed out.",
            )
        if completed.returncode != 0:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                f"Command hook exited with status {completed.returncode}.",
            )
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


class NotificationDispatcher:
    """Fan events out independently and remember successful provider deliveries."""

    def __init__(
        self,
        providers: Sequence[NotificationProvider | ProviderBinding],
        *,
        state: StateStore | None = None,
        deduplicate: bool = True,
        event_allowed: NotificationEventFilter | None = None,
    ) -> None:
        self._bindings = tuple(
            item if isinstance(item, ProviderBinding) else ProviderBinding(item)
            for item in providers
        )
        self._state = state
        self._deduplicate = deduplicate
        self._event_allowed = event_allowed

    @property
    def providers(self) -> tuple[NotificationProvider, ...]:
        return tuple(binding.provider for binding in self._bindings)

    def history(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        """Return bounded, already-sanitized local delivery history."""

        if self._state is None:
            return ()
        if not 1 <= limit <= 500:
            raise ValueError("notification history limit must be between 1 and 500")
        self._state.initialize()
        with self._state.connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, provider, state, detail, attempts, status_code, created_at
                FROM notification_history ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["event_id"] = sanitize_terminal(item["event_id"], max_length=500)
            item["provider"] = sanitize_terminal(item["provider"], max_length=200)
            item["state"] = sanitize_terminal(item["state"], max_length=100)
            item["detail"] = (
                redact_text(item["detail"], redact_emails=True, redact_urls=True)[:1000]
                if item["detail"] is not None
                else None
            )
            history.append(item)
        return tuple(history)

    def _claim_delivery(self, event_id: str, provider: str) -> _ClaimOutcome:
        if self._state is None or not self._deduplicate:
            return _ClaimOutcome.UNTRACKED
        now = datetime.now(UTC)
        with self._state.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT state, lease_until FROM notification_delivery_claims
                WHERE event_id=? AND provider=?
                """,
                (event_id, provider),
            ).fetchone()
            if row is not None and row["state"] == DeliveryState.DELIVERED.value:
                return _ClaimOutcome.ALREADY_DELIVERED
            if row is not None and row["state"] == _ClaimOutcome.IN_PROGRESS.value:
                try:
                    lease_until = datetime.fromisoformat(str(row["lease_until"]))
                except (TypeError, ValueError):
                    lease_until = None
                if (
                    lease_until is not None
                    and lease_until.tzinfo is not None
                    and lease_until.astimezone(UTC) > now
                ):
                    return _ClaimOutcome.IN_PROGRESS
            connection.execute(
                """
                INSERT INTO notification_delivery_claims(
                    event_id, provider, state, lease_until, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id, provider) DO UPDATE SET
                    state=excluded.state,
                    lease_until=excluded.lease_until,
                    updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    provider,
                    _ClaimOutcome.IN_PROGRESS.value,
                    iso(now + timedelta(seconds=_CLAIM_LEASE_SECONDS)),
                    iso(now),
                ),
            )
        return _ClaimOutcome.CLAIMED

    def _record(self, result: DeliveryResult, *, complete_claim: bool = False) -> None:
        if self._state is None:
            return
        detail = (
            redact_text(result.detail, redact_emails=True, redact_urls=True)[:1000]
            if result.detail is not None
            else None
        )
        with self._state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO notification_history(
                    event_id, provider, state, detail, attempts, status_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sanitize_terminal(result.event_id, max_length=500),
                    sanitize_terminal(result.provider, max_length=200),
                    result.state.value,
                    detail,
                    max(0, int(result.attempts)),
                    result.status_code,
                    iso(),
                ),
            )
            if complete_claim:
                if result.state is DeliveryState.DELIVERED:
                    connection.execute(
                        """
                        UPDATE notification_delivery_claims
                        SET state=?, lease_until=NULL, updated_at=?
                        WHERE event_id=? AND provider=?
                        """,
                        (
                            DeliveryState.DELIVERED.value,
                            iso(),
                            result.event_id,
                            result.provider,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM notification_delivery_claims
                        WHERE event_id=? AND provider=?
                        """,
                        (result.event_id, result.provider),
                    )
            connection.execute(
                """
                DELETE FROM notification_history
                WHERE id <= COALESCE(
                    (
                        SELECT id FROM notification_history
                        ORDER BY id DESC LIMIT 1 OFFSET ?
                    ),
                    0
                )
                """,
                (_HISTORY_RETENTION,),
            )

    def _record_safely(self, result: DeliveryResult, *, complete_claim: bool = False) -> None:
        """Keep a history/database failure isolated from later providers."""

        try:
            self._record(result, complete_claim=complete_claim)
        except Exception as exc:
            logger.warning(
                "Notification delivery history unavailable (%s).",
                type(exc).__name__,
            )

    @staticmethod
    def _provider_name(provider: NotificationProvider, index: int) -> str:
        try:
            raw = str(provider.name)
        except Exception:
            return f"invalid-provider-{index + 1}"
        return sanitize_terminal(raw, max_length=200) or f"provider-{index + 1}"

    def dispatch(self, event: NotificationEvent) -> tuple[DeliveryResult, ...]:
        preference = event_preference_result(event, self._event_allowed)
        if preference is not None:
            self._record_safely(preference)
            return (preference,)
        results: list[DeliveryResult] = []
        name_counts: dict[str, int] = {}
        for index, binding in enumerate(self._bindings):
            provider = binding.provider
            base_name = self._provider_name(provider, index)
            name_counts[base_name] = name_counts.get(base_name, 0) + 1
            provider_name = (
                base_name
                if name_counts[base_name] == 1
                else f"{base_name}#{name_counts[base_name]}"
            )
            try:
                matches = binding.policy.matches(event)
            except Exception as exc:
                result = DeliveryResult(
                    event.event_id,
                    provider_name,
                    DeliveryState.FAILED,
                    f"Notification policy is invalid ({type(exc).__name__}).",
                )
                self._record_safely(result)
                results.append(result)
                continue
            if not matches:
                result = DeliveryResult(
                    event.event_id,
                    provider_name,
                    DeliveryState.SKIPPED,
                    "Event did not match this provider's policy.",
                )
                self._record_safely(result)
                results.append(result)
                continue
            preference = event_preference_result(event, self._event_allowed)
            if preference is not None:
                self._record_safely(preference)
                results.append(preference)
                break
            try:
                claim = self._claim_delivery(event.event_id, provider_name)
            except Exception as exc:
                # Fail closed when deduplication state is unavailable. Calling an
                # external provider without a claim could duplicate private data.
                result = DeliveryResult(
                    event.event_id,
                    provider_name,
                    DeliveryState.FAILED,
                    f"Delivery state is unavailable ({type(exc).__name__}).",
                    attempts=0,
                )
                self._record_safely(result)
                results.append(result)
                continue
            if claim in {_ClaimOutcome.ALREADY_DELIVERED, _ClaimOutcome.IN_PROGRESS}:
                detail = (
                    "Already delivered."
                    if claim is _ClaimOutcome.ALREADY_DELIVERED
                    else "Delivery already in progress."
                )
                result = DeliveryResult(
                    event.event_id,
                    provider_name,
                    (
                        DeliveryState.SKIPPED
                        if claim is _ClaimOutcome.ALREADY_DELIVERED
                        else DeliveryState.DEFERRED
                    ),
                    detail,
                    attempts=0,
                )
                self._record_safely(result)
                results.append(result)
                continue
            try:
                result = provider.notify(event)
                if result.event_id != event.event_id or result.provider != provider_name:
                    result = replace(result, event_id=event.event_id, provider=provider_name)
            except Exception as exc:  # A broken plugin must not block other providers.
                result = DeliveryResult(
                    event.event_id,
                    provider_name,
                    DeliveryState.FAILED,
                    f"Provider failed ({type(exc).__name__}).",
                )
            if result.detail is not None:
                result = replace(
                    result,
                    detail=redact_text(
                        result.detail,
                        redact_emails=True,
                        redact_urls=True,
                    )[:1000],
                )
            self._record_safely(result, complete_claim=claim is _ClaimOutcome.CLAIMED)
            results.append(result)
        return tuple(results)


__all__ = [
    "EVENT_FIELDS",
    "CommandHookProvider",
    "ConsoleNotificationProvider",
    "DeliveryResult",
    "DeliveryState",
    "DesktopNotificationProvider",
    "NDJSONNotificationProvider",
    "NotificationDispatcher",
    "NotificationEventFilter",
    "NotificationPolicy",
    "NotificationProvider",
    "ProviderBinding",
    "event_payload",
    "event_preference_result",
    "notification_event_schema",
]
