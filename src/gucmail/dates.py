"""Strict, explainable date parsing for schedules and search filters."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as date_parser
from dateutil import tz

from gucmail.errors import ConfigurationError

# The "in"/"after" prefix is optional: ``send --in "45 minutes"`` passes the bare
# delay. Without this, dateutil's fallback silently read "45 minutes" as 00:45
# today and "20 days" as the 20th of the current month.
RELATIVE_RE = re.compile(
    r"^(?:(?:in|after)\s+)?(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)$",
    re.IGNORECASE,
)
DAY_RE = re.compile(
    r"^(?:(next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:at\s+)?(.+))?$",
    re.IGNORECASE,
)
TODAY_RE = re.compile(r"^(today|tomorrow)(?:\s+(?:at\s+)?(.+))?$", re.IGNORECASE)
# Delay-like text that RELATIVE_RE does not accept must never reach dateutil:
# it read ``--in 30`` as the 30th of this month and ``2 hours 30 minutes`` or
# ``1h`` as an early-morning wall time.
DURATION_LIKE_RE = re.compile(
    r"^[+-]?\s*\d+(?:[.,]\d+)?$"
    r"|(?:^|\s)[+-]?\d+(?:[.,]\d+)?\s*"
    r"(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?)(?:\s|$)",
    re.IGNORECASE,
)
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True, slots=True)
class ResolvedDateTime:
    original: str
    local: datetime
    utc: datetime
    timezone: str
    explanation: str


def _parse_clock(value: str | None, default: time = time(9, 0)) -> time:
    if not value:
        return default
    try:
        parsed = date_parser.parse(value, fuzzy=False)
    except (ValueError, OverflowError) as exc:
        raise ConfigurationError(f"Could not understand time: {value!r}") from exc
    return parsed.time().replace(tzinfo=None)


def _localize_strict(naive: datetime, zone_name: str) -> datetime:
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown time zone: {zone_name}") from exc
    dateutil_zone = tz.gettz(zone_name)
    if dateutil_zone is not None:
        tentative = naive.replace(tzinfo=dateutil_zone)
        if not tz.datetime_exists(tentative):
            raise ConfigurationError(
                f"{naive.isoformat(sep=' ')} does not exist in {zone_name} because of a "
                "daylight-saving transition. Choose another time."
            )
        if tz.datetime_ambiguous(tentative):
            raise ConfigurationError(
                f"{naive.isoformat(sep=' ')} occurs twice in {zone_name} because of a "
                "daylight-saving transition. Supply an explicit UTC offset."
            )
    return naive.replace(tzinfo=zone)


def parse_datetime_expression(
    expression: str,
    *,
    timezone: str,
    now: datetime | None = None,
    future_required: bool = False,
) -> ResolvedDateTime:
    """Parse a deliberately small natural-language grammar without ambiguous guesses."""

    original = expression.strip()
    if not original:
        raise ConfigurationError("A date and time are required.")
    if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", original):
        raise ConfigurationError(
            "Slash-separated dates are ambiguous. Use YYYY-MM-DD, for example 2027-04-03."
        )
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown time zone: {timezone}") from exc
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = _localize_strict(current, timezone)
    else:
        current = current.astimezone(zone)

    relative = RELATIVE_RE.fullmatch(original)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower().rstrip("s")
        keyword = {
            "second": "seconds",
            "sec": "seconds",
            "min": "minutes",
            "hr": "hours",
            "minute": "minutes",
            "hour": "hours",
            "day": "days",
            "week": "weeks",
        }[unit]
        local = current + timedelta(**{keyword: amount})
        noun = keyword[:-1]
        explanation = f"{amount} {noun}{'' if amount == 1 else 's'} after {current.isoformat()}"
    else:
        today_match = TODAY_RE.fullmatch(original)
        day_match = DAY_RE.fullmatch(original)
        if today_match:
            offset = 1 if today_match.group(1).lower() == "tomorrow" else 0
            target_date = current.date() + timedelta(days=offset)
            clock = _parse_clock(today_match.group(2))
            local = _localize_strict(datetime.combine(target_date, clock), timezone)
            explanation = f"{today_match.group(1).lower()} in {timezone}"
        elif day_match:
            force_next = bool(day_match.group(1))
            weekday_name = day_match.group(2).lower()
            days_ahead = (WEEKDAYS[weekday_name] - current.weekday()) % 7
            clock = _parse_clock(day_match.group(3))
            if force_next and days_ahead == 0:
                days_ahead = 7
            candidate = _localize_strict(
                datetime.combine(current.date() + timedelta(days=days_ahead), clock), timezone
            )
            if not force_next and candidate <= current:
                candidate = _localize_strict(
                    datetime.combine(candidate.date() + timedelta(days=7), clock), timezone
                )
            local = candidate
            explanation = f"next occurrence of {weekday_name.title()} in {timezone}"
        else:
            if DURATION_LIKE_RE.search(original):
                raise ConfigurationError(
                    f"{original!r} is not a supported date or delay. Use one whole-number delay "
                    "with a unit, for example 'in 45 minutes', or a full date and time such as "
                    "YYYY-MM-DD HH:MM."
                )
            normalized = original.replace("T", " ", 1)
            try:
                parsed = date_parser.isoparse(original)
            except (ValueError, OverflowError):
                try:
                    parsed = date_parser.parse(
                        normalized, fuzzy=False, dayfirst=False, yearfirst=True
                    )
                except (ValueError, OverflowError) as exc:
                    raise ConfigurationError(
                        "Could not understand the date. Use YYYY-MM-DD HH:MM, an ISO timestamp, "
                        "'tomorrow 10 AM', 'next Monday 9 AM', or 'in 45 minutes'."
                    ) from exc
            if parsed.tzinfo is None:
                local = _localize_strict(parsed, timezone)
                explanation = f"wall time interpreted in {timezone}"
            else:
                local = parsed.astimezone(zone)
                explanation = f"explicit offset converted to {timezone}"

    if future_required and local <= current:
        raise ConfigurationError(
            f"Resolved time {local.isoformat()} is not in the future. Choose a future time."
        )
    return ResolvedDateTime(
        original=original,
        local=local,
        utc=local.astimezone(UTC),
        timezone=timezone,
        explanation=explanation,
    )


def next_working_time(
    value: datetime,
    *,
    start: time = time(8, 0),
    end: time = time(17, 0),
    weekdays: frozenset[int] = frozenset(range(5)),
) -> datetime:
    if start >= end:
        raise ConfigurationError("Working-hours start must be before the end.")
    candidate = value
    for _ in range(8):
        if candidate.weekday() not in weekdays:
            candidate = datetime.combine(
                candidate.date() + timedelta(days=1), start, candidate.tzinfo
            )
            continue
        if candidate.time() < start:
            return datetime.combine(candidate.date(), start, candidate.tzinfo)
        if candidate.time() <= end:
            return candidate
        candidate = datetime.combine(candidate.date() + timedelta(days=1), start, candidate.tzinfo)
    raise ConfigurationError("Could not find a working-hours slot in the next week.")


def backoff_seconds(
    attempt: int,
    *,
    base: float = 2.0,
    maximum: float = 120.0,
    server_delay: float | None = None,
    random_source: random.Random | None = None,
) -> float:
    if attempt < 0 or base < 0 or maximum < 0:
        raise ValueError("Backoff inputs may not be negative.")
    cap = min(maximum, base * (2**attempt))
    if server_delay is not None:
        cap = max(cap, min(server_delay, maximum))
    generator = random_source or random.SystemRandom()
    return generator.uniform(0.0, cap)
