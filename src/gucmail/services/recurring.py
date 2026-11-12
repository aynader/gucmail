"""Local recurring-message rules and occurrence materialization.

Rules never point repeated sends at one mutable Exchange draft.  Every
occurrence receives a deterministic local draft copy and its own scheduler job,
which makes a crash between generation steps safe to resume.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import tz

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ItemNotFoundError,
    LocalStateError,
)
from gucmail.services.drafts import DraftService
from gucmail.services.scheduler import SchedulerService
from gucmail.state import StateStore, iso, utc_now


class RecurrenceFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RecurringState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    start_at: datetime
    frequency: RecurrenceFrequency
    timezone: str
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = None
    count: int | None = None
    until: datetime | None = None
    require_manual_approval: bool = False

    def validate(self) -> RecurrenceRule:
        try:
            frequency = RecurrenceFrequency(self.frequency)
        except ValueError as exc:
            raise ConfigurationError(
                "Recurring frequency must be daily, weekly, or monthly."
            ) from exc
        if frequency is not self.frequency:
            return replace(self, frequency=frequency).validate()
        if self.start_at.tzinfo is None:
            raise ConfigurationError("Recurring start_at must include a time zone.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"Unknown recurring time zone: {self.timezone}") from exc
        if self.interval < 1:
            raise ConfigurationError("Recurring interval must be at least one.")
        if self.count is not None and self.count < 1:
            raise ConfigurationError("Recurring count must be at least one.")
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise ConfigurationError("Recurring weekdays must be integers from 0 to 6.")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ConfigurationError("Recurring weekdays may not contain duplicates.")
        if self.day_of_month is not None and not 1 <= self.day_of_month <= 31:
            raise ConfigurationError("day_of_month must be between 1 and 31.")
        if self.frequency is not RecurrenceFrequency.WEEKLY and self.weekdays:
            raise ConfigurationError("weekdays are valid only for weekly rules.")
        if self.frequency is not RecurrenceFrequency.MONTHLY and self.day_of_month is not None:
            raise ConfigurationError("day_of_month is valid only for monthly rules.")
        if self.until is not None:
            if self.until.tzinfo is None:
                raise ConfigurationError("Recurring end time must include a time zone.")
            if self.until.astimezone(UTC) < self.start_at.astimezone(UTC):
                raise ConfigurationError("Recurring end time cannot precede its start.")
        return self


@dataclass(frozen=True, slots=True)
class RecurringRecord:
    reference: str
    template_draft_reference: str
    rule: RecurrenceRule
    state: RecurringState
    next_occurrence: datetime | None
    generated_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GeneratedOccurrence:
    recurring_reference: str
    occurrence: datetime
    draft_reference: str
    job_reference: str


def _localize_strict(day: date, clock: time, zone_name: str) -> datetime:
    zone = ZoneInfo(zone_name)
    naive = datetime.combine(day, clock.replace(tzinfo=None))
    dateutil_zone = tz.gettz(zone_name)
    if dateutil_zone is not None:
        tentative = naive.replace(tzinfo=dateutil_zone)
        if not tz.datetime_exists(tentative):
            raise ConfigurationError(
                f"Recurring time {naive.isoformat(sep=' ')} does not exist in {zone_name}."
            )
        if tz.datetime_ambiguous(tentative):
            raise ConfigurationError(
                f"Recurring time {naive.isoformat(sep=' ')} occurs twice in {zone_name}; "
                "choose an unambiguous time."
            )
    return naive.replace(tzinfo=zone)


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    zero_based = year * 12 + (month - 1) + offset
    return divmod(zero_based, 12)[0], divmod(zero_based, 12)[1] + 1


def next_occurrence(
    rule: RecurrenceRule,
    *,
    after: datetime,
    generated_count: int = 0,
) -> datetime | None:
    """Return the first occurrence strictly after ``after``.

    Weekly intervals are anchored to the Monday of the rule's start week.
    Monthly dates such as the 31st clamp to the final day of shorter months.
    """

    rule.validate()
    if after.tzinfo is None:
        raise ConfigurationError("The recurrence comparison time must include a time zone.")
    if generated_count < 0:
        raise ValueError("generated_count may not be negative")
    if rule.count is not None and generated_count >= rule.count:
        return None
    zone = ZoneInfo(rule.timezone)
    start = rule.start_at.astimezone(zone)
    boundary = after.astimezone(zone)
    clock = start.timetz().replace(tzinfo=None)
    weekdays = tuple(sorted(rule.weekdays or (start.weekday(),)))

    candidate: datetime | None = None
    # Bounded search protects callers from corrupt rules.  A valid rule always
    # resolves far sooner than a century.
    if rule.frequency is RecurrenceFrequency.DAILY:
        elapsed = max(0, (boundary.date() - start.date()).days)
        step = max(0, elapsed // rule.interval)
        for offset in range(step, step + 40_000):
            day = start.date() + timedelta(days=offset * rule.interval)
            possible = _localize_strict(day, clock, rule.timezone)
            if possible >= start and possible > boundary:
                candidate = possible
                break
    elif rule.frequency is RecurrenceFrequency.WEEKLY:
        anchor = start.date() - timedelta(days=start.weekday())
        first_day = max(start.date(), boundary.date() - timedelta(days=1))
        for offset in range(0, 366 * 200):
            day = first_day + timedelta(days=offset)
            weeks = (day - anchor).days // 7
            if weeks < 0 or weeks % rule.interval or day.weekday() not in weekdays:
                continue
            possible = _localize_strict(day, clock, rule.timezone)
            if possible >= start and possible > boundary:
                candidate = possible
                break
    else:
        elapsed_months = max(
            0,
            (boundary.year - start.year) * 12 + boundary.month - start.month,
        )
        step = max(0, elapsed_months // rule.interval)
        wanted_day = rule.day_of_month or start.day
        for offset in range(step, step + 2_400):
            year, month = _add_months(start.year, start.month, offset * rule.interval)
            day_number = min(wanted_day, calendar.monthrange(year, month)[1])
            possible = _localize_strict(date(year, month, day_number), clock, rule.timezone)
            if possible >= start and possible > boundary:
                candidate = possible
                break

    if candidate is None:
        raise ConfigurationError("Could not resolve the next recurrence within the safety bound.")
    if rule.until is not None and candidate.astimezone(UTC) > rule.until.astimezone(UTC):
        return None
    return candidate


def iter_occurrences(
    rule: RecurrenceRule,
    *,
    after: datetime,
    limit: int = 10,
    generated_count: int = 0,
) -> tuple[datetime, ...]:
    if limit < 0:
        raise ValueError("limit may not be negative")
    result: list[datetime] = []
    boundary = after
    count = generated_count
    while len(result) < limit:
        occurrence = next_occurrence(rule, after=boundary, generated_count=count)
        if occurrence is None:
            break
        result.append(occurrence)
        boundary = occurrence
        count += 1
    return tuple(result)


def recurrence_to_dict(rule: RecurrenceRule, *, generated_count: int = 0) -> dict[str, Any]:
    return {
        "start_at": rule.start_at.isoformat(),
        "frequency": rule.frequency.value,
        "timezone": rule.timezone,
        "interval": rule.interval,
        "weekdays": list(rule.weekdays),
        "day_of_month": rule.day_of_month,
        "count": rule.count,
        "until": rule.until.isoformat() if rule.until else None,
        "require_manual_approval": rule.require_manual_approval,
        "generated_count": generated_count,
    }


def recurrence_from_dict(data: dict[str, Any]) -> tuple[RecurrenceRule, int]:
    try:
        weekdays = data.get("weekdays", [])
        if not isinstance(weekdays, list):
            raise ValueError("weekdays must be a list")
        rule = RecurrenceRule(
            start_at=datetime.fromisoformat(str(data["start_at"])),
            frequency=RecurrenceFrequency(str(data["frequency"])),
            timezone=str(data["timezone"]),
            interval=int(data.get("interval", 1)),
            weekdays=tuple(int(item) for item in weekdays),
            day_of_month=(
                int(data["day_of_month"]) if data.get("day_of_month") is not None else None
            ),
            count=int(data["count"]) if data.get("count") is not None else None,
            until=(datetime.fromisoformat(str(data["until"])) if data.get("until") else None),
            require_manual_approval=bool(data.get("require_manual_approval", False)),
        ).validate()
        generated_count = int(data.get("generated_count", 0))
        if generated_count < 0:
            raise ValueError("generated_count may not be negative")
        return rule, generated_count
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalStateError(f"Recurring rule data is invalid: {exc}") from exc


class RecurringService:
    """Persist rules and materialize auditable, independent scheduler jobs."""

    def __init__(
        self,
        state: StateStore,
        drafts: DraftService,
        scheduler: SchedulerService,
        *,
        automation_enabled: bool = False,
    ) -> None:
        self.state = state
        self.drafts = drafts
        self.scheduler = scheduler
        self.automation_enabled = automation_enabled

    def create(
        self,
        template_draft_reference: str,
        rule: RecurrenceRule,
        *,
        activate: bool = False,
        reference: str | None = None,
        now: datetime | None = None,
    ) -> RecurringRecord:
        rule.validate()
        template = self.drafts.get(template_draft_reference)
        if not (*template.to, *template.cc, *template.bcc):
            raise ConfigurationError("A recurring template must have explicit recipients.")
        if activate and not self.automation_enabled:
            raise ConfirmationRequiredError(
                "Recurring automation is disabled; enable it explicitly before activation."
            )
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        initial_boundary = rule.start_at.astimezone(UTC) - timedelta(microseconds=1)
        boundary = max(current.astimezone(UTC), initial_boundary)
        upcoming = next_occurrence(rule, after=boundary, generated_count=0)
        state = RecurringState.ACTIVE if activate else RecurringState.PAUSED
        if upcoming is None:
            state = RecurringState.COMPLETED
        recurring_reference = reference or f"rec-{uuid.uuid4().hex}"
        now_text = iso(current)
        with self.state.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO recurring_rules(
                        reference, template_draft_reference, rule_json, state,
                        next_occurrence, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recurring_reference,
                        template_draft_reference,
                        json.dumps(recurrence_to_dict(rule), sort_keys=True),
                        state.value,
                        iso(upcoming) if upcoming else None,
                        now_text,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Recurring rule already exists: {recurring_reference}"
                ) from exc
        return self.get(recurring_reference)

    def get(self, reference: str) -> RecurringRecord:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recurring_rules WHERE reference=?", (reference,)
            ).fetchone()
        if row is None:
            raise ItemNotFoundError(f"Recurring rule not found: {reference}")
        try:
            payload = json.loads(str(row["rule_json"]))
            if not isinstance(payload, dict):
                raise ValueError("rule_json is not an object")
            rule, generated = recurrence_from_dict(payload)
            return RecurringRecord(
                reference=str(row["reference"]),
                template_draft_reference=str(row["template_draft_reference"]),
                rule=rule,
                state=RecurringState(str(row["state"])),
                next_occurrence=(
                    datetime.fromisoformat(str(row["next_occurrence"]))
                    if row["next_occurrence"]
                    else None
                ),
                generated_count=generated,
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LocalStateError(f"Recurring rule is corrupt: {reference}") from exc

    def list(self, *, state: RecurringState | None = None) -> list[RecurringRecord]:
        self.state.initialize()
        query = "SELECT reference FROM recurring_rules"
        parameters: tuple[Any, ...] = ()
        if state is not None:
            query += " WHERE state=?"
            parameters = (state.value,)
        query += " ORDER BY COALESCE(next_occurrence, '9999'), created_at"
        with self.state.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self.get(str(row["reference"])) for row in rows]

    list_rules = list

    def preview(
        self,
        reference: str,
        *,
        limit: int = 10,
        after: datetime | None = None,
    ) -> tuple[datetime, ...]:
        record = self.get(reference)
        boundary = after or utc_now()
        return iter_occurrences(
            record.rule,
            after=boundary,
            limit=limit,
            generated_count=record.generated_count,
        )

    def pause(self, reference: str) -> RecurringRecord:
        record = self.get(reference)
        if record.state is RecurringState.COMPLETED:
            raise ConflictError("A completed recurring rule cannot be paused.")
        self._set_state(reference, RecurringState.PAUSED, expected=record.state)
        return self.get(reference)

    def resume(self, reference: str, *, now: datetime | None = None) -> RecurringRecord:
        if not self.automation_enabled:
            raise ConfirmationRequiredError(
                "Recurring automation is disabled; enable it explicitly before resuming."
            )
        record = self.get(reference)
        if record.state is RecurringState.COMPLETED:
            raise ConflictError("A completed recurring rule has no future occurrences.")
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        upcoming = next_occurrence(
            record.rule, after=current, generated_count=record.generated_count
        )
        state = RecurringState.ACTIVE if upcoming else RecurringState.COMPLETED
        self._update_rule_row(
            record,
            rule=record.rule,
            generated_count=record.generated_count,
            state=state,
            next_at=upcoming,
        )
        return self.get(reference)

    def _set_state(
        self,
        reference: str,
        state: RecurringState,
        *,
        expected: RecurringState,
    ) -> None:
        now_text = iso()
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE recurring_rules SET state=?, updated_at=? WHERE reference=? AND state=?",
                (state.value, now_text, reference, expected.value),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Recurring rule changed concurrently: {reference}")

    def skip(self, reference: str) -> RecurringRecord:
        record = self.get(reference)
        if record.next_occurrence is None or record.state is RecurringState.COMPLETED:
            raise ConflictError("The recurring rule has no occurrence to skip.")
        generated = record.generated_count + 1
        upcoming = next_occurrence(
            record.rule,
            after=record.next_occurrence,
            generated_count=generated,
        )
        state = record.state if upcoming else RecurringState.COMPLETED
        self._update_rule_row(
            record,
            rule=record.rule,
            generated_count=generated,
            state=state,
            next_at=upcoming,
        )
        return self.get(reference)

    skip_next = skip

    def edit(
        self,
        reference: str,
        *,
        now: datetime | None = None,
        **changes: Any,
    ) -> RecurringRecord:
        record = self.get(reference)
        allowed = {
            "start_at",
            "frequency",
            "timezone",
            "interval",
            "weekdays",
            "day_of_month",
            "count",
            "until",
            "require_manual_approval",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ConfigurationError(f"Unknown recurring field(s): {', '.join(sorted(unknown))}")
        rule = replace(record.rule, **changes).validate()
        boundary = now or utc_now()
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=UTC)
        upcoming = next_occurrence(rule, after=boundary, generated_count=record.generated_count)
        state = record.state if upcoming else RecurringState.COMPLETED
        self._update_rule_row(
            record,
            rule=rule,
            generated_count=record.generated_count,
            state=state,
            next_at=upcoming,
        )
        return self.get(reference)

    def _update_rule_row(
        self,
        record: RecurringRecord,
        *,
        rule: RecurrenceRule,
        generated_count: int,
        state: RecurringState,
        next_at: datetime | None,
    ) -> None:
        now_text = iso()
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE recurring_rules SET rule_json=?, state=?, next_occurrence=?, updated_at=?
                WHERE reference=? AND updated_at=?
                """,
                (
                    json.dumps(
                        recurrence_to_dict(rule, generated_count=generated_count), sort_keys=True
                    ),
                    state.value,
                    iso(next_at) if next_at else None,
                    now_text,
                    record.reference,
                    record.updated_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Recurring rule changed concurrently: {record.reference}")

    def materialize_until(
        self,
        horizon: datetime,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[GeneratedOccurrence, ...]:
        if not self.automation_enabled:
            return ()
        if horizon.tzinfo is None:
            raise ConfigurationError("Recurring horizon must include a time zone.")
        if limit < 1:
            raise ValueError("limit must be positive")
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        result: list[GeneratedOccurrence] = []
        for initial in self.list(state=RecurringState.ACTIVE):
            record = initial
            while (
                record.next_occurrence is not None
                and record.next_occurrence.astimezone(UTC) <= horizon.astimezone(UTC)
                and len(result) < limit
            ):
                occurrence = record.next_occurrence
                generated = record.generated_count + 1
                if occurrence.astimezone(UTC) <= current.astimezone(UTC):
                    # A sleeping machine must not unleash stale recurring mail.
                    upcoming = next_occurrence(
                        record.rule, after=occurrence, generated_count=generated
                    )
                    self._update_rule_row(
                        record,
                        rule=record.rule,
                        generated_count=generated,
                        state=(RecurringState.ACTIVE if upcoming else RecurringState.COMPLETED),
                        next_at=upcoming,
                    )
                    record = self.get(record.reference)
                    continue

                occurrence_key = hashlib.sha256(
                    f"{record.reference}\0{occurrence.astimezone(UTC).isoformat()}".encode()
                ).hexdigest()[:24]
                draft_reference = f"dft-occ-{occurrence_key}"
                job_reference = f"job-occ-{occurrence_key}"
                try:
                    occurrence_draft = self.drafts.duplicate(
                        record.template_draft_reference, new_reference=draft_reference
                    )
                except ConflictError:
                    occurrence_draft = self.drafts.get(draft_reference)
                try:
                    job = self.scheduler.schedule_at(
                        occurrence_draft.reference,
                        occurrence,
                        timezone=record.rule.timezone,
                        original_expression=(
                            f"Recurring {record.reference} occurrence {occurrence.isoformat()}"
                        ),
                        require_final_confirmation=record.rule.require_manual_approval,
                        recurrence_id=record.reference,
                        reference=job_reference,
                        now=current,
                    )
                except (ConflictError, sqlite3.IntegrityError):
                    existing = self.scheduler.get(job_reference).message
                    job = existing
                result.append(
                    GeneratedOccurrence(
                        recurring_reference=record.reference,
                        occurrence=occurrence,
                        draft_reference=occurrence_draft.reference,
                        job_reference=job.reference,
                    )
                )
                upcoming = next_occurrence(
                    record.rule,
                    after=occurrence,
                    generated_count=generated,
                )
                self._update_rule_row(
                    record,
                    rule=record.rule,
                    generated_count=generated,
                    state=RecurringState.ACTIVE if upcoming else RecurringState.COMPLETED,
                    next_at=upcoming,
                )
                record = self.get(record.reference)
            if len(result) >= limit:
                break
        return tuple(result)

    def delete(self, reference: str) -> bool:
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM recurring_rules WHERE reference=?", (reference,)
            )
        return cursor.rowcount > 0


# Backwards-friendly aliases for the obvious public spelling.
RecurringRule = RecurrenceRule
Frequency = RecurrenceFrequency
