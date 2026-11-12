"""Local working-time preferences and conservative shared free-time selection."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError
from gucmail.models import Address
from gucmail.services.calendar import localize_datetime, validate_timezone

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WORKING_TIME_SETTINGS = (
    "calendar.working_days",
    "calendar.workday_start",
    "calendar.workday_end",
)
MAX_AVAILABILITY_DAYS = 31


def normalize_working_days(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ConfigurationError("Choose at least one working day by its full weekday name.")
    if not all(isinstance(day, str) and day.strip().casefold() in WEEKDAYS for day in value):
        raise ConfigurationError("Working days must use full names from Monday to Sunday.")
    normalized = tuple(day.strip().casefold() for day in value)
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("A working day may appear only once.")
    return tuple(day for day in WEEKDAYS if day in normalized)


def normalize_workday_time(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value.strip()):
        raise ConfigurationError("Working hours must use local 24-hour HH:MM, such as 09:00.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class CalendarWorkingTime:
    days: tuple[str, ...] = WEEKDAYS[:5]
    start: str = "09:00"
    end: str = "17:00"

    def __post_init__(self) -> None:
        object.__setattr__(self, "days", normalize_working_days(self.days))
        object.__setattr__(self, "start", normalize_workday_time(self.start))
        object.__setattr__(self, "end", normalize_workday_time(self.end))
        if self.end <= self.start:
            raise ConfigurationError(
                "Working-hour end must follow start on the same local day. "
                "Use all-hours free-time search for an overnight window."
            )

    @classmethod
    def from_settings(cls, settings: SettingsService) -> CalendarWorkingTime:
        values = {
            item.spec.setting_id: item.effective_value
            for item in settings.snapshot(WORKING_TIME_SETTINGS).values
        }
        return cls(
            values[WORKING_TIME_SETTINGS[0]],
            values[WORKING_TIME_SETTINGS[1]],
            values[WORKING_TIME_SETTINGS[2]],
        )

    @property
    def weekdays(self) -> tuple[int, ...]:
        return tuple(WEEKDAYS.index(day) for day in self.days)

    @property
    def description(self) -> str:
        return f"{', '.join(day[:3].title() for day in self.days)} · {self.start}-{self.end} local"

    def window(self, day: date, timezone: str) -> tuple[datetime, datetime] | None:
        if day.weekday() not in self.weekdays:
            return None
        # Existing calendar localization rejects ambiguous and missing DST wall times.
        start = localize_datetime(datetime.combine(day, time.fromisoformat(self.start)), timezone)
        end = localize_datetime(datetime.combine(day, time.fromisoformat(self.end)), timezone)
        return start.astimezone(UTC), end.astimezone(UTC)

    def windows(
        self, start: datetime, end: datetime, timezone: str
    ) -> tuple[tuple[datetime, datetime], ...]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ConfigurationError("Working windows need timestamps with UTC offsets.")
        elapsed = end.astimezone(UTC) - start.astimezone(UTC)
        if not timedelta(0) < elapsed <= timedelta(days=MAX_AVAILABILITY_DAYS):
            raise ConfigurationError("Working-window search must span at most 31 elapsed days.")
        zone = validate_timezone(timezone)
        current = start.astimezone(zone).date()
        last = end.astimezone(zone).date()
        values = []
        while current <= last:
            window = self.window(current, timezone)
            if window is not None and window[0] < end and window[1] > start:
                values.append(window)
            if current == last:
                break
            current += timedelta(days=1)
        return tuple(values)


def validate_free_time_request(
    attendees: Sequence[Address],
    start: datetime,
    end: datetime,
    interval_minutes: int,
    duration_minutes: int,
) -> None:
    if not attendees or len(attendees) > 100:
        raise ConfigurationError("Availability requires between 1 and 100 attendees.")
    addresses = [attendee.email.strip().casefold() for attendee in attendees]
    if len(set(addresses)) != len(addresses):
        raise ConfigurationError("Each attendee must appear only once in an availability request.")
    if start.tzinfo is None or end.tzinfo is None or end.astimezone(UTC) <= start.astimezone(UTC):
        raise ConfigurationError("Availability needs an aware, increasing time window.")
    if end.astimezone(UTC) - start.astimezone(UTC) > timedelta(days=MAX_AVAILABILITY_DAYS):
        raise ConfigurationError("Free-time search is bounded to at most 31 elapsed days.")
    for label, value in (
        ("Availability interval", interval_minutes),
        ("Meeting duration", duration_minutes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 1440:
            raise ConfigurationError(f"{label} must be a whole number between 5 and 1440 minutes.")


@dataclass(frozen=True, slots=True)
class FreeTimeSuggestion:
    start: datetime | None
    end: datetime | None
    complete: bool
    reason: str


def find_common_free_time(
    values: Sequence[Mapping[str, object]],
    *,
    attendees: Sequence[Address],
    start: datetime,
    end: datetime,
    interval_minutes: int = 30,
    duration_minutes: int = 60,
    timezone: str = "UTC",
    working_time: CalendarWorkingTime | None = None,
) -> FreeTimeSuggestion:
    """Require every requested attendee, every covered grid cell and the whole local window."""

    validate_free_time_request(attendees, start, end, interval_minutes, duration_minutes)
    zone = validate_timezone(timezone)
    window_start, window_end = start.astimezone(UTC), end.astimezone(UTC)
    windows = working_time.windows(window_start, window_end, timezone) if working_time else ()
    count = math.ceil((window_end - window_start).total_seconds() / (interval_minutes * 60))
    needed = math.ceil(duration_minutes / interval_minutes)
    merged_values: list[str] = []
    returned: list[str] = []
    for value in values:
        attendee, merged = value.get("attendee"), value.get("merged")
        if (
            value.get("available") is True
            and isinstance(attendee, (Address, str))
            and isinstance(merged, str)
            and len(merged) >= count
            and set(merged) <= set("01234")
        ):
            returned.append(
                (attendee.email if isinstance(attendee, Address) else attendee).strip().casefold()
            )
            merged_values.append(merged)
    expected = Counter(attendee.email.strip().casefold() for attendee in attendees)
    if len(values) != len(attendees) or Counter(returned) != expected:
        return FreeTimeSuggestion(
            None,
            None,
            False,
            "Missing, failed or incomplete attendee availability is not treated as free.",
        )
    busy = [False] * count
    for merged in merged_values:
        for index, state in enumerate(merged[:count]):
            busy[index] |= state != "0"
    prefix = [0]
    for busy_cell in busy:
        prefix.append(prefix[-1] + busy_cell)
    for index in range(max(0, count - needed + 1)):
        candidate_start = window_start + timedelta(minutes=index * interval_minutes)
        candidate_end = candidate_start + timedelta(minutes=duration_minutes)
        if candidate_end > window_end:
            break
        if working_time and not any(
            a <= candidate_start and candidate_end <= b for a, b in windows
        ):
            continue
        if prefix[index + needed] == prefix[index]:
            return FreeTimeSuggestion(
                candidate_start.astimezone(zone),
                candidate_end.astimezone(zone),
                True,
                "All attendees are free for every covered grid cell.",
            )
    return FreeTimeSuggestion(
        None, None, True, "No common free slot fits the requested window and working-time policy."
    )
