"""Actual Backup Center cancellation, late-result rejection and clean reopening."""

import asyncio
import hashlib
from threading import Event
from time import perf_counter

import pytest
from textual.widgets import Button, Static

from gucmail.services.backup import BackupItem, BackupService
from gucmail.services.restore_journal import RestoreJournal
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ViewState
from gucmail.tui.backup_screen import BackupScreen, RestorePlanScreen
from test_tui import context_for


async def until(pilot, condition, *, timeout=20):
    async with asyncio.timeout(timeout):
        while True:
            if condition():
                await pilot.pause()
                if condition():
                    return
            # Poll the observable state without injecting compositor barriers
            # into every widget on each iteration while disk work is running.
            await asyncio.sleep(0.005)


async def planning_cancel_workflow(context, monkeypatch, *, size, mode, capture=None, theme=None):
    service = BackupService(context.paths.backup_dir, context.state)
    archive = service.create((
        BackupItem(f"synthetic-plan-{i}", "message",
                   {"subject": f"Synthetic [literal] · جامعة · {i}"}, folder_path="Synthetic/Inbox")
        for i in range(12)
    ), backup_id="synthetic-cancel-preview")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in service.root.rglob("*") if p.is_file()}
    started, release, worker_finished, callback_finished = (Event() for _ in range(4))
    held_tokens = []
    original = BackupService.verify

    def held_verify(self, *args, **kwargs):
        if not held_tokens:
            token = kwargs.get("cancelled")
            assert token is not None, "Planning failed to pass its actual cancellation token"
            held_tokens.append(token)
            started.set()
            if not release.wait(30):
                raise TimeoutError("Owned verification hold was not released")
            return original(self, *args, **kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BackupService, "verify", held_verify)
    app = GUCmailApp(context, offline=True)
    observed = {}

    def save(name):
        if capture is not None:
            capture(name, app)

    try:
        async with app.run_test(size=size) as pilot:
            try:
                if theme is not None:
                    app.preview_theme(theme)
                app._open_area("backups")
                await until(pilot, lambda: isinstance(app.screen, BackupScreen)
                            and app.screen.is_mounted
                            and app.screen._selected_id == archive.backup_id
                            and not app.screen._operation_active)
                center = app.screen
                finish = center._finish_operation
                run_worker = center.run_worker

                def observe_worker(work, *args, **kwargs):
                    # Keep Textual's actual threaded dispatch and entire operation
                    # body, including its attempted UI handoff after backend exit.
                    def completed():
                        try:
                            return work()
                        finally:
                            worker_finished.set()

                    return run_worker(completed, *args, **kwargs)

                monkeypatch.setattr(center, "run_worker", observe_worker)

                def observe_finish(*args, **kwargs):
                    try:
                        return finish(*args, **kwargs)
                    finally:
                        callback_finished.set()

                monkeypatch.setattr(center, "_finish_operation", observe_finish)
                start = center.query_one("#backup-restore-plan", Button)
                assert not start.disabled
                start.focus()
                await until(pilot, lambda: start.has_focus)
                await pilot.press("enter")
                await until(pilot, lambda: started.is_set() and center._operation_active)
                save("planning-pending")
                if mode in {"shortcut", "button"}:
                    cancel = center.query_one("#area-cancel-operation", Button)
                    cancel.focus()
                    await until(pilot, lambda: cancel.has_focus)
                    await pilot.press("ctrl+c" if mode == "shortcut" else "enter")
                    await until(pilot, lambda: center.view_state is ViewState.CANCELLED
                                and not center._operation_active)
                    assert app.screen is center
                elif mode == "close":
                    await pilot.press("escape")
                    await until(pilot, lambda: app.screen is not center)
                else:
                    assert mode == "timeout"
                    center._operation_timed_out(
                        center._operation_generation, "Generating restore plan",
                    )
                    assert center.view_state is ViewState.ERROR
                    assert "timed out" in str(center.query_one("#area-state", Static).content)
                assert held_tokens[0].is_set()
                if mode != "close":
                    await pilot.pause()
                    status = center.query_one("#area-state", Static)
                    assert center.region.contains_region(status.region), (
                        "Stopped-operation status is outside the visible terminal", status.region,
                    )
                    assert not str(center.query_one("#area-progress", Static).content)
                save("cancel-requested")
                release.set()
                await until(pilot, worker_finished.is_set)
                if mode != "close":
                    assert callback_finished.is_set()
                else:
                    # Textual's is_mounted flag means mount completed once; the
                    # current app registry/stack establish that Close removed it.
                    await until(pilot, lambda: not app.is_mounted(center))
                    assert center not in app.screen_stack
                await pilot.pause()
                assert not isinstance(app.screen, RestorePlanScreen)
                assert not RestoreJournal(context.state).operations()
                assert context._gateway is None and app.gateway is None
                save("late-result-ignored")
                if mode == "close":
                    app._open_area("backups")
                    await until(pilot, lambda: isinstance(app.screen, BackupScreen)
                                and app.screen.is_mounted
                                and app.screen._selected_id == archive.backup_id
                                and not app.screen._operation_active)
                    center = app.screen
                start = center.query_one("#backup-restore-plan", Button)
                await until(pilot, lambda: not start.disabled and not start.has_class("-active"))
                start.focus()
                await until(pilot, lambda: start.has_focus)
                fresh_started = perf_counter()
                await pilot.press("enter")
                try:
                    await until(pilot, lambda: isinstance(app.screen, RestorePlanScreen)
                                and app.screen.is_mounted
                                and bool(app.screen.query("#restore-plan-range"))
                                and app.screen.view_state is ViewState.READY, timeout=90)
                except TimeoutError:
                    import sys
                    import traceback

                    details = {
                        "mode": mode, "screen": type(app.screen).__name__,
                        "focus": str(app.screen.focused),
                        "state": str(getattr(app.screen, "view_state", None)),
                        "busy": getattr(app.screen, "_operation_active", None),
                        "status": [str(item.content) for item in app.screen.query("#area-state")],
                        "buttons": [(b.id, b.disabled, b.has_class("-active"))
                                    for b in app.screen.query(Button)],
                        "worker_stacks": [
                            [(frame.filename, frame.name, frame.lineno) for frame in
                             traceback.extract_stack(stack)[-10:]]
                            for stack in sys._current_frames().values()
                        ],
                    }
                    raise AssertionError(details) from None
                fresh = app.screen
                assert fresh.plan.create_count == 12 and len(fresh.plan.entries) == 12
                assert fresh.query_one("#restore-execute", Button).disabled
                assert not RestoreJournal(context.state).operations()
                assert context._gateway is None and app.gateway is None
                save("fresh-preview")
                observed = {"mode": mode, "size": list(size), "worker_finished": True,
                            "late_plan_suppressed": True, "fresh_plan_entries": 12,
                            "provider_initialized": False, "restore_operations": 0,
                            "fresh_preview_seconds": perf_counter() - fresh_started,
                            "performance_qualified": False}
            finally:
                release.set()
    finally:
        release.set()
        context.close()
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in service.root.rglob("*") if p.is_file()}
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,size",
    [("shortcut", (80, 24)), ("button", (70, 20)), ("close", (120, 36)),
     ("timeout", (80, 24))],
)
async def test_actual_plan_cancellation_finishes_worker_and_reopens_cleanly(
    tmp_path, monkeypatch, mode, size,
):
    await planning_cancel_workflow(context_for(tmp_path), monkeypatch, size=size, mode=mode)
