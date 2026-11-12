"""Meaningful Task mutation/readback and normal mounted local workflows."""

from dataclasses import replace
from decimal import Decimal
from threading import Event

import pytest
from exchangelib import Account, Task
from exchangelib.folders import Root, Tasks
from exchangelib.services import CreateItem, UpdateItem
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static, TextArea

from gucmail.command_catalog import build_core_command_registry
from gucmail.commands import CommandInvocation
from gucmail.errors import ConfigurationError, LocalStateError, MutationResultUnavailableError
from gucmail.models import TaskInfo
from gucmail.services.task_history import TaskHistory
from gucmail.services.tasks import TaskPatch, TaskService
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.pim_screens import TaskEditorScreen, TasksScreen
from test_acceptance_ews_regressions import projected_gateway as projected_gateway
from test_compose_preferences import wait_for
from test_contact_operations import activate
from test_settings_secondpass import _context
from test_tui import FakeGateway, catalog_route, context_for


@pytest.mark.parametrize("categories", [("Course", "Review"), (), None])
def test_native_category_update_or_clear_has_exact_fields_and_separate_fresh_readback(
    projected_gateway, monkeypatch, categories
):
    gateway = projected_gateway
    item = Task(
        account=gateway.account,
        id="synthetic-task-category",
        changekey="old",
        subject="Original subject",
        body="Original private body",
        status="NotStarted",
        percent_complete=Decimal(0),
        categories=["Old"],
    )
    reference = gateway._remember("task", item)
    payloads, reads, server = [], [], {}

    def save(value, *, update_fields):
        assert update_fields == ["categories"]
        payloads.append(
            UpdateItem(account=value.account).get_payload(
                items=[(value, update_fields)],
                message_disposition="SaveOnly",
                conflict_resolution="AutoResolve",
                send_meeting_invitations_or_cancellations="SendToNone",
                suppress_read_receipts=True,
            )
        )
        server["categories"] = tuple(value.categories or ())
        value.changekey = "accepted-key"

    def fetch(account, *, ids, only_fields):
        reads.append((tuple(ids), tuple(only_fields)))
        return [
            Task(
                account=account,
                id=item.id,
                changekey="fresh-readback",
                subject="Original subject",
                body="Original private body",
                status="NotStarted",
                percent_complete=Decimal(0),
                categories=list(server["categories"]),
            )
        ]

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    service = TaskService(gateway)
    current = TaskInfo(
        reference,
        "Original subject",
        "not_started",
        body="Original private body",
        categories=("Old",),
    )
    if categories is None:
        with pytest.raises(ConfigurationError, match="categories must be a sequence"):
            service.update(reference, TaskPatch(categories=categories), current=current)
        assert payloads == reads == []
        assert item.categories == ["Old"]
        assert gateway.get_task_history(reference).observations == ()
        return
    result = service.update(reference, TaskPatch(categories=categories), current=current)
    fresh = service.get(reference)
    assert fresh.categories == result.categories == tuple(categories or ())
    assert fresh.subject == current.subject and fresh.body == current.body
    assert len(payloads) == len(reads) == 1
    assert reads[0][0] == ((item.id, "accepted-key"),)
    assert "categories" in reads[0][1]
    uri = payloads[0].xpath('//*[local-name()="FieldURI"]/@FieldURI')
    assert uri == ["item:Categories"]
    if categories:
        assert payloads[0].xpath(
            '//*[local-name()="Categories"]/*[local-name()="String"]/text()'
        ) == list(categories)
    else:
        assert len(payloads[0].xpath('//*[local-name()="DeleteItemField"]')) == 1
    assert not payloads[0].xpath(
        '//*[local-name()="Body" or local-name()="Subject" or local-name()="CompleteDate"]'
    )
    observation = gateway.get_task_history(reference).observations[0]
    assert observation.action == "edit" and observation.outcome == "readback_not_requested"
    assert not observation.distinct_instance


def test_native_task_from_message_create_xml_and_fresh_getitem_preserve_link_without_source_read(
    projected_gateway, monkeypatch
):
    gateway = projected_gateway
    folder = Tasks(
        root=Root(account=gateway.account), id="synthetic-task-folder", changekey="folder"
    )
    monkeypatch.setattr(gateway, "_tasks_folder", lambda _: folder)
    payloads, reads = [], []
    source_reference = "msg_synthetic_source"

    def save(value):
        payloads.append(
            CreateItem(account=value.account).get_payload(
                items=[value],
                folder=folder,
                message_disposition="SaveOnly",
                send_meeting_invitations="SendToNone",
            )
        )
        value.id, value.changekey = "synthetic-created-task", "accepted-key"

    def fetch(account, *, ids, only_fields):
        assert ids == [("synthetic-created-task", "accepted-key")]
        reads.append((tuple(ids), tuple(only_fields)))
        return [
            Task(
                account=account,
                id="synthetic-created-task",
                changekey="fresh-readback",
                subject="Follow up: Synthetic message",
                body=f"Source message: {source_reference}",
                status="NotStarted",
                percent_complete=Decimal(0),
                categories=["From message"],
            )
        ]

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    service = TaskService(gateway)
    created = service.create(
        TaskInfo(
            "plan-only",
            "Follow up: Synthetic message",
            "not_started",
            body=f"Source message: {source_reference}",
            categories=("From message",),
        )
    )
    fresh = service.get(created.reference)
    assert fresh.reference == created.reference and fresh.reference != source_reference
    assert fresh.body == f"Source message: {source_reference}"
    assert fresh.categories == ("From message",)
    assert len(payloads) == len(reads) == 1
    assert payloads[0].xpath('//*[local-name()="Body"]/text()') == [fresh.body]
    assert payloads[0].xpath('//*[local-name()="Categories"]/*[local-name()="String"]/text()') == [
        "From message"
    ]
    assert payloads[0].get("MessageDisposition") == "SaveOnly"
    assert gateway.state.resolve_reference(source_reference) is None
    assert gateway.get_task_history(created.reference).observations == ()


@pytest.mark.parametrize("failure", ["missing_id", "state"])
def test_accepted_native_task_creation_failure_never_replays_or_reports_plain_validation(
    projected_gateway, monkeypatch, failure
):
    gateway = projected_gateway
    folder = Tasks(root=Root(account=gateway.account), id="synthetic-task-folder")
    monkeypatch.setattr(gateway, "_tasks_folder", lambda _: folder)
    writes = []

    def save(value):
        writes.append(value.subject)
        if failure != "missing_id":
            value.id, value.changekey = "synthetic-accepted-task", "accepted"

    def failed_record(*args, **kwargs):
        raise LocalStateError("Synthetic post-creation identity recording failure")

    monkeypatch.setattr(Task, "save", save)
    if failure == "state":
        monkeypatch.setattr(gateway.state, "put_reference", failed_record)
    with pytest.raises(MutationResultUnavailableError) as caught:
        TaskService(gateway).create(TaskInfo("plan", "Synthetic created Task", "not_started"))
    assert writes == ["Synthetic created Task"]
    assert caught.value.as_dict()["retry_safe"] is False
    assert caught.value.as_dict()["mutation_uncertain"] is True


class CreatingGateway(FakeGateway):
    def __init__(self):
        self.creates = []
        self.task_reads = []
        self.message_reads = []
        self.created = None

    def show_message(self, reference, *, mark_read=False):
        self.message_reads.append((reference, mark_read))
        return super().show_message(reference, mark_read=mark_read)

    def create_task(self, plan):
        self.creates.append(plan)
        self.created = replace(plan.task, reference="task_created_from_message")
        return self.created

    def list_tasks(self, **kwargs):
        return [self.created] if self.created is not None else super().list_tasks(**kwargs)

    def get_task(self, reference):
        self.task_reads.append(reference)
        if self.created is not None:
            assert reference == self.created.reference
            return self.created
        return super().get_task(reference)

    def open_attachment(self, *args, **kwargs):
        pytest.fail("Task-from-message downloaded source content")

    def mark_messages(self, *args, **kwargs):
        pytest.fail("Task-from-message mutated source messages")

    def move_messages(self, *args, **kwargs):
        pytest.fail("Task-from-message moved source messages")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("save", [False, True], ids=["cancel", "save"])
async def test_message_to_task_form_save_or_cancel_preserves_source_and_reads_created_task(
    tmp_path, size, save
):
    gateway = CreatingGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=size) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        route = catalog_route("tasks.create_task_from_message")
        app._catalog_open(route, CommandInvocation(route.command_id))
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        assert editor.query_one("#task-edit-subject", Input).value == "Follow up: Synthetic welcome"
        assert editor.query_one("#task-editor-body", TextArea).text == "Source message: m-demo"
        assert editor._value().categories == ("From message",)
        message_reads_before_save = tuple(gateway.message_reads)
        assert gateway.creates == []
        await pilot.press("ctrl+s" if save else "escape")
        await wait_for(
            pilot, lambda: isinstance(app.screen, TasksScreen) and not app.screen._operation_active
        )
        workspace = app.screen
        assert gateway.message_reads == list(message_reads_before_save)
        assert all(not mark_read for _, mark_read in gateway.message_reads)
        if save:
            await wait_for(
                pilot,
                lambda: (
                    len(gateway.creates) == 1
                    and gateway.created.reference in workspace._by_reference
                    and not workspace._operation_active
                ),
            )
            plan = gateway.creates[0]
            assert plan.task.subject == "Follow up: Synthetic welcome"
            assert plan.task.body == "Source message: m-demo"
            assert plan.task.categories == ("From message",)
            assert workspace._highlighted == gateway.created.reference
            workspace.query_one("#tasks-table").focus()
            await pilot.press("enter")
            await wait_for(
                pilot,
                lambda: (
                    workspace._complete_detail_reference == gateway.created.reference
                    and not workspace._operation_active
                ),
            )
            assert gateway.task_reads == [gateway.created.reference]
            detail = str(workspace.query_one("#tasks-detail", Static).content)
            assert "Source message: m-demo" in detail and "From message" in detail
            assert len(gateway.creates) == 1
        else:
            assert gateway.creates == [] and gateway.task_reads == []
        assert gateway.message_reads == list(message_reads_before_save)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("state", ["populated", "empty", "error"])
async def test_mounted_history_uses_local_reader_with_pagination_scrolling_and_error_states(
    tmp_path, monkeypatch, size, state
):
    context = _context(tmp_path)
    history = TaskHistory(context.state)
    if state == "populated":
        for index in range(25):
            entry = history.accepted(
                "task-demo", f"task_instance_{index}", action="complete", distinct_instance=True
            )
            history.finish(entry.operation_id, verified=index % 2 == 0)
    gateway = FakeGateway()
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=size) as pilot:
        app._open_area("tasks")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, TasksScreen)
                and bool(app.screen._tasks)
                and not app.screen._operation_active
            ),
        )
        workspace = app.screen
        monkeypatch.setattr(
            app, "_ensure_gateway", lambda: pytest.fail("Local history resolved a gateway")
        )
        if state == "error":

            def unavailable(*args, **kwargs):
                raise LocalStateError("Synthetic local history is unavailable; data preserved.")

            workspace.history_reader = unavailable
        await activate(pilot, workspace, "#tasks-history")
        await wait_for(pilot, lambda: not workspace._operation_active)
        if state == "error":
            assert "Synthetic local history" in str(
                workspace.query_one("#area-state", Static).content
            )
            assert workspace.query_one("#area-retry", Button).display
            assert workspace._history_page is None
            return
        await wait_for(pilot, lambda: workspace._history_page is not None)
        detail = str(workspace.query_one("#tasks-detail", Static).content)
        assert "unseen server history is unavailable" in detail
        assert "persistent local state" in detail
        pane = workspace.query_one("#tasks-detail-pane", VerticalScroll)
        pane.focus()
        await wait_for(
            pilot, lambda: pane.has_focus and (state != "populated" or pane.max_scroll_y > 0)
        )
        await pilot.press("end")
        await wait_for(pilot, lambda: state != "populated" or pane.scroll_y > 0)
        if state == "populated":
            assert len(workspace._history_page.observations) == 20
            assert pane.scroll_y > 0
            first_ids = {r.operation_id for r in workspace._history_page.observations}
            await activate(pilot, workspace, "#tasks-history-older")
            await wait_for(
                pilot,
                lambda: (
                    not workspace._operation_active
                    and len(workspace._history_page.observations) == 5
                ),
            )
            assert not first_ids.intersection(
                r.operation_id for r in workspace._history_page.observations
            )
            assert not workspace.query_one("#tasks-history-older", Button).display
            await activate(pilot, workspace, "#tasks-history-newest")
            await wait_for(
                pilot,
                lambda: (
                    not workspace._operation_active
                    and len(workspace._history_page.observations) == 20
                ),
            )
            assert {r.operation_id for r in workspace._history_page.observations} == first_ids
        else:
            assert "No retained updates" in detail
            assert workspace._history_page.observations == ()
            assert not workspace.query_one("#tasks-history-older", Button).display


@pytest.mark.asyncio
async def test_late_local_history_is_discarded_after_selection_changes(tmp_path):
    from gucmail.tui.pim_screens import TasksServiceBackend

    class TwoTasks(FakeGateway):
        def list_tasks(self, **kwargs):
            return [
                TaskInfo("task-one", "One", "not_started"),
                TaskInfo("task-two", "Two", "not_started"),
            ]

    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    history = TaskHistory(context.state)
    started, release = Event(), Event()

    def reader(reference, **kwargs):
        started.set()
        assert release.wait(5), "Owned local history fixture was not released"
        return history.page(reference, **kwargs)

    screen = TasksScreen(TasksServiceBackend(TaskService(TwoTasks())), history_reader=reader)
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen)
        await wait_for(
            pilot, lambda: screen._highlighted is not None and not screen._operation_active
        )
        screen._select("task-one")
        await activate(pilot, screen, "#tasks-history")
        await wait_for(pilot, started.is_set)
        screen._select("task-two")
        release.set()
        await wait_for(pilot, lambda: not screen._operation_active)
        assert screen._highlighted == "task-two" and screen._history_page is None
        assert "History result discarded" in str(screen.query_one("#area-state", Static).content)
        assert "Two" in str(screen.query_one("#tasks-detail", Static).content)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_actual_palette_task_history_uses_canonical_read_only_local_command(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    recorded = TaskHistory(context.state).accepted(
        "task-demo", "task-demo", action="edit", distinct_instance=False
    )
    spec = build_core_command_registry().get("tasks.show_local_task_history")
    assert spec.cli_path == ("tasks", "history")
    assert spec.offline and not spec.requires_connection and not spec.changes_server
    assert not spec.confirmation_required and not spec.changes_local
    app = GUCmailApp(context, gateway=FakeGateway())
    async with app.run_test(size=size) as pilot:
        app._open_area("tasks")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, TasksScreen)
                and bool(app.screen._tasks)
                and not app.screen._operation_active
            ),
        )
        workspace = app.screen
        monkeypatch.setattr(
            app, "_ensure_gateway", lambda: pytest.fail("Palette local history resolved Exchange")
        )
        await pilot.press(app._palette_primary_shortcut)
        await wait_for(
            pilot, lambda: isinstance(app.screen, PaletteScreen) and app.screen.is_mounted
        )
        app.screen.query_one("#palette-input", Input).value = "Show local Task history"
        await pilot.pause()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                app.screen is workspace
                and workspace._history_page is not None
                and not workspace._operation_active
            ),
        )
        assert workspace._history_page.observations[0].operation_id == recorded.operation_id
        assert "Local Task history" in str(workspace.query_one("#tasks-detail", Static).content)
