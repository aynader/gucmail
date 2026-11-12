"""Bounded CalendarView continuation without pretending it supports EWS offsets.

Split saturated time intervals. Each event belongs to the interval containing
its start (or the first interval for an event already in progress), so overlap
queries do not duplicate long events. Unresolvable dense intervals stay explicit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from gucmail.errors import ConfigurationError
from gucmail.models import CalendarEvent


class CalendarReader(Protocol):
    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> Sequence[CalendarEvent]: ...


@dataclass(frozen=True, slots=True)
class CalendarInterval:
    start: datetime
    end: datetime
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CalendarCursor:
    fingerprint: str
    pending: tuple[CalendarInterval, ...]
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class CalendarPage:
    items: tuple[CalendarEvent, ...]
    cursor: CalendarCursor | None
    complete: bool
    notice: str
    examined_candidates: int = 0


def query_calendar_window(
    gateway: CalendarReader,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    query: str = "",
    calendar_reference: str | None = None,
    cursor: CalendarCursor | None = None,
    cancelled: Callable[[], bool] | None = None,
    working_days: tuple[int, ...] | None = None,
    timezone: str = "UTC",
    request_budget: int = 4,
) -> CalendarPage:
    """Search before the result limit, continuing even after an empty scan page.

    At most four 200-candidate provider reads happen per default call. A dense
    one-second interval may exceed the provider's view bound; it is reported as
    incomplete rather than manufacturing an offset the provider cannot honor.
    """
    from gucmail.services.calendar import calendar_event_search_text, normalize_event

    if start.tzinfo is None or end.tzinfo is None:
        raise ConfigurationError("Calendar window bounds must include a timezone.")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start or not 1 <= limit <= 200 or not 1 <= request_budget <= 16:
        raise ConfigurationError("Calendar window or page budget is outside safe bounds.")
    if len(query) > 1024:
        raise ConfigurationError("Calendar search text must be at most 1024 characters.")
    text = query.strip().casefold()
    fingerprint = hashlib.sha256(
        json.dumps(
            (start.isoformat(), end.isoformat(), text, calendar_reference, working_days, timezone)
        ).encode()
    ).hexdigest()
    if cursor is not None:
        if cursor.fingerprint != fingerprint or not 1 <= len(cursor.pending) <= 64:
            raise ConfigurationError(
                "Calendar results changed; refresh this view before continuing."
            )
        if any(
            not start <= item.start < item.end <= end or not 0 <= item.offset <= 200
            for item in cursor.pending
        ):
            raise ConfigurationError("Calendar continuation is outside the requested window.")
    pending = list(cursor.pending if cursor else (CalendarInterval(start, end),))
    incomplete = cursor.incomplete if cursor else False
    matches: list[CalendarEvent] = []
    requests = 0
    examined = 0

    def wanted_day(event: CalendarEvent) -> bool:
        if working_days is None:
            return True
        zone = ZoneInfo(timezone)
        day = max(start, event.start.astimezone(UTC)).astimezone(zone).date()
        last = (
            (min(end, event.end.astimezone(UTC)) - timedelta(microseconds=1))
            .astimezone(zone)
            .date()
        )
        return any(
            (day + timedelta(days=offset)).weekday() in working_days
            for offset in range((last - day).days + 1)
        )

    while pending and requests < request_budget and len(matches) < limit:
        if cancelled is not None and cancelled():
            raise InterruptedError("Calendar search cancelled before the next provider request.")
        interval = pending.pop(0)
        values = gateway.list_events(
            start=interval.start,
            end=interval.end,
            calendar_reference=calendar_reference,
            limit=200,
        )
        requests += 1
        # Defend against a provider that ignores the requested bound.
        if len(values) > 200:
            incomplete = True
        normalized = [normalize_event(event) for event in values[:200]]
        events = sorted(
            {event.reference: event for event in normalized}.values(),
            key=lambda event: (
                event.start.astimezone(UTC),
                event.end.astimezone(UTC),
                event.subject.casefold(),
                event.reference,
            ),
        )
        examined += len(events)
        saturated = len(values) >= 200
        if (
            saturated
            and interval.offset == 0
            and interval.end - interval.start > timedelta(seconds=1)
        ):
            middle = interval.start + (interval.end - interval.start) / 2
            pending[:0] = [
                CalendarInterval(interval.start, middle),
                CalendarInterval(middle, interval.end),
            ]
            continue
        if saturated:
            incomplete = True
        for index in range(interval.offset, len(events)):
            if cancelled is not None and cancelled():
                raise InterruptedError("Calendar search cancelled while matching candidates.")
            event = events[index]
            event_start, event_end = event.start.astimezone(UTC), event.end.astimezone(UTC)
            owned = interval.start <= event_start < interval.end or (
                interval.start == start and event_start < start < event_end
            )
            if not owned or not event_start < end or event_end <= start:
                continue
            if wanted_day(event) and (
                not text or text in calendar_event_search_text(event).casefold()
            ):
                matches.append(event)
                if len(matches) == limit:
                    if index + 1 < len(events):
                        pending.insert(0, CalendarInterval(interval.start, interval.end, index + 1))
                    break
    continuation = CalendarCursor(fingerprint, tuple(pending), incomplete) if pending else None
    complete = not pending and not incomplete
    notice = f"{len(matches)} event{'s' if len(matches) != 1 else ''}"
    if pending:
        notice += " · More results available"
    if incomplete:
        notice += " · Incomplete: too many simultaneous events"
    return CalendarPage(tuple(matches), continuation, complete, notice, examined)
