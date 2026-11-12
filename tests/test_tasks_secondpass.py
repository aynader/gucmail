from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError
from gucmail.ews.adapter import TASK_FIELDS, TASK_METADATA_FIELDS, EWSGateway
from gucmail.ews.mapping import map_task
from gucmail.models import TaskInfo
from gucmail.services.tasks import (
    TaskCreatePlan,
    TaskDeletePlan,
    TaskPatch,
    TaskQuery,
    TaskService,
    TaskUpdatePlan,
)
from gucmail.tui.pim_screens import TasksServiceBackend


class _TasksGateway:
    def __init__(self, task: TaskInfo) -> None:
        self.task = task
        self.queries: list[TaskQuery] = []

    def list_tasks(self, **_kwargs: object) -> tuple[TaskInfo, ...]:
        return (self.task,)

    def search_tasks(self, query: TaskQuery, **_kwargs: object) -> tuple[TaskInfo, ...]:
        self.queries.append(query)
        return (self.task,)

    def get_task(self, reference: str) -> TaskInfo:
        assert reference == self.task.reference
        return self.task

    def create_task(self, plan: TaskCreatePlan) -> TaskInfo:
        return plan.task

    def update_task(self, plan: TaskUpdatePlan) -> TaskInfo:
        self.task = replace(self.task, **plan.changes)
        return self.task

    def delete_task(self, _plan: TaskDeletePlan) -> None:
        return None


def _task() -> TaskInfo:
    return TaskInfo(reference="task-1", subject="Synthetic task", status="not_started")


def test_task_reminder_is_validated_planned_and_exported() -> None:
    reminder = datetime(2026, 8, 9, 8, 30, tzinfo=UTC)
    task = replace(_task(), reminder_at=reminder)
    service = TaskService(_TasksGateway(task))

    normalized = service.get(task.reference)
    clear_plan = service.plan_update(
        task.reference,
        TaskPatch(reminder_at=None),
        current=task,
    )

    assert normalized.reminder_at == reminder
    assert clear_plan.update_fields == ("reminder_at",)
    assert clear_plan.changes == {"reminder_at": None}
    assert reminder.isoformat() in service.export(task, format="csv")
    with pytest.raises(ConfigurationError, match="reminder time must include a time zone"):
        service.plan_create(replace(task, reminder_at=datetime(2026, 8, 9, 8, 30)))


def test_task_ews_mapping_and_mutation_preserve_reminder_state() -> None:
    reminder = datetime(2026, 8, 9, 8, 30, tzinfo=UTC)
    mapped = map_task(
        SimpleNamespace(
            id="task-reminder",
            subject="Reminder task",
            status="NotStarted",
            percent_complete=0,
            reminder_is_set=True,
            reminder_due_by=reminder,
        )
    )

    assert mapped.reminder_at == reminder
    assert {"reminder_is_set", "reminder_due_by"}.issubset(TASK_FIELDS)
    assert {"reminder_is_set", "reminder_due_by"}.issubset(TASK_METADATA_FIELDS)

    saved_fields: list[tuple[str, ...]] = []
    item = SimpleNamespace(
        id="task-reminder",
        subject="Reminder task",
        status="NotStarted",
        percent_complete=0,
        reminder_is_set=True,
        reminder_due_by=reminder,
        save=lambda *, update_fields: saved_fields.append(tuple(update_fields)),
    )
    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: SimpleNamespace(protocol=SimpleNamespace(close=lambda: None)),
    )
    gateway._resolve_item = lambda _reference, _kind: (None, item)  # type: ignore[method-assign]
    gateway._remember = lambda *_args, **_kwargs: "task-local"  # type: ignore[method-assign]

    updated = gateway.update_task(
        SimpleNamespace(reference="task-local", changes={"reminder_at": None})
    )

    assert updated.reminder_at is None
    assert item.reminder_due_by is None
    assert item.reminder_is_set is False
    assert saved_fields == [("reminder_due_by", "reminder_is_set")]


def test_task_create_kwargs_include_explicit_reminder_switch() -> None:
    reminder = datetime(2026, 8, 9, 8, 30, tzinfo=UTC)
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())

    kwargs = gateway._task_kwargs(
        TaskInfo(
            reference="task-new",
            subject="Reminder task",
            status="not_started",
            reminder_at=reminder,
        )
    )

    assert kwargs["reminder_is_set"] is True
    assert kwargs["reminder_due_by"] == reminder


def test_task_this_week_view_uses_a_bounded_seven_day_query() -> None:
    gateway = _TasksGateway(_task())
    backend = TasksServiceBackend(TaskService(gateway))

    values = backend.list_tasks(
        "",
        "this_week",
        100,
        cancel=Event(),
        progress=lambda _event: None,
    )

    assert values == (_task(),)
    assert len(gateway.queries) == 1
    query = gateway.queries[0]
    assert query.due_after == date.today() - timedelta(days=1)
    assert query.due_before == date.today() + timedelta(days=7)


@pytest.mark.parametrize(
    "view, days", [("today", {0}), ("this_week", set(range(7))), ("due", {-1, 0})]
)
def test_task_date_views_filter_real_adapter_boundaries(view, days):
    today = date.today()
    objects = [
        SimpleNamespace(
            id=f"synthetic-{day}",
            changekey="key",
            subject="Boundary",
            status="NotStarted",
            percent_complete=0,
            due_date=today + timedelta(days=day),
        )
        for day in range(-1, 8)
    ]
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    gateway._tasks_folder = lambda _: objects
    gateway._query_source = lambda folder: iter(folder)
    gateway._remember = lambda kind, item, **kwargs: item.id
    backend = TasksServiceBackend(TaskService(gateway))
    values = backend.list_tasks("", view, 100, cancel=Event(), progress=lambda _: None)
    assert {(value.due_date - today).days for value in values} == days
