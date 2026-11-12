"""Shared bounded Task view and sorting policy without changing task records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError
from gucmail.models import Importance, TaskInfo
from gucmail.security import sanitize_terminal
from gucmail.services.calendar import validate_timezone
from gucmail.services.pim import PimPage
from gucmail.services.tasks import TaskQuery, TaskStatus

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

TASK_DISPLAY_SETTINGS = (
    "tasks.default_view",
    "tasks.show_completed",
    "tasks.sort_order",
    "calendar.timezone",
)


class TaskView(StrEnum):
    ALL = "all"
    OPEN = "open"
    DUE = "due"
    TODAY = "today"
    THIS_WEEK = "this_week"
    OVERDUE = "overdue"
    COMPLETED = "completed"


class TaskSortOrder(StrEnum):
    DUE_DATE = "due_date"
    SUBJECT = "subject"
    IMPORTANCE = "importance"
    STATUS = "status"


def _shift(day: date, days: int) -> date:
    try:
        return day + timedelta(days=days)
    except OverflowError as exc:
        raise ConfigurationError("This task date window exceeds the supported date range.") from exc


@dataclass(frozen=True, slots=True)
class TaskDisplayPreferences:
    default_view: TaskView = TaskView.OPEN
    show_completed: bool = False
    sort_order: TaskSortOrder = TaskSortOrder.DUE_DATE
    timezone: str = "Africa/Cairo"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "default_view", TaskView(self.default_view))
            object.__setattr__(self, "sort_order", TaskSortOrder(self.sort_order))
        except ValueError as exc:
            raise ConfigurationError("Choose a supported task view and sorting order.") from exc
        if not isinstance(self.show_completed, bool):
            raise ConfigurationError("Completed-task visibility must be true or false.")
        validate_timezone(self.timezone)

    @classmethod
    def from_settings(
        cls,
        service: SettingsService,
        *,
        view: TaskView | None = None,
        show_completed: bool | None = None,
        sort_order: TaskSortOrder | None = None,
    ) -> TaskDisplayPreferences:
        overrides = {
            "tasks.default_view": view,
            "tasks.show_completed": show_completed,
            "tasks.sort_order": sort_order,
        }
        values = {
            row.spec.setting_id: row.effective_value
            for row in service.snapshot(
                tuple(key for key in TASK_DISPLAY_SETTINGS if overrides.get(key) is None)
            ).values
        }
        return cls(
            view if view is not None else values["tasks.default_view"],
            show_completed if show_completed is not None else values["tasks.show_completed"],
            sort_order if sort_order is not None else values["tasks.sort_order"],
            str(values["calendar.timezone"]),
        )

    def today(self, now: datetime | None = None) -> date:
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None:
            raise ConfigurationError("The task clock must include a time zone.")
        return instant.astimezone(validate_timezone(self.timezone)).date()

    def query(
        self, *, view: TaskView | str | None = None, text: str = "", today: date | None = None
    ) -> TaskQuery:
        try:
            selected = TaskView(view) if view is not None else self.default_view
        except ValueError as exc:
            raise ConfigurationError("Choose a supported task view.") from exc
        current = today or self.today()
        result = TaskQuery(text=text.strip() or None, exclude_completed=not self.show_completed)
        if selected is TaskView.COMPLETED:
            return replace(result, status=TaskStatus.COMPLETED, exclude_completed=False)
        if selected in {TaskView.OPEN, TaskView.DUE, TaskView.OVERDUE}:
            result = replace(result, exclude_completed=True)
        if selected is TaskView.DUE:
            return replace(result, due_before=_shift(current, 1))
        if selected is TaskView.OVERDUE:
            return replace(result, overdue_at=current)
        if selected is TaskView.TODAY:
            return replace(result, due_after=_shift(current, -1), due_before=_shift(current, 1))
        if selected is TaskView.THIS_WEEK:
            return replace(result, due_after=_shift(current, -1), due_before=_shift(current, 7))
        return result

    def search_query(self, query: TaskQuery) -> TaskQuery:
        """Explicit status/overdue wins; a saved default view never narrows a search."""
        if query.overdue_at is not None:
            if query.status is TaskStatus.COMPLETED:
                raise ConfigurationError("Overdue tasks are incomplete; remove completed status.")
            return replace(query, exclude_completed=True)
        return replace(
            query,
            exclude_completed=query.exclude_completed
            or (query.status is None and not self.show_completed),
        )

    def sort(self, tasks: Iterable[TaskInfo]) -> tuple[TaskInfo, ...]:
        importance = {Importance.HIGH: 0, Importance.NORMAL: 1, Importance.LOW: 2}

        def key(task: TaskInfo) -> tuple[object, ...]:
            subject = sanitize_terminal(task.subject).casefold()
            fallback = (task.due_date is None, task.due_date or date.max, subject, task.reference)
            if self.sort_order is TaskSortOrder.SUBJECT:
                return subject, task.reference
            if self.sort_order is TaskSortOrder.IMPORTANCE:
                return importance[task.importance], *fallback
            if self.sort_order is TaskSortOrder.STATUS:
                return task.status, *fallback
            return task.status == TaskStatus.COMPLETED.value, *fallback

        return tuple(sorted(tasks, key=key))

    def page(self, page: PimPage[TaskInfo], query: TaskQuery) -> PimPage[TaskInfo]:
        """Confirm metadata restrictions locally and retain every candidate offset."""

        def matches(task: TaskInfo) -> bool:
            completed = task.status == TaskStatus.COMPLETED.value or task.percent_complete >= 100
            if query.exclude_completed and completed:
                return False
            if query.status is not None and task.status != query.status.value:
                return False
            if query.due_before and (task.due_date is None or task.due_date >= query.due_before):
                return False
            if query.due_after and (task.due_date is None or task.due_date <= query.due_after):
                return False
            return not (
                query.overdue_at and (task.due_date is None or task.due_date >= query.overdue_at)
            )

        return replace(page, items=self.sort(task for task in page.items if matches(task)))
