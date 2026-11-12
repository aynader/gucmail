from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from dateutil.rrule import rrulestr

from gucmail.errors import ConfigurationError
from gucmail.services.interchange import _format_rrule
from gucmail.services.recurrence import (
    AbsoluteMonthlyPattern,
    AbsoluteYearlyPattern,
    DailyPattern,
    EndDateBoundary,
    Month,
    NoEndBoundary,
    NumberedBoundary,
    OccurrenceReference,
    RecurrenceDefinition,
    RecurrenceScope,
    RecurrenceTarget,
    RelativeMonthlyPattern,
    RelativeWeekday,
    RelativeYearlyPattern,
    Weekday,
    WeekIndex,
    WeeklyPattern,
    boundary_from_mapping,
    boundary_to_mapping,
    normalize_pattern,
    normalize_recurrence,
    normalize_recurrence_mapping,
    normalize_recurrence_scope,
    occurrence_reference_from_mapping,
    occurrence_reference_to_mapping,
    parse_recurrence,
    pattern_from_mapping,
    pattern_to_mapping,
    recurrence_from_mapping,
    recurrence_to_mapping,
    recurrence_to_rfc_mapping,
)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (DailyPattern(interval=3), {"type": "daily", "interval": 3}),
        (
            WeeklyPattern(
                weekdays=(Weekday.WEDNESDAY, Weekday.MONDAY),
                interval=2,
                first_day_of_week=Weekday.SUNDAY,
            ),
            {
                "type": "weekly",
                "interval": 2,
                "weekdays": ["monday", "wednesday"],
                "first_day_of_week": "sunday",
            },
        ),
        (
            AbsoluteMonthlyPattern(day_of_month=31, interval=2),
            {"type": "absolute_monthly", "interval": 2, "day_of_month": 31},
        ),
        (
            RelativeMonthlyPattern(
                weekday=RelativeWeekday.WEEKDAY,
                week_index=WeekIndex.LAST,
                interval=3,
            ),
            {
                "type": "relative_monthly",
                "interval": 3,
                "weekday": "weekday",
                "week_index": "last",
            },
        ),
        (
            AbsoluteYearlyPattern(month=Month.FEBRUARY, day_of_month=29),
            {"type": "absolute_yearly", "month": 2, "day_of_month": 29},
        ),
        (
            RelativeYearlyPattern(
                month=Month.NOVEMBER,
                weekday=RelativeWeekday.THURSDAY,
                week_index=WeekIndex.FOURTH,
            ),
            {
                "type": "relative_yearly",
                "month": 11,
                "weekday": "thursday",
                "week_index": "fourth",
            },
        ),
    ],
)
def test_all_pattern_kinds_have_canonical_round_trips(
    pattern: object,
    expected: dict[str, object],
) -> None:
    assert isinstance(
        pattern,
        (
            DailyPattern,
            WeeklyPattern,
            AbsoluteMonthlyPattern,
            RelativeMonthlyPattern,
            AbsoluteYearlyPattern,
            RelativeYearlyPattern,
        ),
    )
    typed_pattern = pattern
    assert pattern_to_mapping(typed_pattern) == expected
    assert pattern_from_mapping(expected) == normalize_pattern(typed_pattern)


@pytest.mark.parametrize(
    "boundary",
    [
        NumberedBoundary(start=date(2026, 8, 10), occurrences=8),
        EndDateBoundary(start=date(2026, 8, 10), end=date(2026, 12, 31)),
        NoEndBoundary(start=date(2026, 8, 10)),
    ],
)
def test_all_boundary_kinds_have_canonical_round_trips(boundary: object) -> None:
    assert isinstance(boundary, (NumberedBoundary, EndDateBoundary, NoEndBoundary))
    mapping = boundary_to_mapping(boundary)
    assert boundary_from_mapping(mapping) == boundary


@pytest.mark.parametrize("target", list(RecurrenceTarget))
def test_complete_definition_round_trip_is_json_safe(target: RecurrenceTarget) -> None:
    original = RecurrenceDefinition(
        target=target,
        pattern=WeeklyPattern(
            weekdays=(Weekday.FRIDAY, Weekday.MONDAY),
            interval=2,
        ),
        boundary=NumberedBoundary(start=date(2026, 8, 10), occurrences=12),
    )

    mapping = recurrence_to_mapping(original)
    restored = parse_recurrence(json.dumps(mapping))

    assert mapping["schema_version"] == 1
    assert restored == normalize_recurrence(original)
    assert recurrence_to_mapping(restored) == mapping


def test_mapping_may_inherit_target_but_cannot_contradict_it() -> None:
    mapping: dict[str, object] = {
        "pattern": {"type": "daily", "interval": 1},
        "boundary": {"type": "no_end", "start": "2026-08-10"},
    }

    recurrence = recurrence_from_mapping(mapping, target=RecurrenceTarget.TASK)

    assert recurrence.target is RecurrenceTarget.TASK
    with pytest.raises(ConfigurationError, match="target does not match"):
        recurrence_from_mapping(
            {**mapping, "target": "calendar"},
            target=RecurrenceTarget.TASK,
        )
    with pytest.raises(ConfigurationError, match="target is required"):
        recurrence_from_mapping(mapping)


def test_pattern_parser_normalizes_common_names_and_iso_weekdays() -> None:
    weekly = pattern_from_mapping(
        {
            "type": "weekly-recurrence",
            "interval": "2",
            "weekdays": (7, "Wed", "mon"),
            "first_day_of_week": "Sun",
        }
    )
    relative = pattern_from_mapping(
        {
            "type": "monthly-relative",
            "weekday": "business day",
            "week_index": "-1",
        }
    )

    assert weekly == WeeklyPattern(
        weekdays=(Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.SUNDAY),
        interval=2,
        first_day_of_week=Weekday.SUNDAY,
    )
    assert relative == RelativeMonthlyPattern(
        weekday=RelativeWeekday.WEEKDAY,
        week_index=WeekIndex.LAST,
    )


def test_rfc_ics_weekly_mapping_normalizes_using_enclosing_event_start() -> None:
    canonical = normalize_recurrence_mapping(
        {"freq": "weekly", "count": 3, "byday": "WE"},
        start=datetime(2026, 8, 5, 9, tzinfo=UTC),
    )

    assert canonical == {
        "schema_version": 1,
        "target": "calendar",
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "weekdays": ["wednesday"],
            "first_day_of_week": "monday",
        },
        "boundary": {
            "type": "numbered",
            "start": "2026-08-05",
            "occurrences": 3,
        },
    }


@pytest.mark.parametrize(
    ("mapping", "expected_pattern", "expected_boundary"),
    [
        (
            {"freq": "daily", "interval": 4},
            DailyPattern(interval=4),
            NoEndBoundary(start=date(2026, 1, 10)),
        ),
        (
            {"freq": "monthly", "interval": 2, "bymonthday": 15, "count": 7},
            AbsoluteMonthlyPattern(day_of_month=15, interval=2),
            NumberedBoundary(start=date(2026, 1, 10), occurrences=7),
        ),
        (
            {"freq": "monthly", "byday": "-1FR", "until": "20261231"},
            RelativeMonthlyPattern(
                weekday=RelativeWeekday.FRIDAY,
                week_index=WeekIndex.LAST,
            ),
            EndDateBoundary(start=date(2026, 1, 10), end=date(2026, 12, 31)),
        ),
        (
            {"freq": "yearly", "bymonth": "2", "bymonthday": "28"},
            AbsoluteYearlyPattern(month=Month.FEBRUARY, day_of_month=28),
            NoEndBoundary(start=date(2026, 1, 10)),
        ),
        (
            {"freq": "yearly", "bymonth": 11, "byday": "4TH"},
            RelativeYearlyPattern(
                month=Month.NOVEMBER,
                weekday=RelativeWeekday.THURSDAY,
                week_index=WeekIndex.FOURTH,
            ),
            NoEndBoundary(start=date(2026, 1, 10)),
        ),
    ],
)
def test_rfc_daily_monthly_and_yearly_forms(
    mapping: dict[str, object],
    expected_pattern: object,
    expected_boundary: object,
) -> None:
    recurrence = recurrence_from_mapping(
        mapping,
        target="task",
        start=date(2026, 1, 10),
    )

    assert recurrence.target is RecurrenceTarget.TASK
    assert recurrence.pattern == expected_pattern
    assert recurrence.boundary == expected_boundary


def test_task_compatibility_entry_point_sets_task_target() -> None:
    mapping = normalize_recurrence_mapping(
        {
            "pattern": {"type": "absolute_monthly", "day_of_month": 31},
            "boundary": {"type": "no_end", "start": "2026-01-31"},
        },
        task=True,
    )

    assert mapping["target"] == "task"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DailyPattern(interval=0),
        lambda: DailyPattern(interval=1_000),
        lambda: WeeklyPattern(weekdays=(Weekday.MONDAY,), interval=100),
        lambda: AbsoluteMonthlyPattern(day_of_month=1, interval=0),
        lambda: AbsoluteMonthlyPattern(day_of_month=1, interval=100),
        lambda: RelativeMonthlyPattern(
            weekday=RelativeWeekday.MONDAY,
            week_index=WeekIndex.FIRST,
            interval=100,
        ),
        lambda: DailyPattern(interval=True),
    ],
)
def test_interval_range_and_type_errors(factory: object) -> None:
    assert callable(factory)
    with pytest.raises(ConfigurationError, match="interval"):
        factory()


@pytest.mark.parametrize(
    "mapping",
    [
        {"type": "weekly", "weekdays": []},
        {"type": "weekly", "weekdays": ["MO", "MO"]},
        {"type": "weekly", "weekdays": [0]},
        {"type": "weekly", "weekdays": [8]},
        {"type": "weekly", "weekdays": ["weekday"]},
        {"type": "relative_monthly", "weekday": "holiday", "week_index": "first"},
        {"type": "relative_monthly", "weekday": "monday", "week_index": "sixth"},
    ],
)
def test_weekday_and_week_index_validation(mapping: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        pattern_from_mapping(mapping)


@pytest.mark.parametrize(
    "mapping",
    [
        {"type": "absolute_monthly", "day_of_month": 0},
        {"type": "absolute_monthly", "day_of_month": 32},
        {"type": "absolute_yearly", "month": 0, "day_of_month": 1},
        {"type": "absolute_yearly", "month": 13, "day_of_month": 1},
        {"type": "absolute_yearly", "month": 2, "day_of_month": 32},
        {"type": "relative_yearly", "month": "Smarch", "weekday": "MO", "week_index": 1},
    ],
)
def test_month_and_day_ranges(mapping: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        pattern_from_mapping(mapping)


def test_boundary_ranges_and_order_are_validated() -> None:
    with pytest.raises(ConfigurationError, match="between 1 and 999"):
        NumberedBoundary(start=date(2026, 1, 1), occurrences=0)
    with pytest.raises(ConfigurationError, match="between 1 and 999"):
        NumberedBoundary(start=date(2026, 1, 1), occurrences=1_000)
    with pytest.raises(ConfigurationError, match="before"):
        EndDateBoundary(start=date(2026, 2, 1), end=date(2026, 1, 31))
    with pytest.raises(ConfigurationError, match="calendar date"):
        NoEndBoundary(start=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    "mapping",
    [
        {
            "target": "calendar",
            "pattern": {"type": "daily", "interval": 1, "typo": 2},
            "boundary": {"type": "no_end", "start": "2026-01-01"},
        },
        {
            "target": "calendar",
            "pattern": {"type": "daily"},
            "boundary": {"type": "no_end", "start": "2026-01-01"},
            "typo": True,
        },
        {
            "schema_version": 2,
            "target": "calendar",
            "pattern": {"type": "daily"},
            "boundary": {"type": "no_end", "start": "2026-01-01"},
        },
    ],
)
def test_unknown_fields_and_schema_versions_are_rejected(mapping: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        recurrence_from_mapping(mapping)


def test_invalid_json_and_non_object_json_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="invalid"):
        parse_recurrence("{")
    with pytest.raises(ConfigurationError, match="one object"):
        parse_recurrence("[]")
    with pytest.raises(ConfigurationError, match="non-empty"):
        parse_recurrence("  ")


@pytest.mark.parametrize(
    "mapping",
    [
        {"freq": "weekly", "count": 3, "byday": "WE"},
        {"freq": "monthly", "byday": "MO"},
        {"freq": "monthly", "byday": "5MO"},
        {"freq": "monthly", "byday": "1MO", "bymonthday": 1},
        {"freq": "yearly", "interval": 2},
        {"freq": "daily", "count": 2, "until": "20261231"},
    ],
)
def test_rfc_forms_reject_missing_context_or_lossy_combinations(
    mapping: dict[str, object],
) -> None:
    kwargs: dict[str, object] = {"target": "calendar"}
    if mapping.get("freq") != "weekly" or mapping.get("count") != 3:
        kwargs["start"] = date(2026, 1, 1)
    with pytest.raises(ConfigurationError):
        recurrence_from_mapping(mapping, **kwargs)


@pytest.mark.parametrize(
    ("pattern", "start", "expected"),
    [
        (
            AbsoluteMonthlyPattern(29),
            datetime(2023, 1, 29),
            [(2023, 1, 29), (2023, 2, 28), (2023, 3, 29)],
        ),
        (
            AbsoluteMonthlyPattern(30),
            datetime(2024, 1, 30),
            [(2024, 1, 30), (2024, 2, 29), (2024, 3, 30)],
        ),
        (
            AbsoluteYearlyPattern(Month.FEBRUARY, 29),
            datetime(2024, 2, 29),
            [(2024, 2, 29), (2025, 2, 28), (2026, 2, 28)],
        ),
    ],
)
def test_clipped_exchange_days_export_and_reimport_without_changing_occurrences(
    pattern: AbsoluteMonthlyPattern | AbsoluteYearlyPattern,
    start: datetime,
    expected: list[tuple[int, int, int]],
) -> None:
    original = RecurrenceDefinition(
        RecurrenceTarget.CALENDAR, pattern, NumberedBoundary(start.date(), 3)
    )
    rfc = recurrence_to_rfc_mapping(original, start=start)
    occurrences = list(rrulestr(_format_rrule(rfc), dtstart=start))
    assert [(item.year, item.month, item.day) for item in occurrences] == expected
    assert recurrence_from_mapping(rfc, target="calendar", start=start) == original


@pytest.mark.parametrize(
    "mapping",
    [
        {"freq": "monthly", "bymonthday": 29},
        {"freq": "monthly", "bymonthday": 30},
        {"freq": "monthly", "bymonthday": 31},
        {"freq": "yearly", "bymonth": 2, "bymonthday": 29},
        {"freq": "yearly", "bymonth": 4, "bymonthday": 31},
        {"freq": "monthly", "bymonthday": (28, 30), "bysetpos": -1},
    ],
)
def test_rfc_skipped_dates_are_not_silently_changed_to_clipped_exchange_dates(
    mapping: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError, match="lossless"):
        recurrence_from_mapping(mapping, target="calendar", start=date(2024, 1, 29))


def test_occurrence_reference_round_trips_by_index_or_original_start() -> None:
    by_index = OccurrenceReference(series_reference="  cal-series-1  ", index=4)
    by_time = OccurrenceReference(
        series_reference="cal-series-1",
        original_start=datetime(2026, 8, 10, 9, tzinfo=UTC),
    )

    assert occurrence_reference_to_mapping(by_index) == {
        "series_reference": "cal-series-1",
        "index": 4,
    }
    for value in (by_index, by_time):
        assert occurrence_reference_from_mapping(occurrence_reference_to_mapping(value)) == value


def test_occurrence_reference_requires_one_valid_locator() -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        OccurrenceReference(series_reference="series")
    with pytest.raises(ConfigurationError, match="exactly one"):
        OccurrenceReference(
            series_reference="series",
            index=1,
            original_start=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ConfigurationError, match="between"):
        OccurrenceReference(series_reference="series", index=0)
    with pytest.raises(ConfigurationError, match="UTC offset"):
        OccurrenceReference(
            series_reference="series",
            original_start=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("occurrence", RecurrenceScope.OCCURRENCE),
        ("single", RecurrenceScope.OCCURRENCE),
        ("instance", RecurrenceScope.OCCURRENCE),
        ("series", RecurrenceScope.SERIES),
        ("entire series", RecurrenceScope.SERIES),
        ("all", RecurrenceScope.SERIES),
    ],
)
def test_recurrence_scope_aliases(value: str, expected: RecurrenceScope) -> None:
    assert normalize_recurrence_scope(value) is expected


def test_recurrence_scope_rejects_unsupported_this_and_future_semantics() -> None:
    with pytest.raises(ConfigurationError, match="Unknown recurrence scope"):
        normalize_recurrence_scope("this and future")
