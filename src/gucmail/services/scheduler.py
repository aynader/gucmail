"""Durable, at-most-once-biased local scheduled sending.

Exchange installations do not consistently provide a dependable deferred-send
operation.  Jobs here are therefore persisted locally, claimed transactionally,
and passed to an injected sender.  The sender is the only network boundary.
After that boundary is crossed, an unclassified failure is treated as an
ambiguous outcome and is never retried automatically.
"""

from __future__ import annotations

import builtins
import inspect
import json
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from enum import StrEnum
from typing import Any, Protocol

from gucmail.constants import DEFAULT_MAX_ATTACHMENT_BYTES
from gucmail.dates import backoff_seconds, next_working_time, parse_datetime_expression
from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ErrorContext,
    GUCmailError,
    ItemNotFoundError,
    LocalStateError,
    NetworkError,
    SendOutcomeUncertainError,
    ThrottledError,
)
from gucmail.models import DraftData, DraftLocation, ScheduledMessage, SendState
from gucmail.security import redact_text
from gucmail.services.compose import ComposeService
from gucmail.services.drafts import (
    AttachmentIntegrityError,
    DraftService,
    build_attachment_manifest,
    draft_content_hash,
    validate_attachment_manifest,
)
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.state import ClaimedJob, StateStore, iso, utc_now


class SendCallable(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> object: ...


class DefiniteSendFailure(GUCmailError):
    """The sender knows Exchange did not accept the message."""


class RetryableSendFailure(NetworkError):
    """A failure known to have happened before the request crossed the wire."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, context=ErrorContext(retry_safe=True))
        self.retry_after = retry_after


class JobAction(StrEnum):
    SENT = "sent"
    RETRY = "retry_scheduled"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review_required"
    SKIPPED = "skipped"


class SendCertainty(StrEnum):
    """What the sender can prove about Exchange acceptance."""

    CONFIRMED_SENT = "confirmed_sent"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    UNCERTAIN = "uncertain"


class ClockEvent(StrEnum):
    NORMAL = "normal"
    FIRST_OBSERVATION = "first_observation"
    SLEEP_OR_RESUME = "sleep_or_resume"
    CLOCK_MOVED_BACKWARD = "clock_moved_backward"


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome_reference: str | None = None
    certainty: SendCertainty = SendCertainty.CONFIRMED_SENT
    detail: str | None = None
    idempotency_marker: str | None = None


# New code should use the explicit name.  The alias preserves the first-pass API.
SendOutcome = SendResult


class SendReconciler(Protocol):
    def reconcile_send(
        self,
        *,
        idempotency_key: str,
        draft: DraftData,
        scheduled_for: datetime,
    ) -> SendOutcome: ...


@dataclass(frozen=True, slots=True)
class JobExecution:
    reference: str
    action: JobAction
    state: SendState
    detail: str | None = None
    outcome_reference: str | None = None
    certainty: SendCertainty | None = None
    idempotency_marker: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    started_at: datetime
    finished_at: datetime
    claimed: int
    executions: tuple[JobExecution, ...]
    stale_recovered: int = 0
    paused: bool = False
    clock_event: ClockEvent = ClockEvent.NORMAL
    overdue_claimed: int = 0

    @property
    def sent(self) -> int:
        return sum(item.action is JobAction.SENT for item in self.executions)

    @property
    def needs_review(self) -> int:
        return sum(item.action is JobAction.MANUAL_REVIEW for item in self.executions)

    @property
    def exit_success(self) -> bool:
        return not self.needs_review and all(
            item.action not in {JobAction.FAILED} for item in self.executions
        )


@dataclass(frozen=True, slots=True)
class ScheduledJobRecord:
    message: ScheduledMessage
    attachment_manifest: tuple[dict[str, Any], ...]
    last_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None


TERMINAL_STATES = frozenset(
    {
        SendState.SENT,
        SendState.CANCELLED,
        SendState.UNCERTAIN,
        SendState.MANUAL_REVIEW,
    }
)
EDITABLE_STATES = frozenset(
    {
        SendState.SCHEDULED,
        SendState.UNDO_DELAY,
        SendState.DUE,
        SendState.WAITING_CONNECTIVITY,
        SendState.WAITING_AUTHENTICATION,
        SendState.RETRY_SCHEDULED,
        SendState.FAILED,
    }
)
CLAIMABLE_STATES = frozenset(
    {
        SendState.SCHEDULED,
        SendState.DUE,
        SendState.RETRY_SCHEDULED,
        SendState.WAITING_CONNECTIVITY,
        SendState.WAITING_AUTHENTICATION,
    }
)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ConflictError("Scheduled timestamps must include a time zone.")
    return value.astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


def _manifest_for_draft(
    draft: DraftData,
    *,
    maximum_bytes: int | None,
) -> tuple[dict[str, Any], ...]:
    attachments = build_attachment_manifest(draft.attachment_paths, maximum_bytes=maximum_bytes)
    snapshot = {
        "kind": "draft_snapshot",
        "draft_revision": draft.revision,
        "draft_hash": draft_content_hash(draft),
    }
    return (snapshot, *(dict(item, kind="attachment") for item in attachments))


def _split_manifest(
    manifest: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, tuple[Mapping[str, Any], ...]]:
    snapshot: Mapping[str, Any] | None = None
    attachments: list[Mapping[str, Any]] = []
    for entry in manifest:
        if entry.get("kind") == "draft_snapshot":
            snapshot = entry
        else:
            attachments.append(entry)
    return snapshot, tuple(attachments)


class SchedulerService:
    """Manage durable send jobs and execute them through an injected callable."""

    def __init__(
        self,
        state: StateStore,
        drafts: DraftService,
        sender: SendCallable,
        *,
        timezone: str = "UTC",
        worker_id: str | None = None,
        lease_seconds: int = 300,
        retry_attempts: int = 3,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 120.0,
        maximum_attachment_bytes: int | None = None,
        random_source: random.Random | None = None,
        sleep_resume_threshold_seconds: float = 300.0,
        missed_schedule_grace_seconds: float = 86_400.0,
        backward_clock_tolerance_seconds: float = 5.0,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if retry_attempts < 0:
            raise ValueError("retry_attempts may not be negative")
        if sleep_resume_threshold_seconds <= 0:
            raise ValueError("sleep_resume_threshold_seconds must be positive")
        if missed_schedule_grace_seconds < sleep_resume_threshold_seconds:
            raise ValueError(
                "missed_schedule_grace_seconds must not be shorter than the resume threshold"
            )
        if backward_clock_tolerance_seconds < 0:
            raise ValueError("backward_clock_tolerance_seconds may not be negative")
        self.state = state
        self.drafts = drafts
        self.sender = sender
        self.timezone = timezone
        self.worker_id = worker_id or f"scheduler-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.maximum_attachment_bytes = maximum_attachment_bytes
        self.random_source = random_source
        self.sleep_resume_threshold_seconds = sleep_resume_threshold_seconds
        self.missed_schedule_grace_seconds = missed_schedule_grace_seconds
        self.backward_clock_tolerance_seconds = backward_clock_tolerance_seconds

    def schedule(
        self,
        draft_reference: str,
        expression: str,
        *,
        timezone: str | None = None,
        now: datetime | None = None,
        require_final_confirmation: bool = False,
        working_hours: tuple[datetime_time, datetime_time] | None = None,
        recurrence_id: str | None = None,
        reference: str | None = None,
    ) -> ScheduledMessage:
        zone_name = timezone or self.timezone
        resolved = parse_datetime_expression(
            expression,
            timezone=zone_name,
            now=now,
            future_required=True,
        )
        scheduled_for = resolved.local
        if working_hours is not None:
            scheduled_for = next_working_time(
                scheduled_for, start=working_hours[0], end=working_hours[1]
            )
        return self.schedule_at(
            draft_reference,
            scheduled_for,
            timezone=zone_name,
            original_expression=expression,
            require_final_confirmation=require_final_confirmation,
            recurrence_id=recurrence_id,
            reference=reference,
            now=now,
        )

    schedule_draft = schedule

    def schedule_at(
        self,
        draft_reference: str,
        scheduled_for: datetime,
        *,
        timezone: str | None = None,
        original_expression: str | None = None,
        require_final_confirmation: bool = False,
        recurrence_id: str | None = None,
        reference: str | None = None,
        now: datetime | None = None,
        state: SendState = SendState.SCHEDULED,
        allow_empty_subject: bool = False,
        allow_empty_body: bool = False,
    ) -> ScheduledMessage:
        when = _ensure_aware(scheduled_for)
        current = _ensure_aware(now) if now is not None else utc_now()
        if when <= current:
            raise ConflictError("A new scheduled message must be set for a future time.")
        draft = self.drafts.get(draft_reference)
        if draft.location is DraftLocation.CONFLICTED:
            raise ConflictError("Resolve the draft conflict before scheduling it.")
        manifest = _manifest_for_draft(draft, maximum_bytes=self.maximum_attachment_bytes)
        manifest[0].update(
            allow_empty_subject=allow_empty_subject, allow_empty_body=allow_empty_body
        )
        job_reference = reference or f"job-{uuid.uuid4().hex}"
        idempotency_key = f"gucmail-{uuid.uuid4().hex}"
        self.state.add_scheduled_job(
            reference=job_reference,
            draft_reference=draft_reference,
            scheduled_for=when,
            timezone=timezone or self.timezone,
            original_expression=original_expression or scheduled_for.isoformat(),
            idempotency_key=idempotency_key,
            require_final_confirmation=require_final_confirmation,
            attachment_manifest=manifest,
            state=state,
            recurrence_id=recurrence_id,
        )
        return self.get(job_reference).message

    def queue_undo_send(
        self,
        draft_reference: str,
        delay_seconds: int,
        *,
        now: datetime | None = None,
        require_final_confirmation: bool = False,
        allow_empty_subject: bool = False,
    ) -> ScheduledMessage:
        if delay_seconds < 1:
            raise ValueError("Undo Send delay must be at least one second.")
        current = _ensure_aware(now) if now is not None else utc_now()
        return self.schedule_at(
            draft_reference,
            current + timedelta(seconds=delay_seconds),
            timezone=self.timezone,
            original_expression=f"Undo Send delay: {delay_seconds} seconds",
            require_final_confirmation=require_final_confirmation,
            now=current,
            state=SendState.UNDO_DELAY,
            allow_empty_subject=allow_empty_subject,
        )

    def undo(self, reference: str) -> ScheduledMessage:
        record = self.get(reference)
        if record.message.state is not SendState.UNDO_DELAY:
            raise ConflictError(
                "Undo Send is available only while the message is still locally delayed."
            )
        return self.cancel(reference)

    def get(self, reference: str) -> ScheduledJobRecord:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE reference=?", (reference,)
            ).fetchone()
        if row is None:
            raise ItemNotFoundError(f"Scheduled message not found: {reference}")
        return self._record_from_row(dict(row))

    get_job = get

    @staticmethod
    def _record_from_row(row: Mapping[str, Any]) -> ScheduledJobRecord:
        try:
            manifest = json.loads(str(row["attachment_manifest_json"]))
            if not isinstance(manifest, list):
                raise ValueError("manifest is not a list")
            created_at = datetime.fromisoformat(str(row["created_at"]))
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
            return ScheduledJobRecord(
                message=ScheduledMessage(
                    reference=str(row["reference"]),
                    draft_reference=str(row["draft_reference"]),
                    scheduled_for=datetime.fromisoformat(str(row["scheduled_for"])),
                    timezone=str(row["timezone"]),
                    original_expression=str(row["original_expression"]),
                    state=SendState(str(row["state"])),
                    created_at=created_at,
                    updated_at=updated_at,
                    attempts=int(row["attempts"]),
                    next_attempt_at=_parse_optional_datetime(row.get("next_attempt_at")),
                    last_error=str(row["last_error"]) if row.get("last_error") else None,
                    idempotency_key=str(row["idempotency_key"]),
                    require_final_confirmation=bool(row["require_final_confirmation"]),
                    outcome_reference=(
                        str(row["outcome_reference"]) if row.get("outcome_reference") else None
                    ),
                    recurrence_id=(str(row["recurrence_id"]) if row.get("recurrence_id") else None),
                ),
                attachment_manifest=tuple(
                    dict(item) for item in manifest if isinstance(item, Mapping)
                ),
                last_attempt_at=_parse_optional_datetime(row.get("last_attempt_at")),
                lease_owner=str(row["lease_owner"]) if row.get("lease_owner") else None,
                lease_until=_parse_optional_datetime(row.get("lease_until")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LocalStateError("A scheduled job record is corrupt.") from exc

    def list(
        self,
        *,
        states: Iterable[SendState] | None = None,
        before: datetime | None = None,
        after: datetime | None = None,
        draft_reference: str | None = None,
        limit: int | None = None,
    ) -> list[ScheduledJobRecord]:
        if limit is not None and not 1 <= limit <= 10_000:
            raise ConfigurationError("Scheduled-message list limit must be between 1 and 10000.")
        pushdown_limit = (
            limit if before is None and after is None and draft_reference is None else None
        )
        rows = self.state.list_scheduled_jobs(states=states, limit=pushdown_limit)
        records = [self._record_from_row(row) for row in rows]
        if before is not None:
            bound = _ensure_aware(before)
            records = [
                item for item in records if item.message.scheduled_for.astimezone(UTC) < bound
            ]
        if after is not None:
            bound = _ensure_aware(after)
            records = [
                item for item in records if item.message.scheduled_for.astimezone(UTC) > bound
            ]
        if draft_reference is not None:
            records = [item for item in records if item.message.draft_reference == draft_reference]
        if limit is not None:
            records = records[:limit]
        return records

    list_jobs = list

    def history(self, reference: str) -> builtins.list[dict[str, Any]]:
        self.get(reference)
        return self.state.schedule_history(reference)

    def _require_editable(self, reference: str) -> ScheduledJobRecord:
        record = self.get(reference)
        if record.message.state not in EDITABLE_STATES:
            raise ConflictError(
                f"Scheduled message {reference} cannot be changed while in "
                f"{record.message.state.value}."
            )
        return record

    def edit(
        self,
        reference: str,
        *,
        draft_changes: Mapping[str, Any] | None = None,
        require_final_confirmation: bool | None = None,
    ) -> ScheduledMessage:
        record = self._require_editable(reference)
        draft = self.drafts.get(record.message.draft_reference)
        previous_revision = draft.revision
        previous_hash = draft_content_hash(draft)
        if draft_changes:
            draft = self.drafts.edit(
                draft.reference,
                expected_revision=draft.revision,
                **dict(draft_changes),
            )
        manifest = _manifest_for_draft(draft, maximum_bytes=self.maximum_attachment_bytes)
        now_text = iso()
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs SET attachment_manifest_json=?,
                    require_final_confirmation=COALESCE(?, require_final_confirmation),
                    last_error=NULL, updated_at=? WHERE reference=? AND state=?
                """,
                (
                    json.dumps(manifest, sort_keys=True),
                    (
                        int(require_final_confirmation)
                        if require_final_confirmation is not None
                        else None
                    ),
                    now_text,
                    reference,
                    record.message.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Scheduled message changed concurrently: {reference}")
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, 'edited', ?, ?)",
                (
                    reference,
                    json.dumps(
                        {
                            "previous_revision": previous_revision,
                            "revision": draft.revision,
                            "content_changed": previous_hash != draft_content_hash(draft),
                            "final_confirmation": require_final_confirmation,
                        },
                        sort_keys=True,
                    ),
                    now_text,
                ),
            )
        return self.get(reference).message

    def reschedule(
        self,
        reference: str,
        expression: str,
        *,
        timezone: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledMessage:
        record = self._require_editable(reference)
        zone_name = timezone or record.message.timezone
        resolved = parse_datetime_expression(
            expression, timezone=zone_name, now=now, future_required=True
        )
        return self.reschedule_at(
            reference,
            resolved.local,
            timezone=zone_name,
            original_expression=expression,
            expected_state=record.message.state,
            now=now,
        )

    def reschedule_at(
        self,
        reference: str,
        scheduled_for: datetime,
        *,
        timezone: str | None = None,
        original_expression: str | None = None,
        expected_state: SendState | None = None,
        now: datetime | None = None,
    ) -> ScheduledMessage:
        record = self._require_editable(reference)
        if expected_state is not None and record.message.state is not expected_state:
            raise ConflictError(f"Scheduled message changed concurrently: {reference}")
        when = _ensure_aware(scheduled_for)
        current = _ensure_aware(now) if now is not None else utc_now()
        if when <= current:
            raise ConflictError("Rescheduled time must be in the future.")
        now_text = iso(current)
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs SET scheduled_for=?, timezone=?, original_expression=?,
                    state=?, next_attempt_at=NULL, last_error=NULL, lease_owner=NULL,
                    lease_until=NULL, updated_at=? WHERE reference=? AND state=?
                """,
                (
                    iso(when),
                    timezone or record.message.timezone,
                    original_expression or scheduled_for.isoformat(),
                    SendState.SCHEDULED.value,
                    now_text,
                    reference,
                    record.message.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Scheduled message changed concurrently: {reference}")
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, 'rescheduled', ?, ?)",
                (
                    reference,
                    json.dumps(
                        {
                            "previous_scheduled_for": record.message.scheduled_for.isoformat(),
                            "scheduled_for": iso(when),
                            "previous_timezone": record.message.timezone,
                            "timezone": timezone or record.message.timezone,
                            "original_expression": (
                                original_expression or scheduled_for.isoformat()
                            ),
                        },
                        sort_keys=True,
                    ),
                    now_text,
                ),
            )
        return self.get(reference).message

    def cancel(self, reference: str) -> ScheduledMessage:
        record = self._require_editable(reference)
        self.state.update_job_state(reference, SendState.CANCELLED, event="cancelled")
        self._append_history(
            reference,
            "cancelled_detail",
            json.dumps(
                {
                    "previous_state": record.message.state.value,
                    "scheduled_for": record.message.scheduled_for.isoformat(),
                },
                sort_keys=True,
            ),
        )
        return self.get(reference).message

    cancel_job = cancel

    def duplicate(
        self,
        reference: str,
        *,
        expression: str | None = None,
        scheduled_for: datetime | None = None,
        now: datetime | None = None,
    ) -> ScheduledMessage:
        source = self.get(reference).message
        duplicated = self.drafts.duplicate(source.draft_reference)
        if expression is not None:
            return self.schedule(
                duplicated.reference,
                expression,
                timezone=source.timezone,
                now=now,
                require_final_confirmation=source.require_final_confirmation,
            )
        when = scheduled_for or source.scheduled_for
        current = _ensure_aware(now) if now is not None else utc_now()
        if when.astimezone(UTC) <= current:
            raise ConflictError("A duplicated scheduled message needs a new future time.")
        return self.schedule_at(
            duplicated.reference,
            when,
            timezone=source.timezone,
            original_expression=source.original_expression,
            require_final_confirmation=source.require_final_confirmation,
            now=current,
        )

    def send_now(
        self,
        reference: str,
        *,
        confirmed: bool = False,
        now: datetime | None = None,
    ) -> JobExecution:
        record = self._require_editable(reference)
        if record.message.require_final_confirmation and not confirmed:
            raise ConfirmationRequiredError(
                "This scheduled message requires final confirmation before sending."
            )
        current = _ensure_aware(now) if now is not None else utc_now()
        now_text = iso(current)
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs SET scheduled_for=?, next_attempt_at=NULL, state=?,
                    require_final_confirmation=0, last_error=NULL, updated_at=?
                WHERE reference=? AND state=?
                """,
                (
                    now_text,
                    SendState.DUE.value,
                    now_text,
                    reference,
                    record.message.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Scheduled message changed concurrently: {reference}")
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, 'send_now', ?, ?)",
                (
                    reference,
                    json.dumps(
                        {
                            "previous_scheduled_for": record.message.scheduled_for.isoformat(),
                            "requested_at": now_text,
                        },
                        sort_keys=True,
                    ),
                    now_text,
                ),
            )
        claimed = self._claim_reference(reference, now=current)
        return self._process_claimed(claimed, now=current)

    def run_reference_if_due(
        self, reference: str, *, now: datetime | None = None
    ) -> JobExecution | None:
        """Service one locally authorized countdown without advancing other jobs.

        Due time and state are rechecked within the claim transaction, so a
        cancelled, rescheduled, paused, or already-claimed job is never forced.
        """
        current = _ensure_aware(now) if now is not None else utc_now()
        if self.is_paused():
            return None
        try:
            claimed = self._claim_reference(reference, now=current, require_due=True)
        except (ConflictError, ItemNotFoundError):
            return None
        return self._process_claimed(claimed, now=current)

    def _claim_reference(
        self, reference: str, *, now: datetime, require_due: bool = False
    ) -> ClaimedJob:
        current_text = iso(now)
        lease_until = iso(now + timedelta(seconds=self.lease_seconds))
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE reference=?", (reference,)
            ).fetchone()
            if row is None:
                raise ItemNotFoundError(f"Scheduled message not found: {reference}")
            state = SendState(str(row["state"]))
            if state not in CLAIMABLE_STATES and not (
                require_due and state is SendState.UNDO_DELAY
            ):
                raise ConflictError(f"Scheduled message is not claimable in {state.value}.")
            if require_due:
                due = datetime.fromisoformat(str(row["scheduled_for"]))
                retry = _parse_optional_datetime(row["next_attempt_at"])
                if due > now or (retry is not None and retry > now):
                    raise ConflictError("The scheduled message is not due yet.")
            lease = _parse_optional_datetime(row["lease_until"])
            if lease is not None and lease >= now:
                raise ConflictError("Scheduled message is already claimed by another worker.")
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs SET state=?, lease_owner=?, lease_until=?,
                    attempts=attempts+1, last_attempt_at=?, updated_at=?
                WHERE reference=? AND state=?
                """,
                (
                    SendState.SENDING.value,
                    self.worker_id,
                    lease_until,
                    current_text,
                    current_text,
                    reference,
                    state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Scheduled message changed concurrently: {reference}")
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, occurred_at) "
                "VALUES (?, 'claimed', ?)",
                (reference, current_text),
            )
            manifest = json.loads(str(row["attachment_manifest_json"]))
            if not isinstance(manifest, list):
                raise LocalStateError("Scheduled attachment manifest is corrupt.")
            return ClaimedJob(
                reference=reference,
                draft_reference=str(row["draft_reference"]),
                scheduled_for=datetime.fromisoformat(str(row["scheduled_for"])),
                timezone=str(row["timezone"]),
                original_expression=str(row["original_expression"]),
                state=SendState.SENDING,
                idempotency_key=str(row["idempotency_key"]),
                attempts=int(row["attempts"]) + 1,
                require_final_confirmation=bool(row["require_final_confirmation"]),
                attachment_manifest=tuple(
                    dict(item) for item in manifest if isinstance(item, Mapping)
                ),
            )

    def _promote_expired_undo_delays(self, *, now: datetime) -> int:
        current = iso(now)
        with self.state.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT reference FROM scheduled_jobs WHERE state=? AND scheduled_for<=?",
                (SendState.UNDO_DELAY.value, current),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE scheduled_jobs SET state=?, updated_at=? WHERE reference=? AND state=?",
                    (
                        SendState.DUE.value,
                        current,
                        row["reference"],
                        SendState.UNDO_DELAY.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO schedule_history(job_reference, event, occurred_at) "
                    "VALUES (?, 'undo_delay_elapsed', ?)",
                    (row["reference"], current),
                )
        return len(rows)

    def run_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> SchedulerRun:
        if limit < 1:
            raise ValueError("limit must be positive")
        started = _ensure_aware(now) if now is not None else utc_now()
        clock_event = self._observe_clock(started)
        if self.is_paused():
            return SchedulerRun(
                started,
                utc_now(),
                0,
                (),
                paused=True,
                clock_event=clock_event,
            )
        stale = self.state.recover_stale_jobs(now=started)
        self._promote_expired_undo_delays(now=started)
        claimed = self.state.claim_due_jobs(
            worker_id=self.worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
            now=started,
        )
        overdue_claimed = sum(
            1
            for job in claimed
            if (started - _ensure_aware(job.scheduled_for)).total_seconds()
            >= self.sleep_resume_threshold_seconds
        )
        executions = tuple(self._process_claimed(job, now=started) for job in claimed)
        return SchedulerRun(
            started_at=started,
            finished_at=utc_now(),
            claimed=len(claimed),
            executions=executions,
            stale_recovered=stale,
            clock_event=clock_event,
            overdue_claimed=overdue_claimed,
        )

    run_due = run_once

    def _process_claimed(self, job: ClaimedJob, *, now: datetime) -> JobExecution:
        lateness = max(0.0, (now - _ensure_aware(job.scheduled_for)).total_seconds())
        if lateness > self.missed_schedule_grace_seconds:
            detail = (
                "The scheduled time was missed by "
                f"{int(lateness)} seconds, beyond the configured automatic-send grace period. "
                "Review the message and choose Send now or Reschedule."
            )
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="missed_schedule_too_old",
            )
            return JobExecution(
                job.reference, JobAction.MANUAL_REVIEW, SendState.MANUAL_REVIEW, detail
            )
        if lateness >= self.sleep_resume_threshold_seconds:
            self._append_history(
                job.reference,
                "missed_schedule_resumed",
                json.dumps(
                    {
                        "scheduled_for": _ensure_aware(job.scheduled_for).isoformat(),
                        "processed_at": now.isoformat(),
                        "lateness_seconds": int(lateness),
                    },
                    sort_keys=True,
                ),
                occurred_at=now,
            )
        if job.require_final_confirmation:
            detail = "Final confirmation is required before this message can be sent."
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="confirmation_required",
            )
            return JobExecution(
                job.reference, JobAction.MANUAL_REVIEW, SendState.MANUAL_REVIEW, detail
            )
        try:
            draft = self.drafts.get(job.draft_reference)
        except ItemNotFoundError as exc:
            detail = str(exc)
            self.state.update_job_state(
                job.reference, SendState.FAILED, error=detail, event="draft_missing"
            )
            return JobExecution(job.reference, JobAction.FAILED, SendState.FAILED, detail)
        if draft.location is DraftLocation.CONFLICTED:
            detail = "The draft has an unresolved synchronization conflict."
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="draft_conflict",
            )
            return JobExecution(
                job.reference, JobAction.MANUAL_REVIEW, SendState.MANUAL_REVIEW, detail
            )
        snapshot, attachments = _split_manifest(job.attachment_manifest)
        if snapshot is None or snapshot.get("draft_hash") != draft_content_hash(draft):
            detail = "The draft changed after it was scheduled; review and edit the job explicitly."
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="draft_changed",
            )
            return JobExecution(
                job.reference, JobAction.MANUAL_REVIEW, SendState.MANUAL_REVIEW, detail
            )
        try:
            validate_attachment_manifest(attachments, maximum_bytes=self.maximum_attachment_bytes)
        except AttachmentIntegrityError as exc:
            detail = str(exc)
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="attachment_changed",
            )
            return JobExecution(
                job.reference, JobAction.MANUAL_REVIEW, SendState.MANUAL_REVIEW, detail
            )

        validation = ComposeService(
            max_attachment_bytes=self.maximum_attachment_bytes or DEFAULT_MAX_ATTACHMENT_BYTES,
        ).validate(
            draft,
            allow_empty_subject=snapshot.get("allow_empty_subject") is True,
            allow_empty_body=snapshot.get("allow_empty_body") is True,
        )
        if not validation.can_send:
            # These are local validation messages, never draft content or server errors.
            detail = "; ".join(issue.message for issue in validation.errors)
            self.state.update_job_state(
                job.reference,
                SendState.MANUAL_REVIEW,
                error=detail,
                event="draft_invalid",
            )
            return JobExecution(
                job.reference,
                JobAction.MANUAL_REVIEW,
                SendState.MANUAL_REVIEW,
                detail,
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
            )
        draft = validation.draft

        # Everything below is past the send boundary.  Only exceptions explicitly
        # documented as pre-send/retry-safe may enter the automatic retry path.
        try:
            self._record_send_boundary(job, now=now)
        except ConflictError as exc:
            current = self.get(job.reference).message
            return JobExecution(
                job.reference,
                JobAction.SKIPPED,
                current.state,
                redact_text(exc),
                outcome_reference=current.outcome_reference,
                idempotency_marker=job.idempotency_key,
            )
        try:
            raw_result = self._invoke_sender(draft, job)
            result = self._normalize_result(raw_result)
        except (AuthenticationError, ThrottledError, RetryableSendFailure) as exc:
            return self._retry_or_fail(job, exc, now=now)
        except NetworkError as exc:
            if exc.context.retry_safe is True and not exc.context.mutation_uncertain:
                return self._retry_or_fail(job, exc, now=now)
            return self._mark_uncertain(job, exc)
        except SendOutcomeUncertainError as exc:
            return self._mark_uncertain(job, exc)
        except DefiniteSendFailure as exc:
            detail = redact_text(exc)
            self.state.update_job_state(
                job.reference, SendState.FAILED, error=detail, event="send_failed"
            )
            return JobExecution(
                job.reference,
                JobAction.FAILED,
                SendState.FAILED,
                detail,
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
                idempotency_marker=job.idempotency_key,
            )
        except Exception as exc:  # A retry here could send a duplicate.
            return self._mark_uncertain(job, exc)

        if result.idempotency_marker not in {None, job.idempotency_key}:
            return self._mark_uncertain(
                job,
                ConflictError(
                    "The sender confirmed a different idempotency marker than the queued job."
                ),
            )
        if result.certainty is SendCertainty.DEFINITELY_NOT_SENT:
            detail = redact_text(result.detail or "Exchange definitely did not accept the message.")
            self.state.update_job_state(
                job.reference, SendState.FAILED, error=detail, event="send_failed"
            )
            return JobExecution(
                job.reference,
                JobAction.FAILED,
                SendState.FAILED,
                detail,
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
                idempotency_marker=job.idempotency_key,
            )
        if result.certainty is SendCertainty.UNCERTAIN:
            return self._mark_uncertain(
                job,
                SendOutcomeUncertainError(result.detail or "The sender could not prove delivery."),
            )

        self.state.update_job_state(
            job.reference,
            SendState.SENT,
            outcome_reference=result.outcome_reference,
            event="sent",
        )
        self.drafts.remove_recovery(job.draft_reference)
        return JobExecution(
            job.reference,
            JobAction.SENT,
            SendState.SENT,
            outcome_reference=result.outcome_reference,
            certainty=SendCertainty.CONFIRMED_SENT,
            idempotency_marker=job.idempotency_key,
        )

    def _invoke_sender(self, draft: DraftData, job: ClaimedJob) -> object:
        """Share the immediate-send claim across scheduler and foreground processes."""
        attempts = SendAttemptStore(self.state)
        attempts.claim(draft.reference, job.idempotency_key)
        try:
            raw = self._invoke_sender_untracked(draft, job)
            result = self._normalize_result(raw)
        except BaseException as exc:
            definite = isinstance(
                exc,
                (AuthenticationError, ThrottledError, RetryableSendFailure, DefiniteSendFailure),
            ) or (
                isinstance(exc, NetworkError)
                and exc.context.retry_safe is True
                and not exc.context.mutation_uncertain
            )
            attempts.finish(
                draft.reference, job.idempotency_key, "failed" if definite else "uncertain"
            )
            raise
        outcome = {
            SendCertainty.CONFIRMED_SENT: "sent",
            SendCertainty.DEFINITELY_NOT_SENT: "failed",
            SendCertainty.UNCERTAIN: "uncertain",
        }[result.certainty]
        if result.idempotency_marker not in {None, job.idempotency_key}:
            outcome = "uncertain"
        attempts.finish(draft.reference, job.idempotency_key, outcome)
        return raw

    def _invoke_sender_untracked(self, draft: DraftData, job: ClaimedJob) -> object:
        """Choose one call shape before invocation; never retry on a signature TypeError."""

        try:
            signature = inspect.signature(self.sender)
        except (TypeError, ValueError):
            return self.sender(draft, idempotency_key=job.idempotency_key)
        parameters = signature.parameters
        has_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        if has_kwargs or "idempotency_key" in parameters or "job_reference" in parameters:
            keywords: dict[str, Any] = {}
            if has_kwargs or "idempotency_key" in parameters:
                keywords["idempotency_key"] = job.idempotency_key
            if has_kwargs or "job_reference" in parameters:
                keywords["job_reference"] = job.reference
            return self.sender(draft, **keywords)
        positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        if len(positional) >= 2:
            return self.sender(draft, job.idempotency_key)
        return self.sender(draft)

    @staticmethod
    def _normalize_result(result: object) -> SendResult:
        if result is False:
            raise DefiniteSendFailure("The send adapter rejected the message before delivery.")
        if isinstance(result, SendResult):
            return result
        if result is None or result is True:
            return SendResult()
        if isinstance(result, str):
            return SendResult(outcome_reference=result)
        if isinstance(result, Mapping):
            if result.get("accepted") is False:
                raise DefiniteSendFailure("The send adapter rejected the message before delivery.")
            raw = result.get("outcome_reference", result.get("reference"))
            raw_certainty = result.get("certainty", SendCertainty.CONFIRMED_SENT.value)
            try:
                certainty = SendCertainty(str(raw_certainty))
            except ValueError as exc:
                raise SendOutcomeUncertainError(
                    "The send adapter returned an unknown delivery certainty."
                ) from exc
            return SendResult(
                str(raw) if raw is not None else None,
                certainty=certainty,
                detail=str(result["detail"]) if result.get("detail") else None,
                idempotency_marker=(
                    str(result["idempotency_marker"]) if result.get("idempotency_marker") else None
                ),
            )
        raw_reference = getattr(result, "outcome_reference", getattr(result, "reference", None))
        raw_certainty = getattr(result, "certainty", SendCertainty.CONFIRMED_SENT)
        try:
            certainty = (
                raw_certainty
                if isinstance(raw_certainty, SendCertainty)
                else SendCertainty(str(raw_certainty))
            )
        except ValueError as exc:
            raise SendOutcomeUncertainError(
                "The send adapter returned an unknown delivery certainty."
            ) from exc
        raw_detail = getattr(result, "detail", None)
        raw_marker = getattr(result, "idempotency_marker", None)
        return SendResult(
            str(raw_reference) if raw_reference is not None else None,
            certainty=certainty,
            detail=str(raw_detail) if raw_detail else None,
            idempotency_marker=str(raw_marker) if raw_marker else None,
        )

    def _retry_or_fail(
        self,
        job: ClaimedJob,
        error: Exception,
        *,
        now: datetime,
    ) -> JobExecution:
        detail = redact_text(error)
        if job.attempts > self.retry_attempts:
            self.state.update_job_state(
                job.reference, SendState.FAILED, error=detail, event="retries_exhausted"
            )
            return JobExecution(
                job.reference,
                JobAction.FAILED,
                SendState.FAILED,
                detail,
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
                idempotency_marker=job.idempotency_key,
            )
        server_delay = getattr(error, "retry_after", None)
        delay = backoff_seconds(
            max(0, job.attempts - 1),
            base=self.retry_base_seconds,
            maximum=self.retry_max_seconds,
            server_delay=float(server_delay) if server_delay is not None else None,
            random_source=self.random_source,
        )
        retry_at = now + timedelta(seconds=delay)
        if isinstance(error, AuthenticationError):
            state = SendState.WAITING_AUTHENTICATION
        elif isinstance(error, (NetworkError, RetryableSendFailure)) and not isinstance(
            error, ThrottledError
        ):
            state = SendState.WAITING_CONNECTIVITY
        else:
            state = SendState.RETRY_SCHEDULED
        self.state.update_job_state(
            job.reference,
            state,
            error=detail,
            next_attempt_at=retry_at,
            event="retry_scheduled",
        )
        return JobExecution(
            job.reference,
            JobAction.RETRY,
            state,
            detail,
            certainty=SendCertainty.DEFINITELY_NOT_SENT,
            idempotency_marker=job.idempotency_key,
        )

    def _mark_uncertain(self, job: ClaimedJob, error: BaseException) -> JobExecution:
        detail = (
            "Delivery outcome is uncertain after the send attempt; automatic retry is disabled. "
            f"Manual reconciliation is required. Cause: {redact_text(error)}"
        )
        self.state.update_job_state(
            job.reference,
            SendState.MANUAL_REVIEW,
            error=detail,
            event="send_outcome_uncertain",
        )
        return JobExecution(
            job.reference,
            JobAction.MANUAL_REVIEW,
            SendState.MANUAL_REVIEW,
            detail,
            certainty=SendCertainty.UNCERTAIN,
            idempotency_marker=job.idempotency_key,
        )

    def _record_send_boundary(self, job: ClaimedJob, *, now: datetime) -> None:
        """Atomically claim one network-send boundary for this lease and attempt."""

        current_text = iso(now)
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state, lease_owner, idempotency_key FROM scheduled_jobs WHERE reference=?",
                (job.reference,),
            ).fetchone()
            if row is None:
                raise ItemNotFoundError(f"Scheduled message not found: {job.reference}")
            if (
                str(row["state"]) != SendState.SENDING.value
                or str(row["lease_owner"] or "") != self.worker_id
            ):
                raise ConflictError(
                    "Duplicate send blocked because this worker no longer owns the send lease."
                )
            if str(row["idempotency_key"]) != job.idempotency_key:
                raise ConflictError("Duplicate send blocked because the idempotency key changed.")
            previous = connection.execute(
                "SELECT event FROM schedule_history WHERE job_reference=? ORDER BY id DESC",
                (job.reference,),
            ).fetchall()
            boundary_seen = any(str(item["event"]) == "send_boundary_entered" for item in previous)
            if boundary_seen:
                retry_proof = None
                for item in previous:
                    event = str(item["event"])
                    if event == "claimed":
                        continue
                    retry_proof = event
                    break
                if retry_proof not in {
                    "retry_scheduled",
                    "retries_exhausted",
                    "send_failed",
                    "reconciled_not_sent",
                }:
                    raise ConflictError(
                        "Duplicate send blocked because a prior send boundary has no "
                        "definite-not-sent outcome."
                    )
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, 'send_boundary_entered', ?, ?)",
                (
                    job.reference,
                    json.dumps(
                        {
                            "attempt": job.attempts,
                            "idempotency_marker": job.idempotency_key,
                        },
                        sort_keys=True,
                    ),
                    current_text,
                ),
            )

    def reconcile(self, reference: str, reconciler: SendReconciler) -> JobExecution:
        """Resolve an ambiguous send without ever performing another send."""

        record = self.get(reference)
        if record.message.state not in {SendState.MANUAL_REVIEW, SendState.UNCERTAIN}:
            raise ConflictError("Only a message with an uncertain send outcome can be reconciled.")
        draft = self.drafts.get(record.message.draft_reference)
        raw = reconciler.reconcile_send(
            idempotency_key=record.message.idempotency_key,
            draft=draft,
            scheduled_for=record.message.scheduled_for,
        )
        outcome = self._normalize_result(raw)
        if outcome.idempotency_marker not in {None, record.message.idempotency_key}:
            outcome = SendOutcome(
                certainty=SendCertainty.UNCERTAIN,
                detail="The reconciliation result belongs to a different idempotency marker.",
                idempotency_marker=outcome.idempotency_marker,
            )
        if outcome.certainty is SendCertainty.CONFIRMED_SENT:
            SendAttemptStore(self.state).reconcile(
                draft.reference, record.message.idempotency_key, "sent"
            )
            self.state.update_job_state(
                reference,
                SendState.SENT,
                outcome_reference=outcome.outcome_reference,
                event="reconciled_sent",
            )
            self.drafts.remove_recovery(record.message.draft_reference)
            return JobExecution(
                reference,
                JobAction.SENT,
                SendState.SENT,
                outcome.detail,
                outcome.outcome_reference,
                SendCertainty.CONFIRMED_SENT,
                record.message.idempotency_key,
            )
        if outcome.certainty is SendCertainty.DEFINITELY_NOT_SENT:
            SendAttemptStore(self.state).reconcile(
                draft.reference, record.message.idempotency_key, "failed"
            )
            detail = redact_text(outcome.detail or "Sent Items confirms no matching message.")
            self.state.update_job_state(
                reference,
                SendState.FAILED,
                error=detail,
                event="reconciled_not_sent",
            )
            return JobExecution(
                reference,
                JobAction.FAILED,
                SendState.FAILED,
                detail,
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
                idempotency_marker=record.message.idempotency_key,
            )
        detail = redact_text(
            outcome.detail or "Sent Items reconciliation could not prove either outcome."
        )
        self.state.update_job_state(
            reference,
            SendState.MANUAL_REVIEW,
            error=detail,
            event="reconciliation_uncertain",
        )
        return JobExecution(
            reference,
            JobAction.MANUAL_REVIEW,
            SendState.MANUAL_REVIEW,
            detail,
            certainty=SendCertainty.UNCERTAIN,
            idempotency_marker=record.message.idempotency_key,
        )

    def _observe_clock(self, current: datetime) -> ClockEvent:
        """Persist wall-clock observations for sleep/resume and clock-change diagnostics."""

        key = "scheduler.clock.observation"
        current = _ensure_aware(current)
        event = ClockEvent.FIRST_OBSERVATION
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            previous: datetime | None = None
            if row is not None:
                try:
                    raw = json.loads(str(row[0]))
                    if isinstance(raw, Mapping) and isinstance(raw.get("observed_at"), str):
                        previous = _ensure_aware(datetime.fromisoformat(str(raw["observed_at"])))
                except (ValueError, json.JSONDecodeError):
                    previous = None
            if previous is not None:
                delta = (current - previous).total_seconds()
                if delta < -self.backward_clock_tolerance_seconds:
                    event = ClockEvent.CLOCK_MOVED_BACKWARD
                elif delta >= self.sleep_resume_threshold_seconds:
                    event = ClockEvent.SLEEP_OR_RESUME
                else:
                    event = ClockEvent.NORMAL
            payload = json.dumps(
                {
                    "observed_at": current.isoformat(),
                    "event": event.value,
                    "previous_observed_at": previous.isoformat() if previous else None,
                },
                sort_keys=True,
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )
        return event

    def _append_history(
        self,
        reference: str,
        event: str,
        detail: str | None = None,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        if not event or any(character in event for character in "\x00\r\n"):
            raise ValueError("Schedule-history event is invalid.")
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                (reference, event, detail, iso(occurred_at or utc_now())),
            )

    def run_foreground(
        self,
        *,
        poll_interval: float = 30.0,
        stop_event: threading.Event | None = None,
        max_cycles: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_run: Callable[[SchedulerRun], None] | None = None,
    ) -> builtins.list[SchedulerRun]:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        stopper = stop_event or threading.Event()
        runs: list[SchedulerRun] = []
        cycles = 0
        while not stopper.is_set() and (max_cycles is None or cycles < max_cycles):
            result = self.run_once()
            runs.append(result)
            cycles += 1
            if on_run is not None:
                on_run(result)
            if stopper.is_set() or (max_cycles is not None and cycles >= max_cycles):
                break
            if stop_event is not None:
                stop_event.wait(poll_interval)
            else:
                sleep(poll_interval)
        return runs

    def pause(self) -> None:
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('scheduler.paused', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def resume(self) -> None:
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('scheduler.paused', '0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'"
            )

    def is_paused(self) -> bool:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='scheduler.paused'"
            ).fetchone()
        return bool(row and str(row[0]) == "1")

    def status(self) -> dict[str, Any]:
        records = self.list()
        counts: dict[str, int] = {}
        for record in records:
            value = record.message.state.value
            counts[value] = counts.get(value, 0) + 1
        upcoming = [
            record.message for record in records if record.message.state not in TERMINAL_STATES
        ]
        return {
            "paused": self.is_paused(),
            "worker_id": self.worker_id,
            "counts": counts,
            "next_scheduled": upcoming[0].scheduled_for.isoformat() if upcoming else None,
            "manual_review": counts.get(SendState.MANUAL_REVIEW.value, 0),
        }


# Short, natural alias for callers that do not need to distinguish implementations.
ScheduleService = SchedulerService
