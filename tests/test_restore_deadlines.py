"""Restore deadlines and queued stops retain verified outcomes without replay."""

import asyncio
from threading import Event

import pytest
from tests.test_tui import FakeGateway, context_for
from textual.app import App
from textual.widgets import Button, Input, Static

from gucmail.services.backup import BackupItem, BackupService
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.backup_screen import (
    BackupScreen,
    RestorePlanEntryView,
    RestorePlanScreen,
    RestorePlanView,
)
from gucmail.tui.write_coordinator import app_write_coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_restore_timeout_keeps_pending_result_visible(tmp_path, size):
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
        for _ in range(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Synthetic restore timeout did not settle")

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
            # Inject only the deadline callback; all navigation and mailbox behavior is real.
            plan._operation_timed_out(plan._operation_generation, "Restoring backup")
            assert calls == ["m-one"] and plan._cancel_event.is_set()
            assert plan._operation_active and app_write_coordinator(app).pending
            assert plan.query_one("#restore-execute", Button).disabled
            status = plan.query_one("#area-progress", Static)
            await wait(
                pilot,
                lambda: (
                    "timed out" in str(status.content).casefold()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_queued_restore_stop_never_calls_backend_and_keeps_zero_result(
    tmp_path, monkeypatch, size, stop
):
    context = context_for(tmp_path)
    backup = BackupService(context.paths.backup_dir).create([BackupItem("m-one", "message", {})])
    facade = FakeGateway()
    calls = []
    facade.restore_item = lambda item, _destination: calls.append(item.reference) or "m-new"
    facade.iter_backup_items = lambda *args: pytest.fail("Restore enumerated a source")
    app = GUCmailApp(context, gateway=facade)
    queued, dispatch = asyncio.Event(), asyncio.Event()

    async def wait(pilot, condition):
        for _ in range(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Queued restore did not settle")

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

            # Defer the real executor submission; no mailbox method is replaced here.
            loop = asyncio.get_running_loop()
            submit = loop.run_in_executor

            def queued_submission(executor, function, *args):
                work = args[0] if len(args) == 1 else None
                owners = getattr(work, "args", ())
                if (
                    not owners
                    or owners[0] is not plan
                    or getattr(getattr(work, "func", None), "__name__", None)
                    != "_execute_operation"
                ):
                    return submit(executor, function, *args)

                async def deferred():
                    queued.set()
                    await dispatch.wait()
                    return await submit(executor, function, *args)

                return asyncio.create_task(deferred())

            monkeypatch.setattr(loop, "run_in_executor", queued_submission)
            try:
                await pilot.press("y")
                await wait(pilot, lambda: queued.is_set() and app.screen is plan)
                assert not calls and app_write_coordinator(app).pending
                if stop == "cancel":
                    await pilot.press("ctrl+c")
                else:
                    plan._operation_timed_out(plan._operation_generation, "Restoring backup")
                assert plan._operation_active and app_write_coordinator(app).pending
                assert plan._cancel_event.is_set() and not calls
                await pilot.press("escape")
                assert app.screen is plan and app_write_coordinator(app).pending
                dispatch.set()
                await wait(
                    pilot,
                    lambda: not plan._operation_active and not app_write_coordinator(app).pending,
                )
                assert not calls and plan.view_state is ViewState.CANCELLED
                detail = str(plan.query_one("#restore-plan-detail", Static).content)
                assert (
                    "Attempted: 0" in detail and "Restored: 0" in detail
                    and "Unconfirmed: 0" in detail and "Rejected/not started: 0" in detail
                )
                assert "may have created" not in detail
                assert "invalid restore result" not in str(
                    plan.query_one("#area-state", Static).content
                )
                assert plan.query_one("#restore-execute", Button).disabled
            finally:
                dispatch.set()
    finally:
        dispatch.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_restore_timeout_retains_uncertain_provider_failure(tmp_path, size):
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
        raise ConnectionError("synthetic-private-provider-failure")

    facade = FakeGateway()
    facade.restore_item = restore
    facade.iter_backup_items = lambda *args: pytest.fail("Restore enumerated live source")
    app = GUCmailApp(context, gateway=facade)

    async def wait(pilot, condition):
        for _ in range(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Synthetic restore timeout did not settle")

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
            # Inject only the deadline callback; all navigation and mailbox behavior is real.
            plan._operation_timed_out(plan._operation_generation, "Restoring backup")
            assert calls == ["m-one"] and plan._cancel_event.is_set()
            assert plan._operation_active and app_write_coordinator(app).pending
            assert plan.query_one("#restore-execute", Button).disabled
            status = plan.query_one("#area-progress", Static)
            await wait(
                pilot,
                lambda: (
                    "timed out" in str(status.content).casefold()
                    and plan.region.contains_region(status.region)
                ),
            )
            release.set()
            await wait(pilot, lambda: not app_write_coordinator(app).pending)
            assert calls == ["m-one"], "Cancellation started later Exchange creates"
            await wait(pilot, lambda: not plan._operation_active)
            assert plan.query_one("#restore-execute", Button).disabled
            detail = str(plan.query_one("#restore-plan-detail", Static).content)
            assert "Restored: 0" in detail and "Unconfirmed: 1" in detail
            assert "Rejected/not started: 0" in detail
            assert "Failed reference: m-one" in detail and "may have created" in detail
            assert "synthetic-private-provider-failure" not in detail
            assert plan.view_state is ViewState.ERROR
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_invalid_backend_result_after_entry_is_not_zero_attempt_cancellation(size):
    calls = []

    class InvalidBackend:
        def execute_restore(self, plan, *, cancel, **kwargs):
            calls.append(plan.backup_id)
            cancel.set()
            return None

    plan = RestorePlanView(
        "synthetic-backup",
        (RestorePlanEntryView("m-one", "message", "Restored/Inbox", "create"),),
        1,
        0,
        0,
        confirmation_token="synthetic-token",
    )
    screen = RestorePlanScreen(plan, InvalidBackend(), offline=False)
    app = App()

    async def wait(pilot, condition):
        for _ in range(300):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Invalid backend result was not contained")

    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await wait(pilot, lambda: screen.is_mounted)
        button = screen.query_one("#restore-execute", Button)
        button.focus()
        await wait(pilot, lambda: button.has_focus and screen.region.contains_region(button.region))
        await pilot.press("enter")
        await wait(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        assert not calls
        await pilot.press("y")
        await wait(pilot, lambda: bool(calls) and not screen._operation_active)
        assert calls == ["synthetic-backup"] and not app_write_coordinator(app).pending
        assert screen.view_state is ViewState.ERROR
        assert "invalid restore result" in str(screen.query_one("#area-state", Static).content)
        assert "Attempted: 0" not in str(screen.query_one("#restore-plan-detail", Static).content)
        state = str(screen.query_one("#area-state", Static).content).casefold()
        assert "may have been created" in state
        assert "inspect the destination" in state
        summary = str(screen.query_one("#restore-plan-summary", Static).content).casefold()
        assert "pending" not in summary
        assert "without a valid result" in summary and "may have been created" in summary
        assert button.disabled
