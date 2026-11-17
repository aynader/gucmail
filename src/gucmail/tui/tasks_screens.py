"""Tasks screens: the task list and task editor."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from threading import Event
from typing import ClassVar, cast
from uuid import uuid4

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError
from gucmail.formatting import friendly_date
from gucmail.models import (
    Importance,
    TaskInfo,
)
from gucmail.services.pim import PimPage
from gucmail.services.pim_preferences import TaskCreationPreferences
from gucmail.services.recurrence import normalize_recurrence_mapping
from gucmail.services.task_history import TaskHistoryPage
from gucmail.services.task_history import history_text as task_history_text
from gucmail.services.task_recurrence import TaskRepeatKind, TaskRepeatPreferences
from gucmail.services.tasks import TaskDeletionMode, TaskStatus
from gucmail.services.tasks_display import TaskDisplayPreferences, TaskView
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.pim_backends import (
    TasksServiceBackend,
    _bounded_limit,
    _csv,
    _parse_date,
    _parse_optional_aware_datetime,
    _parse_recurrence,
)
from gucmail.tui.pim_common import (
    BatchActionResult,
    ExportResult,
    FilePathScreen,
    TaskEditorValue,
    TasksScreenBackend,
    _batch_message,
    _capability,
    _capability_summary,
    _PagedPimScreen,
    _PimEditorScreen,
)


class TaskEditorScreen(_PimEditorScreen[TaskEditorValue | None]):
    """Validated Tasks create/edit form."""

    DEFAULT_CSS = """
    TaskEditorScreen { align: center middle; }
    #task-editor {
        width: 96%; max-width: 94; height: 96%; max-height: 36; border: round $border;
        background: $surface; padding: 1 2;
    }
    #task-editor-title { height: 2; text-style: bold; }
    #task-editor-fields { height: 1fr; }
    #task-editor-fields Input, #task-editor-fields Select { height: 3; }
    #task-editor-body { height: 7; }
    #task-repeat-options { height: auto; }
    #task-editor-error { min-height: 1; height: auto; color: $error; }
    #task-editor-actions { height: 3; align-horizontal: right; }
    #task-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(
        self,
        task: TaskInfo | None,
        *,
        creating: bool = False,
        focus_field: str | None = None,
    ) -> None:
        super().__init__()
        self.task_info = task
        self.creating = creating
        self.focus_field = focus_field

    def on_mount(self) -> None:
        self._update_repeat_controls()
        selector = {
            "reminder": "#task-edit-reminder",
            "due_date": "#task-edit-due",
            "priority": "#task-edit-importance",
        }.get(self.focus_field or "")
        if selector:
            self.query_one(selector).focus()

    def compose(self) -> ComposeResult:
        task = self.task_info
        repeat_kind, repeat_interval, repeat_count, repeat_until = "none", "1", "", ""
        if task and task.recurrence:
            repeat_kind = "advanced"
            if self.creating:
                try:
                    rule = normalize_recurrence_mapping(
                        task.recurrence, task=True, start=task.start_date or task.due_date
                    )
                    pattern, boundary = rule["pattern"], rule["boundary"]
                    if not isinstance(pattern, Mapping) or not isinstance(boundary, Mapping):
                        raise TypeError("Recurrence pattern and boundary must be objects.")
                    kind = {
                        "daily": "daily",
                        "weekly": "weekly",
                        "absolute_monthly": "monthly",
                        "absolute_yearly": "yearly",
                    }.get(pattern["type"])
                    if pattern["type"] == "task_regeneration":
                        kind = {
                            "day": "after_completion_daily",
                            "week": "after_completion_weekly",
                            "month": "after_completion_monthly",
                            "year": "after_completion_yearly",
                        }.get(str(pattern.get("unit", "")))
                    if kind is not None:
                        interval = int(pattern.get("interval", 1))
                        count = (
                            int(boundary["occurrences"]) if boundary["type"] == "numbered" else None
                        )
                        until = (
                            date.fromisoformat(str(boundary["end"]))
                            if boundary["type"] == "end_date"
                            else None
                        )
                        anchor = task.start_date or task.due_date
                        if (
                            anchor
                            and TaskRepeatPreferences(TaskRepeatKind(kind), interval, count).rule(
                                anchor, until=until
                            )
                            == rule
                        ):
                            repeat_kind = kind
                            repeat_interval = str(interval)
                            repeat_count = str(count) if count is not None else ""
                            repeat_until = until.isoformat() if until is not None else ""
                except (ConfigurationError, KeyError, TypeError):
                    pass  # Preserve an imported/complex fresh rule in its advanced control.
        with Vertical(id="task-editor"):
            yield Label(
                "Create task" if task is None or self.creating else "Edit task",
                id="task-editor-title",
                markup=False,
            )
            with VerticalScroll(id="task-editor-fields"):
                yield Label("Subject")
                yield Input(
                    value=task.subject if task else "",
                    placeholder="Subject",
                    id="task-edit-subject",
                )
                yield Label("Status")
                yield Select(
                    tuple(
                        (status.value.replace("_", " ").title(), status.value)
                        for status in TaskStatus
                    ),
                    value=task.status if task else TaskStatus.NOT_STARTED.value,
                    allow_blank=False,
                    id="task-edit-status",
                )
                yield Label("Completed · 0-100%")
                yield Input(
                    value=str(task.percent_complete) if task else "0",
                    placeholder="Percent complete",
                    id="task-edit-percent",
                )
                yield Label("Start date · YYYY-MM-DD")
                yield Input(
                    value=task.start_date.isoformat() if task and task.start_date else "",
                    placeholder="Start date (YYYY-MM-DD)",
                    id="task-edit-start",
                )
                yield Label("Due date · YYYY-MM-DD")
                yield Input(
                    value=task.due_date.isoformat() if task and task.due_date else "",
                    placeholder="Due date (YYYY-MM-DD)",
                    id="task-edit-due",
                )
                yield Label("Priority")
                yield Select(
                    (
                        ("Low", Importance.LOW.value),
                        ("Normal", Importance.NORMAL.value),
                        ("High", Importance.HIGH.value),
                    ),
                    value=task.importance.value if task else Importance.NORMAL.value,
                    allow_blank=False,
                    id="task-edit-importance",
                )
                yield Label("Categories · comma separated")
                yield Input(
                    value=", ".join(task.categories) if task else "",
                    placeholder="Categories (comma separated)",
                    id="task-edit-categories",
                )
                yield Label("Reminder · YYYY-MM-DD HH:MM with UTC offset")
                yield Input(
                    value=task.reminder_at.isoformat() if task and task.reminder_at else "",
                    placeholder="Reminder time with UTC offset (optional)",
                    id="task-edit-reminder",
                )
                yield Label("Repeat")
                yield Select(
                    (
                        ("Does not repeat", "none"),
                        ("Daily", "daily"),
                        ("Weekly on start day", "weekly"),
                        ("Monthly on start date", "monthly"),
                        ("Yearly on start date", "yearly"),
                        ("Days after completion", "after_completion_daily"),
                        ("Weeks after completion", "after_completion_weekly"),
                        ("Months after completion", "after_completion_monthly"),
                        ("Years after completion", "after_completion_yearly"),
                        ("Advanced rule", "advanced"),
                    ),
                    value=repeat_kind,
                    allow_blank=False,
                    id="task-edit-repeat",
                )
                with Vertical(id="task-repeat-options"):
                    yield Label("Repeat every (number of periods)")
                    yield Input(value=repeat_interval, type="integer", id="task-repeat-interval")
                    yield Label("Occurrences (blank for no end)")
                    yield Input(value=repeat_count, type="integer", id="task-repeat-count")
                    yield Label("Or end on (YYYY-MM-DD)")
                    yield Input(value=repeat_until, id="task-repeat-until")
                yield Input(
                    value=json.dumps(task.recurrence, sort_keys=True)
                    if task and task.recurrence
                    else "",
                    placeholder="Advanced recurrence JSON",
                    id="task-edit-recurrence",
                )
                yield TextArea(task.body if task else "", id="task-editor-body")
            yield Static("", id="task-editor-error", markup=False)
            with Horizontal(id="task-editor-actions"):
                yield Button("Cancel", id="task-edit-cancel")
                yield Button("Save", id="task-edit-save", variant="primary")

    @on(Select.Changed, "#task-edit-repeat")
    def _repeat_changed(self) -> None:
        self._update_repeat_controls()

    def _update_repeat_controls(self) -> None:
        kind = str(self.query_one("#task-edit-repeat", Select).value)
        self.query_one("#task-repeat-options").display = kind not in {"none", "advanced"}
        self.query_one("#task-edit-recurrence").display = kind == "advanced"
        self.call_after_refresh(self._reveal_focus)

    def _recurrence_value(
        self, start: date | None, due: date | None
    ) -> Mapping[str, object] | None:
        kind = str(self.query_one("#task-edit-repeat", Select).value)
        if kind == "none":
            return None
        if kind == "advanced":
            rule = _parse_recurrence(self.query_one("#task-edit-recurrence", Input).value)
            if rule is None:
                raise PublicOperationError(
                    "Advanced recurrence needs a task rule.", retryable=False
                )
            try:
                return normalize_recurrence_mapping(rule, task=True, start=start or due)
            except ConfigurationError as exc:
                raise PublicOperationError(str(exc), retryable=False) from exc
        anchor = start or due
        if anchor is None:
            raise PublicOperationError(
                "Set a start or due date for a repeating task.", retryable=False
            )
        try:
            interval = int(self.query_one("#task-repeat-interval", Input).value.strip() or "1")
            count_text = self.query_one("#task-repeat-count", Input).value.strip()
            count = int(count_text) if count_text else None
            until = _parse_date(
                self.query_one("#task-repeat-until", Input).value, field="Repeat-until date"
            )
            return TaskRepeatPreferences(TaskRepeatKind(kind), interval, count).rule(
                anchor, until=until
            )
        except (ConfigurationError, ValueError) as exc:
            raise PublicOperationError(f"Invalid task repeat: {exc}", retryable=False) from exc

    def _value(self) -> TaskEditorValue:
        subject = self.query_one("#task-edit-subject", Input).value.strip()
        if not subject:
            raise PublicOperationError("A task needs a subject.", retryable=False)
        status_value = self.query_one("#task-edit-status", Select).value
        importance_value = self.query_one("#task-edit-importance", Select).value
        if status_value is Select.BLANK or importance_value is Select.BLANK:
            raise PublicOperationError("Choose task status and importance.", retryable=False)
        try:
            percent = int(self.query_one("#task-edit-percent", Input).value.strip())
        except ValueError as exc:
            raise PublicOperationError(
                "Percent complete must be a whole number.", retryable=False
            ) from exc
        if not 0 <= percent <= 100:
            raise PublicOperationError("Percent complete must be from 0 to 100.", retryable=False)
        status = TaskStatus(str(status_value))
        if status is TaskStatus.COMPLETED:
            percent = 100
        elif percent == 100:
            raise PublicOperationError(
                "Only completed tasks may be 100 percent complete.", retryable=False
            )
        elif status is TaskStatus.NOT_STARTED and percent != 0:
            raise PublicOperationError(
                "Not-started tasks must be zero percent complete.", retryable=False
            )
        start = _parse_date(self.query_one("#task-edit-start", Input).value, field="Start date")
        due = _parse_date(self.query_one("#task-edit-due", Input).value, field="Due date")
        if start and due and due < start:
            raise PublicOperationError("Due date cannot be before start date.", retryable=False)
        reminder = _parse_optional_aware_datetime(
            self.query_one("#task-edit-reminder", Input).value,
            field="Reminder time",
        )
        return TaskEditorValue(
            subject=subject,
            status=status,
            percent_complete=percent,
            start_date=start,
            due_date=due,
            body=self.query_one("#task-editor-body", TextArea).text,
            categories=_csv(self.query_one("#task-edit-categories", Input).value),
            importance=Importance(str(importance_value)),
            reminder_at=reminder,
            recurrence=self._recurrence_value(start, due),
        )

    def action_save(self) -> None:
        if self._submission_blocked():
            return
        try:
            value = self._value()
        except PublicOperationError as exc:
            self._validation_failed(str(exc))
            return
        self._submit_value(value)

    def action_cancel(self) -> None:
        self._cancel_edit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "task-edit-save":
            self.action_save()
        elif event.button.id == "task-edit-cancel":
            self.action_cancel()


class TasksScreen(_PagedPimScreen):
    """Bounded Exchange Tasks view with reviewed lifecycle and batch outcomes."""

    DEFAULT_CSS = """
    TasksScreen { layout: vertical; }
    #tasks-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #tasks-title { height: 2; text-style: bold; }
    #tasks-toolbar { height: 3; }
    #tasks-search { width: 2fr; margin-right: 1; }
    #tasks-view { width: 1fr; margin-right: 1; }
    #tasks-toolbar Button { margin-right: 1; }
    #tasks-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #tasks-content { height: 1fr; min-height: 4; }
    #tasks-table { width: 3fr; height: 1fr; }
    #tasks-detail-pane {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1; overflow-y: auto;
    }
    #tasks-detail-pane:focus { border-left: solid $accent; }
    #tasks-detail { width: 100%; height: auto; }
    TasksScreen.compact-tasks #tasks-root { padding: 0 1; }
    TasksScreen.compact-tasks #tasks-capabilities { display: none; }
    #tasks-actions, #tasks-footer { height: auto; min-height: 3; }
    #tasks-actions Button { margin-right: 1; }
    #tasks-footer { dock: bottom; height: 3; align-horizontal: right; }
    #tasks-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create", "New task", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("x", "complete", "Complete", show=False),
        Binding("space", "toggle_selection", "Select", show=False),
        Binding("enter", "details", "Details", show=False),
    ]
    VIEWS = ("all", "open", "due", "today", "this_week", "overdue", "completed")

    def on_resize(self) -> None:
        self.set_class(self.size.height < 26, "compact-tasks")

    def __init__(
        self,
        backend: TasksScreenBackend,
        *,
        page_size: int = 100,
        view: str = "all",
        show_completed: bool = True,
        offline: bool = False,
        timezone: str = "UTC",
        creation_preferences: Callable[[], TaskCreationPreferences] = TaskCreationPreferences,
        display_preferences: Callable[[], TaskDisplayPreferences] | None = None,
        clock: Callable[[], datetime] | None = None,
        history_reader: Callable[..., TaskHistoryPage] | None = None,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="tasks",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if view not in self.VIEWS:
            raise ValueError(f"Unknown task view: {view}")
        self.backend = backend
        self.history_reader = history_reader
        self._history_page: TaskHistoryPage | None = None
        self.page_size = _bounded_limit(page_size)
        self._reset_paging()
        self.view = view
        self.show_completed = show_completed
        self.timezone = timezone
        self.creation_preferences = creation_preferences
        self.display_preferences = display_preferences or (
            lambda: TaskDisplayPreferences(
                default_view=TaskView(view), show_completed=show_completed, timezone=timezone
            )
        )
        self._display = self.display_preferences()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._view_today = self._display.today(self.clock())
        self._page_tasks: tuple[TaskInfo, ...] = ()
        self._complete_detail_reference: str | None = None
        self._tasks: tuple[TaskInfo, ...] = ()
        self._by_reference: dict[str, TaskInfo] = {}
        self._highlighted: str | None = None
        self._selected: set[str] = set()
        self._pending_notice: tuple[ViewState, str] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-root"):
            yield Label("Tasks", id="tasks-title")
            yield Static("Loading a task page…", id="area-state", markup=False)
            with Horizontal(id="tasks-toolbar"):
                yield Input(placeholder="Search task subject or body", id="tasks-search")
                yield Select(
                    ((name.title(), name) for name in self.VIEWS),
                    value=self.view,
                    allow_blank=False,
                    id="tasks-view",
                )
                yield Button("Search", id="tasks-search-button")
                yield Button("Clear", id="tasks-clear")
            yield Static(
                _capability_summary(self.backend.capabilities),
                id="tasks-capabilities",
                markup=False,
            )
            with Horizontal(id="tasks-content"):
                yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="tasks-detail-pane"):
                    yield Static("Select a task to inspect it.", id="tasks-detail", markup=False)
            with Horizontal(id="tasks-actions"):
                yield Button("Create", id="tasks-create", variant="primary")
                yield Button("Edit", id="tasks-edit", disabled=True)
                yield Button("Complete", id="tasks-complete", disabled=True)
                yield Button("Reopen", id="tasks-reopen", disabled=True)
                yield Button("Delete", id="tasks-delete", disabled=True)
                yield Button("Delete options", id="tasks-delete-options", disabled=True)
                yield Button("Local history", id="tasks-history", disabled=True)
                yield Button("Export", id="tasks-export", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with HorizontalScroll(id="tasks-footer"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                older_history = Button("Older history", id="tasks-history-older")
                older_history.display = False
                yield older_history
                newest_history = Button("Newest history", id="tasks-history-newest")
                newest_history.display = False
                yield newest_history
                yield Button("Previous", id="tasks-prev", disabled=True)
                yield Button("Next", id="tasks-next", disabled=True)
                yield Button("Refresh", id="tasks-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        self.on_resize()
        table.add_column("✓", key="selected", width=2)
        table.add_column("Status", key="status")
        table.add_column("Due", key="due")
        table.add_column("Subject", key="subject")
        table.add_column("Done", key="percent")
        table.add_column("Importance", key="importance")
        self._apply_capabilities()
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Tasks are unavailable offline. Reopen without --offline to connect.",
            )
            return
        self.call_after_refresh(self._initial_refresh)

    def _apply_capabilities(self) -> None:
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        self.query_one("#tasks-create", Button).disabled = not mutate
        self.query_one("#tasks-capabilities").display = bool(
            _capability_summary(self.backend.capabilities)
        )
        self._update_buttons()

    def action_refresh(self) -> None:
        self._reset_paging()
        self._load_page()

    def _load_page(self) -> None:
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Tasks are unavailable offline. Reopen without --offline to connect.",
            )
            return
        try:
            self._display = self.display_preferences()
            self.show_completed = self._display.show_completed
            self.timezone = self._display.timezone
            self._view_today = self._display.today(self.clock())
        except ConfigurationError as exc:
            self._show_error("Task preferences", exc)
            return
        query = self.query_one("#tasks-search", Input).value.strip()
        offset = self._prepare_page(
            query, self.view, str(self.show_completed), self.timezone, self._view_today.isoformat()
        )
        page_loader = getattr(self.backend, "list_tasks_page", None)
        view = self.view
        display, today = self._display, self._view_today

        def load(cancel: Event, progress: ProgressReporter) -> object:
            if isinstance(self.backend, TasksServiceBackend):
                return self.backend.list_tasks_page(
                    query,
                    view,
                    self.page_size,
                    offset=offset,
                    cancel=cancel,
                    progress=progress,
                    preferences=display,
                    today=today,
                )
            if callable(page_loader):
                return page_loader(
                    query, view, self.page_size, offset=offset, cancel=cancel, progress=progress
                )
            return tuple(
                self.backend.list_tasks(
                    query, view, self.page_size, cancel=cancel, progress=progress
                )
            )

        self._begin_operation(
            "Loading tasks",
            load,
            self._tasks_loaded,
            retry_safe=True,
        )

    def _tasks_loaded(self, result: object) -> None:
        tasks = cast(tuple[TaskInfo, ...], result.items if isinstance(result, PimPage) else result)
        if not all(isinstance(item, TaskInfo) for item in tasks):
            raise PublicOperationError("The Tasks backend returned invalid records.")
        self._page_result(
            self._display.page(result, self._display.query(view=self.view, today=self._view_today))
            if isinstance(result, PimPage)
            else result
        )
        by_reference = {task.reference: task for task in tasks}
        if len(by_reference) != len(tasks):
            raise PublicOperationError("The Tasks backend returned duplicate references.")
        self._page_tasks = tasks
        self._complete_detail_reference = None
        self._by_reference = by_reference
        self._render_tasks()
        tasks = self._tasks
        if tasks:
            state, message = self._pending_notice or (
                ViewState.READY,
                f"{len(tasks)} tasks loaded · {self.view} · {self.timezone} · one page.",
            )
        else:
            state, message = self._pending_notice or (
                ViewState.EMPTY,
                "No tasks match this task view.",
            )
        if self._page_notice:
            message = (
                message + " " + self._page_summary(len(tasks))
                if self._pending_notice
                else self._page_summary(len(tasks))
            )
        reviewing_mutation = (
            self._pending_notice is not None and self._pending_notice[0] is ViewState.ERROR
        )
        if reviewing_mutation:
            message += (
                " Refresh for review reads the current list; it does not repeat the mutation."
            )
        self._pending_notice = None
        self._set_view_state(state, message)
        self.query_one("#area-retry", Button).label = (
            "Refresh for review" if reviewing_mutation else "Retry"
        )

    def refresh_display_preferences(self) -> None:
        """Re-sort/filter loaded candidates; refresh explicitly to fetch a new window."""
        self._display = self.display_preferences()
        self.show_completed = self._display.show_completed
        self.timezone = self._display.timezone
        self._view_today = self._display.today(self.clock())
        if self.is_mounted:
            self._render_tasks()
            self._set_view_state(
                ViewState.READY if self._tasks else ViewState.EMPTY,
                f"Task display updated · {self.timezone}. Refresh to update the candidate window.",
            )

    def _render_tasks(self) -> None:
        query = self._display.query(view=self.view, today=self._view_today)
        tasks = self._display.page(
            PimPage(tuple(self._by_reference[t.reference] for t in self._page_tasks), 0, 0), query
        ).items
        self._tasks = tasks
        visible = {task.reference for task in tasks}
        self._selected.intersection_update(visible)
        table = self.query_one("#tasks-table", DataTable)
        table.clear()
        for task in tasks:
            table.add_row(
                "●" if task.reference in self._selected else "",
                clean_cell(task.status.replace("_", " ").capitalize(), max_length=30),
                friendly_date(task.due_date),
                clean_cell(task.subject or "(untitled)", max_length=100),
                f"{task.percent_complete}%",
                task.importance.value.title(),
                key=task.reference,
            )
        if tasks:
            reference = self._highlighted if self._highlighted in visible else tasks[0].reference
            self._select(reference)
            table.move_cursor(
                row=next(i for i, item in enumerate(tasks) if item.reference == reference)
            )
        else:
            self._highlighted = None
            self.query_one("#tasks-detail", Static).update("No tasks match this view.")
            self._update_buttons()

    @on(DataTable.RowHighlighted, "#tasks-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#tasks-table")
    def _row_selected(self) -> None:
        self.action_details()

    def _select(self, reference: str) -> None:
        task = self._by_reference.get(reference)
        if task is None or not any(item.reference == reference for item in self._tasks):
            return
        changed = reference != self._highlighted
        self._highlighted = reference
        self._clear_history_view()
        self.query_one("#tasks-detail", Static).update(
            _render_task(task, complete=reference == self._complete_detail_reference)
        )
        if changed:
            self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._update_buttons()

    @property
    def selected_task_reference(self) -> str | None:
        return self._highlighted

    def _targets(self) -> tuple[str, ...]:
        if self._selected:
            return tuple(task.reference for task in self._tasks if task.reference in self._selected)
        return (
            (self._highlighted,)
            if self._highlighted is not None and self._highlighted in self._by_reference
            else ()
        )

    def _update_buttons(self) -> None:
        targets = self._targets()
        self.query_one("#tasks-history", Button).disabled = (
            self._highlighted is None or self.history_reader is None
        )
        values = tuple(self._by_reference[reference] for reference in targets)
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        lifecycle = not self.offline and _capability(self.backend.capabilities, "complete")
        self.query_one("#tasks-edit", Button).disabled = len(targets) != 1 or not mutate
        self.query_one("#tasks-delete", Button).disabled = not targets or not mutate
        self.query_one("#tasks-delete-options", Button).disabled = (
            not targets or not mutate or not _capability(self.backend.capabilities, "delete_scope")
        )
        self.query_one("#tasks-complete", Button).disabled = (
            not any(task.status != TaskStatus.COMPLETED.value for task in values) or not lifecycle
        )
        self.query_one("#tasks-reopen", Button).disabled = (
            not any(task.status == TaskStatus.COMPLETED.value for task in values) or not lifecycle
        )
        self.query_one("#tasks-export", Button).disabled = not targets or not _capability(
            self.backend.capabilities, "export"
        )

    def action_toggle_selection(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        if reference in self._selected:
            self._selected.remove(reference)
        else:
            self._selected.add(reference)
        self.query_one("#tasks-table", DataTable).update_cell(
            reference,
            "selected",
            "●" if reference in self._selected else "",
        )
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"{len(self._selected)} tasks selected for batch actions.",
        )

    def _clear_history_view(self) -> None:
        self._history_page = None
        for identifier in ("#tasks-history-older", "#tasks-history-newest"):
            self.query_one(identifier, Button).display = False

    def action_history(self) -> None:
        self._load_history()

    def action_history_older(self) -> None:
        page = self._history_page
        if page is not None and page.next_before is not None:
            self._load_history(before=page.next_before)

    def _load_history(self, *, before: int | None = None) -> None:
        reference, reader = self._highlighted, self.history_reader
        if reference is None or reader is None:
            return
        self._begin_operation(
            "Loading task history",
            lambda cancel, progress: reader(reference, limit=20, before=before),
            self._history_loaded,
            retry_safe=True,
        )

    def _history_loaded(self, result: object) -> None:
        if not isinstance(result, TaskHistoryPage):
            raise PublicOperationError("The local Task history returned an invalid page.")
        if result.requested_reference != self._highlighted:
            self._set_view_state(
                ViewState.READY, "History result discarded after selection changed."
            )
            return
        self._history_page = result
        self.query_one("#tasks-detail", Static).update(task_history_text(result))
        self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self.query_one("#tasks-history-older", Button).display = result.next_before is not None
        self.query_one("#tasks-history-newest", Button).display = True
        self._set_view_state(
            ViewState.READY, "Local observations loaded; unseen server history is unavailable."
        )

    def action_details(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading task details",
            lambda cancel, progress: self.backend.get_task(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._detail_loaded,
            retry_safe=True,
        )

    def _detail_loaded(self, result: object) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned invalid details.")
        self._clear_history_view()
        self._by_reference[result.reference] = result
        self._complete_detail_reference = result.reference
        self._highlighted = result.reference
        self.query_one("#tasks-detail", Static).update(_render_task(result, complete=True))
        self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._set_view_state(ViewState.READY, "Complete task details loaded.")

    def action_create(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create task", OfflineOperationError())
            return
        try:
            task = self.creation_preferences().new_task(
                timezone=self.display_preferences().timezone, now=self.clock()
            )
        except ConfigurationError as exc:
            self._show_error("Create task", exc)
            return
        self._open_submission_editor(TaskEditorScreen(task, creating=True), self._create_value)

    def configure_search(self, query: str) -> None:
        """Apply a palette query to the bounded Tasks search view."""

        search = self.query_one("#tasks-search", Input)
        search.value = query.strip()
        search.focus()
        self.action_refresh()

    def action_create_prefilled(self, subject: str, *, source_reference: str) -> None:
        """Open a real create form linked to one selected message reference."""

        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create task from message", OfflineOperationError())
            return
        safe_subject = clean_cell(subject, max_length=180).strip() or "Selected message"
        safe_reference = clean_cell(source_reference, max_length=500).strip()
        if not safe_reference:
            self._set_view_state(ViewState.EMPTY, "Select a message before creating a task.")
            return
        try:
            seed = self.creation_preferences().new_task(
                reference=f"prefill-{uuid4().hex}",
                subject=f"Follow up: {safe_subject}",
                timezone=self.display_preferences().timezone,
                now=self.clock(),
            )
        except ConfigurationError as exc:
            self._show_error("Create task from message", exc)
            return
        task = replace(
            seed,
            body=f"Source message: {safe_reference}",
            categories=("From message",),
        )
        self._open_submission_editor(TaskEditorScreen(task, creating=True), self._create_value)

    def _create_value(self, value: TaskEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Creating task",
            lambda cancel, progress: self.backend.create_task(
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def action_edit(self, focus_field: str | None = None) -> None:
        targets = self._targets()
        if len(targets) != 1 or self.offline:
            return
        reference = targets[0]
        self._begin_operation(
            "Loading task for edit",
            lambda cancel, progress: self.backend.get_task(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._open_editor(result, focus_field=focus_field),
            retry_safe=True,
        )

    def _open_editor(self, result: object, *, focus_field: str | None = None) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned invalid details.")
        self._open_submission_editor(
            TaskEditorScreen(result, focus_field=focus_field),
            lambda value: self._edit_value(result.reference, value),
        )

    def _edit_value(self, reference: str, value: TaskEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Updating task",
            lambda cancel, progress: self.backend.update_task(
                reference,
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def _mutation_loaded(self, result: object) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned an invalid task.")
        self._highlighted = result.reference
        self._pending_notice = (ViewState.READY, "Task mutation completed.")
        self.action_refresh()

    def action_complete(self) -> None:
        references = tuple(
            reference
            for reference in self._targets()
            if self._by_reference[reference].status != TaskStatus.COMPLETED.value
        )
        if not references or self.offline:
            return
        self._confirm(
            "Complete selected tasks?",
            f"Tasks: {len(references)}\nOnly non-completed tasks in this selection change.",
            lambda: self._start_lifecycle(references, complete=True),
            confirm_label="Complete",
        )

    def action_reopen(self) -> None:
        references = tuple(
            reference
            for reference in self._targets()
            if self._by_reference[reference].status == TaskStatus.COMPLETED.value
        )
        if not references or self.offline:
            return
        self._confirm(
            "Reopen selected tasks?",
            f"Tasks: {len(references)}\nCompletion dates are cleared after confirmation.",
            lambda: self._start_lifecycle(references, complete=False),
            confirm_label="Reopen",
        )

    def _start_lifecycle(self, references: tuple[str, ...], *, complete: bool) -> None:
        action = "Completing tasks" if complete else "Reopening tasks"
        self._begin_operation(
            action,
            lambda cancel, progress: (
                self.backend.complete_tasks(
                    references,
                    cancel=cancel,
                    progress=progress,
                )
                if complete
                else self.backend.reopen_tasks(
                    references,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            lambda result: self._batch_finished(result, noun="task"),
        )

    def action_delete(self) -> None:
        references = self._targets()
        if not references or self.offline:
            return
        self._confirm(
            "Delete selected tasks?",
            f"Tasks: {len(references)}\n\nThey are moved to Deleted Items after confirmation.",
            lambda: self._start_delete(references),
            confirm_label="Delete",
        )

    def action_delete_options(self) -> None:
        from gucmail.tui.task_delete_screen import TaskDeleteOptionsScreen

        references = self._targets()
        if (
            not references
            or self.offline
            or not _capability(self.backend.capabilities, "delete_scope")
            or not callable(getattr(self.backend, "delete_tasks_scoped", None))
        ):
            return
        self.app.push_screen(
            TaskDeleteOptionsScreen(references),
            lambda mode: self._scoped_delete_chosen(references, mode),
        )

    def _scoped_delete_chosen(
        self, references: tuple[str, ...], mode: TaskDeletionMode | None
    ) -> None:
        if mode is None:
            return
        scope = {
            TaskDeletionMode.MOVE: "Move stored tasks to Deleted Items with their recurrence",
            TaskDeletionMode.OCCURRENCE: "Permanently delete the specified occurrences",
            TaskDeletionMode.SERIES: "Permanently delete the active recurring series",
        }[mode]
        self._confirm(
            "Confirm task deletion scope?",
            f"Tasks: {len(references)}\nScope: {scope}.\n"
            + ("Permanent deletion leaves no Deleted Items copy.\n" if mode.hard_delete else "")
            + "\n".join(references),
            lambda: self._start_scoped_delete(references, mode),
            confirm_label="Delete permanently" if mode.hard_delete else "Move to Deleted Items",
        )

    def _start_scoped_delete(self, references: tuple[str, ...], mode: TaskDeletionMode) -> None:
        operation = getattr(self.backend, "delete_tasks_scoped", None)
        if not callable(operation):
            return
        self._begin_operation(
            "Deleting tasks",
            lambda cancel, progress: operation(
                references,
                hard_delete=mode.hard_delete,
                delete_series=mode.delete_series,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="task"),
            retry_safe=False,
        )

    def _start_delete(self, references: tuple[str, ...]) -> None:
        self._begin_operation(
            "Deleting tasks",
            lambda cancel, progress: self.backend.delete_tasks(
                references,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="task"),
        )

    def _batch_finished(self, result: object, *, noun: str) -> None:
        if not isinstance(result, BatchActionResult):
            raise PublicOperationError("The Tasks backend returned an invalid batch result.")
        self._selected.clear()
        self._pending_notice = _batch_message(result, noun=noun)
        self.action_refresh()

    def action_export(self) -> None:
        references = self._targets()
        if not references or not _capability(self.backend.capabilities, "export"):
            return
        self.app.push_screen(
            FilePathScreen("Export selected tasks", "New .json or .csv destination"),
            lambda path: self._export_path(references, path),
        )

    def _export_path(self, references: tuple[str, ...], path: str | None) -> None:
        if path is None:
            return
        self._begin_operation(
            "Exporting tasks",
            lambda cancel, progress: self.backend.export_tasks(
                references,
                path,
                cancel=cancel,
                progress=progress,
            ),
            self._export_finished,
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            raise PublicOperationError("The Tasks backend returned an invalid export result.")
        self._set_view_state(
            ViewState.READY,
            f"Exported {result.item_count} tasks to {clean_cell(result.path, max_length=300)}.",
        )

    @on(Input.Submitted, "#tasks-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Select.Changed, "#tasks-view")
    def _view_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK or str(event.value) == self.view:
            return
        self.view = str(event.value)
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "tasks-search-button": self.action_refresh,
            "tasks-prev": self.action_previous_page,
            "tasks-next": self.action_next_page,
            "tasks-create": self.action_create,
            "tasks-edit": self.action_edit,
            "tasks-complete": self.action_complete,
            "tasks-reopen": self.action_reopen,
            "tasks-delete": self.action_delete,
            "tasks-delete-options": self.action_delete_options,
            "tasks-history": self.action_history,
            "tasks-history-older": self.action_history_older,
            "tasks-history-newest": self.action_history,
            "tasks-export": self.action_export,
            "tasks-refresh": self.action_refresh,
        }
        if event.button.id == "tasks-clear":
            self.query_one("#tasks-search", Input).value = ""
            self.view = "all"
            self.query_one("#tasks-view", Select).value = "all"
            self.action_refresh()
            return
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


def _render_task(task: TaskInfo, *, complete: bool = False) -> str:
    lines = [
        clean_cell(task.subject or "(untitled task)", max_length=180),
        f"Status: {clean_cell(task.status.replace('_', ' ').capitalize(), max_length=40)}"
        + (f", {task.percent_complete}% done" if 0 < task.percent_complete < 100 else ""),
    ]

    def day(value: date) -> str:
        return f"{value.day} {value.strftime('%b %Y')}"

    if task.due_date:
        lines.append(f"Due: {day(task.due_date)}")
    if task.start_date:
        lines.append(f"Start: {day(task.start_date)}")
    if task.completed_at:
        lines.append(f"Done on: {day(task.completed_at)}")
    if task.importance.value != "normal":
        lines.append(f"Importance: {task.importance.value.capitalize()}")
    if task.reminder_at:
        lines.append(f"Reminder: {day(task.reminder_at)} {task.reminder_at.strftime('%H:%M')}")
    if task.categories:
        lines.append(
            "Categories: " + ", ".join(clean_cell(item, max_length=80) for item in task.categories)
        )
    if task.recurrence:
        lines.append("Repeats")
    if complete and task.body:
        lines.extend(("", clean_cell(task.body, max_length=2_000)))
    return "\n".join(lines)
