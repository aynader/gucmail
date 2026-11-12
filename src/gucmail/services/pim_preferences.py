"""Shared defaults for fresh Calendar and Task forms and CLI creation previews."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError
from gucmail.models import CalendarEvent, Importance, TaskInfo
from gucmail.services.calendar import localize_datetime, validate_timezone
from gucmail.services.exchange_values import normalize_availability
from gucmail.services.task_recurrence import (
    TASK_REPEAT_SETTINGS,
    TaskRepeatKind,
    TaskRepeatPreferences,
)

if TYPE_CHECKING:
    from gucmail.settings import SettingsService


class _Unset(Enum):
    VALUE = "unset"


UNSET = _Unset.VALUE
OPTIONAL_INTEGER_SETTINGS = frozenset(
    {
        "calendar.default_reminder_minutes",
        "tasks.default_reminder_minutes",
        "tasks.default_due_days",
        "tasks.default_repeat_count",
    }
)


def optional_whole_number(value: int | float | None) -> int | None:
    """Keep optional numeric settings integral instead of silently truncating them."""

    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise ConfigurationError(
            "Choose a whole number, or leave this setting blank to disable it."
        )
    return int(value)


def _minutes(value: int | None, *, maximum: int, minimum: int = 0) -> int | None:
    result = optional_whole_number(value)
    if result is not None and not minimum <= result <= maximum:
        raise ConfigurationError(f"Choose a whole number from {minimum} to {maximum}.")
    return result


@dataclass(frozen=True, slots=True)
class CalendarCreationPreferences:
    duration_minutes: int = 60
    reminder_minutes: int | None = None
    availability: str = "busy"

    def __post_init__(self) -> None:
        _minutes(self.duration_minutes, minimum=1, maximum=10080)
        _minutes(self.reminder_minutes, maximum=525600)
        normalize_availability(self.availability)

    @classmethod
    def from_settings(cls, service: SettingsService) -> CalendarCreationPreferences:
        snapshot = service.snapshot(
            (
                "calendar.default_duration_minutes",
                "calendar.default_reminder_minutes",
                "calendar.default_availability",
            )
        )
        values = {row.spec.setting_id: row.effective_value for row in snapshot.values}
        return cls(
            int(values["calendar.default_duration_minutes"]),
            optional_whole_number(values["calendar.default_reminder_minutes"]),
            str(values["calendar.default_availability"]),
        )

    def new_event(
        self,
        *,
        timezone: str,
        reference: str = "preview:event",
        subject: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        duration_minutes: int | None = None,
        reminder_minutes: int | _Unset | None = UNSET,
        availability: str | None = None,
        all_day: bool = False,
        now: datetime | None = None,
    ) -> CalendarEvent:
        """Seed once; elapsed durations cross DST, all-day ends use local midnight."""

        validate_timezone(timezone)
        if end is not None and duration_minutes is not None:
            raise ConfigurationError("Choose either --end or --duration-minutes.")
        if all_day and duration_minutes is not None:
            raise ConfigurationError("Use an explicit end date for an all-day duration.")
        current = localize_datetime(now or datetime.now(UTC), timezone).replace(
            second=0, microsecond=0
        )
        explicit_start = start is not None
        start = (
            localize_datetime(start, timezone)
            if start
            else (current.astimezone(UTC) + timedelta(hours=1)).astimezone(
                validate_timezone(timezone)
            )
        )
        if end is None:
            if all_day:
                if not explicit_start:
                    start = localize_datetime(datetime.combine(start.date(), time.min), timezone)
                end = localize_datetime(
                    datetime.combine(start.date() + timedelta(days=1), time.min), timezone
                )
            else:
                duration = self.duration_minutes if duration_minutes is None else duration_minutes
                _minutes(duration, minimum=1, maximum=10080)
                end = (start.astimezone(UTC) + timedelta(minutes=duration)).astimezone(
                    validate_timezone(timezone)
                )
        reminder = (
            self.reminder_minutes if isinstance(reminder_minutes, _Unset) else reminder_minutes
        )
        _minutes(reminder, maximum=525600)
        return CalendarEvent(
            reference=reference,
            subject=subject,
            start=start,
            end=localize_datetime(end, timezone),
            timezone=timezone,
            is_all_day=all_day,
            reminder_minutes=reminder,
            availability=normalize_availability(
                self.availability if availability is None else availability
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskCreationPreferences:
    due_days: int | None = None
    reminder_minutes: int | None = None
    importance: Importance = Importance.NORMAL
    repeat: TaskRepeatPreferences = field(default_factory=TaskRepeatPreferences)

    def __post_init__(self) -> None:
        _minutes(self.due_days, maximum=3650)
        _minutes(self.reminder_minutes, maximum=525600)
        Importance(self.importance)

    @classmethod
    def from_settings(cls, service: SettingsService) -> TaskCreationPreferences:
        snapshot = service.snapshot(
            (
                "tasks.default_due_days",
                "tasks.default_reminder_minutes",
                "tasks.default_importance",
                *TASK_REPEAT_SETTINGS,
            )
        )
        values = {row.spec.setting_id: row.effective_value for row in snapshot.values}
        return cls(
            optional_whole_number(values["tasks.default_due_days"]),
            optional_whole_number(values["tasks.default_reminder_minutes"]),
            Importance(str(values["tasks.default_importance"])),
            TaskRepeatPreferences(
                TaskRepeatKind(str(values["tasks.default_repeat"])),
                values["tasks.default_repeat_interval"],
                optional_whole_number(values["tasks.default_repeat_count"]),
            ),
        )

    def new_task(
        self,
        *,
        timezone: str,
        reference: str = "preview:task",
        subject: str = "",
        start_date: date | None = None,
        due_date: date | _Unset | None = UNSET,
        reminder_at: datetime | _Unset | None = UNSET,
        importance: Importance | None = None,
        now: datetime | None = None,
        repeat: TaskRepeatPreferences | None = None,
        repeat_until: date | None = None,
    ) -> TaskInfo:
        """Due days follow the start date or local today; reminders follow creation."""

        current = localize_datetime(now or datetime.now(UTC), timezone)
        if isinstance(due_date, _Unset):
            try:
                due_date = (
                    (start_date or current.date()) + timedelta(days=self.due_days)
                    if self.due_days is not None
                    else None
                )
            except OverflowError as exc:
                raise ConfigurationError(
                    "The default due date exceeds the supported date range."
                ) from exc
        if isinstance(reminder_at, _Unset):
            reminder_at = (
                current.astimezone(UTC) + timedelta(minutes=self.reminder_minutes)
                if self.reminder_minutes is not None
                else None
            )
        repeat_policy = repeat if repeat is not None else self.repeat
        anchor = start_date or due_date or current.date()
        recurrence = repeat_policy.rule(anchor, until=repeat_until)
        if recurrence is not None and start_date is None and due_date is None:
            start_date = anchor
        return TaskInfo(
            reference=reference,
            subject=subject,
            status="not_started",
            start_date=start_date,
            due_date=due_date,
            reminder_at=reminder_at,
            importance=importance if importance is not None else self.importance,
            recurrence=recurrence,
        )
