"""Stopping restore preserves completed and uncertain item results without replay."""

import json
from threading import Event

import pytest
from tests.test_cli import _environment
from tests.test_tui import FakeGateway, context_for
from textual.widgets import Button, Input, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ExitCode
from gucmail.services.backup import BackupItem, BackupService
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_backends import BackupAreaBackend
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.backup_screen import BackupScreen, RestorePlanScreen
from gucmail.tui.write_coordinator import app_write_coordinator
from waiting import rounds


def test_backend_cancellation_after_one_create_stops_later_creations(tmp_path):
    root = tmp_path / "owned-backups"
    backup = BackupService(root).create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    cancel = Event()
    calls = []

    class Target:
        def iter_backup_items(self, request, completed_item_keys):
            raise AssertionError("Restore attempted to enumerate the source")

        def restore_item(self, item, destination):
            calls.append(item.reference)
            cancel.set()
            return "restored-" + item.reference

    backend = BackupAreaBackend(root, source_factory=lambda: Target())
    plan = backend.create_restore_plan(
        backup.backup_id, (), cancel=cancel, progress=lambda _p: None
    )
    result = backend.execute_restore(plan, confirmed=True, cancel=cancel, progress=lambda _p: None)
    assert calls == ["m-one"]
    assert result.stopped_early and result.restored_count == 1


def test_cli_interrupted_create_reports_uncertainty_in_partial_result(tmp_path, monkeypatch):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create([BackupItem("m-one", "message", {})])
    calls = []

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            raise KeyboardInterrupt()

    monkeypatch.setattr(cli.backup, "_backup_service", lambda _ctx: service)
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: Target())
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "--yes", "backup", "restore", backup.backup_id, "--apply"],
        env=_environment(tmp_path / "owned-cli"),
    )
    assert calls == ["m-one"] and result.exit_code == int(ExitCode.INTERRUPTED)
    assert result.output.strip(), "Interrupted create discarded all outcome evidence"
    data = json.loads(result.output)["data"]
    assert data["mutation_uncertain"] and data["failed_count"] == 1
    assert data["interrupted"] and data["stopped_early"] and not data["cancelled"]
    assert data["results"][0]["source_reference"] == "m-one"
    assert data["results"][0]["error_type"] == "KeyboardInterrupt"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_restore_cancel_stops_batch_and_retains_result(tmp_path, size):
    context = context_for(tmp_path)
    service = BackupService(context.paths.backup_dir)
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    started, release = Event(), Event()
    calls = []

    def restore(item, destination):
        calls.append(item.reference)
        if len(calls) == 1:
            started.set()
            assert release.wait(30), "Synthetic held create was not released"
        return "restored-" + item.reference

    facade = FakeGateway()
    facade.restore_item = restore
    facade.iter_backup_items = lambda *args: pytest.fail("Restore enumerated live source")
    app = GUCmailApp(context, gateway=facade)

    async def wait(pilot, condition):
        for _ in rounds(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Synthetic restore cancellation did not settle")

    async def activate(pilot, identifier):
        button = app.screen.query_one(identifier, Button)
        assert not button.disabled
        button.focus()
        await wait(
            pilot, lambda: button.has_focus and app.screen.region.contains_region(button.region)
        )
        await pilot.press("enter")

    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.ui.online)
            await pilot.press("ctrl+k")
            await wait(
                pilot, lambda: isinstance(app.screen, PaletteScreen) and app.screen.is_mounted
            )
            app.screen.query_one("#palette-input", Input).value = "Open Backup Center"
            await pilot.pause()
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: (
                    isinstance(app.screen, BackupScreen)
                    and app.screen._selected_id == backup.backup_id
                    and not app.screen._operation_active
                ),
            )
            await activate(pilot, "#backup-restore-plan")
            await wait(
                pilot, lambda: isinstance(app.screen, RestorePlanScreen) and app.screen.is_mounted
            )
            plan = app.screen
            await activate(pilot, "#restore-execute")
            await wait(
                pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
            )
            assert not calls
            await pilot.press("y")
            await wait(pilot, lambda: started.is_set() and app.screen is plan)
            summary = str(plan.query_one("#restore-plan-summary", Static).content)
            assert "No mailbox changes have been made" not in summary
            assert "may be created" in summary
            await pilot.press("ctrl+c")
            assert calls == ["m-one"] and plan._cancel_event.is_set()
            assert plan._operation_active and app_write_coordinator(app).pending
            assert plan.query_one("#restore-execute", Button).disabled
            status = plan.query_one("#area-progress", Static)
            await wait(
                pilot,
                lambda: (
                    "Stop requested" in str(status.content)
                    and plan.region.contains_region(status.region)
                ),
            )
            release.set()
            await wait(pilot, lambda: not app_write_coordinator(app).pending)
            assert calls == ["m-one"], "Cancellation started later Exchange creates"
            await wait(pilot, lambda: not plan._operation_active)
            assert plan.query_one("#restore-execute", Button).disabled
            assert "Restored: 1" in str(plan.query_one("#restore-plan-detail", Static).content)
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["before_first", "after_hydration"])
def test_cancel_before_provider_entry_never_claims_creation(tmp_path, monkeypatch, stage):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create([BackupItem("m-one", "message", {})])
    plan = service.plan_restore(backup.backup_id)
    stop = Event()
    if stage == "before_first":
        stop.set()
    else:
        hydrate = service._hydrate_restore_attachments

        def stop_after_hydration(*args):
            item = hydrate(*args)
            stop.set()
            return item

        monkeypatch.setattr(service, "_hydrate_restore_attachments", stop_after_hydration)

    class Target:
        def restore_item(self, *args):
            pytest.fail("Cancelled local preparation reached the provider")

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        target=Target(),
        confirmation_token=plan.confirmation_token,
        cancelled=stop.is_set,
    )
    assert result.cancelled and result.stopped_early and not result.interrupted
    assert result.attempted_count == result.restored_count == result.failed_count == 0
    assert not result.results and not result.server_mutations_performed
    assert not result.mutation_uncertain


@pytest.mark.parametrize("continue_on_error", [False, True])
@pytest.mark.parametrize("stage", ["local_read", "provider_create"])
def test_interrupt_keeps_completed_results_and_stops_even_with_continue(
    tmp_path, monkeypatch, stage, continue_on_error
):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    plan = service.plan_restore(backup.backup_id)
    calls = []
    if stage == "local_read":
        hydrate = service._hydrate_restore_attachments

        def interrupt_local(archive, item):
            if item.reference == "m-two":
                raise KeyboardInterrupt("synthetic-private-interruption")
            return hydrate(archive, item)

        monkeypatch.setattr(service, "_hydrate_restore_attachments", interrupt_local)

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            if item.reference == "m-two":
                raise KeyboardInterrupt("synthetic-private-interruption")
            return "restored-" + item.reference

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        target=Target(),
        confirmation_token=plan.confirmation_token,
        continue_on_error=continue_on_error,
    )
    assert calls == (["m-one", "m-two"] if stage == "provider_create" else ["m-one"])
    assert result.interrupted and not result.cancelled and result.stopped_early
    assert result.attempted_count == 2 and result.restored_count == result.failed_count == 1
    assert result.results[0].restored_reference == "restored-m-one"
    assert result.results[1].source_reference == "m-two"
    assert result.results[1].error_type == "KeyboardInterrupt"
    assert result.results[1].mutation_uncertain is (stage == "provider_create")
    assert result.mutation_uncertain is (stage == "provider_create")
    assert result.server_mutations_performed


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_restore_close_keeps_pending_result_visible(tmp_path, size):
    context = context_for(tmp_path)
    service = BackupService(context.paths.backup_dir)
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    started, release = Event(), Event()
    calls = []

    def restore(item, destination):
        calls.append(item.reference)
        if len(calls) == 1:
            started.set()
            assert release.wait(30), "Synthetic held create was not released"
        return "restored-" + item.reference

    facade = FakeGateway()
    facade.restore_item = restore
    facade.iter_backup_items = lambda *args: pytest.fail("Restore enumerated live source")
    app = GUCmailApp(context, gateway=facade)

    async def wait(pilot, condition):
        for _ in rounds(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Synthetic restore cancellation did not settle")

    async def activate(pilot, identifier):
        button = app.screen.query_one(identifier, Button)
        assert not button.disabled
        button.focus()
        await wait(
            pilot, lambda: button.has_focus and app.screen.region.contains_region(button.region)
        )
        await pilot.press("enter")

    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.ui.online)
            await pilot.press("ctrl+k")
            await wait(
                pilot, lambda: isinstance(app.screen, PaletteScreen) and app.screen.is_mounted
            )
            app.screen.query_one("#palette-input", Input).value = "Open Backup Center"
            await pilot.pause()
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: (
                    isinstance(app.screen, BackupScreen)
                    and app.screen._selected_id == backup.backup_id
                    and not app.screen._operation_active
                ),
            )
            await activate(pilot, "#backup-restore-plan")
            await wait(
                pilot, lambda: isinstance(app.screen, RestorePlanScreen) and app.screen.is_mounted
            )
            plan = app.screen
            await activate(pilot, "#restore-execute")
            await wait(
                pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
            )
            assert not calls
            await pilot.press("y")
            await wait(pilot, lambda: started.is_set() and app.screen is plan)
            summary = str(plan.query_one("#restore-plan-summary", Static).content)
            assert "No mailbox changes have been made" not in summary
            assert "may be created" in summary
            await pilot.press("ctrl+c")
            assert calls == ["m-one"] and plan._cancel_event.is_set()
            assert plan._operation_active and app_write_coordinator(app).pending
            assert plan.query_one("#restore-execute", Button).disabled
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is plan, "Closing discarded the pending restore result"
            assert plan._operation_active and app_write_coordinator(app).pending
            status = plan.query_one("#area-progress", Static)
            await wait(
                pilot,
                lambda: (
                    "Stop requested" in str(status.content)
                    and plan.region.contains_region(status.region)
                ),
            )
            release.set()
            await wait(pilot, lambda: not app_write_coordinator(app).pending)
            assert calls == ["m-one"], "Cancellation started later Exchange creates"
            await wait(pilot, lambda: not plan._operation_active)
            assert plan.query_one("#restore-execute", Button).disabled
            assert "Restored: 1" in str(plan.query_one("#restore-plan-detail", Static).content)
    finally:
        release.set()


@pytest.mark.parametrize("output", ["json", "human"])
def test_cli_interrupt_retains_earlier_success_without_replaying(tmp_path, monkeypatch, output):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    calls = []

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            if item.reference == "m-two":
                raise KeyboardInterrupt("synthetic-private-interruption")
            return "restored-" + item.reference

    monkeypatch.setattr(cli.backup, "_backup_service", lambda _ctx: service)
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: Target())
    options = ["--json"] if output == "json" else ["--no-color"]
    result = CliRunner().invoke(
        cli.app,
        [*options, "--non-interactive", "--yes", "backup", "restore", backup.backup_id, "--apply"],
        env=_environment(tmp_path / "owned-cli"),
    )
    assert calls == ["m-one", "m-two"] and result.exit_code == int(ExitCode.INTERRUPTED)
    assert "synthetic-private-interruption" not in result.output
    assert "KeyboardInterrupt" in result.output and "m-two" in result.output
    if output == "json":
        data = json.loads(result.output)["data"]
        assert data["interrupted"] and data["mutation_uncertain"] and data["stopped_early"]
        assert data["restored_count"] == data["failed_count"] == 1
        assert data["results"][0]["restored_reference"] == "restored-m-one"
        assert data["results"][1]["source_reference"] == "m-two"
        assert data["results"][1]["mutation_uncertain"]
