"""Fixed-date repeats and server-managed completion-relative task regeneration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from gucmail.errors import ConfigurationError
from gucmail.services.recurrence import normalize_recurrence_mapping

TASK_REPEAT_SETTINGS = (
    "tasks.default_repeat",
    "tasks.default_repeat_interval",
    "tasks.default_repeat_count",
)


class TaskRepeatKind(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    AFTER_COMPLETION_DAILY = "after_completion_daily"
    AFTER_COMPLETION_WEEKLY = "after_completion_weekly"
    AFTER_COMPLETION_MONTHLY = "after_completion_monthly"
    AFTER_COMPLETION_YEARLY = "after_completion_yearly"


def _integer(value: object, *, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise ConfigurationError(f"{label} must be a whole number.")
    if not 1 <= value <= maximum:
        raise ConfigurationError(f"{label} must be from 1 to {maximum}.")
    return int(value)


@dataclass(frozen=True, slots=True)
class TaskRepeatPreferences:
    kind: TaskRepeatKind = TaskRepeatKind.NONE
    interval: int = 1
    count: int | None = None

    def __post_init__(self) -> None:
        try:
            kind = TaskRepeatKind(self.kind)
        except ValueError as exc:
            raise ConfigurationError("Choose a supported task repeat pattern.") from exc
        object.__setattr__(self, "kind", kind)
        maximum = 99 if kind in {TaskRepeatKind.WEEKLY, TaskRepeatKind.MONTHLY} else 999
        object.__setattr__(
            self, "interval", _integer(self.interval, label="Repeat interval", maximum=maximum)
        )
        if kind is TaskRepeatKind.YEARLY and self.interval != 1:
            raise ConfigurationError("Yearly task repeats require an interval of one year.")
        if self.count is not None:
            object.__setattr__(
                self, "count", _integer(self.count, label="Occurrence count", maximum=999)
            )

    def rule(self, anchor: date, *, until: date | None = None) -> dict[str, object] | None:
        if self.kind is TaskRepeatKind.NONE:
            return None
        if self.count is not None and until is not None:
            raise ConfigurationError("Choose an occurrence count or an end date, not both.")
        if self.kind is TaskRepeatKind.DAILY:
            pattern: dict[str, object] = {"type": "daily", "interval": self.interval}
        elif self.kind.value.startswith("after_completion_"):
            pattern = {
                "type": "task_regeneration",
                "unit": {
                    TaskRepeatKind.AFTER_COMPLETION_DAILY: "day",
                    TaskRepeatKind.AFTER_COMPLETION_WEEKLY: "week",
                    TaskRepeatKind.AFTER_COMPLETION_MONTHLY: "month",
                    TaskRepeatKind.AFTER_COMPLETION_YEARLY: "year",
                }[self.kind],
                "interval": self.interval,
            }
        elif self.kind is TaskRepeatKind.WEEKLY:
            weekday = (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )[anchor.weekday()]
            pattern = {
                "type": "weekly",
                "interval": self.interval,
                "weekdays": [weekday],
                "first_day_of_week": "monday",
            }
        elif self.kind is TaskRepeatKind.MONTHLY:
            pattern = {
                "type": "absolute_monthly",
                "interval": self.interval,
                "day_of_month": anchor.day,
            }
        else:
            pattern = {"type": "absolute_yearly", "month": anchor.month, "day_of_month": anchor.day}
        if self.count is not None:
            boundary: dict[str, object] = {
                "type": "numbered",
                "start": anchor.isoformat(),
                "occurrences": self.count,
            }
        elif until is not None:
            boundary = {"type": "end_date", "start": anchor.isoformat(), "end": until.isoformat()}
        else:
            boundary = {"type": "no_end", "start": anchor.isoformat()}
        return normalize_recurrence_mapping(
            {"pattern": pattern, "boundary": boundary}, task=True, start=anchor
        )
