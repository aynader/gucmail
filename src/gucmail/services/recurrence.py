"""Typed, transport-neutral Exchange recurrence definitions.

This module deliberately contains no :mod:`exchangelib` imports.  Calendar and
task services can therefore validate and persist recurrence rules without
depending on the EWS transport.  Adapters may translate the normalized values
to their native recurrence objects at the boundary.

The canonical mapping schema is JSON-safe and versioned::

    {
        "schema_version": 1,
        "target": "calendar",
        "pattern": {"type": "weekly", "interval": 2,
                    "weekdays": ["monday", "wednesday"],
                    "first_day_of_week": "monday"},
        "boundary": {"type": "numbered", "start": "2026-08-10",
                     "occurrences": 8},
    }

Parsing is intentionally strict about unknown fields and incompatible values.
This prevents a misspelled recurrence option from being silently ignored.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import IntEnum, StrEnum
from typing import Any, TypeAlias, cast

from gucmail.errors import ConfigurationError

RECURRENCE_SCHEMA_VERSION = 1


class RecurrenceTarget(StrEnum):
    """Exchange item family to which a recurrence definition belongs."""

    CALENDAR = "calendar"
    TASK = "task"


class Weekday(StrEnum):
    """Concrete ISO weekday names, ordered Monday through Sunday."""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @property
    def iso_number(self) -> int:
        """Return the ISO weekday number (Monday is 1)."""

        return _WEEKDAY_ORDER[self]


class RelativeWeekday(StrEnum):
    """A concrete or aggregate day accepted by relative EWS patterns."""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"
    DAY = "day"
    WEEKDAY = "weekday"
    WEEKEND_DAY = "weekend_day"


class WeekIndex(StrEnum):
    """Ordinal week within a month for relative recurrence patterns."""

    FIRST = "first"
    SECOND = "second"
    THIRD = "third"
    FOURTH = "fourth"
    LAST = "last"


class Month(IntEnum):
    """Calendar month represented by its ISO month number."""

    JANUARY = 1
    FEBRUARY = 2
    MARCH = 3
    APRIL = 4
    MAY = 5
    JUNE = 6
    JULY = 7
    AUGUST = 8
    SEPTEMBER = 9
    OCTOBER = 10
    NOVEMBER = 11
    DECEMBER = 12


class RecurrenceScope(StrEnum):
    """Scope for an operation initiated on a recurring item.

    EWS has reliable concepts for one occurrence and for the entire series.
    A synthetic "this and future" scope is deliberately not exposed here.
    """

    OCCURRENCE = "occurrence"
    SERIES = "series"


@dataclass(frozen=True, slots=True)
class DailyPattern:
    """Repeat every ``interval`` days (EWS range: 1 through 999)."""

    interval: int = 1

    def __post_init__(self) -> None:
        _require_int(self.interval, field="Daily interval", minimum=1, maximum=999)


@dataclass(frozen=True, slots=True)
class WeeklyPattern:
    """Repeat on one or more concrete weekdays every N weeks."""

    weekdays: tuple[Weekday, ...]
    interval: int = 1
    first_day_of_week: Weekday = Weekday.MONDAY

    def __post_init__(self) -> None:
        _require_int(self.interval, field="Weekly interval", minimum=1, maximum=99)
        if not isinstance(self.weekdays, tuple):
            raise ConfigurationError("Weekly weekdays must be a tuple of Weekday values.")
        if not self.weekdays:
            raise ConfigurationError("A weekly recurrence needs at least one weekday.")
        if any(not isinstance(item, Weekday) for item in self.weekdays):
            raise ConfigurationError("Weekly weekdays must contain only concrete Weekday values.")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ConfigurationError("Weekly weekdays may not contain duplicates.")
        if not isinstance(self.first_day_of_week, Weekday):
            raise ConfigurationError("The first day of the week must be a concrete Weekday.")


@dataclass(frozen=True, slots=True)
class AbsoluteMonthlyPattern:
    """Repeat on a numbered day of every N months."""

    day_of_month: int
    interval: int = 1

    def __post_init__(self) -> None:
        _require_int(self.interval, field="Monthly interval", minimum=1, maximum=99)
        _require_int(self.day_of_month, field="Day of month", minimum=1, maximum=31)


@dataclass(frozen=True, slots=True)
class RelativeMonthlyPattern:
    """Repeat on an ordinal weekday (for example, last weekday) every N months."""

    weekday: RelativeWeekday
    week_index: WeekIndex
    interval: int = 1

    def __post_init__(self) -> None:
        _require_int(self.interval, field="Monthly interval", minimum=1, maximum=99)
        if not isinstance(self.weekday, RelativeWeekday):
            raise ConfigurationError("Relative monthly weekday is invalid.")
        if not isinstance(self.week_index, WeekIndex):
            raise ConfigurationError("Relative monthly week index is invalid.")


@dataclass(frozen=True, slots=True)
class AbsoluteYearlyPattern:
    """Repeat on a numbered day of one month each year."""

    month: Month
    day_of_month: int

    def __post_init__(self) -> None:
        if not isinstance(self.month, Month):
            raise ConfigurationError("Yearly month is invalid.")
        _require_int(self.day_of_month, field="Day of month", minimum=1, maximum=31)


@dataclass(frozen=True, slots=True)
class RelativeYearlyPattern:
    """Repeat on an ordinal weekday of one month each year."""

    month: Month
    weekday: RelativeWeekday
    week_index: WeekIndex

    def __post_init__(self) -> None:
        if not isinstance(self.month, Month):
            raise ConfigurationError("Yearly month is invalid.")
        if not isinstance(self.weekday, RelativeWeekday):
            raise ConfigurationError("Relative yearly weekday is invalid.")
        if not isinstance(self.week_index, WeekIndex):
            raise ConfigurationError("Relative yearly week index is invalid.")


class TaskRegenerationUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True, slots=True)
class TaskRegenerationPattern:
    """A next task due N periods after completion, never a fixed-date RRULE."""

    unit: TaskRegenerationUnit
    interval: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.unit, TaskRegenerationUnit):
            raise ConfigurationError("Task regeneration unit must be day, week, month, or year.")
        _require_int(self.interval, field="Task regeneration interval", minimum=1, maximum=999)


RecurrencePattern: TypeAlias = (
    DailyPattern
    | WeeklyPattern
    | AbsoluteMonthlyPattern
    | RelativeMonthlyPattern
    | AbsoluteYearlyPattern
    | RelativeYearlyPattern
    | TaskRegenerationPattern
)


@dataclass(frozen=True, slots=True)
class NumberedBoundary:
    """Start on ``start`` and stop after a fixed number of occurrences."""

    start: date
    occurrences: int

    def __post_init__(self) -> None:
        _require_date(self.start, field="Recurrence start")
        _require_int(
            self.occurrences,
            field="Number of occurrences",
            minimum=1,
            maximum=999,
        )


@dataclass(frozen=True, slots=True)
class EndDateBoundary:
    """Start on ``start`` and include occurrences through ``end``."""

    start: date
    end: date

    def __post_init__(self) -> None:
        _require_date(self.start, field="Recurrence start")
        _require_date(self.end, field="Recurrence end")
        if self.end < self.start:
            raise ConfigurationError("Recurrence end date cannot be before its start date.")


@dataclass(frozen=True, slots=True)
class NoEndBoundary:
    """Start on ``start`` without a server-side end condition."""

    start: date

    def __post_init__(self) -> None:
        _require_date(self.start, field="Recurrence start")


RecurrenceBoundary: TypeAlias = NumberedBoundary | EndDateBoundary | NoEndBoundary


@dataclass(frozen=True, slots=True)
class RecurrenceDefinition:
    """A fully typed recurrence for a calendar item or task."""

    target: RecurrenceTarget
    pattern: RecurrencePattern
    boundary: RecurrenceBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.target, RecurrenceTarget):
            raise ConfigurationError("Recurrence target must be calendar or task.")
        if not isinstance(self.pattern, _PATTERN_CLASSES):
            raise ConfigurationError("Recurrence pattern has an unsupported type.")
        if (
            isinstance(self.pattern, TaskRegenerationPattern)
            and self.target is not RecurrenceTarget.TASK
        ):
            raise ConfigurationError(
                "Completion-relative regeneration is supported only for tasks."
            )
        if not isinstance(self.boundary, _BOUNDARY_CLASSES):
            raise ConfigurationError("Recurrence boundary has an unsupported type.")


@dataclass(frozen=True, slots=True)
class OccurrenceReference:
    """Stable description of one occurrence in a recurring series.

    Exactly one locator is required.  ``index`` is the one-based EWS occurrence
    index.  ``original_start`` is useful when an occurrence has been moved and
    must include an explicit UTC offset.  This value does not itself perform an
    EWS lookup.
    """

    series_reference: str
    index: int | None = None
    original_start: datetime | None = None

    def __post_init__(self) -> None:
        reference = self.series_reference.strip()
        if not reference:
            raise ConfigurationError("A recurring-series reference is required.")
        object.__setattr__(self, "series_reference", reference)
        if (self.index is None) == (self.original_start is None):
            raise ConfigurationError(
                "An occurrence reference needs exactly one of index or original_start."
            )
        if self.index is not None:
            _require_int(
                self.index,
                field="Occurrence index",
                minimum=1,
                maximum=2_147_483_647,
            )
        if self.original_start is not None:
            if not isinstance(self.original_start, datetime):
                raise ConfigurationError("Occurrence original_start must be a datetime.")
            if self.original_start.tzinfo is None or self.original_start.utcoffset() is None:
                raise ConfigurationError(
                    "Occurrence original_start must include an explicit UTC offset."
                )


RecurrenceInput: TypeAlias = RecurrenceDefinition | Mapping[str, object] | str

_PATTERN_CLASSES = (
    DailyPattern,
    WeeklyPattern,
    AbsoluteMonthlyPattern,
    RelativeMonthlyPattern,
    AbsoluteYearlyPattern,
    RelativeYearlyPattern,
    TaskRegenerationPattern,
)
_BOUNDARY_CLASSES = (NumberedBoundary, EndDateBoundary, NoEndBoundary)
_WEEKDAY_ORDER = {weekday: index for index, weekday in enumerate(Weekday, start=1)}
_WEEKDAY_ALIASES: dict[str, Weekday] = {
    "mon": Weekday.MONDAY,
    "monday": Weekday.MONDAY,
    "tue": Weekday.TUESDAY,
    "tues": Weekday.TUESDAY,
    "tuesday": Weekday.TUESDAY,
    "wed": Weekday.WEDNESDAY,
    "wednesday": Weekday.WEDNESDAY,
    "thu": Weekday.THURSDAY,
    "thur": Weekday.THURSDAY,
    "thurs": Weekday.THURSDAY,
    "thursday": Weekday.THURSDAY,
    "fri": Weekday.FRIDAY,
    "friday": Weekday.FRIDAY,
    "sat": Weekday.SATURDAY,
    "saturday": Weekday.SATURDAY,
    "sun": Weekday.SUNDAY,
    "sunday": Weekday.SUNDAY,
}
_RELATIVE_WEEKDAY_ALIASES: dict[str, RelativeWeekday] = {
    **{key: RelativeWeekday(value.value) for key, value in _WEEKDAY_ALIASES.items()},
    "day": RelativeWeekday.DAY,
    "any_day": RelativeWeekday.DAY,
    "weekday": RelativeWeekday.WEEKDAY,
    "week_day": RelativeWeekday.WEEKDAY,
    "business_day": RelativeWeekday.WEEKDAY,
    "weekend": RelativeWeekday.WEEKEND_DAY,
    "weekend_day": RelativeWeekday.WEEKEND_DAY,
}
_WEEK_INDEX_ALIASES: dict[str, WeekIndex] = {
    "1": WeekIndex.FIRST,
    "first": WeekIndex.FIRST,
    "1st": WeekIndex.FIRST,
    "2": WeekIndex.SECOND,
    "second": WeekIndex.SECOND,
    "2nd": WeekIndex.SECOND,
    "3": WeekIndex.THIRD,
    "third": WeekIndex.THIRD,
    "3rd": WeekIndex.THIRD,
    "4": WeekIndex.FOURTH,
    "fourth": WeekIndex.FOURTH,
    "4th": WeekIndex.FOURTH,
    "5": WeekIndex.LAST,
    "fifth": WeekIndex.LAST,
    "5th": WeekIndex.LAST,
    "last": WeekIndex.LAST,
}
_MONTH_ALIASES: dict[str, Month] = {
    **{month.name.casefold(): month for month in Month},
    "jan": Month.JANUARY,
    "feb": Month.FEBRUARY,
    "mar": Month.MARCH,
    "apr": Month.APRIL,
    "jun": Month.JUNE,
    "jul": Month.JULY,
    "aug": Month.AUGUST,
    "sep": Month.SEPTEMBER,
    "sept": Month.SEPTEMBER,
    "oct": Month.OCTOBER,
    "nov": Month.NOVEMBER,
    "dec": Month.DECEMBER,
}
_PATTERN_TYPE_ALIASES = {
    "task_regeneration": "task_regeneration",
    "regeneration": "task_regeneration",
    "daily": "daily",
    "daily_pattern": "daily",
    "daily_recurrence": "daily",
    "weekly": "weekly",
    "weekly_pattern": "weekly",
    "weekly_recurrence": "weekly",
    "absolute_monthly": "absolute_monthly",
    "monthly_absolute": "absolute_monthly",
    "absolute_monthly_pattern": "absolute_monthly",
    "relative_monthly": "relative_monthly",
    "monthly_relative": "relative_monthly",
    "relative_monthly_pattern": "relative_monthly",
    "absolute_yearly": "absolute_yearly",
    "yearly_absolute": "absolute_yearly",
    "absolute_yearly_pattern": "absolute_yearly",
    "relative_yearly": "relative_yearly",
    "yearly_relative": "relative_yearly",
    "relative_yearly_pattern": "relative_yearly",
}
_BOUNDARY_TYPE_ALIASES = {
    "numbered": "numbered",
    "count": "numbered",
    "numbered_boundary": "numbered",
    "end_date": "end_date",
    "end": "end_date",
    "until": "end_date",
    "end_date_boundary": "end_date",
    "no_end": "no_end",
    "forever": "no_end",
    "no_end_boundary": "no_end",
}
_RFC_WEEKDAY_CODES: dict[str, Weekday] = {
    "MO": Weekday.MONDAY,
    "TU": Weekday.TUESDAY,
    "WE": Weekday.WEDNESDAY,
    "TH": Weekday.THURSDAY,
    "FR": Weekday.FRIDAY,
    "SA": Weekday.SATURDAY,
    "SU": Weekday.SUNDAY,
}
_RFC_WEEKDAY_BY_NUMBER = {weekday.iso_number: code for code, weekday in _RFC_WEEKDAY_CODES.items()}


def pattern_to_mapping(pattern: RecurrencePattern) -> dict[str, object]:
    """Serialize a validated pattern to its canonical JSON-safe mapping."""

    normalized = normalize_pattern(pattern)
    if isinstance(normalized, TaskRegenerationPattern):
        return {
            "type": "task_regeneration",
            "unit": normalized.unit.value,
            "interval": normalized.interval,
        }
    if isinstance(normalized, DailyPattern):
        return {"type": "daily", "interval": normalized.interval}
    if isinstance(normalized, WeeklyPattern):
        weekdays = sorted(normalized.weekdays, key=lambda item: item.iso_number)
        return {
            "type": "weekly",
            "interval": normalized.interval,
            "weekdays": [item.value for item in weekdays],
            "first_day_of_week": normalized.first_day_of_week.value,
        }
    if isinstance(normalized, AbsoluteMonthlyPattern):
        return {
            "type": "absolute_monthly",
            "interval": normalized.interval,
            "day_of_month": normalized.day_of_month,
        }
    if isinstance(normalized, RelativeMonthlyPattern):
        return {
            "type": "relative_monthly",
            "interval": normalized.interval,
            "weekday": normalized.weekday.value,
            "week_index": normalized.week_index.value,
        }
    if isinstance(normalized, AbsoluteYearlyPattern):
        return {
            "type": "absolute_yearly",
            "month": int(normalized.month),
            "day_of_month": normalized.day_of_month,
        }
    return {
        "type": "relative_yearly",
        "month": int(normalized.month),
        "weekday": normalized.weekday.value,
        "week_index": normalized.week_index.value,
    }


def pattern_from_mapping(value: Mapping[str, object]) -> RecurrencePattern:
    """Parse and validate a recurrence pattern mapping.

    Common type aliases are accepted, but field names are strict and unknown
    fields raise :class:`~gucmail.errors.ConfigurationError`.
    """

    data = _string_key_mapping(value, field="Recurrence pattern")
    raw_type = _required(data, "type", subject="Recurrence pattern")
    pattern_type = _parse_named_alias(
        raw_type,
        aliases=_PATTERN_TYPE_ALIASES,
        field="Recurrence pattern type",
    )
    if pattern_type == "task_regeneration":
        _check_keys(
            data,
            required={"type", "unit"},
            optional={"interval"},
            subject="Task regeneration pattern",
        )
        raw_unit = data["unit"]
        if not isinstance(raw_unit, str):
            raise ConfigurationError("Task regeneration unit must be day, week, month, or year.")
        try:
            unit = TaskRegenerationUnit(raw_unit)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "Task regeneration unit must be day, week, month, or year."
            ) from exc
        return TaskRegenerationPattern(
            unit=unit, interval=_mapping_int(data, "interval", default=1)
        )
    if pattern_type == "daily":
        _check_keys(data, required={"type"}, optional={"interval"}, subject="Daily pattern")
        return DailyPattern(interval=_mapping_int(data, "interval", default=1))
    if pattern_type == "weekly":
        _check_keys(
            data,
            required={"type", "weekdays"},
            optional={"interval", "first_day_of_week"},
            subject="Weekly pattern",
        )
        raw_weekdays = data["weekdays"]
        if isinstance(raw_weekdays, (str, bytes)) or not isinstance(raw_weekdays, Sequence):
            raise ConfigurationError("Weekly weekdays must be a list or tuple.")
        weekdays = tuple(_parse_weekday(item) for item in raw_weekdays)
        if len(set(weekdays)) != len(weekdays):
            raise ConfigurationError("Weekly weekdays may not contain duplicates.")
        weekdays = tuple(sorted(weekdays, key=lambda item: item.iso_number))
        return WeeklyPattern(
            weekdays=weekdays,
            interval=_mapping_int(data, "interval", default=1),
            first_day_of_week=_parse_weekday(data.get("first_day_of_week", "monday")),
        )
    if pattern_type == "absolute_monthly":
        _check_keys(
            data,
            required={"type", "day_of_month"},
            optional={"interval"},
            subject="Absolute monthly pattern",
        )
        return AbsoluteMonthlyPattern(
            interval=_mapping_int(data, "interval", default=1),
            day_of_month=_mapping_int(data, "day_of_month"),
        )
    if pattern_type == "relative_monthly":
        _check_keys(
            data,
            required={"type", "weekday", "week_index"},
            optional={"interval"},
            subject="Relative monthly pattern",
        )
        return RelativeMonthlyPattern(
            interval=_mapping_int(data, "interval", default=1),
            weekday=_parse_relative_weekday(data["weekday"]),
            week_index=_parse_week_index(data["week_index"]),
        )
    if pattern_type == "absolute_yearly":
        _check_keys(
            data,
            required={"type", "month", "day_of_month"},
            optional=set(),
            subject="Absolute yearly pattern",
        )
        return AbsoluteYearlyPattern(
            month=_parse_month(data["month"]),
            day_of_month=_mapping_int(data, "day_of_month"),
        )
    _check_keys(
        data,
        required={"type", "month", "weekday", "week_index"},
        optional=set(),
        subject="Relative yearly pattern",
    )
    return RelativeYearlyPattern(
        month=_parse_month(data["month"]),
        weekday=_parse_relative_weekday(data["weekday"]),
        week_index=_parse_week_index(data["week_index"]),
    )


def normalize_pattern(value: RecurrencePattern | Mapping[str, object]) -> RecurrencePattern:
    """Return a validated pattern with deterministic weekday ordering."""

    if isinstance(value, Mapping):
        return pattern_from_mapping(value)
    if not isinstance(value, _PATTERN_CLASSES):
        raise ConfigurationError("Recurrence pattern has an unsupported type.")
    if isinstance(value, WeeklyPattern):
        weekdays = tuple(sorted(value.weekdays, key=lambda item: item.iso_number))
        return WeeklyPattern(
            weekdays=weekdays,
            interval=value.interval,
            first_day_of_week=value.first_day_of_week,
        )
    return value


def boundary_to_mapping(boundary: RecurrenceBoundary) -> dict[str, object]:
    """Serialize a validated recurrence boundary."""

    normalized = normalize_boundary(boundary)
    if isinstance(normalized, NumberedBoundary):
        return {
            "type": "numbered",
            "start": normalized.start.isoformat(),
            "occurrences": normalized.occurrences,
        }
    if isinstance(normalized, EndDateBoundary):
        return {
            "type": "end_date",
            "start": normalized.start.isoformat(),
            "end": normalized.end.isoformat(),
        }
    return {"type": "no_end", "start": normalized.start.isoformat()}


def boundary_from_mapping(value: Mapping[str, object]) -> RecurrenceBoundary:
    """Parse and validate a recurrence boundary mapping."""

    data = _string_key_mapping(value, field="Recurrence boundary")
    raw_type = _required(data, "type", subject="Recurrence boundary")
    boundary_type = _parse_named_alias(
        raw_type,
        aliases=_BOUNDARY_TYPE_ALIASES,
        field="Recurrence boundary type",
    )
    if boundary_type == "numbered":
        _check_keys(
            data,
            required={"type", "start", "occurrences"},
            optional=set(),
            subject="Numbered boundary",
        )
        return NumberedBoundary(
            start=_parse_date(data["start"], field="Recurrence start"),
            occurrences=_mapping_int(data, "occurrences"),
        )
    if boundary_type == "end_date":
        _check_keys(
            data,
            required={"type", "start", "end"},
            optional=set(),
            subject="End-date boundary",
        )
        return EndDateBoundary(
            start=_parse_date(data["start"], field="Recurrence start"),
            end=_parse_date(data["end"], field="Recurrence end"),
        )
    _check_keys(
        data,
        required={"type", "start"},
        optional=set(),
        subject="No-end boundary",
    )
    return NoEndBoundary(start=_parse_date(data["start"], field="Recurrence start"))


def normalize_boundary(
    value: RecurrenceBoundary | Mapping[str, object],
) -> RecurrenceBoundary:
    """Return a validated recurrence boundary."""

    if isinstance(value, Mapping):
        return boundary_from_mapping(value)
    if not isinstance(value, _BOUNDARY_CLASSES):
        raise ConfigurationError("Recurrence boundary has an unsupported type.")
    return value


def recurrence_to_mapping(value: RecurrenceDefinition) -> dict[str, object]:
    """Serialize a recurrence definition to the canonical versioned schema."""

    normalized = normalize_recurrence(value)
    return {
        "schema_version": RECURRENCE_SCHEMA_VERSION,
        "target": normalized.target.value,
        "pattern": pattern_to_mapping(normalized.pattern),
        "boundary": boundary_to_mapping(normalized.boundary),
    }


def recurrence_from_mapping(
    value: Mapping[str, object],
    *,
    target: RecurrenceTarget | str | None = None,
    start: date | datetime | None = None,
) -> RecurrenceDefinition:
    """Parse a recurrence definition from a mapping.

    ``target`` may supply the item family when an embedding domain model already
    establishes it.  If both the mapping and argument specify a target, they
    must agree.
    """

    data = _string_key_mapping(value, field="Recurrence")
    if "freq" in data and "pattern" not in data:
        return _recurrence_from_rfc_mapping(data, target=target, start=start)
    _check_keys(
        data,
        required={"pattern", "boundary"},
        optional={"schema_version", "target"},
        subject="Recurrence",
    )
    schema_version = data.get("schema_version", RECURRENCE_SCHEMA_VERSION)
    _require_int(
        schema_version,
        field="Recurrence schema_version",
        minimum=RECURRENCE_SCHEMA_VERSION,
        maximum=RECURRENCE_SCHEMA_VERSION,
    )
    supplied_target = _parse_target(target) if target is not None else None
    declared_target = _parse_target(data["target"]) if data.get("target") is not None else None
    if (
        supplied_target is not None
        and declared_target is not None
        and supplied_target is not declared_target
    ):
        raise ConfigurationError(
            "Recurrence target does not match the enclosing calendar/task item."
        )
    effective_target = supplied_target or declared_target
    if effective_target is None:
        raise ConfigurationError("Recurrence target is required; use 'calendar' or 'task'.")
    pattern_data = data["pattern"]
    boundary_data = data["boundary"]
    if not isinstance(pattern_data, Mapping):
        raise ConfigurationError("Recurrence pattern must be an object.")
    if not isinstance(boundary_data, Mapping):
        raise ConfigurationError("Recurrence boundary must be an object.")
    definition = RecurrenceDefinition(
        target=effective_target,
        pattern=pattern_from_mapping(cast(Mapping[str, object], pattern_data)),
        boundary=boundary_from_mapping(cast(Mapping[str, object], boundary_data)),
    )
    return _validate_enclosing_start(definition, start=start)


def parse_recurrence(
    payload: str,
    *,
    target: RecurrenceTarget | str | None = None,
    start: date | datetime | None = None,
) -> RecurrenceDefinition:
    """Parse a recurrence definition from a JSON object string."""

    if not isinstance(payload, str) or not payload.strip():
        raise ConfigurationError("Recurrence JSON must be a non-empty string.")
    try:
        decoded: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigurationError("Recurrence JSON is invalid.") from exc
    if not isinstance(decoded, Mapping):
        raise ConfigurationError("Recurrence JSON must contain one object.")
    return recurrence_from_mapping(
        cast(Mapping[str, object], decoded),
        target=target,
        start=start,
    )


def normalize_recurrence(
    value: RecurrenceInput,
    *,
    target: RecurrenceTarget | str | None = None,
    start: date | datetime | None = None,
) -> RecurrenceDefinition:
    """Normalize a typed value, mapping, or JSON string.

    Supplying ``target`` acts as an invariant check for typed definitions and as
    the target source for mappings that intentionally omit it.
    """

    expected_target = _parse_target(target) if target is not None else None
    if isinstance(value, str):
        return parse_recurrence(value, target=expected_target, start=start)
    if isinstance(value, Mapping):
        return recurrence_from_mapping(value, target=expected_target, start=start)
    if not isinstance(value, RecurrenceDefinition):
        raise ConfigurationError("Recurrence must be a definition, object, or JSON string.")
    if expected_target is not None and value.target is not expected_target:
        raise ConfigurationError(
            "Recurrence target does not match the enclosing calendar/task item."
        )
    definition = RecurrenceDefinition(
        target=value.target,
        pattern=normalize_pattern(value.pattern),
        boundary=normalize_boundary(value.boundary),
    )
    return _validate_enclosing_start(definition, start=start)


def normalize_recurrence_mapping(
    value: Mapping[str, object],
    *,
    task: bool = False,
    start: date | datetime | None = None,
) -> dict[str, object]:
    """Return a canonical mapping suitable for ``CalendarEvent`` or ``TaskInfo``.

    In addition to the canonical schema, this compatibility entry point accepts
    the RFC-style mappings produced by the ICS importer, for example::

        {"freq": "weekly", "count": 3, "byday": "WE"}

    An RFC recurrence does not repeat its ``DTSTART`` value.  Callers embedding
    such a mapping must therefore supply the event/task ``start`` unless the
    mapping contains ``start`` or ``dtstart`` itself.  Canonical mappings already
    contain their boundary start and do not need this argument.
    """

    if not isinstance(task, bool):
        raise ConfigurationError("task must be true or false.")
    target = RecurrenceTarget.TASK if task else RecurrenceTarget.CALENDAR
    return recurrence_to_mapping(recurrence_from_mapping(value, target=target, start=start))


def recurrence_to_rfc_mapping(
    value: RecurrenceInput,
    *,
    target: RecurrenceTarget | str | None = None,
    start: date | datetime | None = None,
    all_day: bool = False,
) -> dict[str, object]:
    """Render a typed Exchange recurrence as a lossless RFC 5545 RRULE subset.

    Exchange clamps days beyond a month's end, whereas RFC rules skip invalid
    dates.  A BYMONTHDAY set and BYSETPOS preserve the clipped 29th/30th.
    End-date boundaries are inclusive.  For a
    timed event, ``UNTIL`` is rendered at the event's local start time on the final
    date and converted to UTC, which preserves the last included occurrence.
    """

    recurrence = normalize_recurrence(value, target=target, start=start)
    pattern = recurrence.pattern
    if isinstance(pattern, TaskRegenerationPattern):
        raise ConfigurationError(
            "Completion-relative task regeneration cannot be represented by a fixed-date RRULE. "
            "Use the canonical task JSON representation to retain its meaning."
        )
    result: dict[str, object]
    if isinstance(pattern, DailyPattern):
        result = {"freq": "daily"}
        if pattern.interval != 1:
            result["interval"] = pattern.interval
    elif isinstance(pattern, WeeklyPattern):
        result = {
            "freq": "weekly",
            "byday": tuple(_RFC_WEEKDAY_BY_NUMBER[item.iso_number] for item in pattern.weekdays),
        }
        if pattern.interval != 1:
            result["interval"] = pattern.interval
        if pattern.first_day_of_week is not Weekday.MONDAY:
            result["wkst"] = _RFC_WEEKDAY_BY_NUMBER[pattern.first_day_of_week.iso_number]
    elif isinstance(pattern, AbsoluteMonthlyPattern):
        result = {
            "freq": "monthly",
            **_clipped_month_day_rfc(pattern.day_of_month),
        }
        if pattern.interval != 1:
            result["interval"] = pattern.interval
    elif isinstance(pattern, RelativeMonthlyPattern):
        byday, bysetpos = _relative_rfc_fields(pattern.week_index, pattern.weekday)
        result = {
            "freq": "monthly",
            "byday": byday,
        }
        if bysetpos is not None:
            result["bysetpos"] = bysetpos
        if pattern.interval != 1:
            result["interval"] = pattern.interval
    elif isinstance(pattern, AbsoluteYearlyPattern):
        result = {
            "freq": "yearly",
            "bymonth": int(pattern.month),
            **_clipped_month_day_rfc(pattern.day_of_month, month=pattern.month),
        }
    else:
        byday, bysetpos = _relative_rfc_fields(pattern.week_index, pattern.weekday)
        result = {
            "freq": "yearly",
            "bymonth": int(pattern.month),
            "byday": byday,
        }
        if bysetpos is not None:
            result["bysetpos"] = bysetpos

    boundary = recurrence.boundary
    if isinstance(boundary, NumberedBoundary):
        result["count"] = boundary.occurrences
    elif isinstance(boundary, EndDateBoundary):
        result["until"] = _rfc_until_value(boundary.end, start=start, all_day=all_day)
    return result


def _recurrence_from_rfc_mapping(
    data: Mapping[str, object],
    *,
    target: RecurrenceTarget | str | None,
    start: date | datetime | None,
) -> RecurrenceDefinition:
    """Translate the supported RFC 5545 RRULE subset without losing semantics."""

    _check_keys(
        data,
        required={"freq"},
        optional={
            "target",
            "interval",
            "count",
            "until",
            "byday",
            "bymonthday",
            "bymonth",
            "bysetpos",
            "wkst",
            "start",
            "dtstart",
        },
        subject="RFC recurrence",
    )
    supplied_target = _parse_target(target) if target is not None else None
    declared_target = _parse_target(data["target"]) if data.get("target") is not None else None
    if (
        supplied_target is not None
        and declared_target is not None
        and supplied_target is not declared_target
    ):
        raise ConfigurationError(
            "Recurrence target does not match the enclosing calendar/task item."
        )
    effective_target = supplied_target or declared_target
    if effective_target is None:
        raise ConfigurationError("Recurrence target is required; use 'calendar' or 'task'.")

    raw_start = start
    embedded_start = data.get("dtstart", data.get("start"))
    if raw_start is not None and embedded_start is not None:
        supplied_start = _rfc_date(raw_start, field="Recurrence start")
        declared_start = _rfc_date(embedded_start, field="Recurrence start")
        if supplied_start != declared_start:
            raise ConfigurationError(
                "RFC recurrence start does not match the enclosing item start."
            )
        boundary_start = supplied_start
    elif raw_start is not None:
        boundary_start = _rfc_date(raw_start, field="Recurrence start")
    elif embedded_start is not None:
        boundary_start = _rfc_date(embedded_start, field="Recurrence start")
    else:
        raise ConfigurationError("RFC recurrence needs the enclosing event/task start date.")

    interval = _mapping_int(data, "interval", default=1)
    raw_frequency = _required(data, "freq", subject="RFC recurrence")
    frequency = _normalize_token(raw_frequency) if isinstance(raw_frequency, str) else ""
    if frequency not in {"daily", "weekly", "monthly", "yearly"}:
        raise ConfigurationError(
            "RFC recurrence frequency must be daily, weekly, monthly, or yearly."
        )
    byday = _rfc_values(data.get("byday"), field="BYDAY")
    bymonthday = _rfc_int_values(data.get("bymonthday"), field="BYMONTHDAY")
    bymonth = _rfc_int_values(data.get("bymonth"), field="BYMONTH")
    bysetpos = _rfc_int_values(data.get("bysetpos"), field="BYSETPOS")

    pattern: RecurrencePattern
    if frequency == "daily":
        if byday or bymonthday or bymonth or bysetpos or data.get("wkst") is not None:
            raise ConfigurationError(
                "Daily RFC recurrence contains fields not representable by "
                "Exchange daily recurrence."
            )
        pattern = DailyPattern(interval=interval)
    elif frequency == "weekly":
        if bymonthday or bymonth or bysetpos:
            raise ConfigurationError(
                "Weekly RFC recurrence contains unsupported month/day position fields."
            )
        raw_days = byday or (_RFC_WEEKDAY_BY_NUMBER[boundary_start.isoweekday()],)
        weekdays: list[Weekday] = []
        for token in raw_days:
            ordinal, weekday = _parse_rfc_byday(token)
            if ordinal is not None:
                raise ConfigurationError("Weekly BYDAY values may not contain ordinals.")
            weekdays.append(weekday)
        if len(set(weekdays)) != len(weekdays):
            raise ConfigurationError("Weekly BYDAY values may not contain duplicates.")
        first_day = (
            _parse_rfc_weekday_only(data["wkst"])
            if data.get("wkst") is not None
            else Weekday.MONDAY
        )
        pattern = WeeklyPattern(
            weekdays=tuple(sorted(set(weekdays), key=lambda item: item.iso_number)),
            interval=interval,
            first_day_of_week=first_day,
        )
    elif frequency == "monthly":
        if bymonth:
            raise ConfigurationError("Monthly RFC recurrence may not contain BYMONTH.")
        if byday and bymonthday:
            raise ConfigurationError(
                "Monthly RFC recurrence cannot combine BYDAY and BYMONTHDAY in this Exchange model."
            )
        if len(bysetpos) > 1:
            raise ConfigurationError(
                "Exchange monthly recurrence supports one day or one ordinal weekday."
            )
        if byday:
            ordinal, relative_weekday = _parse_rfc_relative_weekdays(byday, bysetpos)
            pattern = RelativeMonthlyPattern(
                weekday=relative_weekday,
                week_index=_rfc_ordinal_to_week_index(ordinal),
                interval=interval,
            )
        else:
            day_number = _rfc_absolute_day(bymonthday, bysetpos, boundary_start.day)
            pattern = AbsoluteMonthlyPattern(
                day_of_month=day_number,
                interval=interval,
            )
    else:
        if interval != 1:
            raise ConfigurationError(
                "Exchange yearly recurrence cannot represent an INTERVAL other than 1."
            )
        if len(bymonth) > 1 or len(bysetpos) > 1:
            raise ConfigurationError(
                "Exchange yearly recurrence supports one month and one day expression."
            )
        month = _parse_month(bymonth[0] if bymonth else boundary_start.month)
        if byday and bymonthday:
            raise ConfigurationError(
                "Yearly RFC recurrence cannot combine BYDAY and BYMONTHDAY in this Exchange model."
            )
        if byday:
            ordinal, relative_weekday = _parse_rfc_relative_weekdays(byday, bysetpos)
            pattern = RelativeYearlyPattern(
                month=month,
                weekday=relative_weekday,
                week_index=_rfc_ordinal_to_week_index(ordinal),
            )
        else:
            day_number = _rfc_absolute_day(bymonthday, bysetpos, boundary_start.day, month=month)
            pattern = AbsoluteYearlyPattern(month=month, day_of_month=day_number)

    count = data.get("count")
    until = data.get("until")
    if count is not None and until is not None:
        raise ConfigurationError("RFC recurrence cannot contain both COUNT and UNTIL.")
    boundary: RecurrenceBoundary
    if count is not None:
        boundary = NumberedBoundary(
            start=boundary_start,
            occurrences=_coerce_int(count, field="COUNT"),
        )
    elif until is not None:
        boundary = EndDateBoundary(
            start=boundary_start,
            end=_rfc_until_date(
                until,
                start=raw_start if raw_start is not None else embedded_start,
            ),
        )
    else:
        boundary = NoEndBoundary(start=boundary_start)
    return RecurrenceDefinition(
        target=effective_target,
        pattern=pattern,
        boundary=boundary,
    )


def _validate_enclosing_start(
    value: RecurrenceDefinition,
    *,
    start: date | datetime | None,
) -> RecurrenceDefinition:
    """Ensure a persisted recurrence cannot drift from its enclosing item start."""

    if start is None:
        return value
    enclosing_start = _rfc_date(start, field="Enclosing item start")
    if value.boundary.start != enclosing_start:
        raise ConfigurationError(
            "Recurrence start does not match the enclosing calendar/task item start date."
        )
    return value


def _relative_rfc_fields(
    index: WeekIndex, weekday: RelativeWeekday
) -> tuple[str | tuple[str, ...], int | None]:
    ordinal = {
        WeekIndex.FIRST: "1",
        WeekIndex.SECOND: "2",
        WeekIndex.THIRD: "3",
        WeekIndex.FOURTH: "4",
        WeekIndex.LAST: "-1",
    }[index]
    aggregate_days = {
        RelativeWeekday.DAY: ("MO", "TU", "WE", "TH", "FR", "SA", "SU"),
        RelativeWeekday.WEEKDAY: ("MO", "TU", "WE", "TH", "FR"),
        RelativeWeekday.WEEKEND_DAY: ("SA", "SU"),
    }.get(weekday)
    if aggregate_days is not None:
        return aggregate_days, int(ordinal)
    concrete = Weekday(weekday.value)
    return ordinal + _RFC_WEEKDAY_BY_NUMBER[concrete.iso_number], None


def _rfc_until_value(
    end: date,
    *,
    start: date | datetime | None,
    all_day: bool,
) -> str:
    if all_day or not isinstance(start, datetime):
        return end.strftime("%Y%m%d")
    if start.tzinfo is None or start.utcoffset() is None:
        raise ConfigurationError(
            "A timed recurrence export requires an enclosing start with a UTC offset."
        )
    local_start = start
    wall_time = time(
        local_start.hour,
        local_start.minute,
        local_start.second,
        local_start.microsecond,
        fold=local_start.fold,
    )
    final_start = datetime.combine(end, wall_time, tzinfo=local_start.tzinfo)
    return final_start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _rfc_until_date(value: object, *, start: object | None) -> date:
    """Convert RFC UNTIL to Exchange's inclusive local end-date boundary.

    A datetime cutoff can fall before the recurrence's local start time.  In that
    case the last included Exchange date is the preceding date.  This avoids the
    previous behavior of blindly truncating UTC ``UNTIL`` to a date and potentially
    adding an extra occurrence.
    """

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if re.fullmatch(r"\d{8}", raw):
            return _rfc_date(raw, field="UNTIL")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return _parse_date(raw, field="UNTIL")
    cutoff = _parse_rfc_aware_datetime(value, field="UNTIL")
    if not isinstance(start, datetime):
        raise ConfigurationError(
            "A datetime UNTIL needs the enclosing time-zone-aware event/task start."
        )
    if start.tzinfo is None or start.utcoffset() is None:
        raise ConfigurationError(
            "A datetime UNTIL needs an enclosing start with an explicit UTC offset."
        )
    local_cutoff = cutoff.astimezone(start.tzinfo)
    local_start = start.astimezone(start.tzinfo)
    end_date = local_cutoff.date()
    if local_cutoff.timetz().replace(tzinfo=None) < local_start.timetz().replace(tzinfo=None):
        end_date -= timedelta(days=1)
    return end_date


def _parse_rfc_aware_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            if re.fullmatch(r"\d{8}T\d{4}(?:\d{2})?Z", raw, flags=re.IGNORECASE):
                format_name = "%Y%m%dT%H%M%SZ" if len(raw) == 16 else "%Y%m%dT%H%MZ"
                parsed = datetime.strptime(raw.upper(), format_name).replace(tzinfo=UTC)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"{field} contains an invalid RFC datetime.") from exc
    else:
        raise ConfigurationError(f"{field} must contain an RFC or ISO 8601 datetime.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{field} datetime must include an explicit UTC offset.")
    return parsed


def occurrence_reference_to_mapping(value: OccurrenceReference) -> dict[str, object]:
    """Serialize an occurrence locator to a JSON-safe mapping."""

    if not isinstance(value, OccurrenceReference):
        raise ConfigurationError("Occurrence reference has an unsupported type.")
    result: dict[str, object] = {"series_reference": value.series_reference}
    if value.index is not None:
        result["index"] = value.index
    else:
        assert value.original_start is not None
        result["original_start"] = value.original_start.isoformat()
    return result


def occurrence_reference_from_mapping(
    value: Mapping[str, object],
) -> OccurrenceReference:
    """Parse a one-based index or offset-aware original-start occurrence locator."""

    data = _string_key_mapping(value, field="Occurrence reference")
    _check_keys(
        data,
        required={"series_reference"},
        optional={"index", "original_start"},
        subject="Occurrence reference",
    )
    index = data.get("index")
    original = data.get("original_start")
    parsed_index = None
    if index is not None:
        parsed_index = _coerce_int(index, field="Occurrence index")
    parsed_original = None
    if original is not None:
        parsed_original = _parse_aware_datetime(original, field="Occurrence original_start")
    reference = data["series_reference"]
    if not isinstance(reference, str):
        raise ConfigurationError("Recurring-series reference must be text.")
    return OccurrenceReference(
        series_reference=reference,
        index=parsed_index,
        original_start=parsed_original,
    )


def normalize_recurrence_scope(value: RecurrenceScope | str) -> RecurrenceScope:
    """Normalize safe aliases for occurrence/series mutation scope."""

    if isinstance(value, RecurrenceScope):
        return value
    if not isinstance(value, str):
        raise ConfigurationError("Recurrence scope must be occurrence or series.")
    normalized = _normalize_token(value)
    aliases = {
        "occurrence": RecurrenceScope.OCCURRENCE,
        "instance": RecurrenceScope.OCCURRENCE,
        "single": RecurrenceScope.OCCURRENCE,
        "series": RecurrenceScope.SERIES,
        "all": RecurrenceScope.SERIES,
        "entire_series": RecurrenceScope.SERIES,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ConfigurationError(
            f"Unknown recurrence scope {value!r}; use occurrence or series."
        ) from exc


def _rfc_values(value: object | None, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values: Sequence[object]
    if isinstance(value, str):
        raw_values = tuple(value.split(","))
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_values = value
    else:
        raise ConfigurationError(f"{field} must be text or a list of values.")
    result: list[str] = []
    for item in raw_values:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{field} contains an invalid empty/non-text value.")
        result.append(item.strip().upper())
    if not result:
        raise ConfigurationError(f"{field} may not be empty when supplied.")
    return tuple(result)


def _rfc_int_values(value: object | None, *, field: str) -> tuple[int, ...]:
    if value is None:
        return ()
    raw_values: Sequence[object]
    if isinstance(value, str):
        raw_values = tuple(value.split(","))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_values = value
    else:
        raw_values = (value,)
    if not raw_values:
        raise ConfigurationError(f"{field} may not be empty when supplied.")
    return tuple(_coerce_int(item, field=field) for item in raw_values)


def _parse_rfc_byday(value: str) -> tuple[int | None, Weekday]:
    match = re.fullmatch(r"(?P<ordinal>[+-]?[1-5])?(?P<weekday>MO|TU|WE|TH|FR|SA|SU)", value)
    if match is None:
        raise ConfigurationError(
            f"Invalid RFC BYDAY value {value!r}; use values such as MO, 1TU, or -1FR."
        )
    raw_ordinal = match.group("ordinal")
    ordinal = int(raw_ordinal) if raw_ordinal is not None else None
    return ordinal, _RFC_WEEKDAY_CODES[match.group("weekday")]


def _parse_rfc_weekday_only(value: object) -> Weekday:
    values = _rfc_values(value, field="WKST")
    if len(values) != 1:
        raise ConfigurationError("WKST must contain exactly one weekday.")
    ordinal, weekday = _parse_rfc_byday(values[0])
    if ordinal is not None:
        raise ConfigurationError("WKST may not contain an ordinal.")
    return weekday


def _parse_rfc_relative_weekdays(
    byday: tuple[str, ...], bysetpos: tuple[int, ...]
) -> tuple[int, RelativeWeekday]:
    """Translate the lossless RFC subset used by EWS relative recurrence.

    EWS can express one ordinal concrete weekday (``-1FR``), or an ordinal
    applied to exactly all days, weekdays, or weekend days through
    ``BYSETPOS``.  Other RFC combinations would change meaning if coerced and
    are rejected instead.
    """

    parsed = tuple(_parse_rfc_byday(token) for token in byday)
    embedded_ordinals = tuple(ordinal for ordinal, _weekday in parsed if ordinal is not None)
    if len(parsed) == 1:
        embedded, weekday = parsed[0]
        if embedded is not None and bysetpos:
            raise ConfigurationError(
                "Relative recurrence may not combine ordinal BYDAY and BYSETPOS."
            )
        if embedded is None:
            if len(bysetpos) != 1:
                raise ConfigurationError(
                    "A relative BYDAY recurrence needs one ordinal or one BYSETPOS value."
                )
            embedded = bysetpos[0]
        return embedded, RelativeWeekday(weekday.value)

    if embedded_ordinals:
        raise ConfigurationError(
            "Multiple BYDAY values may not contain individual ordinals in Exchange recurrence."
        )
    if len(bysetpos) != 1:
        raise ConfigurationError("Multiple BYDAY values require exactly one BYSETPOS ordinal.")
    weekdays = frozenset(weekday for _ordinal, weekday in parsed)
    aggregates = {
        frozenset(Weekday): RelativeWeekday.DAY,
        frozenset(
            {
                Weekday.MONDAY,
                Weekday.TUESDAY,
                Weekday.WEDNESDAY,
                Weekday.THURSDAY,
                Weekday.FRIDAY,
            }
        ): RelativeWeekday.WEEKDAY,
        frozenset({Weekday.SATURDAY, Weekday.SUNDAY}): RelativeWeekday.WEEKEND_DAY,
    }
    try:
        relative_weekday = aggregates[weekdays]
    except KeyError as exc:
        raise ConfigurationError(
            "Exchange relative recurrence supports only all days, weekdays, or weekend days "
            "when BYDAY contains multiple values."
        ) from exc
    if len(weekdays) != len(parsed):
        raise ConfigurationError("BYDAY values may not contain duplicates.")
    return bysetpos[0], relative_weekday


def _rfc_ordinal_to_week_index(value: int) -> WeekIndex:
    mapping = {
        1: WeekIndex.FIRST,
        2: WeekIndex.SECOND,
        3: WeekIndex.THIRD,
        4: WeekIndex.FOURTH,
        -1: WeekIndex.LAST,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ConfigurationError(
            "Exchange relative recurrence supports RFC ordinals 1, 2, 3, 4, and -1 only."
        ) from exc


def _clipped_month_day_rfc(day: int, *, month: Month | None = None) -> dict[str, object]:
    """Express Exchange's month-end clipping in an RFC recurrence rule."""

    # February's length varies by year; monthrange on 2025 gives its minimum.
    minimum = monthrange(2025, int(month))[1] if month is not None else 28
    maximum = monthrange(2024, int(month))[1] if month is not None else 31
    if day == 31 or day > maximum:
        return {"bymonthday": -1}
    if day > minimum:
        return {"bymonthday": tuple(range(minimum, day + 1)), "bysetpos": -1}
    return {"bymonthday": day}


def _rfc_absolute_day(
    days: tuple[int, ...],
    positions: tuple[int, ...],
    default_day: int,
    *,
    month: Month | None = None,
) -> int:
    """Accept only RFC day rules with the same semantics as Exchange."""

    minimum = monthrange(2025, int(month))[1] if month is not None else 28
    if positions:
        # This is the unambiguous clipped form emitted by our exporter.
        if (
            positions == (-1,)
            and days
            in (
                tuple(range(minimum, 30)),
                tuple(range(minimum, 31)),
            )
            and len(days) > 1
        ):
            return days[-1]
        raise ConfigurationError(
            "BYMONTHDAY/BYSETPOS cannot be represented losslessly by Exchange."
        )
    if len(days) > 1:
        raise ConfigurationError("Exchange recurrence supports only one unclipped month day.")
    day = days[0] if days else default_day
    if day == -1:
        return 31
    if not 1 <= day <= 31:
        raise ConfigurationError("BYMONTHDAY must be 1 through 31, or -1 for the last day.")
    if day > minimum:
        raise ConfigurationError(
            "This RFC recurrence skips months without the requested day, but Exchange "
            "would use the last day instead; it cannot be imported losslessly."
        )
    return day


def _rfc_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must contain a date.")
    raw = value.strip()
    if re.fullmatch(r"\d{8}(?:T\d{4}(?:\d{2})?Z?)?", raw, flags=re.IGNORECASE):
        try:
            return datetime.strptime(raw[:8], "%Y%m%d").date()
        except ValueError as exc:
            raise ConfigurationError(f"{field} contains an invalid RFC date.") from exc
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return _parse_date(raw, field=field)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ConfigurationError(f"{field} must contain an ISO 8601 or RFC date.") from exc


def _require_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{field} must be an integer.")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{field} must be between {minimum} and {maximum}.")


def _coerce_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("+-").isdigit():
            return int(stripped)
    raise ConfigurationError(f"{field} must be an integer.")


def _mapping_int(
    data: Mapping[str, object],
    key: str,
    *,
    default: int | None = None,
) -> int:
    if key not in data:
        if default is None:
            raise ConfigurationError(f"Recurrence field {key!r} is required.")
        return default
    return _coerce_int(data[key], field=key.replace("_", " ").capitalize())


def _require_date(value: object, *, field: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ConfigurationError(f"{field} must be a calendar date, not a datetime.")


def _parse_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        raise ConfigurationError(f"{field} must be a calendar date, not a datetime.")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must use YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{field} must use YYYY-MM-DD format.") from exc
    if parsed.isoformat() != value.strip():
        raise ConfigurationError(f"{field} must use YYYY-MM-DD format.")
    return parsed


def _parse_aware_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"{field} must be an ISO 8601 datetime.") from exc
    else:
        raise ConfigurationError(f"{field} must be an ISO 8601 datetime.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{field} must include an explicit UTC offset.")
    return parsed


def _parse_weekday(value: object) -> Weekday:
    if isinstance(value, Weekday):
        return value
    if isinstance(value, bool):
        raise ConfigurationError("Weekday must be Monday through Sunday.")
    if isinstance(value, int):
        try:
            return tuple(Weekday)[value - 1] if 1 <= value <= 7 else _invalid_weekday(value)
        except IndexError as exc:  # Defensive; the range check above normally handles this.
            raise ConfigurationError("Weekday must be in ISO range 1 through 7.") from exc
    if isinstance(value, str):
        try:
            return _WEEKDAY_ALIASES[_normalize_token(value)]
        except KeyError as exc:
            raise ConfigurationError(
                f"Unknown weekday {value!r}; use Monday through Sunday."
            ) from exc
    raise ConfigurationError("Weekday must be Monday through Sunday.")


def _invalid_weekday(value: object) -> Any:
    raise ConfigurationError(f"Weekday {value!r} must be in ISO range 1 through 7.")


def _parse_relative_weekday(value: object) -> RelativeWeekday:
    if isinstance(value, RelativeWeekday):
        return value
    if isinstance(value, Weekday):
        return RelativeWeekday(value.value)
    if isinstance(value, bool):
        raise ConfigurationError("Relative weekday is invalid.")
    if isinstance(value, int):
        if 1 <= value <= 7:
            return RelativeWeekday(tuple(Weekday)[value - 1].value)
        aggregate = {
            8: RelativeWeekday.DAY,
            9: RelativeWeekday.WEEKDAY,
            10: RelativeWeekday.WEEKEND_DAY,
        }
        try:
            return aggregate[value]
        except KeyError as exc:
            raise ConfigurationError(
                "Relative weekday must be Monday-Sunday, day, weekday, or weekend_day."
            ) from exc
    if isinstance(value, str):
        try:
            return _RELATIVE_WEEKDAY_ALIASES[_normalize_token(value)]
        except KeyError as exc:
            raise ConfigurationError(
                f"Unknown relative weekday {value!r}; use a weekday, day, or weekend_day."
            ) from exc
    raise ConfigurationError("Relative weekday is invalid.")


def _parse_week_index(value: object) -> WeekIndex:
    if isinstance(value, WeekIndex):
        return value
    if isinstance(value, bool):
        raise ConfigurationError("Week index must be first, second, third, fourth, or last.")
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str):
        if value.strip() == "-1":
            return WeekIndex.LAST
        try:
            return _WEEK_INDEX_ALIASES[_normalize_token(value)]
        except KeyError as exc:
            raise ConfigurationError(
                f"Unknown week index {value!r}; use first, second, third, fourth, or last."
            ) from exc
    raise ConfigurationError("Week index must be first, second, third, fourth, or last.")


def _parse_month(value: object) -> Month:
    if isinstance(value, Month):
        return value
    if isinstance(value, bool):
        raise ConfigurationError("Month must be 1 through 12 or a month name.")
    if isinstance(value, int):
        try:
            return Month(value)
        except ValueError as exc:
            raise ConfigurationError("Month must be between 1 and 12.") from exc
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return _parse_month(int(stripped))
        try:
            return _MONTH_ALIASES[_normalize_token(stripped)]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown month {value!r}; use 1-12 or a month name.") from exc
    raise ConfigurationError("Month must be 1 through 12 or a month name.")


def _parse_target(value: object) -> RecurrenceTarget:
    if isinstance(value, RecurrenceTarget):
        return value
    if not isinstance(value, str):
        raise ConfigurationError("Recurrence target must be calendar or task.")
    normalized = _normalize_token(value)
    aliases = {
        "calendar": RecurrenceTarget.CALENDAR,
        "event": RecurrenceTarget.CALENDAR,
        "appointment": RecurrenceTarget.CALENDAR,
        "task": RecurrenceTarget.TASK,
        "tasks": RecurrenceTarget.TASK,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ConfigurationError(
            f"Unknown recurrence target {value!r}; use calendar or task."
        ) from exc


def _normalize_token(value: str) -> str:
    return "_".join(value.strip().casefold().replace("-", " ").split())


def _parse_named_alias(
    value: object,
    *,
    aliases: Mapping[str, str],
    field: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be text.")
    try:
        return aliases[_normalize_token(value)]
    except KeyError as exc:
        allowed = ", ".join(sorted(set(aliases.values())))
        raise ConfigurationError(
            f"Unknown {field.casefold()} {value!r}; use one of: {allowed}."
        ) from exc


def _string_key_mapping(value: Mapping[str, object], *, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be an object.")
    if any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{field} field names must be text.")
    return dict(value)


def _required(data: Mapping[str, object], key: str, *, subject: str) -> object:
    if key not in data or data[key] is None:
        raise ConfigurationError(f"{subject} field {key!r} is required.")
    return data[key]


def _check_keys(
    data: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
    subject: str,
) -> None:
    missing = required - data.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigurationError(f"{subject} is missing required field(s): {names}.")
    unknown = data.keys() - required - optional
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigurationError(f"{subject} has unknown field(s): {names}.")


__all__ = [
    "RECURRENCE_SCHEMA_VERSION",
    "AbsoluteMonthlyPattern",
    "AbsoluteYearlyPattern",
    "DailyPattern",
    "EndDateBoundary",
    "Month",
    "NoEndBoundary",
    "NumberedBoundary",
    "OccurrenceReference",
    "RecurrenceBoundary",
    "RecurrenceDefinition",
    "RecurrenceInput",
    "RecurrencePattern",
    "RecurrenceScope",
    "RecurrenceTarget",
    "RelativeMonthlyPattern",
    "RelativeWeekday",
    "RelativeYearlyPattern",
    "TaskRegenerationPattern",
    "TaskRegenerationUnit",
    "WeekIndex",
    "Weekday",
    "WeeklyPattern",
    "boundary_from_mapping",
    "boundary_to_mapping",
    "normalize_boundary",
    "normalize_pattern",
    "normalize_recurrence",
    "normalize_recurrence_mapping",
    "normalize_recurrence_scope",
    "occurrence_reference_from_mapping",
    "occurrence_reference_to_mapping",
    "parse_recurrence",
    "pattern_from_mapping",
    "pattern_to_mapping",
    "recurrence_from_mapping",
    "recurrence_to_mapping",
]
