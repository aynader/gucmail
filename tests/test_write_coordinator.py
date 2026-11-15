"""Unfinished backend requests must survive UI cancellation and navigation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import perf_counter

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Static, TextArea

from gucmail.models import Address, DraftData
from gucmail.services.compose import ComposeService
from gucmail.services.drafts import DraftService
from gucmail.services.messages import MessageService
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.area_common import OperationalScreen, ViewState
from gucmail.tui.write_coordinator import WriteCoordinator, app_write_coordinator
from test_tui_visual_regressions import _context
from waiting import WAIT_SECONDS


async def _until(predicate):
    deadline = perf_counter() + WAIT_SECONDS
    while perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("Synthetic UI operation did not reach its expected state")


class _Workspace(OperationalScreen):
    def __init__(self):
        super().__init__(area_name="synthetic", operation_timeout_seconds=15)

    def compose(self) -> ComposeResult:
        yield Static(id="area-state")
        yield Button("Retry", id="area-retry")


class _Gateway:
    def __init__(self, *, fail=False):
        self.started, self.release, self.finished = Event(), Event(), Event()
        self.calls = []
        self.fail = fail

    def flag(self, reference, flagged):
        self.calls.append(("flag", reference, flagged))
        self.started.set()
        try:
            assert self.release.wait(15), "Synthetic gateway request was not released"
            if self.fail:
                raise RuntimeError("synthetic backend failure")
        finally:
            self.finished.set()

    def mark_read(self, reference, read):
        self.calls.append(("read-state", reference, read))

    def delete_message(self, reference, *, permanent=False):
        self.calls.append(("delete", reference, permanent))

    def send(self, draft):
        self.calls.append(("send", draft.reference))


def test_contending_threads_cannot_reserve_multiple_changes():
    coordinator = WriteCoordinator()
    before, after = Barrier(12), Barrier(12)

    def contend():
        before.wait(timeout=10)
        lease = coordinator.reserve()
        accepted = lease is not None and lease.start()
        after.wait(timeout=10)
        if lease is not None:
            lease.finish()
        return accepted

    with ThreadPoolExecutor(max_workers=12) as threads:
        results = list(threads.map(lambda _: contend(), range(12)))
    assert results.count(True) == 1
    assert not coordinator.pending


def test_cancelled_reservation_cannot_start_or_release_a_new_owner():
    coordinator = WriteCoordinator()
    old = coordinator.reserve()
    assert old is not None and old.cancel_if_unstarted()
    assert old.finished.is_set() and not old.start()
    fresh = coordinator.reserve()
    assert fresh is not None and fresh.start() and not fresh.start()
    old.finish()
    assert coordinator.pending and not fresh.finished.is_set()
    assert not fresh.cancel_if_unstarted()
    assert coordinator.pending
    fresh.finish()
    assert not coordinator.pending


def test_coordinator_is_shared_by_one_app_and_isolated_from_another():
    first, second = App(), App()
    lease = app_write_coordinator(first).reserve()
    assert lease is not None
    assert app_write_coordinator(first).reserve() is None
    assert not app_write_coordinator(second).pending
    lease.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout", "close", "worker-cancel"])
async def test_navigation_does_not_release_a_started_workspace_write(stop):
    app = App()
    first, second = _Workspace(), _Workspace()
    started, release = Event(), Event()
    calls, accepted = [], []

    def held_write(*_):
        calls.append("original-write")
        started.set()
        assert release.wait(15), "Synthetic workspace request was not released"
        return "saved"

    async with app.run_test() as pilot:
        await app.push_screen(first)
        try:
            first._begin_operation("Save", held_write, accepted.append)
            await _until(started.is_set)
            if stop == "cancel":
                first.action_cancel_operation()
            elif stop == "timeout":
                first._operation_timed_out(first._operation_generation, "Save")
            elif stop == "worker-cancel":
                assert first._active_worker is not None
                first._active_worker.cancel()
                await _until(lambda: not first._operation_active)
            if stop == "close":
                first.action_close()
                await _until(lambda: first not in app.screen_stack)
                await pilot.pause()
            else:
                await app.pop_screen()
            await app.push_screen(second)
            assert app_write_coordinator(app).pending
            # Inspection on another screen is safe; a second change is rejected.
            second._begin_operation(
                "Inspect", lambda *_: calls.append("read"), lambda _: None, retry_safe=True
            )
            await _until(lambda: second.view_state is ViewState.READY)
            second._begin_operation("Change", lambda *_: calls.append("blocked"), lambda _: None)
            second._confirm("Change?", "Synthetic", lambda: calls.append("confirmed"))
            await pilot.pause()
            assert app.screen is second
            assert calls == ["original-write", "read"] and not accepted
            assert "still running" in str(second.query_one("#area-state", Static).content)
            assert not second.query_one("#area-retry", Button).display
        finally:
            release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await pilot.pause()
        assert not accepted
        second.action_retry()
        await pilot.pause()
        assert calls == ["original-write", "read"]
        second._begin_operation("Fresh change", lambda *_: calls.append("fresh"), lambda _: None)
        await app.workers.wait_for_complete()
        assert calls == ["original-write", "read", "fresh"]


@pytest.mark.asyncio
async def test_unmounted_cancelled_queued_workspace_write_cannot_run_later():
    app = App()
    screen = _Workspace()
    calls = []
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        screen._begin_operation("Queued save", lambda *_: calls.append("stale"), lambda _: None)
        assert screen._active_worker is not None
        screen._active_worker.cancel()  # no event-loop yield before cancellation
        await app.pop_screen()
        assert not app_write_coordinator(app).pending
        replacement = _Workspace()
        await app.push_screen(replacement)
        replacement._begin_operation("New save", lambda *_: calls.append("fresh"), lambda _: None)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["fresh"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_root_worker_retains_guard_until_gateway_returns(tmp_path, cancel, fail):
    gateway = _Gateway(fail=fail)
    app = GUCmailApp(_context(tmp_path), gateway=gateway, offline=True)
    async with app.run_test() as pilot:
        try:
            worker = app._set_message_flag("original", flagged=True)
            await _until(gateway.started.is_set)
            if cancel:
                worker.cancel()
                await _until(lambda: worker.is_cancelled)
            app._set_message_flag("duplicate", flagged=False)
            app._toggle_message_read("other", read=True)
            app._delete_message("other")
            plan = MessageService(gateway).plan_mark_read(("one", "two"))
            app._execute_bulk_mark_read(plan)
            await _until(lambda: "No new action" in app._status_activity)
            await pilot.pause()
            assert app_write_coordinator(app).pending
            assert gateway.calls == [("flag", "original", True)]
        finally:
            gateway.release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await _until(gateway.finished.is_set)
        await pilot.pause()
        assert gateway.calls == [("flag", "original", True)]
        app._toggle_message_read("explicit-after-finish", read=False)
        await app.workers.wait_for_complete()
        assert gateway.calls[-1] == ("read-state", "explicit-after-finish", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "save"])
async def test_blocked_composer_submission_preserves_recovery_without_send_or_sync(
    tmp_path, action
):
    gateway = _Gateway()
    context = _context(tmp_path)
    app = GUCmailApp(context, gateway=gateway, offline=True)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    payload = {
        "action": action,
        "reference": "dft-overlapping-review",
        "to": "recipient@example.test",
        "subject": "Synthetic held-request review",
        "body": "Recover this exact composed text",
    }
    payload["confirmation_scope"] = (
        ComposeService().plan_send(app._draft_from_payload(payload)).confirmation_scope
    )
    async with app.run_test() as pilot:
        try:
            app._set_message_flag("original", flagged=True)
            await _until(gateway.started.is_set)
            await app.push_screen(ComposeScreen())
            app.submit_composer(payload)
            await _until(lambda: payload["reference"] in drafts.recovery_references())
            await _until(
                lambda: (
                    "recovery copy is retained"
                    in str(app.screen.query_one("#compose-note", Static).content)
                )
            )
            recovered = drafts.read_recovery(payload["reference"])
            assert recovered.subject == payload["subject"]
            assert recovered.body_text == payload["body"]
            assert recovered.to == [Address("recipient@example.test")]
            assert not drafts.exists(payload["reference"])
            assert not app._scheduler_service().list()
            assert gateway.calls == [("flag", "original", True)]
        finally:
            gateway.release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await pilot.pause()
        assert gateway.calls == [("flag", "original", True)]
        assert drafts.read_recovery(payload["reference"]).body_text == payload["body"]


@pytest.mark.asyncio
async def test_workspace_write_blocks_root_and_delayed_send_without_claiming_job(tmp_path):
    gateway = _Gateway()
    context = _context(tmp_path)
    app = GUCmailApp(context, gateway=gateway, offline=True)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    draft = drafts.create(
        DraftData("dft-held-delay", to=[Address("recipient@example.test")], subject="Synthetic")
    )
    job = app._scheduler_service().queue_undo_send(
        draft.reference, 1, now=datetime.now(UTC) - timedelta(seconds=5)
    )
    screen = _Workspace()
    started, release = Event(), Event()

    def held(*_):
        started.set()
        assert release.wait(15)

    # Both blocked changes below report on the one status line, in whichever order their
    # threads run, so look at every status shown rather than only the last.
    statuses: list[str] = []
    set_status = app._set_status

    def recording(text: str) -> None:
        statuses.append(text)
        set_status(text)

    app._set_status = recording  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        # Enable the injected synthetic gateway after startup, without invoking
        # authentication or folder loading. The job is genuinely due.
        app.offline = False
        await app.push_screen(screen)
        try:
            screen._begin_operation("Synthetic write", held, lambda _: None)
            await _until(started.is_set)
            app._delete_message("blocked-root")
            app._finish_undo_delay(job.reference)
            await _until(lambda: any("job remains queued" in text for text in statuses))
            await pilot.pause()
            assert not gateway.calls
            current = app._scheduler_service().get(job.reference)
            assert current.message.state == job.state
            assert current.message.attempts == 0
            assert "still running" in str(screen.query_one("#area-state", Static).content)
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not gateway.calls  # rejection never arranges a delayed replay


@pytest.mark.asyncio
async def test_cancelled_queued_root_worker_does_not_start_or_strand_the_guard(tmp_path):
    gateway = _Gateway()
    app = GUCmailApp(_context(tmp_path), gateway=gateway, offline=True)
    async with app.run_test() as pilot:
        worker = app._set_message_flag("cancelled-before-thread", flagged=True)
        worker.cancel()
        await pilot.pause()
        assert not gateway.calls and not app_write_coordinator(app).pending
        app._toggle_message_read("fresh", read=True)
        await app.workers.wait_for_complete()
        assert gateway.calls == [("read-state", "fresh", True)]


@pytest.mark.asyncio
async def test_already_open_confirmation_rechecks_shared_guard_before_change(tmp_path):
    gateway = _Gateway()
    app = GUCmailApp(_context(tmp_path), gateway=gateway, offline=True)
    screen = _Workspace()
    calls = []
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        screen._confirm(
            "Synthetic change?",
            "Exactly one synthetic target",
            lambda: screen._begin_operation(
                "Confirmed change", lambda *_: calls.append("changed"), lambda _: None
            ),
        )
        await pilot.pause()
        try:
            app._set_message_flag("original", flagged=True)
            await _until(gateway.started.is_set)
            await pilot.press("y")
            await pilot.pause()
            assert app.screen is screen and not calls
            assert "still running" in str(screen.query_one("#area-state", Static).content)
            assert not screen.query_one("#area-retry", Button).display
        finally:
            gateway.release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_composer_actions_keep_content_open_during_another_change(tmp_path, size):
    gateway = _Gateway()
    app = GUCmailApp(_context(tmp_path), gateway=gateway, offline=True)
    draft = DraftData(
        "dft-open-while-pending",
        to=[Address("recipient@example.test")],
        subject="Synthetic unsent draft",
        body_text="Keep this exact text open",
    )
    async with app.run_test(size=size) as pilot:
        screen = ComposeScreen(draft)
        await app.push_screen(screen)
        await _until(lambda: not screen._attempt_pending)
        try:
            app._set_message_flag("original", flagged=True)
            await _until(gateway.started.is_set)
            screen.query_one("#compose-send", Button).press()
            # Button.Pressed crosses message queues. One idle-frame pause can
            # finish before its parent handler; wait for the visible acknowledgement.
            await _until(
                lambda: (
                    "composer remains open"
                    in str(screen.query_one("#compose-note", Static).content)
                )
            )
            assert app.screen is screen
            assert "composer remains open" in str(screen.query_one("#compose-note", Static).content)
            screen.query_one("#compose-note", Static).update("")
            screen.query_one("#compose-save", Button).press()
            await _until(
                lambda: (
                    "composer remains open"
                    in str(screen.query_one("#compose-note", Static).content)
                )
            )
            screen.query_one("#compose-note", Static).update("")
            screen.action_schedule_draft()
            await pilot.pause()
            assert app.screen is screen
            await _until(lambda: not screen._attempt_pending)
            assert "composer remains open" in str(screen.query_one("#compose-note", Static).content)
            assert screen.query_one("#compose-body", TextArea).text == draft.body_text
            assert not screen.query_one("#compose-send", Button).disabled
            assert not screen.query_one("#compose-save", Button).disabled
            assert gateway.calls == [("flag", "original", True)]
        finally:
            gateway.release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await pilot.pause()
        assert gateway.calls == [("flag", "original", True)]
        screen.action_review_send()
        await _until(lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        app.screen.dismiss(False)


@pytest.mark.asyncio
async def test_send_review_opened_before_another_change_keeps_composer_on_confirm(tmp_path):
    gateway = _Gateway()
    app = GUCmailApp(_context(tmp_path), gateway=gateway, offline=True)
    draft = DraftData(
        "dft-reviewed-before-pending",
        to=[Address("recipient@example.test")],
        subject="Synthetic unsent draft",
        body_text="Keep this reviewed text open",
    )
    async with app.run_test() as pilot:
        screen = ComposeScreen(draft)
        await app.push_screen(screen)
        await _until(lambda: not screen._attempt_pending)
        screen.action_review_send()
        await _until(lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        try:
            app._set_message_flag("original", flagged=True)
            await _until(gateway.started.is_set)
            app.screen.query_one("#confirm", Button).press()
            await pilot.pause()
            assert app.screen is screen
            await _until(lambda: not screen._attempt_pending)
            assert "composer remains open" in str(screen.query_one("#compose-note", Static).content)
            assert screen.query_one("#compose-body", TextArea).text == draft.body_text
        finally:
            gateway.release.set()
        await _until(lambda: not app_write_coordinator(app).pending)
        await app.workers.wait_for_complete()
        assert gateway.calls == [("flag", "original", True)]


def test_status_notification_failure_cannot_strand_the_actual_write_lease():
    def unavailable():
        raise RuntimeError("Synthetic UI notification failure")

    coordinator = WriteCoordinator(on_change=unavailable)
    lease = coordinator.reserve()
    assert lease is not None and coordinator.pending
    assert lease.start()
    lease.finish()
    assert lease.finished.is_set() and not coordinator.pending


@pytest.mark.asyncio
async def test_closed_workspace_keeps_pending_change_visible_on_mail(tmp_path):
    from gucmail.tui.write_coordinator import CHANGE_IN_PROGRESS_MESSAGE

    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    workspace = _Workspace()
    started, release = Event(), Event()

    def held(*_):
        started.set()
        assert release.wait(15), "Synthetic workspace request was not released"
        return "completed"

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(workspace)
            workspace._begin_operation("Synthetic change", held, lambda _: None)
            await _until(started.is_set)
            workspace.action_close()
            await _until(lambda: workspace not in app.screen_stack)
            await pilot.pause()
            assert app_write_coordinator(app).pending
            assert CHANGE_IN_PROGRESS_MESSAGE in str(app.query_one("#status-bar", Static).render())
            release.set()
            await _until(lambda: not app_write_coordinator(app).pending)
            await pilot.pause()
            assert CHANGE_IN_PROGRESS_MESSAGE not in str(
                app.query_one("#status-bar", Static).render()
            )
    finally:
        release.set()
        context.close()
