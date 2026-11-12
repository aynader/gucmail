"""High-level task lifecycle with explicit, transport-neutral update semantics."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable

from gucmail.errors import ConfigurationError
from gucmail.models import Importance, TaskInfo, to_primitive
from gucmail.services.pim import (
    MAX_PIM_PAGE_SIZE,
    BatchFailure,
    BatchOperationResult,
    BatchSuccess,
    PimPage,
    bounded_pim_limit,
)


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()
MaybeUnset: TypeAlias = Any | _Unset


class TaskStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING_ON_OTHERS = "waiting_on_others"
    DEFERRED = "deferred"


class TaskLifecycleAction(StrEnum):
    EDIT = "edit"
    COMPLETE = "complete"
    REOPEN = "reopen"


class TaskDeletionMode(StrEnum):
    MOVE = "move"
    OCCURRENCE = "occurrence"
    SERIES = "series"

    @property
    def hard_delete(self) -> bool:
        return self is not TaskDeletionMode.MOVE

    @property
    def delete_series(self) -> bool:
        return self is TaskDeletionMode.SERIES

    def plan(self, reference: str) -> TaskDeletePlan:
        return TaskDeletePlan(reference, self.hard_delete, self.delete_series)


@dataclass(frozen=True, slots=True)
class TaskQuery:
    text: str | None = None
    status: TaskStatus | None = None
    due_before: date | None = None
    due_after: date | None = None
    category: str | None = None
    overdue_at: date | None = None
    exclude_completed: bool = False


@dataclass(frozen=True, slots=True)
class TaskCreatePlan:
    task: TaskInfo
    folder_reference: str | None = None


@dataclass(frozen=True, slots=True)
class TaskPatch:
    """Explicit task changes.

    ``UNSET`` preserves a server field. ``None`` explicitly clears fields where null is
    valid (dates, completion/reminder times, and recurrence).  This distinction prevents the EWS
    adapter from using an unrestricted save on a partially fetched task.
    """

    subject: MaybeUnset = UNSET
    status: MaybeUnset = UNSET
    percent_complete: MaybeUnset = UNSET
    start_date: MaybeUnset = UNSET
    due_date: MaybeUnset = UNSET
    completed_at: MaybeUnset = UNSET
    body: MaybeUnset = UNSET
    categories: MaybeUnset = UNSET
    importance: MaybeUnset = UNSET
    reminder_at: MaybeUnset = UNSET
    recurrence: MaybeUnset = UNSET

    def changes(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("subject", self.subject),
                ("status", self.status),
                ("percent_complete", self.percent_complete),
                ("start_date", self.start_date),
                ("due_date", self.due_date),
                ("completed_at", self.completed_at),
                ("body", self.body),
                ("categories", self.categories),
                ("importance", self.importance),
                ("reminder_at", self.reminder_at),
                ("recurrence", self.recurrence),
            )
            if value is not UNSET
        }


@dataclass(frozen=True, slots=True)
class TaskUpdatePlan:
    reference: str
    changes: Mapping[str, Any]
    action: TaskLifecycleAction = TaskLifecycleAction.EDIT

    @property
    def update_fields(self) -> tuple[str, ...]:
        """Exact adapter field allowlist; this is intentionally never empty."""

        return tuple(self.changes)


@dataclass(frozen=True, slots=True)
class TaskDeletePlan:
    reference: str
    hard_delete: bool = False
    delete_series: bool = False

    def __post_init__(self) -> None:
        _reference(self.reference)
        if type(self.hard_delete) is not bool or type(self.delete_series) is not bool:
            raise ConfigurationError("Task deletion choices must be booleans.")
        if self.delete_series and not self.hard_delete:
            raise ConfigurationError(
                "Task series deletion requires explicit permanent deletion. "
                "Moving a stored task to Deleted Items cannot skip an occurrence."
            )


@runtime_checkable
class TasksGateway(Protocol):
    """Provider-neutral task persistence contract implemented by the EWS adapter."""

    def list_tasks(self, *, folder_reference: str | None, limit: int) -> Sequence[TaskInfo]: ...

    def search_tasks(
        self,
        query: TaskQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> Sequence[TaskInfo]: ...

    def get_task(self, reference: str) -> TaskInfo: ...

    def create_task(self, plan: TaskCreatePlan) -> TaskInfo: ...

    def update_task(self, plan: TaskUpdatePlan) -> TaskInfo: ...

    def delete_task(self, plan: TaskDeletePlan) -> None: ...


@runtime_checkable
class PagedTasksGateway(Protocol):
    def search_tasks_page(
        self, query: TaskQuery, *, folder_reference: str | None, limit: int, offset: int = 0
    ) -> PimPage[TaskInfo]: ...


def normalize_status(value: TaskStatus | str) -> TaskStatus:
    if isinstance(value, TaskStatus):
        return value
    key = value.strip().replace("-", "_").replace(" ", "_").casefold()
    aliases = {
        "notstarted": TaskStatus.NOT_STARTED,
        "not_started": TaskStatus.NOT_STARTED,
        "inprogress": TaskStatus.IN_PROGRESS,
        "in_progress": TaskStatus.IN_PROGRESS,
        "completed": TaskStatus.COMPLETED,
        "waitingonothers": TaskStatus.WAITING_ON_OTHERS,
        "waiting_on_others": TaskStatus.WAITING_ON_OTHERS,
        "deferred": TaskStatus.DEFERRED,
    }
    try:
        return aliases[key]
    except KeyError as exc:
        allowed = ", ".join(item.value for item in TaskStatus)
        raise ConfigurationError(f"Unknown task status {value!r}. Use one of: {allowed}.") from exc


def normalize_task(task: TaskInfo) -> TaskInfo:
    """Validate a complete task returned by a gateway or supplied for creation."""

    subject = task.subject.strip()
    if not subject:
        raise ConfigurationError("A task needs a subject.")
    status = normalize_status(task.status)
    percent = _percent(task.percent_complete)
    start = _date_or_none(task.start_date, field="start date")
    due = _date_or_none(task.due_date, field="due date")
    if start is not None and due is not None and due < start:
        raise ConfigurationError("A task due date cannot be before its start date.")
    completed_at = task.completed_at
    if completed_at is not None and completed_at.tzinfo is None:
        raise ConfigurationError("A task completion time must include a time zone.")
    reminder_at = task.reminder_at
    if reminder_at is not None and reminder_at.tzinfo is None:
        raise ConfigurationError("A task reminder time must include a time zone.")
    if status is TaskStatus.COMPLETED and percent != 100:
        raise ConfigurationError("A completed task must be 100 percent complete.")
    if status is not TaskStatus.COMPLETED and percent == 100:
        raise ConfigurationError("A 100 percent task must have completed status.")
    if status is TaskStatus.NOT_STARTED and percent != 0:
        raise ConfigurationError("A not-started task must be zero percent complete.")
    if status is not TaskStatus.COMPLETED and completed_at is not None:
        raise ConfigurationError("Only a completed task may have a completion time.")
    if task.recurrence is not None and not task.recurrence:
        raise ConfigurationError("Task recurrence may be null, but not an empty rule.")
    try:
        importance = Importance(str(task.importance).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown task importance: {task.importance!r}") from exc
    if not isinstance(task.categories, (tuple, list)) or any(
        not isinstance(item, str) for item in task.categories
    ):
        raise ConfigurationError(
            "Task categories must be a sequence of text labels. "
            "Use an empty sequence to clear them."
        )
    categories = tuple(dict.fromkeys(item.strip() for item in task.categories if item.strip()))
    return replace(
        task,
        subject=subject,
        status=status.value,
        percent_complete=percent,
        start_date=start,
        due_date=due,
        reminder_at=reminder_at.astimezone(UTC) if reminder_at is not None else None,
        categories=categories,
        importance=importance,
    )


def _percent(value: int | Decimal) -> int:
    if isinstance(value, bool):
        raise ConfigurationError("Task completion percentage must be a number from 0 to 100.")
    try:
        decimal_value = Decimal(value)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            "Task completion percentage must be a number from 0 to 100."
        ) from exc
    if decimal_value != decimal_value.to_integral_value() or not 0 <= decimal_value <= 100:
        raise ConfigurationError("Task completion percentage must be a whole number from 0 to 100.")
    return int(decimal_value)


def _date_or_none(value: date | None, *, field: str) -> date | None:
    if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
        raise ConfigurationError(f"Task {field} must be a date without a time.")
    return value


class TaskService:
    """Task application service shared by CLI and TUI surfaces."""

    def __init__(
        self,
        gateway: TasksGateway | None = None,
        *,
        default_folder: str | None = None,
        default_limit: int = 200,
        clock: Any | None = None,
    ) -> None:
        if default_limit <= 0:
            raise ConfigurationError("Task result limit must be positive.")
        self._gateway = gateway
        self.default_folder = default_folder
        self.default_limit = bounded_pim_limit(
            None,
            default=default_limit,
            subject="Task",
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def gateway(self) -> TasksGateway:
        """Pure lifecycle plans need no provider; execution requires one."""

        if self._gateway is None:
            raise ConfigurationError("Task execution requires a connected provider.")
        return self._gateway

    def list(
        self, *, folder_reference: str | None = None, limit: int | None = None
    ) -> tuple[TaskInfo, ...]:
        values = self.gateway.list_tasks(
            folder_reference=folder_reference or self.default_folder,
            limit=self._limit(limit),
        )
        return tuple(
            sorted(
                (normalize_task(task) for task in values),
                key=lambda task: (
                    task.status == TaskStatus.COMPLETED.value,
                    task.due_date is None,
                    task.due_date or date.max,
                    task.subject.casefold(),
                    task.reference,
                ),
            )
        )

    def search(
        self,
        query: TaskQuery | str,
        *,
        folder_reference: str | None = None,
        limit: int | None = None,
    ) -> tuple[TaskInfo, ...]:
        resolved = TaskQuery(text=query.strip()) if isinstance(query, str) else query
        if not any(
            (
                resolved.text,
                resolved.status,
                resolved.due_before,
                resolved.due_after,
                resolved.category,
                resolved.overdue_at,
                resolved.exclude_completed,
            )
        ):
            raise ConfigurationError("Task search needs at least one criterion.")
        if (
            resolved.due_before is not None
            and resolved.due_after is not None
            and resolved.due_before < resolved.due_after
        ):
            raise ConfigurationError("Task due-before date cannot precede due-after date.")
        values = self.gateway.search_tasks(
            resolved,
            folder_reference=folder_reference or self.default_folder,
            limit=self._limit(limit),
        )
        return tuple(normalize_task(task) for task in values)

    def page(
        self,
        query: TaskQuery | str = "",
        *,
        folder_reference: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PimPage[TaskInfo]:
        """Browse or search one candidate window with explicit continuation."""
        if offset < 0:
            raise ConfigurationError("Task offset cannot be negative.")
        resolved = TaskQuery(text=query.strip()) if isinstance(query, str) else query
        if resolved.due_before and resolved.due_after and resolved.due_before < resolved.due_after:
            raise ConfigurationError("Task due-before date cannot precede due-after date.")
        bounded = self._limit(limit)
        if isinstance(self.gateway, PagedTasksGateway):
            page = self.gateway.search_tasks_page(
                resolved,
                folder_reference=folder_reference or self.default_folder,
                limit=bounded,
                offset=offset,
            )
            return replace(page, items=tuple(normalize_task(item) for item in page.items))
        if offset:
            raise ConfigurationError("This task provider does not support continuation.")
        values = (
            self.search(resolved, folder_reference=folder_reference, limit=bounded)
            if any(
                (
                    resolved.text,
                    resolved.status,
                    resolved.due_before,
                    resolved.due_after,
                    resolved.category,
                    resolved.overdue_at,
                    resolved.exclude_completed,
                )
            )
            else self.list(folder_reference=folder_reference, limit=bounded)
        )
        return PimPage(
            values, 0, len(values), residual="This provider exposes a single bounded view."
        )

    def get(self, reference: str) -> TaskInfo:
        _reference(reference)
        return normalize_task(self.gateway.get_task(reference))

    def plan_create(self, task: TaskInfo, *, folder_reference: str | None = None) -> TaskCreatePlan:
        return TaskCreatePlan(normalize_task(task), folder_reference or self.default_folder)

    @staticmethod
    def _mutation_result(result: TaskInfo) -> TaskInfo:
        from gucmail.errors import MutationResultUnavailableError

        try:
            return normalize_task(result)
        except Exception as exc:
            raise MutationResultUnavailableError(
                "The Task operation returned, but its result could not be validated. "
                "The mailbox may already have changed; do not repeat the operation automatically.",
                suggestion=(
                    "Refresh the bounded Tasks list and inspect the original and returned Task."
                ),
            ) from exc

    def create(self, task: TaskInfo, *, folder_reference: str | None = None) -> TaskInfo:
        return self._mutation_result(
            self.gateway.create_task(self.plan_create(task, folder_reference=folder_reference))
        )

    def plan_update(
        self,
        reference: str,
        patch: TaskPatch,
        *,
        current: TaskInfo | None = None,
    ) -> TaskUpdatePlan:
        _reference(reference)
        changes = patch.changes()
        if not changes:
            raise ConfigurationError("No task fields were supplied for update.")
        # Status, completion percentage, completion timestamp, and start/due dates
        # form invariants that cannot be checked safely from an isolated patch.
        current = self.get(reference) if current is None else normalize_task(current)
        updated = normalize_task(replace(current, **changes))
        changes = {name: getattr(updated, name) for name in changes}
        return TaskUpdatePlan(reference, dict(changes), TaskLifecycleAction.EDIT)

    def update(
        self,
        reference: str,
        patch: TaskPatch,
        *,
        current: TaskInfo | None = None,
    ) -> TaskInfo:
        return self._mutation_result(
            self.gateway.update_task(self.plan_update(reference, patch, current=current))
        )

    def plan_complete(
        self,
        reference: str,
        *,
        completed_at: datetime | None = None,
    ) -> TaskUpdatePlan:
        _reference(reference)
        timestamp = completed_at or self._clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ConfigurationError("Task completion time must include a time zone.")
        return TaskUpdatePlan(
            reference,
            {
                "status": TaskStatus.COMPLETED.value,
                "percent_complete": 100,
                "completed_at": timestamp.astimezone(UTC),
            },
            TaskLifecycleAction.COMPLETE,
        )

    def complete(self, reference: str, *, completed_at: datetime | None = None) -> TaskInfo:
        return self._mutation_result(
            self.gateway.update_task(self.plan_complete(reference, completed_at=completed_at))
        )

    def plan_reopen(
        self,
        reference: str,
        *,
        status: TaskStatus | str = TaskStatus.NOT_STARTED,
        percent_complete: int = 0,
    ) -> TaskUpdatePlan:
        _reference(reference)
        resolved_status = normalize_status(status)
        percent = _percent(percent_complete)
        if resolved_status is TaskStatus.COMPLETED:
            raise ConfigurationError("Reopening a task requires a non-completed status.")
        if resolved_status is TaskStatus.NOT_STARTED and percent != 0:
            raise ConfigurationError("A not-started reopened task must be zero percent complete.")
        if percent == 100:
            raise ConfigurationError("A reopened task cannot be 100 percent complete.")
        return TaskUpdatePlan(
            reference,
            {
                "status": resolved_status.value,
                "percent_complete": percent,
                "completed_at": None,
            },
            TaskLifecycleAction.REOPEN,
        )

    def reopen(
        self,
        reference: str,
        *,
        status: TaskStatus | str = TaskStatus.NOT_STARTED,
        percent_complete: int = 0,
    ) -> TaskInfo:
        return self._mutation_result(
            self.gateway.update_task(
                self.plan_reopen(
                    reference,
                    status=status,
                    percent_complete=percent_complete,
                )
            )
        )

    def complete_many(
        self,
        references: Iterable[str],
        *,
        completed_at: datetime | None = None,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[TaskInfo]:
        """Complete a bounded task batch with reconciliation-safe outcomes."""

        values = _batch_references(references)
        timestamp = completed_at or self._clock()
        # Validate the shared timestamp before the first mutation.
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ConfigurationError("Task completion time must include a time zone.")
        return self._update_many(
            values,
            operation="complete task",
            plan=lambda reference: self.plan_complete(reference, completed_at=timestamp),
            continue_on_error=continue_on_error,
        )

    def reopen_many(
        self,
        references: Iterable[str],
        *,
        status: TaskStatus | str = TaskStatus.NOT_STARTED,
        percent_complete: int = 0,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[TaskInfo]:
        """Reopen a bounded task batch while retaining partial-failure detail."""

        values = _batch_references(references)
        # Normalize lifecycle inputs once before mutating any task.
        resolved_status = normalize_status(status)
        resolved_percent = _percent(percent_complete)
        return self._update_many(
            values,
            operation="reopen task",
            plan=lambda reference: self.plan_reopen(
                reference,
                status=resolved_status,
                percent_complete=resolved_percent,
            ),
            continue_on_error=continue_on_error,
        )

    def _update_many(
        self,
        references: tuple[str, ...],
        *,
        operation: str,
        plan: Any,
        continue_on_error: bool,
    ) -> BatchOperationResult[TaskInfo]:
        completed: list[BatchSuccess[TaskInfo]] = []
        failures: list[BatchFailure] = []
        skipped: list[str] = []
        for index, reference in enumerate(references):
            try:
                value = normalize_task(self.gateway.update_task(plan(reference)))
            except Exception as exc:
                failures.append(
                    BatchFailure.from_exception(
                        index=index,
                        operation=operation,
                        reference=reference,
                        error=exc,
                        # A transport failure may have happened after Exchange
                        # accepted the update, so an automatic retry is unsafe.
                        retry_safe=False,
                    )
                )
                if not continue_on_error:
                    skipped.extend(references[index + 1 :])
                    break
            else:
                completed.append(
                    BatchSuccess(
                        index=index,
                        operation=operation,
                        reference=value.reference,
                        value=value,
                    )
                )
        return BatchOperationResult(
            attempted=len(references),
            completed=tuple(completed),
            failures=tuple(failures),
            skipped_references=tuple(skipped),
        )

    def plan_delete(
        self,
        reference: str,
        *,
        hard_delete: bool = False,
        delete_series: bool = False,
    ) -> TaskDeletePlan:
        _reference(reference)
        return TaskDeletePlan(reference, hard_delete, delete_series)

    def delete(self, plan: TaskDeletePlan) -> None:
        self.gateway.delete_task(plan)

    def delete_many(
        self,
        references: Iterable[str],
        *,
        hard_delete: bool = False,
        delete_series: bool = False,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[None]:
        """Delete a bounded batch and report every completed or uncertain step."""

        values = _batch_references(references)
        completed: list[BatchSuccess[None]] = []
        failures: list[BatchFailure] = []
        skipped: list[str] = []
        for index, reference in enumerate(values):
            try:
                self.gateway.delete_task(
                    self.plan_delete(
                        reference,
                        hard_delete=hard_delete,
                        delete_series=delete_series,
                    )
                )
            except Exception as exc:
                failures.append(
                    BatchFailure.from_exception(
                        index=index,
                        operation="delete task",
                        reference=reference,
                        error=exc,
                        retry_safe=False,
                    )
                )
                if not continue_on_error:
                    skipped.extend(values[index + 1 :])
                    break
            else:
                completed.append(
                    BatchSuccess(
                        index=index,
                        operation="delete task",
                        reference=reference,
                    )
                )
        return BatchOperationResult(
            attempted=len(values),
            completed=tuple(completed),
            failures=tuple(failures),
            skipped_references=tuple(skipped),
        )

    def export(
        self,
        tasks: TaskInfo | Iterable[TaskInfo],
        *,
        format: str = "json",
    ) -> str:
        """Export selected tasks as deterministic JSON or CSV without a server call."""

        values = (tasks,) if isinstance(tasks, TaskInfo) else tuple(tasks)
        normalized = tuple(normalize_task(task) for task in values)
        if not normalized:
            raise ConfigurationError("At least one task is required for export.")
        format_name = format.casefold()
        if format_name == "json":
            return (
                json.dumps(to_primitive(normalized), ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
        if format_name != "csv":
            raise ConfigurationError("Task export format must be 'json' or 'csv'.")
        output = io.StringIO(newline="")
        fieldnames = (
            "reference",
            "subject",
            "status",
            "percent_complete",
            "start_date",
            "due_date",
            "completed_at",
            "body",
            "categories",
            "importance",
            "reminder_at",
            "recurrence",
            "source",
        )
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for task in normalized:
            writer.writerow(
                {
                    "reference": task.reference,
                    "subject": task.subject,
                    "status": task.status,
                    "percent_complete": task.percent_complete,
                    "start_date": task.start_date.isoformat() if task.start_date else "",
                    "due_date": task.due_date.isoformat() if task.due_date else "",
                    "completed_at": task.completed_at.isoformat() if task.completed_at else "",
                    "body": task.body,
                    "categories": ";".join(task.categories),
                    "importance": task.importance.value,
                    "reminder_at": task.reminder_at.isoformat() if task.reminder_at else "",
                    "recurrence": json.dumps(task.recurrence, sort_keys=True)
                    if task.recurrence
                    else "",
                    "source": task.source.value,
                }
            )
        return output.getvalue()

    def _limit(self, value: int | None) -> int:
        return bounded_pim_limit(
            value,
            default=self.default_limit,
            subject="Task",
        )


def _reference(value: str) -> str:
    if not value.strip():
        raise ConfigurationError("A task reference is required.")
    return value


def _batch_references(references: Iterable[str]) -> tuple[str, ...]:
    """Materialize one bounded, duplicate-free task mutation batch."""

    values: list[str] = []
    for value in references:
        if len(values) >= MAX_PIM_PAGE_SIZE:
            raise ConfigurationError(
                f"Task batch operations are limited to {MAX_PIM_PAGE_SIZE} references."
            )
        values.append(_reference(value))
    if not values:
        raise ConfigurationError("A task batch needs at least one reference.")
    if len(set(values)) != len(values):
        raise ConfigurationError("A task reference may appear only once in a batch.")
    return tuple(values)


__all__ = [
    "UNSET",
    "TaskCreatePlan",
    "TaskDeletePlan",
    "TaskLifecycleAction",
    "TaskPatch",
    "TaskQuery",
    "TaskService",
    "TaskStatus",
    "TaskUpdatePlan",
    "TasksGateway",
    "normalize_status",
    "normalize_task",
]
