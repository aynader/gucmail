"""High-level, Exchange-independent calendar workflows.

Gateways receive validated domain plans.  An EWS adapter may map those plans to
``exchangelib`` objects, but no transport type is allowed across this boundary.
"""

from __future__ import annotations

import calendar as month_calendar
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import tz

from gucmail.errors import ConfigurationError, ConflictError, UnsupportedFeatureError
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    Importance,
    Sensitivity,
)
from gucmail.services.calendar_history import CalendarSeriesHistory, history_limit
from gucmail.services.calendar_paging import CalendarCursor, query_calendar_window
from gucmail.services.exchange_values import normalize_availability
from gucmail.services.interchange import export_ics as render_ics
from gucmail.services.interchange import parse_ics
from gucmail.services.pim import (
    MAX_PIM_PAGE_SIZE,
    BatchFailure,
    BatchOperationResult,
    BatchSuccess,
    bounded_pim_limit,
)


class CalendarViewKind(StrEnum):
    AGENDA = "agenda"
    DAY = "day"
    WORK_WEEK = "work_week"
    WEEK = "week"
    MONTH = "month"
    UPCOMING = "upcoming"


class RecurrenceScope(StrEnum):
    OCCURRENCE = "occurrence"
    SERIES = "series"


class InvitationPolicy(StrEnum):
    SEND = "send"
    SEND_CHANGED = "send_changed"
    SEND_NONE = "send_none"


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()
MaybeUnset: TypeAlias = Any | _Unset


@dataclass(frozen=True, slots=True)
class CalendarDay:
    date: date
    events: tuple[CalendarEvent, ...]
    in_primary_range: bool = True


@dataclass(frozen=True, slots=True)
class CalendarView:
    kind: CalendarViewKind
    timezone: str
    start: datetime
    end: datetime
    days: tuple[CalendarDay, ...]
    events: tuple[CalendarEvent, ...]
    complete: bool | None = None
    continuation: CalendarCursor | None = None
    notice: str | None = None
    examined_candidates: int | None = None

    def events_on(self, value: date) -> tuple[CalendarEvent, ...]:
        for bucket in self.days:
            if bucket.date == value:
                return bucket.events
        return ()


@dataclass(frozen=True, slots=True)
class CalendarConflict:
    first: CalendarEvent
    second: CalendarEvent
    starts_at: datetime
    ends_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at


@dataclass(frozen=True, slots=True)
class EventCreatePlan:
    event: CalendarEvent
    calendar_reference: str | None = None
    invitations: InvitationPolicy = InvitationPolicy.SEND
    attachment_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class CalendarPatch:
    """Explicit event changes; ``UNSET`` means preserve and ``None`` means clear."""

    subject: MaybeUnset = UNSET
    start: MaybeUnset = UNSET
    end: MaybeUnset = UNSET
    timezone: MaybeUnset = UNSET
    location: MaybeUnset = UNSET
    body: MaybeUnset = UNSET
    required_attendees: MaybeUnset = UNSET
    optional_attendees: MaybeUnset = UNSET
    resources: MaybeUnset = UNSET
    is_all_day: MaybeUnset = UNSET
    recurrence: MaybeUnset = UNSET
    categories: MaybeUnset = UNSET
    importance: MaybeUnset = UNSET
    sensitivity: MaybeUnset = UNSET
    reminder_minutes: MaybeUnset = UNSET
    availability: MaybeUnset = UNSET

    def changes(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("subject", self.subject),
                ("start", self.start),
                ("end", self.end),
                ("timezone", self.timezone),
                ("location", self.location),
                ("body", self.body),
                ("required_attendees", self.required_attendees),
                ("optional_attendees", self.optional_attendees),
                ("resources", self.resources),
                ("is_all_day", self.is_all_day),
                ("recurrence", self.recurrence),
                ("categories", self.categories),
                ("importance", self.importance),
                ("sensitivity", self.sensitivity),
                ("reminder_minutes", self.reminder_minutes),
                ("availability", self.availability),
            )
            if value is not UNSET
        }


@dataclass(frozen=True, slots=True)
class EventUpdatePlan:
    reference: str
    changes: Mapping[str, Any]
    scope: RecurrenceScope = RecurrenceScope.OCCURRENCE
    invitations: InvitationPolicy = InvitationPolicy.SEND_CHANGED
    attachment_paths: tuple[Path, ...] = ()
    remove_attachment_references: tuple[str, ...] = ()

    @property
    def update_fields(self) -> tuple[str, ...]:
        return tuple(self.changes)


@dataclass(frozen=True, slots=True)
class EventDeletePlan:
    reference: str
    scope: RecurrenceScope = RecurrenceScope.OCCURRENCE
    send_cancellation: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MeetingResponsePlan:
    reference: str
    response: CalendarResponse
    send_response: bool = True
    comment: str | None = None
    proposed_start: datetime | None = None
    proposed_end: datetime | None = None

    @property
    def proposes_new_time(self) -> bool:
        return self.proposed_start is not None


@dataclass(frozen=True, slots=True)
class CalendarImportPlan:
    events: tuple[CalendarEvent, ...]
    calendar_reference: str | None
    invitations: InvitationPolicy
    warnings: tuple[str, ...] = ()


@runtime_checkable
class CalendarGateway(Protocol):
    """Transport contract implemented by an EWS or in-memory adapter.

    Date windows supplied to ``list_events`` are aware UTC values and use half-open
    semantics: ``start <= event < end``.
    """

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> Sequence[CalendarEvent]: ...

    def get_event(self, reference: str) -> CalendarEvent: ...

    def create_event(self, plan: EventCreatePlan) -> CalendarEvent: ...

    def update_event(self, plan: EventUpdatePlan) -> CalendarEvent: ...

    def delete_event(self, plan: EventDeletePlan) -> None: ...

    def respond_to_meeting(self, plan: MeetingResponsePlan) -> CalendarEvent | None: ...


def validate_timezone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown time zone: {timezone}") from exc


def localize_datetime(value: datetime, timezone: str) -> datetime:
    """Attach/convert a zone while rejecting ambiguous and nonexistent wall times."""

    zone = validate_timezone(timezone)
    if value.tzinfo is not None:
        return value.astimezone(zone)
    dateutil_zone = tz.gettz(timezone)
    if dateutil_zone is not None:
        tentative = value.replace(tzinfo=dateutil_zone)
        if not tz.datetime_exists(tentative):
            raise ConfigurationError(
                f"{value.isoformat(sep=' ')} does not exist in {timezone} because of a "
                "daylight-saving transition. Choose another time."
            )
        if tz.datetime_ambiguous(tentative):
            raise ConfigurationError(
                f"{value.isoformat(sep=' ')} occurs twice in {timezone} because of a "
                "daylight-saving transition. Include an explicit UTC offset."
            )
    return value.replace(tzinfo=zone)


def normalize_event(event: CalendarEvent) -> CalendarEvent:
    """Validate an event and return a normalized, immutable domain value."""

    zone = validate_timezone(event.timezone)
    start = localize_datetime(event.start, event.timezone)
    end = localize_datetime(event.end, event.timezone)
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ConfigurationError("A calendar event must end after it starts.")
    subject = event.subject.strip()
    if not subject:
        raise ConfigurationError("A calendar event needs a subject.")
    if event.is_all_day:
        if start.astimezone(zone).time() != time.min or end.astimezone(zone).time() != time.min:
            raise ConfigurationError("All-day events must start and end at local midnight.")
        if start.astimezone(zone).date() >= end.astimezone(zone).date():
            raise ConfigurationError("An all-day event must cover at least one calendar day.")
    if event.reminder_minutes is not None and event.reminder_minutes < 0:
        raise ConfigurationError("Reminder minutes may not be negative.")
    availability = _normalize_availability(event.availability)
    try:
        importance = Importance(str(event.importance).lower())
        sensitivity = Sensitivity(str(event.sensitivity).lower())
    except ValueError as exc:
        raise ConfigurationError("The event has an unknown importance or sensitivity.") from exc
    if event.recurrence is not None and not event.recurrence:
        raise ConfigurationError("Event recurrence may be null, but not an empty rule.")
    return replace(
        event,
        subject=subject,
        start=start,
        end=end,
        required_attendees=_validated_addresses(event.required_attendees),
        optional_attendees=_validated_addresses(event.optional_attendees),
        resources=_validated_addresses(event.resources),
        categories=tuple(dict.fromkeys(item.strip() for item in event.categories if item.strip())),
        importance=importance,
        sensitivity=sensitivity,
        availability=availability,
    )


def _validated_addresses(values: Sequence[Address]) -> tuple[Address, ...]:
    result: list[Address] = []
    seen: set[str] = set()
    for value in values:
        email = value.email.strip()
        if not email or "@" not in email or any(char in email for char in "\r\n"):
            raise ConfigurationError(f"Invalid calendar attendee address: {value.email!r}")
        key = email.casefold()
        if key not in seen:
            result.append(replace(value, email=email))
            seen.add(key)
    return tuple(result)


def _normalize_availability(value: str) -> str:
    return normalize_availability(value)


def detect_conflicts(
    events: Iterable[CalendarEvent],
    *,
    blocking_availability: frozenset[str] = frozenset(
        {"busy", "tentative", "out_of_office", "oof"}
    ),
) -> tuple[CalendarConflict, ...]:
    """Return pairwise overlaps using half-open intervals; adjacent events do not clash."""

    normalized = sorted(
        (normalize_event(event) for event in events),
        key=lambda event: (event.start.astimezone(UTC), event.end.astimezone(UTC), event.reference),
    )
    result: list[CalendarConflict] = []
    for index, first in enumerate(normalized):
        if first.availability not in blocking_availability:
            continue
        first_end = first.end.astimezone(UTC)
        for second in normalized[index + 1 :]:
            second_start = second.start.astimezone(UTC)
            if second_start >= first_end:
                break
            if (
                first.reference == second.reference
                or second.availability not in blocking_availability
            ):
                continue
            starts_at = max(first.start.astimezone(UTC), second_start)
            ends_at = min(first_end, second.end.astimezone(UTC))
            if starts_at < ends_at:
                result.append(
                    CalendarConflict(
                        first=first,
                        second=second,
                        starts_at=starts_at,
                        ends_at=ends_at,
                    )
                )
    return tuple(result)


class CalendarService:
    """Calendar application service shared by CLI and TUI surfaces."""

    def __init__(
        self,
        gateway: CalendarGateway,
        *,
        display_timezone: str = "UTC",
        default_calendar: str | None = None,
        default_limit: int = MAX_PIM_PAGE_SIZE,
    ) -> None:
        validate_timezone(display_timezone)
        if default_limit <= 0:
            raise ConfigurationError("Calendar result limit must be positive.")
        self.gateway = gateway
        self.display_timezone = display_timezone
        self.default_calendar = default_calendar
        self.default_limit = bounded_pim_limit(
            None,
            default=default_limit,
            subject="Calendar",
        )

    def get(
        self, reference: str, *, scope: RecurrenceScope = RecurrenceScope.OCCURRENCE
    ) -> CalendarEvent:
        """Load and validate one complete event for details or safe patch planning."""

        if not reference.strip():
            raise ConfigurationError("An event reference is required.")
        scope = _mutation_scope(scope)
        if scope is RecurrenceScope.SERIES:
            operation = getattr(self.gateway, "get_event_series", None)
            if not callable(operation):
                raise UnsupportedFeatureError(
                    "This calendar connection cannot load the entire recurring series."
                )
            return normalize_event(cast(CalendarEvent, operation(reference)))
        return normalize_event(self.gateway.get_event(reference))

    def history(self, reference: str, *, limit: int = 100) -> CalendarSeriesHistory:
        """Read current exception/deletion metadata, with no mutation or expansion."""
        limit = history_limit(limit)
        if not reference.strip():
            raise ConfigurationError("An event reference is required.")
        operation = getattr(self.gateway, "get_calendar_history", None)
        if not callable(operation):
            raise UnsupportedFeatureError(
                "This calendar connection cannot read occurrence history."
            )
        result = operation(reference, limit=limit)
        if not isinstance(result, CalendarSeriesHistory):
            raise ConfigurationError("The calendar connection returned invalid occurrence history.")
        if (
            result.limit_per_list != limit
            or len(result.modified_occurrences) > limit
            or len(result.deleted_occurrences) > limit
        ):
            raise ConfigurationError(
                "The calendar connection exceeded the occurrence history limit."
            )
        return result

    def agenda(
        self,
        *,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        days: int = 30,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> CalendarView:
        zone = validate_timezone(self.display_timezone)
        resolved_start = self._day_start(start or datetime.now(zone).date())
        if end is None:
            if days <= 0:
                raise ConfigurationError("Agenda days must be positive.")
            resolved_end = self._day_start(resolved_start.date() + timedelta(days=days))
        else:
            resolved_end = self._exclusive_end(end)
        return self._view(
            CalendarViewKind.AGENDA,
            resolved_start,
            resolved_end,
            calendar_reference=calendar_reference,
            limit=limit,
        )

    def upcoming(
        self,
        *,
        now: datetime | None = None,
        days: int = 30,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> CalendarView:
        if days <= 0:
            raise ConfigurationError("Upcoming days must be positive.")
        zone = validate_timezone(self.display_timezone)
        resolved_start = localize_datetime(now or datetime.now(zone), self.display_timezone)
        resolved_end = self._day_start(resolved_start.date() + timedelta(days=days + 1))
        return self._view(
            CalendarViewKind.UPCOMING,
            resolved_start,
            resolved_end,
            calendar_reference=calendar_reference,
            limit=limit,
        )

    def day(
        self,
        value: date | datetime | None = None,
        *,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> CalendarView:
        anchor = value or datetime.now(validate_timezone(self.display_timezone)).date()
        start = self._day_start(anchor)
        end = self._day_start(start.date() + timedelta(days=1))
        return self._view(
            CalendarViewKind.DAY,
            start,
            end,
            calendar_reference=calendar_reference,
            limit=limit,
        )

    def week(
        self,
        value: date | datetime | None = None,
        *,
        first_weekday: int = 0,
        work_week: bool = False,
        working_days: Sequence[int] = (0, 1, 2, 3, 4),
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> CalendarView:
        if first_weekday not in range(7):
            raise ConfigurationError("First weekday must be between 0 (Monday) and 6 (Sunday).")
        anchor = _as_local_date(
            value or datetime.now(validate_timezone(self.display_timezone)), self.display_timezone
        )
        delta = (anchor.weekday() - first_weekday) % 7
        first = anchor - timedelta(days=delta)
        if work_week and (
            not working_days
            or any(
                isinstance(day, bool) or not isinstance(day, int) or day not in range(7)
                for day in working_days
            )
            or len(set(working_days)) != len(working_days)
        ):
            raise ConfigurationError("Choose one or more distinct working weekdays from 0 to 6.")
        start = self._day_start(first)
        end = self._day_start(first + timedelta(days=7))
        view = self._view(
            CalendarViewKind.WORK_WEEK if work_week else CalendarViewKind.WEEK,
            start,
            end,
            calendar_reference=calendar_reference,
            limit=limit,
        )
        if not work_week:
            return view
        days = tuple(bucket for bucket in view.days if bucket.date.weekday() in working_days)
        references = {event.reference for bucket in days for event in bucket.events}
        return replace(
            view,
            days=days,
            events=tuple(event for event in view.events if event.reference in references),
        )

    def month(
        self,
        value: date | datetime | None = None,
        *,
        include_adjacent_days: bool = False,
        first_weekday: int = 0,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> CalendarView:
        if first_weekday not in range(7):
            raise ConfigurationError("First weekday must be between 0 (Monday) and 6 (Sunday).")
        anchor = _as_local_date(
            value or datetime.now(validate_timezone(self.display_timezone)), self.display_timezone
        )
        first = anchor.replace(day=1)
        last = date(
            anchor.year, anchor.month, month_calendar.monthrange(anchor.year, anchor.month)[1]
        )
        if include_adjacent_days:
            first -= timedelta(days=(first.weekday() - first_weekday) % 7)
            last += timedelta(days=(first_weekday - 1 - last.weekday()) % 7)
        start = self._day_start(first)
        end = self._day_start(last + timedelta(days=1))
        view = self._view(
            CalendarViewKind.MONTH,
            start,
            end,
            calendar_reference=calendar_reference,
            limit=limit,
        )
        if not include_adjacent_days:
            return view
        marked = tuple(
            replace(bucket, in_primary_range=bucket.date.month == anchor.month)
            for bucket in view.days
        )
        return replace(view, days=marked)

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> tuple[CalendarEvent, ...]:
        start = localize_datetime(start, self.display_timezone)
        end = localize_datetime(end, self.display_timezone)
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise ConfigurationError("Calendar query end must be after its start.")
        result_limit = self._limit(limit)
        events = self.gateway.list_events(
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            calendar_reference=calendar_reference or self.default_calendar,
            limit=result_limit,
        )
        normalized = tuple(normalize_event(event) for event in events)
        return tuple(
            sorted(
                normalized,
                key=lambda event: (
                    event.start.astimezone(UTC),
                    event.end.astimezone(UTC),
                    event.subject.casefold(),
                    event.reference,
                ),
            )
        )

    def conflicts(
        self,
        candidate: CalendarEvent,
        *,
        existing: Iterable[CalendarEvent] | None = None,
        calendar_reference: str | None = None,
        limit: int | None = None,
    ) -> tuple[CalendarConflict, ...]:
        candidate = normalize_event(candidate)
        if existing is None:
            existing = self.list_events(
                start=candidate.start,
                end=candidate.end,
                calendar_reference=calendar_reference,
                limit=limit,
            )
        return tuple(
            conflict
            for conflict in detect_conflicts((*tuple(existing), candidate))
            if conflict.first.reference == candidate.reference
            or conflict.second.reference == candidate.reference
        )

    def plan_create(
        self,
        event: CalendarEvent,
        *,
        calendar_reference: str | None = None,
        invitations: InvitationPolicy = InvitationPolicy.SEND,
        require_no_conflicts: bool = False,
        attachment_paths: Sequence[Path] = (),
    ) -> EventCreatePlan:
        event = normalize_event(event)
        if require_no_conflicts:
            clashes = self.conflicts(
                event,
                calendar_reference=calendar_reference,
            )
            if clashes:
                raise ConflictError(
                    f"The event overlaps {len(clashes)} existing blocking event(s)."
                )
        return EventCreatePlan(
            event=event,
            calendar_reference=calendar_reference or self.default_calendar,
            invitations=invitations,
            attachment_paths=_validated_attachment_paths(attachment_paths),
        )

    def create(
        self,
        event: CalendarEvent,
        *,
        calendar_reference: str | None = None,
        invitations: InvitationPolicy = InvitationPolicy.SEND,
        require_no_conflicts: bool = False,
        attachment_paths: Sequence[Path] = (),
    ) -> CalendarEvent:
        return self.gateway.create_event(
            self.plan_create(
                event,
                calendar_reference=calendar_reference,
                invitations=invitations,
                require_no_conflicts=require_no_conflicts,
                attachment_paths=attachment_paths,
            )
        )

    def plan_update(
        self,
        reference: str,
        patch: CalendarPatch,
        *,
        current: CalendarEvent | None = None,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
        invitations: InvitationPolicy = InvitationPolicy.SEND_CHANGED,
        attachment_paths: Sequence[Path] = (),
        remove_attachment_references: Sequence[str] = (),
    ) -> EventUpdatePlan:
        if not reference.strip():
            raise ConfigurationError("An event reference is required.")
        changes = patch.changes()
        paths = _validated_attachment_paths(attachment_paths)
        removals = _validated_attachment_references(remove_attachment_references)
        if not changes and not paths and not removals:
            raise ConfigurationError("No event fields or attachment changes were supplied.")
        # A partial patch cannot validate start/end, all-day, timezone, or recurrence
        # coherence in isolation.  Resolve current state when callers (notably the CLI)
        # do not already have it, then produce a strict explicit field allowlist.
        current = self.get(reference) if current is None else normalize_event(current)
        scope = _mutation_scope(scope)
        _validate_mutation_scope(current, scope, changes)
        if scope is RecurrenceScope.SERIES and _is_occurrence(current):
            current = self.get(reference, scope=scope)
            reference = current.reference
        updated = normalize_event(replace(current, **changes))
        changes = {name: getattr(updated, name) for name in changes}
        return EventUpdatePlan(
            reference=reference,
            changes=dict(changes),
            scope=scope,
            invitations=invitations,
            attachment_paths=paths,
            remove_attachment_references=removals,
        )

    def update(
        self,
        reference: str,
        patch: CalendarPatch,
        *,
        current: CalendarEvent | None = None,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
        invitations: InvitationPolicy = InvitationPolicy.SEND_CHANGED,
        attachment_paths: Sequence[Path] = (),
        remove_attachment_references: Sequence[str] = (),
    ) -> CalendarEvent:
        return self.gateway.update_event(
            self.plan_update(
                reference,
                patch,
                current=current,
                scope=scope,
                invitations=invitations,
                attachment_paths=attachment_paths,
                remove_attachment_references=remove_attachment_references,
            )
        )

    def plan_delete(
        self,
        reference: str,
        *,
        current: CalendarEvent | None = None,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
        send_cancellation: bool | None = None,
        reason: str | None = None,
    ) -> EventDeletePlan:
        if not reference.strip():
            raise ConfigurationError("An event reference is required.")
        scope = _mutation_scope(scope)
        if current is not None:
            _validate_mutation_scope(current, scope)
        inferred_cancellation = bool(current and current.is_meeting)
        cancellation = inferred_cancellation if send_cancellation is None else send_cancellation
        if reason is not None:
            reason = reason.strip() or None
        return EventDeletePlan(reference, scope, cancellation, reason)

    def delete(self, plan: EventDeletePlan) -> None:
        self.gateway.delete_event(plan)

    def plan_response(
        self,
        event: CalendarEvent,
        response: CalendarResponse | str,
        *,
        send_response: bool = True,
        comment: str | None = None,
        proposed_start: datetime | None = None,
        proposed_end: datetime | None = None,
    ) -> MeetingResponsePlan:
        event = normalize_event(event)
        if not event.is_meeting:
            raise ConfigurationError(
                "Only meetings can be accepted, tentatively accepted, or declined."
            )
        if (
            event.response_status
            and event.response_status.rsplit(".", 1)[-1].casefold() == "organizer"
        ):
            raise ConfigurationError("The meeting organizer cannot respond to their own meeting.")
        try:
            response_value = CalendarResponse(response)
        except ValueError as exc:
            raise ConfigurationError(f"Unknown meeting response: {response!r}") from exc
        if (proposed_start is None) != (proposed_end is None):
            raise ConfigurationError("A proposed time requires both a start and an end.")
        if proposed_start is not None and proposed_end is not None:
            if response_value is CalendarResponse.DECLINE:
                raise ConfigurationError(
                    "A declined meeting response cannot also propose a new time."
                )
            proposed_start = localize_datetime(proposed_start, event.timezone)
            proposed_end = localize_datetime(proposed_end, event.timezone)
            if proposed_end.astimezone(UTC) <= proposed_start.astimezone(UTC):
                raise ConfigurationError("A proposed meeting end must be after its start.")
        if comment is not None:
            comment = comment.strip() or None
            if comment and len(comment) > 10_000:
                raise ConfigurationError(
                    "Meeting response comments are limited to 10,000 characters."
                )
        return MeetingResponsePlan(
            reference=event.reference,
            response=response_value,
            send_response=send_response,
            comment=comment,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
        )

    def respond(self, plan: MeetingResponsePlan) -> CalendarEvent | None:
        return self.gateway.respond_to_meeting(plan)

    def import_ics(
        self,
        value: str,
        *,
        calendar_reference: str | None = None,
        invitations: InvitationPolicy = InvitationPolicy.SEND_NONE,
    ) -> CalendarImportPlan:
        if len(value.encode("utf-8")) > 5 * 1024 * 1024:
            raise ConfigurationError("Calendar ICS imports are limited to 5 MiB.")
        events = tuple(
            normalize_event(event)
            for event in parse_ics(value, default_timezone=self.display_timezone)
        )
        if len(events) > MAX_PIM_PAGE_SIZE:
            raise ConfigurationError(
                f"Calendar ICS imports are limited to {MAX_PIM_PAGE_SIZE} events."
            )
        warnings: list[str] = []
        if any(event.is_meeting for event in events) and invitations is InvitationPolicy.SEND_NONE:
            warnings.append("Meeting attendees will not receive invitations during this import.")
        return CalendarImportPlan(
            events=events,
            calendar_reference=calendar_reference or self.default_calendar,
            invitations=invitations,
            warnings=tuple(warnings),
        )

    def apply_import(self, plan: CalendarImportPlan) -> tuple[CalendarEvent, ...]:
        created: list[CalendarEvent] = []
        for event in plan.events:
            created.append(
                self.gateway.create_event(
                    EventCreatePlan(event, plan.calendar_reference, plan.invitations)
                )
            )
        return tuple(created)

    def apply_import_result(
        self,
        plan: CalendarImportPlan,
        *,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[CalendarEvent]:
        """Apply a bounded import and report every completed, failed, or skipped step.

        Calendar creates are not retried because a transport timeout may have occurred
        after Exchange accepted the event.  The caller can reconcile the returned
        references before deciding whether a retry is safe.
        """

        completed: list[BatchSuccess[CalendarEvent]] = []
        failures: list[BatchFailure] = []
        skipped: list[str] = []
        for index, event in enumerate(plan.events):
            try:
                created = self.gateway.create_event(
                    EventCreatePlan(event, plan.calendar_reference, plan.invitations)
                )
            except Exception as exc:
                failures.append(
                    BatchFailure.from_exception(
                        index=index,
                        operation="create calendar event",
                        reference=event.reference,
                        error=exc,
                        retry_safe=False,
                    )
                )
                if not continue_on_error:
                    skipped.extend(item.reference for item in plan.events[index + 1 :])
                    break
            else:
                normalized = normalize_event(created)
                completed.append(
                    BatchSuccess(
                        index=index,
                        operation="create calendar event",
                        reference=normalized.reference,
                        value=normalized,
                    )
                )
        return BatchOperationResult(
            attempted=len(plan.events),
            completed=tuple(completed),
            failures=tuple(failures),
            skipped_references=tuple(skipped),
        )

    def export_ics(self, events: CalendarEvent | Iterable[CalendarEvent]) -> str:
        if isinstance(events, CalendarEvent):
            return render_ics(normalize_event(events))
        return render_ics(tuple(normalize_event(event) for event in events))

    def export_event(self, reference: str) -> str:
        return self.export_ics(self.get(reference))

    def query_view(
        self,
        *,
        kind: CalendarViewKind,
        anchor: date | datetime | None = None,
        first_weekday: int = 0,
        working_days: Sequence[int] = (0, 1, 2, 3, 4),
        agenda_days: int = 30,
        limit: int | None = None,
        query: str = "",
        cursor: CalendarCursor | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CalendarView:
        """A bounded search with explicit continuation over the entire date window."""
        if first_weekday not in range(7) or agenda_days <= 0:
            raise ConfigurationError("Invalid calendar weekday or agenda range.")
        if not working_days or any(day not in range(7) for day in working_days):
            raise ConfigurationError("Choose working weekdays from 0 to 6.")
        day = _as_local_date(
            anchor or datetime.now(ZoneInfo(self.display_timezone)), self.display_timezone
        )
        if kind is CalendarViewKind.MONTH:
            first = day.replace(day=1)
            last = day.replace(day=month_calendar.monthrange(day.year, day.month)[1])
            first -= timedelta(days=(first.weekday() - first_weekday) % 7)
            last += timedelta(days=(first_weekday - 1 - last.weekday()) % 7)
            start, end = self._day_start(first), self._day_start(last + timedelta(days=1))
        elif kind in {CalendarViewKind.WEEK, CalendarViewKind.WORK_WEEK}:
            first = day - timedelta(days=(day.weekday() - first_weekday) % 7)
            start, end = self._day_start(first), self._day_start(first + timedelta(days=7))
        else:
            start = self._day_start(day)
            end = self._day_start(
                day + timedelta(days=1 if kind is CalendarViewKind.DAY else agenda_days)
            )
        page = query_calendar_window(
            self.gateway,
            start=start,
            end=end,
            query=query,
            cursor=cursor,
            calendar_reference=self.default_calendar,
            limit=self._limit(limit),
            cancelled=cancelled,
            working_days=tuple(working_days) if kind is CalendarViewKind.WORK_WEEK else None,
            timezone=self.display_timezone,
        )
        view = self._build_view(kind, start, end, page.items)
        if kind is CalendarViewKind.MONTH:
            view = replace(
                view,
                days=tuple(
                    replace(bucket, in_primary_range=bucket.date.month == day.month)
                    for bucket in view.days
                ),
            )
        if kind is CalendarViewKind.WORK_WEEK:
            view = replace(
                view,
                days=tuple(bucket for bucket in view.days if bucket.date.weekday() in working_days),
            )
        return replace(
            view,
            complete=page.complete,
            continuation=page.cursor,
            notice=page.notice,
            examined_candidates=page.examined_candidates,
        )

    def _view(
        self,
        kind: CalendarViewKind,
        start: datetime,
        end: datetime,
        *,
        calendar_reference: str | None,
        limit: int | None,
    ) -> CalendarView:
        events = self.list_events(
            start=start,
            end=end,
            calendar_reference=calendar_reference,
            limit=limit,
        )
        return replace(
            self._build_view(kind, start, end, events),
            complete=len(events) < self._limit(limit),
            notice="This bounded calendar view may omit later events."
            if len(events) >= self._limit(limit)
            else None,
        )

    def _build_view(
        self,
        kind: CalendarViewKind,
        start: datetime,
        end: datetime,
        events: tuple[CalendarEvent, ...],
    ) -> CalendarView:
        buckets: list[CalendarDay] = []
        current = start.date()
        while current < end.date() or (current == start.date() and start.date() == end.date()):
            day_start = self._day_start(current)
            day_end = self._day_start(current + timedelta(days=1))
            day_events = tuple(
                event
                for event in events
                if event.start.astimezone(UTC) < day_end.astimezone(UTC)
                and event.end.astimezone(UTC) > day_start.astimezone(UTC)
            )
            buckets.append(CalendarDay(current, day_events))
            current += timedelta(days=1)
        return CalendarView(kind, self.display_timezone, start, end, tuple(buckets), events)

    def _day_start(self, value: date | datetime) -> datetime:
        local_date = _as_local_date(value, self.display_timezone)
        # A view boundary is the first real instant of the local day. Some zones
        # (for example Africa/Cairo) skip local midnight when daylight saving
        # starts; user-entered wall times still reject such gaps, but a window
        # covering that date must not fail. fold=0 maps a skipped midnight to the
        # pre-transition offset, which normalizes to the first existing instant.
        zone = validate_timezone(self.display_timezone)
        midnight = datetime.combine(local_date, time.min).replace(tzinfo=zone)
        return midnight.astimezone(UTC).astimezone(zone)

    def _exclusive_end(self, value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return localize_datetime(value, self.display_timezone)
        return self._day_start(value + timedelta(days=1))

    def _limit(self, value: int | None) -> int:
        return bounded_pim_limit(
            value,
            default=self.default_limit,
            subject="Calendar",
        )


def _mutation_scope(scope: object) -> RecurrenceScope:
    if not isinstance(scope, str):
        raise UnsupportedFeatureError("Choose this occurrence or the entire series.")
    try:
        return RecurrenceScope(scope)
    except ValueError as exc:
        raise UnsupportedFeatureError(
            "Choose this occurrence or the entire series; this-and-future edits are unavailable."
        ) from exc


def _is_occurrence(event: CalendarEvent) -> bool:
    return (event.item_type or "").casefold() in {"occurrence", "exception"}


def _validate_mutation_scope(
    event: CalendarEvent,
    scope: RecurrenceScope,
    changes: Mapping[str, object] | None = None,
) -> None:
    if scope is RecurrenceScope.OCCURRENCE:
        if (event.item_type or "").casefold() == "recurringmaster":
            raise ConfigurationError("Choose entire series to change a recurring series master.")
        if _is_occurrence(event) and changes and "recurrence" in changes:
            raise ConfigurationError("Change the repetition pattern with entire-series scope.")


def _as_local_date(value: date | datetime, timezone: str) -> date:
    if isinstance(value, datetime):
        return localize_datetime(value, timezone).date()
    return value


def _validated_attachment_paths(values: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in values:
        path = Path(raw).expanduser().resolve()
        if path in seen:
            continue
        if not path.is_file():
            raise ConfigurationError(f"Calendar attachment is not a regular file: {path.name}")
        result.append(path)
        seen.add(path)
    return tuple(result)


def _validated_attachment_references(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        reference = str(raw).strip()
        if not reference or any(character in reference for character in "\r\n\x00"):
            raise ConfigurationError("Calendar attachment references may not be empty.")
        if reference not in result:
            result.append(reference)
    return tuple(result)


def calendar_event_search_text(event: CalendarEvent) -> str:
    """Return bounded provider-neutral fields used by Calendar view filtering."""

    participants = (
        *((event.organizer,) if event.organizer else ()),
        *event.required_attendees,
        *event.optional_attendees,
        *event.resources,
    )
    return " ".join(
        (
            event.subject,
            event.location or "",
            event.body,
            " ".join(event.categories),
            " ".join(
                " ".join(part for part in (person.name, person.email) if part)
                for person in participants
            ),
            " ".join(item.name for item in event.attachments),
        )
    )


__all__ = [
    "UNSET",
    "CalendarConflict",
    "CalendarDay",
    "CalendarGateway",
    "CalendarImportPlan",
    "CalendarPatch",
    "CalendarService",
    "CalendarView",
    "CalendarViewKind",
    "EventCreatePlan",
    "EventDeletePlan",
    "EventUpdatePlan",
    "InvitationPolicy",
    "MeetingResponsePlan",
    "RecurrenceScope",
    "calendar_event_search_text",
    "detect_conflicts",
    "localize_datetime",
    "normalize_event",
    "validate_timezone",
]
