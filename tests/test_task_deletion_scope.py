"""Task deletion intent, native request shape and local previews; no live requests."""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from exchangelib import Account, Task
from exchangelib.recurrence import DailyPattern, NoEndPattern, TaskRecurrence
from exchangelib.services import DeleteItem
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import (
    ConfigurationError,
    ExitCode,
    GUCmailError,
    MutationResultUnavailableError,
)
from gucmail.models import TaskInfo
from gucmail.services.tasks import TaskDeletePlan, TaskDeletionMode, TaskService
from test_acceptance_ews_regressions import projected_gateway as projected_gateway


@pytest.fixture
def local_paths(tmp_path, monkeypatch):
    for kind in ("CONFIG", "DATA", "CACHE", "LOG"):
        monkeypatch.setenv("GUCMAIL_" + kind + "_DIR", str(tmp_path / kind.lower()))
    return tmp_path / "owned-config.toml"


def invoke(config, args, *, yes=False):
    return CliRunner().invoke(
        cli.app,
        [
            "--config",
            str(config),
            "--json",
            "--non-interactive",
            *(["--yes"] if yes else []),
            "tasks",
            *args,
        ],
    )


@pytest.mark.parametrize("command", ["complete", "reopen", "delete"])
def test_lifecycle_preview_has_no_context_credentials_or_gateway(local_paths, monkeypatch, command):
    monkeypatch.setattr(cli.core, "gateway", lambda *_: pytest.fail("Preview resolved Exchange"))
    monkeypatch.setattr(
        cli.core, "build_context", lambda **_: pytest.fail("Preview opened app state")
    )
    result = invoke(local_paths, [command, "synthetic:task", "--dry-run"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)["data"]
    assert value["reference"] == "synthetic:task"
    if command == "delete":
        assert value["hard_delete"] is False and value["delete_series"] is False
    else:
        assert value["action"] == command
        assert value["changes"]["status"] == (
            "completed" if command == "complete" else "not_started"
        )
    assert not local_paths.exists()


def test_planning_service_requires_provider_only_for_execution():
    service = TaskService()
    assert service.plan_delete("synthetic:task") == TaskDeletePlan("synthetic:task")
    with pytest.raises(ConfigurationError, match="connected provider"):
        service.delete(service.plan_delete("synthetic:task"))


@pytest.mark.parametrize("value", [0, 1, None, "true", 1.0])
@pytest.mark.parametrize("field", ["hard_delete", "delete_series"])
def test_delete_plan_rejects_non_boolean_choices(value, field):
    options = {"hard_delete": True, "delete_series": False, field: value}
    with pytest.raises(ConfigurationError, match="booleans"):
        TaskDeletePlan("synthetic:task", **options)


def test_series_move_rejected_by_service_and_adapter_before_resolution(
    projected_gateway, monkeypatch
):
    with pytest.raises(ConfigurationError, match="series"):
        TaskService().plan_delete("synthetic:task", delete_series=True)
    monkeypatch.setattr(projected_gateway, "_resolve_item", lambda *_: pytest.fail("Resolved item"))
    with pytest.raises(ConfigurationError, match="series"):
        projected_gateway.delete_task(
            SimpleNamespace(reference="synthetic:task", hard_delete=False, delete_series=True)
        )


@pytest.mark.parametrize("mode", ["occurrence", "series", "legacy_occurrence"])
def test_real_task_delete_uses_exact_hard_scope_and_minimal_series_read(
    projected_gateway, monkeypatch, mode
):
    series = mode == "series"
    gateway = projected_gateway
    task = Task(account=gateway.account, id="synthetic-task", changekey="old")
    reference = gateway._remember("task", task)
    fetched = Task(
        account=gateway.account,
        id=task.id,
        changekey="fresh",
        is_recurring=True,
        recurrence=TaskRecurrence(
            pattern=DailyPattern(interval=1), boundary=NoEndPattern(start=date(2030, 1, 1))
        ),
    )
    reads, writes = [], []

    def fetch(account, *, ids, only_fields):
        reads.append((tuple(ids), tuple(only_fields)))
        return [fetched]

    def delete(item, **kwargs):
        # Exercise the real public Task.delete() and pure native EWS serializer.
        payload = DeleteItem(account=gateway.account).get_payload(items=[item], **kwargs)
        writes.append((item.id, item.changekey, payload))

    monkeypatch.setattr(Account, "fetch", fetch)
    monkeypatch.setattr(Task, "_delete", delete)
    plan = (
        SimpleNamespace(reference=reference, hard_delete=True)
        if mode == "legacy_occurrence"
        else TaskDeletePlan(reference, hard_delete=True, delete_series=series)
    )
    gateway.delete_task(plan)
    assert len(writes) == 1
    item_id, key, payload = writes[0]
    assert item_id == "synthetic-task" and key == ("fresh" if series else "old")
    assert payload.get("DeleteType") == "HardDelete"
    assert payload.get("AffectedTaskOccurrences") == (
        "AllOccurrences" if series else "SpecifiedOccurrenceOnly"
    )
    assert payload.get("SuppressReadReceipts") == "true"
    assert len(reads) == int(series)
    if series:
        assert reads[0][1] == ("recurrence", "is_recurring")
    names = {node.tag.rsplit("}", 1)[-1] for node in payload.iter()}
    assert "ItemId" in names and not names & {"Body", "Attachments", "CompleteDate"}


@pytest.mark.parametrize("failure", ["not_recurring", "different_id", "missing", "read_error"])
def test_unverified_series_never_deletes(projected_gateway, monkeypatch, failure):
    gateway = projected_gateway
    task = Task(account=gateway.account, id="synthetic-series", changekey="old")
    reference = gateway._remember("task", task)
    reads = []

    def fetch(account, **kwargs):
        reads.append(kwargs)
        if failure == "read_error":
            raise TimeoutError("Synthetic preflight timeout")
        if failure == "missing":
            return []
        return [
            Task(
                account=account,
                id="other" if failure == "different_id" else task.id,
                is_recurring=failure != "not_recurring",
            )
        ]

    monkeypatch.setattr(Account, "fetch", fetch)
    monkeypatch.setattr(Task, "_delete", lambda *a, **kw: pytest.fail("Unverified task deleted"))
    with pytest.raises(GUCmailError) as caught:
        gateway.delete_task(TaskDeletePlan(reference, hard_delete=True, delete_series=True))
    assert caught.value.exit_code == (
        ExitCode.NETWORK if failure == "read_error" else ExitCode.CONFIGURATION
    )
    assert len(reads) == 1 and task.id == "synthetic-series"


@pytest.mark.parametrize(
    "reply", ["wrong_native_type", "explicitly_nonrecurring", "unknown_flag", "malformed_object"]
)
def test_series_response_requires_native_task_and_explicit_recurring_flag(
    projected_gateway, monkeypatch, reply
):
    from exchangelib import CalendarItem

    gateway = projected_gateway
    original = Task(account=gateway.account, id="synthetic-scope-item", changekey="old")
    reference = gateway._remember("task", original)
    recurrence = TaskRecurrence(
        pattern=DailyPattern(interval=1), boundary=NoEndPattern(start=date(2030, 1, 1))
    )
    reads, writes = [], []
    if reply == "wrong_native_type":
        fetched = CalendarItem(
            account=gateway.account, id=original.id, changekey="fresh", is_recurring=True
        )
    elif reply == "malformed_object":
        fetched = SimpleNamespace(
            id=original.id,
            changekey="fresh",
            is_recurring=True,
            recurrence=recurrence,
            delete=lambda **kwargs: writes.append(kwargs),
        )
    else:
        fetched = Task(
            account=gateway.account,
            id=original.id,
            changekey="fresh",
            is_recurring=False if reply == "explicitly_nonrecurring" else None,
            recurrence=recurrence,
        )

    def fetch(account, *, ids, only_fields):
        reads.append((tuple(ids), tuple(only_fields)))
        return [fetched]

    def record_delete(item, **kwargs):
        writes.append((type(item).__name__, kwargs))

    monkeypatch.setattr(Account, "fetch", fetch)
    monkeypatch.setattr(Task, "_delete", record_delete)
    monkeypatch.setattr(CalendarItem, "_delete", record_delete)
    with pytest.raises(ConfigurationError, match="No deletion was attempted"):
        gateway.delete_task(TaskDeletePlan(reference, hard_delete=True, delete_series=True))
    assert len(reads) == 1 and reads[0][1] == ("recurrence", "is_recurring")
    assert writes == []
    assert gateway._objects[("task", reference)] is original
    assert original.id == "synthetic-scope-item" and original.changekey == "old"


@pytest.mark.parametrize(
    "options,code",
    [
        (["--series"], ExitCode.CONFIGURATION),
        (["--series", "--permanent"], ExitCode.CONFIRMATION_REQUIRED),
        (["--series", "--permanent", "--confirm-series", "different"], ExitCode.CONFIGURATION),
        (["--confirm-series", "synthetic:task"], ExitCode.CONFIGURATION),
    ],
)
def test_bad_or_unconfirmed_series_intent_never_resolves_gateway(
    local_paths, monkeypatch, options, code
):
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Invalid request resolved Exchange")
    )
    result = invoke(local_paths, ["delete", "synthetic:task", *options], yes=True)
    assert result.exit_code == code, result.output


@pytest.mark.parametrize("mode", ["move", "occurrence", "series"])
def test_cli_confirmation_and_exact_delete_plan_once(local_paths, monkeypatch, mode):
    calls = []
    gateway = SimpleNamespace(delete_task=calls.append)
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    options = [] if mode == "move" else ["--permanent"]
    if mode == "series":
        options += ["--series", "--confirm-series", "synthetic:task"]
    rejected = invoke(local_paths, ["delete", "synthetic:task", *options])
    assert rejected.exit_code == ExitCode.CONFIRMATION_REQUIRED and calls == []
    preview = invoke(local_paths, ["delete", "synthetic:task", *options, "--dry-run"])
    assert preview.exit_code == 0 and calls == []
    approved = invoke(local_paths, ["delete", "synthetic:task", *options], yes=True)
    assert approved.exit_code == 0, approved.output
    assert calls == [TaskDeletePlan("synthetic:task", mode != "move", mode == "series")]
    assert json.loads(approved.output)["data"]["series"] is (mode == "series")


@pytest.mark.parametrize(
    "references", [(), ("same", "same"), tuple(str(n) for n in range(201)), ("a", "")]
)
def test_scoped_batch_validates_entire_selection_before_any_write(references):
    from gucmail.tui.pim_screens import TasksServiceBackend

    writes = []
    backend = TasksServiceBackend(TaskService(SimpleNamespace(delete_task=writes.append)))
    with pytest.raises(ConfigurationError):
        backend.delete_tasks_scoped(
            references,
            hard_delete=True,
            delete_series=True,
            cancel=Event(),
            progress=lambda *_: None,
        )
    assert writes == []


@pytest.mark.parametrize("interruption", ["cancel", "uncertain"])
def test_permanent_batch_retains_progress_and_stops_remaining_writes(interruption):
    from gucmail.tui.pim_screens import TasksServiceBackend

    cancel = Event()
    writes = []

    def delete(plan):
        writes.append(plan)
        if interruption == "cancel":
            cancel.set()
        else:
            raise MutationResultUnavailableError(
                "Synthetic uncertain deletion", suggestion="Inspect state."
            )

    backend = TasksServiceBackend(TaskService(SimpleNamespace(delete_task=delete)))
    result = backend.delete_tasks_scoped(
        ("a", "b", "c"),
        hard_delete=True,
        delete_series=True,
        cancel=cancel,
        progress=lambda *_: None,
    )
    assert writes == [TaskDeletePlan("a", True, True)]
    assert result.attempted == 3 and result.skipped_references == ("b", "c")
    assert result.completed == (1 if interruption == "cancel" else 0)
    assert result.failed_references == (() if interruption == "cancel" else ("a",))
    assert result.uncertain_references == (() if interruption == "cancel" else ("a",))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("mode", list(TaskDeletionMode))
async def test_normal_task_options_require_current_phrase_and_final_confirmation(
    tmp_path, monkeypatch, size, mode
):
    from textual.widgets import Input, Select, Static

    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.area_common import ConfirmationScreen
    from gucmail.tui.pim_screens import TasksScreen
    from gucmail.tui.task_delete_screen import TaskDeleteOptionsScreen
    from test_compose_preferences import wait_for
    from test_contact_operations import activate
    from test_settings_secondpass import _context

    class OwnedTasks:
        def __init__(self):
            self.tasks = (TaskInfo("synthetic:task", "Synthetic deletion review", "not_started"),)
            self.deletes = []

        def search_tasks(self, query, **kwargs):
            return self.tasks

        def delete_task(self, plan):
            self.deletes.append(plan)
            self.tasks = ()

    context = _context(tmp_path)
    gateway = OwnedTasks()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("tasks")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, TasksScreen)
                and bool(app.screen._tasks)
                and not app.screen._operation_active
            ),
        )
        tasks = app.screen
        for approved in (False, True):
            await activate(pilot, tasks, "#tasks-delete-options")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, TaskDeleteOptionsScreen) and app.screen.is_mounted,
            )
            options = app.screen
            select = options.query_one("#task-delete-mode", Select)
            select.value = mode.value
            await pilot.pause()
            challenge = options.query_one("#task-delete-challenge", Input)
            assert challenge.display is mode.hard_delete
            if mode.hard_delete:
                challenge.value = "DELETE SERIES" if mode.delete_series else "DELETE OCCURRENCE"
                select.value = "move"
                await pilot.pause()
                select.value = mode.value
                await pilot.pause()
                assert challenge.value == ""
                await activate(pilot, options, "#task-delete-review")
                assert app.screen is options and gateway.deletes == []
                assert "exactly" in str(options.query_one("#task-delete-error", Static).content)
                challenge.value = "DELETE SERIES" if mode.delete_series else "DELETE OCCURRENCE"
                await pilot.pause()
                assert not str(options.query_one("#task-delete-error", Static).content)
            await activate(pilot, options, "#task-delete-review")
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert gateway.deletes == []
            await pilot.press("y" if approved else "n")
            await wait_for(pilot, lambda: app.screen is tasks and not tasks._operation_active)
            if not approved:
                assert gateway.deletes == []
        assert gateway.deletes == [mode.plan("synthetic:task")]
        assert context._gateway is None
    context.close()


def test_cold_shortcut_discovery_includes_lazy_task_options_without_provider(tmp_path):
    import gucmail

    package_root = str(Path(gucmail.__file__).resolve().parent.parent)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GUCMAIL_")
    }
    environment.update(
        {
            f"GUCMAIL_{kind}_DIR": str(tmp_path / kind.lower())
            for kind in ("CONFIG", "DATA", "CACHE", "LOG")
        }
    )
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gucmail
assert Path(gucmail.__file__).resolve().parent.parent == Path(sys.argv[1])
from gucmail.bootstrap import build_context
from gucmail.credentials import CredentialResolver
from gucmail.services.keyboard_bindings import keyboard_catalog
from gucmail.tui.app import GUCmailApp
from gucmail.tui.shortcuts_screen import runtime_shortcut_records
assert 'gucmail.tui.task_delete_screen' not in sys.modules
def unavailable(*args, **kwargs):
    raise AssertionError('Cold shortcut discovery attempted provider or credentials')
CredentialResolver.resolve = unavailable
GUCmailApp._ensure_gateway = unavailable
context = build_context()
try:
    app = GUCmailApp(context, offline=True)
    rows = runtime_shortcut_records(app, app._current_shortcuts())
    matches = [row for row in rows if row.context == 'TaskDeleteOptionsScreen']
    assert any(row.key == 'escape' and row.binding_action == 'cancel' for row in matches)
    assert all(row.scope == 'Tasks' and not row.customizable for row in matches)
    assert app.gateway is None and context._gateway is None
    fixed = [row for row in keyboard_catalog()[1]
             if row.context == 'gucmail.tui.task_delete_screen.TaskDeleteOptionsScreen']
    assert {(row.key, row.action) for row in fixed} == {
        ('escape', 'cancel'), ('tab', 'app.focus_next'),
        ('shift+tab', 'app.focus_previous'), ('ctrl+c', 'screen.copy_text'),
        ('super+c', 'screen.copy_text')}
    print('cold Task shortcut discovery and fixed navigation pass')
finally:
    context.close()
"""
    flags = ["-I"]
    if os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1":
        from qa.offline.children import guarded_environment

        environment = guarded_environment(environment)
        flags = []
    result = subprocess.run(  # noqa: S603 -- Fixed test script/interpreter; no shell or user input.
        [sys.executable, *flags, "-X", "utf8", "-c", code, package_root],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cold Task shortcut discovery and fixed navigation pass" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("route", ["button", "escape"])
async def test_task_scope_options_cancel_preserves_selection_without_reads_or_writes(
    tmp_path, monkeypatch, size, route
):
    from textual.widgets import Input, Select

    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.pim_screens import TasksScreen
    from gucmail.tui.task_delete_screen import TaskDeleteOptionsScreen
    from test_compose_preferences import wait_for
    from test_contact_operations import activate
    from test_settings_secondpass import _context

    calls = []

    class OwnedTasks:
        def search_tasks(self, query, **kwargs):
            calls.append("list")
            return (TaskInfo("synthetic:task", "Synthetic cancel", "not_started"),)

        def delete_task(self, plan):
            pytest.fail("Cancelled task options attempted a write")

        def get_task(self, reference):
            pytest.fail("Cancelled task options attempted a read")

    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: OwnedTasks())
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("tasks")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, TasksScreen)
                and bool(app.screen._tasks)
                and not app.screen._operation_active
            ),
        )
        tasks = app.screen
        tasks._selected = {"synthetic:task"}
        await activate(pilot, tasks, "#tasks-delete-options")
        await wait_for(pilot, lambda: isinstance(app.screen, TaskDeleteOptionsScreen))
        options = app.screen
        options.query_one("#task-delete-mode", Select).value = "series"
        await pilot.pause()
        challenge = options.query_one("#task-delete-challenge", Input)
        challenge.value = "DELETE SERIES"
        await pilot.pause()
        if route == "button":
            await activate(pilot, options, "#task-delete-cancel")
        else:
            challenge.focus()
            await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is tasks)
        assert tasks._selected == {"synthetic:task"} and not tasks._operation_active
        assert calls == ["list"] and context._gateway is None
    context.close()
