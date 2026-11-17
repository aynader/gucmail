"""Local Calendar view policy and presentation without rewriting stored events."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from gucmail.errors import ConfigurationError
from gucmail.models import CalendarEvent, to_primitive
from gucmail.services.calendar import (
    CalendarService,
    CalendarView,
    CalendarViewKind,
    validate_timezone,
)
from gucmail.services.calendar_paging import CalendarCursor

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

DISPLAY_SETTINGS = ("calendar.default_view", "calendar.week_start", "calendar.timezone")
WEEK_STARTS = {"monday": 0, "saturday": 5, "sunday": 6}


@dataclass(frozen=True, slots=True)
class CalendarDisplayPreferences:
    default_view: CalendarViewKind = CalendarViewKind.AGENDA
    first_weekday: int = 5
    timezone: str = "Africa/Cairo"

    def __post_init__(self) -> None:
        try:
            kind = CalendarViewKind(self.default_view)
        except ValueError as exc:
            raise ConfigurationError("Choose agenda, day, work week, week or month.") from exc
        if kind is CalendarViewKind.UPCOMING:
            raise ConfigurationError("Upcoming is not a configurable default Calendar view.")
        if (
            isinstance(self.first_weekday, bool)
            or not isinstance(self.first_weekday, int)
            or self.first_weekday not in range(7)
        ):
            raise ConfigurationError("Calendar week start must be a weekday from 0 to 6.")
        validate_timezone(self.timezone)
        object.__setattr__(self, "default_view", kind)

    @classmethod
    def from_settings(cls, service: SettingsService) -> CalendarDisplayPreferences:
        values = {
            row.spec.setting_id: row.effective_value
            for row in service.snapshot(DISPLAY_SETTINGS).values
        }
        try:
            first = WEEK_STARTS[str(values["calendar.week_start"])]
        except KeyError as exc:
            raise ConfigurationError(
                "Choose Monday, Saturday or Sunday as Calendar week start."
            ) from exc
        return cls(
            CalendarViewKind(str(values["calendar.default_view"])),
            first,
            str(values["calendar.timezone"]),
        )


def load_calendar_view(
    service: CalendarService,
    *,
    kind: CalendarViewKind,
    anchor: date | datetime | None = None,
    first_weekday: int = 5,
    working_days: Sequence[int] = (0, 1, 2, 3, 4),
    agenda_days: int = 14,
    limit: int = 200,
    query: str = "",
    cursor: CalendarCursor | None = None,
    cancelled: Callable[[], bool] | None = None,
    paged: bool = False,
) -> CalendarView:
    """One bounded query, with complete aligned weeks in the month grid."""

    if paged:
        return service.query_view(
            kind=kind,
            anchor=anchor,
            first_weekday=first_weekday,
            working_days=working_days,
            agenda_days=agenda_days,
            limit=limit,
            query=query,
            cursor=cursor,
            cancelled=cancelled,
        )
    if kind is CalendarViewKind.DAY:
        return service.day(anchor, limit=limit)
    if kind in (CalendarViewKind.WEEK, CalendarViewKind.WORK_WEEK):
        return service.week(
            anchor,
            first_weekday=first_weekday,
            work_week=kind is CalendarViewKind.WORK_WEEK,
            working_days=working_days,
            limit=limit,
        )
    if kind is CalendarViewKind.MONTH:
        return service.month(
            anchor, include_adjacent_days=True, first_weekday=first_weekday, limit=limit
        )
    return service.agenda(start=anchor, days=agenda_days, limit=limit)


def event_time_labels(event: CalendarEvent, timezone: str) -> tuple[str, str]:
    """Timed instants use the display zone; all-day dates keep their event zone."""

    zone = validate_timezone(timezone)
    if event.is_all_day:
        source_zone = validate_timezone(event.timezone)
        return (
            event.start.astimezone(source_zone).date().isoformat() + " (all day)",
            event.end.astimezone(source_zone).date().isoformat() + " (exclusive)",
        )
    return (
        event.start.astimezone(zone).isoformat(sep=" ", timespec="minutes"),
        event.end.astimezone(zone).isoformat(sep=" ", timespec="minutes"),
    )


def friendly_event_times(
    event: CalendarEvent, timezone: str, *, now: datetime | None = None
) -> tuple[str, str]:
    """Short screen labels, such as ("Today 10:00", "11:30") or ("Mon", "All day")."""

    from datetime import timedelta

    from gucmail.formatting import friendly_time

    if event.is_all_day:
        zone = event.timezone
        start = friendly_time(event.start, timezone=zone, now=now, date_only=True)
        last = event.end - timedelta(days=1)
        if (
            last.astimezone(validate_timezone(zone)).date()
            > event.start.astimezone(validate_timezone(zone)).date()
        ):
            return start, "Until " + friendly_time(last, timezone=zone, now=now, date_only=True)
        return start, "All day"
    zone_info = validate_timezone(timezone)
    start = friendly_time(event.start, timezone=timezone, now=now)
    if event.end.astimezone(zone_info).date() == event.start.astimezone(zone_info).date():
        return start, event.end.astimezone(zone_info).strftime("%H:%M")
    return start, friendly_time(event.end, timezone=timezone, now=now)


def event_display_data(event: CalendarEvent, timezone: str) -> dict[str, Any]:
    """A human-output copy; original domain values remain the JSON/export contract."""

    result = dict(to_primitive(event))
    result["stored_start"], result["stored_end"] = result["start"], result["end"]
    result["start"], result["end"] = event_time_labels(event, timezone)
    result["display_timezone"] = timezone
    return result


def calendar_view_rows(view: CalendarView) -> list[dict[str, object]]:
    return [
        {
            "reference": event.reference,
            "start": event_time_labels(event, view.timezone)[0],
            "end": event_time_labels(event, view.timezone)[1],
            "subject": event.subject,
            "location": event.location,
            "display_zone": view.timezone,
            "event_zone": event.timezone,
        }
        for event in view.events
    ]
