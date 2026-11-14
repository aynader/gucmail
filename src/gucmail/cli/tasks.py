"""Tasks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
)
from gucmail.models import (
    Importance,
)
from gucmail.services.gateway import DeferredGateway
from gucmail.services.pim_preferences import (
    UNSET as PIM_UNSET,
)
from gucmail.services.pim_preferences import TaskCreationPreferences
from gucmail.services.task_recurrence import TaskRepeatKind, TaskRepeatPreferences
from gucmail.services.tasks import TaskPatch, TaskQuery, TaskService, TaskStatus
from gucmail.services.tasks_display import TaskDisplayPreferences, TaskSortOrder, TaskView

tasks_app = typer.Typer(help="Your to-do tasks.")


@tasks_app.command("list")
def tasks_list(
    ctx: typer.Context,
    view: TaskView | None = typer.Option(None, case_sensitive=False),
    completed: bool | None = typer.Option(
        None, "--completed/--no-completed", help="Override ordinary completed-task visibility."
    ),
    sort: TaskSortOrder | None = typer.Option(None, case_sensitive=False),
    limit: int = typer.Option(200, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """List one bounded task page using saved view, visibility and sorting."""

    warnings: list[str] = []

    def action() -> Any:
        display = TaskDisplayPreferences.from_settings(
            core.settings_service(ctx), view=view, show_completed=completed, sort_order=sort
        )
        query = display.query()
        result = TaskService(DeferredGateway(lambda: core.gateway(ctx))).page(
            query, limit=limit, offset=offset
        )
        return core.pim_page_output(display.page(result, query), details=page, warnings=warnings)

    core.run(
        ctx,
        "tasks list",
        action,
        title="Tasks",
        source="live_server",
        warnings=warnings,
    )


@tasks_app.command("search")
def tasks_search(
    ctx: typer.Context,
    text: str | None = typer.Argument(None),
    status: TaskStatus | None = typer.Option(None, case_sensitive=False),
    due_before: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    overdue: bool = typer.Option(False),
    completed: bool | None = typer.Option(
        None, "--completed/--no-completed", help="Explicit status takes precedence."
    ),
    sort: TaskSortOrder | None = typer.Option(None, case_sensitive=False),
    limit: int = typer.Option(200, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """Search tasks by text, status, or due date."""

    query = TaskQuery(
        text=text,
        status=status,
        due_before=core.iso_date(due_before, parameter="--due-before"),
    )
    warnings: list[str] = []

    def action() -> Any:
        if not any((query.text, query.status, query.due_before, overdue)):
            raise ConfigurationError("Task search needs at least one criterion.")
        display = TaskDisplayPreferences.from_settings(
            core.settings_service(ctx), show_completed=completed, sort_order=sort
        )
        criteria = display.search_query(
            replace(query, overdue_at=display.today() if overdue else None)
        )
        result = TaskService(DeferredGateway(lambda: core.gateway(ctx))).page(
            criteria, limit=limit, offset=offset
        )
        return core.pim_page_output(
            display.page(result, criteria),
            details=page,
            warnings=warnings,
        )

    core.run(
        ctx,
        "tasks search",
        action,
        title="Task results",
        warnings=warnings,
    )


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one task."""

    core.run(ctx, "tasks show", lambda: TaskService(core.gateway(ctx)).get(reference), title="Task")


@tasks_app.command("history")
def tasks_history(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    limit: int = typer.Option(20, min=1, max=200),
    before: int | None = typer.Option(None, min=1, help="Continue before the returned sequence."),
) -> None:
    """Read bounded local accepted-update history without contacting Exchange."""
    from gucmail.services.task_history import TaskHistory, history_reference, history_text

    def read() -> Any:
        checked = history_reference(reference)
        return TaskHistory(core.cli_state(ctx).context.state).page(
            checked, limit=limit, before=before
        )

    core.run(
        ctx,
        "tasks history",
        read,
        title="Local Task history",
        source="local",
        human_data=history_text,
    )


@tasks_app.command("create")
def tasks_create(
    ctx: typer.Context,
    subject: str = typer.Option(...),
    due: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    no_due: bool = typer.Option(
        False, "--no-due", help="Leave the due date unset instead of using the saved default."
    ),
    start: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    reminder_at: str | None = typer.Option(
        None,
        help="ISO timestamp with UTC offset, for example 2026-08-24T09:00:00+02:00.",
    ),
    no_reminder: bool = typer.Option(
        False, "--no-reminder", help="Disable the saved reminder for this task."
    ),
    body: str = typer.Option(""),
    importance: Importance | None = typer.Option(
        None, case_sensitive=False, help="Override the saved importance."
    ),
    repeat: TaskRepeatKind | None = typer.Option(
        None,
        case_sensitive=False,
        help="Override the saved repeat pattern; after_completion_* uses task regeneration; "
        "none disables repetition.",
    ),
    repeat_interval: int | None = typer.Option(
        None,
        help="Repeat every N periods; fixed-date yearly requires 1; after-completion uses 1-999.",
    ),
    repeat_count: int | None = typer.Option(
        None, help="Occurrence limit, including the first (1-999)."
    ),
    repeat_forever: bool = typer.Option(
        False, help="Clear the saved occurrence limit for this task."
    ),
    repeat_until: str | None = typer.Option(
        None, help="Last repeating date (YYYY-MM-DD); overrides the saved count."
    ),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create an Exchange task."""

    def action() -> Any:
        if no_due and due is not None:
            raise ConfigurationError("Choose either --due or --no-due.")
        if no_reminder and reminder_at is not None:
            raise ConfigurationError("Choose either --reminder-at or --no-reminder.")
        settings = core.settings_service(ctx)
        preferences = TaskCreationPreferences.from_settings(settings)
        if sum((repeat_count is not None, repeat_forever, repeat_until is not None)) > 1:
            raise ConfigurationError("Choose --repeat-count, --repeat-forever or --repeat-until.")
        repeat_policy = TaskRepeatPreferences(
            repeat if repeat is not None else preferences.repeat.kind,
            repeat_interval if repeat_interval is not None else preferences.repeat.interval,
            repeat_count
            if repeat_count is not None
            else None
            if repeat_forever or repeat_until is not None
            else preferences.repeat.count,
        )
        if repeat_policy.kind is TaskRepeatKind.NONE and any(
            (
                repeat_interval is not None,
                repeat_count is not None,
                repeat_forever,
                repeat_until is not None,
            )
        ):
            raise ConfigurationError("Choose a repeat pattern before supplying repeat options.")
        service = TaskService(DeferredGateway(lambda: core.gateway(ctx)))
        task = replace(
            preferences.new_task(
                subject=subject,
                timezone=str(settings.value("calendar.timezone").effective_value),
                start_date=core.iso_date(start, parameter="--start"),
                due_date=None
                if no_due
                else core.iso_date(due, parameter="--due")
                if due is not None
                else PIM_UNSET,
                reminder_at=None
                if no_reminder
                else core.iso_datetime(reminder_at, parameter="--reminder-at")
                if reminder_at is not None
                else PIM_UNSET,
                importance=importance,
                repeat=repeat_policy,
                repeat_until=core.iso_date(repeat_until, parameter="--repeat-until"),
            ),
            body=body,
        )
        plan = service.plan_create(task)
        if dry_run:
            return plan
        core.confirm(ctx, f"Create task {subject!r}?", f"task-create:{subject}")
        return service.gateway.create_task(plan)

    core.run(ctx, "tasks create", action, title="Task creation")


@tasks_app.command("edit")
@tasks_app.command("update")
def tasks_update(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    subject: str | None = typer.Option(None),
    due: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    reminder_at: str | None = typer.Option(
        None,
        help="ISO timestamp with UTC offset.",
    ),
    clear_reminder: bool = typer.Option(False, help="Disable the task reminder."),
    body: str | None = typer.Option(None),
    percent: int | None = typer.Option(None, min=0, max=100),
    dry_run: bool = typer.Option(False),
) -> None:
    """Update only supplied task fields."""

    if reminder_at is not None and clear_reminder:
        raise typer.BadParameter(
            "use either --reminder-at or --clear-reminder, not both",
            param_hint="--reminder-at",
        )

    def action() -> Any:
        changes: dict[str, Any] = {
            name: value
            for name, value in (
                ("subject", subject),
                ("due_date", core.iso_date(due, parameter="--due")),
                ("body", body),
                ("percent_complete", percent),
            )
            if value is not None
        }
        if reminder_at is not None:
            changes["reminder_at"] = core.iso_datetime(reminder_at, parameter="--reminder-at")
        elif clear_reminder:
            changes["reminder_at"] = None
        service = TaskService(core.gateway(ctx))
        current = None
        if percent is not None:
            # --percent is the only progress control here, so it must move the
            # lifecycle state with it instead of tripping the status invariants.
            if percent == 100:
                raise ConfigurationError(
                    "Use 'gucmail tasks complete' to mark a task 100 percent complete."
                )
            current = service.get(reference)
            if current.status == TaskStatus.COMPLETED.value:
                raise ConfigurationError(
                    "This task is completed. Use 'gucmail tasks reopen' before changing progress."
                )
            if percent > 0 and current.status == TaskStatus.NOT_STARTED.value:
                changes["status"] = TaskStatus.IN_PROGRESS.value
        plan = service.plan_update(reference, TaskPatch(**changes), current=current)
        if dry_run:
            return plan
        core.confirm(ctx, f"Update task {reference}?", f"task-update:{reference}")
        return service.gateway.update_task(plan)

    core.run(ctx, "tasks update", action, title="Task update")


@tasks_app.command("complete")
def tasks_complete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Complete a task using explicit status and percentage fields."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_complete(reference)
        if dry_run:
            return plan
        core.confirm(ctx, f"Complete task {reference}?", f"task-complete:{reference}")
        return core.gateway(ctx).update_task(plan)

    core.run(ctx, "tasks complete", action, title="Task complete")


@tasks_app.command("reopen")
def tasks_reopen(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Reopen a completed task."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_reopen(reference)
        if dry_run:
            return plan
        core.confirm(ctx, f"Reopen task {reference}?", f"task-reopen:{reference}")
        return core.gateway(ctx).update_task(plan)

    core.run(ctx, "tasks reopen", action, title="Task reopen")


@tasks_app.command("delete")
def tasks_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(
        False, help="Permanently delete this task occurrence; otherwise move the stored task."
    ),
    series: bool = typer.Option(
        False, help="With --permanent, delete the entire recurring task series."
    ),
    confirm_series: str | None = typer.Option(
        None, help="For actual series deletion, repeat the exact task reference after reviewing."
    ),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move a task to Deleted Items; permanent deletion is explicit."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_delete(reference, hard_delete=permanent, delete_series=series)
        if confirm_series is not None and (not series or confirm_series != reference):
            raise ConfigurationError(
                "--confirm-series must match the task reference with --series."
            )
        if dry_run:
            return plan
        if series and confirm_series != reference:
            raise ConfirmationRequiredError(
                "Permanent series deletion requires --confirm-series with the exact task reference "
                "as well as ordinary confirmation. Review --dry-run first."
            )
        scope = (
            "whole recurring series"
            if series
            else "this occurrence"
            if permanent
            else "stored task"
        )
        core.confirm(
            ctx, f"Delete {scope} {reference}? Permanent: {permanent}.", f"task-delete:{reference}"
        )
        TaskService(core.gateway(ctx)).delete(plan)
        return {"reference": reference, "deleted": True, "permanent": permanent, "series": series}

    core.run(ctx, "tasks delete", action, title="Task deletion")


@tasks_app.command("export")
def tasks_export(
    ctx: typer.Context,
    output: Path = typer.Option(...),
    format: str = typer.Option("json", help="json or csv"),
    limit: int = typer.Option(200, min=1, max=200),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing export file."),
) -> None:
    """Export a bounded task list to JSON or CSV."""

    def action() -> dict[str, Any]:
        service = TaskService(core.gateway(ctx))
        tasks = service.list(limit=limit)
        content = service.export(tasks, format=format)
        # Same overwrite policy as calendar/contacts export: never silently
        # replace an existing user file.
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            core.confirm(ctx, f"Overwrite existing task export {output}?", f"tasks-export:{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "tasks": len(tasks), "format": format}

    core.run(ctx, "tasks export", action, title="Task export")
