"""User-facing send review and ambiguous-operation regression tests."""

import asyncio
from threading import Event

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Static

from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.area_common import OperationalScreen, ViewState
from test_tui_visual_regressions import _context
from waiting import wait_until


@pytest.mark.asyncio
async def test_send_review_preserves_invalid_composer_and_lists_real_recipients(tmp_path):
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(100, 32)) as pilot:
        async def until(condition):
            async with asyncio.timeout(15):
                while not condition():
                    await asyncio.sleep(0.005)
            await pilot.pause()

        screen = ComposeScreen()
        await app.push_screen(screen)
        await until(lambda: not screen._attempt_pending)
        assert not screen.query_one("#compose-send", Button).disabled
        screen.action_review_send()
        await until(lambda: not screen._review_pending)
        assert app.screen is screen
        assert "recipient" in str(screen.query_one("#compose-note", Static).content)
        assert screen.query_one("#compose-to", Input).has_focus
        screen.query_one("#compose-to", Input).value = "first@example.test; second@example.test"
        screen.query_one("#compose-subject", Input).value = "Synthetic review"
        from textual.widgets import TextArea

        screen.query_one("#compose-body", TextArea).text = "Synthetic body"
        screen.action_review_send()
        await until(lambda: isinstance(app.screen, ConfirmationScreen))
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Recipients: 2" in app.screen.detail
        assert "first@example.test" in app.screen.detail
        assert "second@example.test" in app.screen.detail
        await pilot.press("escape")
        assert app.screen is screen


class OperationScreen(OperationalScreen):
    def __init__(self):
        super().__init__(area_name="synthetic", operation_timeout_seconds=5)

    def compose(self) -> ComposeResult:
        yield Static(id="area-state")
        yield Button("Retry", id="area-retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_safe", [False, True])
async def test_timeout_retry_requires_explicit_read_permission(retry_safe):
    app = App()
    screen = OperationScreen()
    started = Event()
    calls = []

    def operation(cancel, progress):
        calls.append(1)
        started.set()
        cancel.wait(2)

    async with app.run_test() as pilot:
        await app.push_screen(screen)
        screen._begin_operation(
            "Synthetic operation",
            operation,
            lambda _: None,
            timeout_seconds=1.0,  # long enough for a busy machine to start the work
            retry_safe=retry_safe,
        )
        await wait_until(pilot, lambda: screen.view_state is ViewState.ERROR, interval=0.02)
        assert started.is_set()
        assert screen.view_state is ViewState.ERROR
        assert "1 seconds" in str(screen.query_one("#area-state", Static).content)
        assert screen.query_one("#area-retry", Button).display is retry_safe
        screen.action_retry()
        if retry_safe:
            await wait_until(pilot, lambda: len(calls) == 2)
        else:
            await pilot.pause(0.3)  # a wrong retry would have started by now
        assert len(calls) == (2 if retry_safe else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["refresh", "cancel", "timeout"])
async def test_pending_write_cannot_be_superseded_or_repeated(stop):
    app = App()
    screen = OperationScreen()
    started, release = Event(), Event()
    calls, completed = [], []

    def write(*_):
        calls.append("write")
        started.set()
        assert release.wait(10), "Synthetic write was not released"
        return "saved"

    async with app.run_test() as pilot:
        await app.push_screen(screen)
        try:
            screen._begin_operation("Save", write, completed.append)
            await wait_until(pilot, lambda: started.is_set(), interval=0.02)
            assert started.is_set() and screen.mutation_pending
            if stop == "cancel":
                screen.action_cancel_operation()
            elif stop == "timeout":
                screen._operation_timed_out(screen._operation_generation, "Save")
            screen._begin_operation(
                "Refresh", lambda *_: calls.append("read"), lambda _: None, retry_safe=True
            )
            screen._begin_operation("Save again", write, completed.append)
            screen._confirm("Another write?", "Synthetic", lambda: calls.append("confirm"))
            await pilot.pause()
            assert app.screen is screen and calls == ["write"]
            assert "still running" in str(screen.query_one("#area-state", Static).content)
        finally:
            release.set()
        await wait_until(pilot, lambda: not screen.mutation_pending, interval=0.02)
        assert not screen.mutation_pending
        assert completed == (["saved"] if stop == "refresh" else [])
        screen._begin_operation(
            "Refresh", lambda *_: calls.append("read"), lambda _: None, retry_safe=True
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["write", "read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_cancelled_unstarted_write_releases_guard_without_executing(stop):
    app = App()
    screen = OperationScreen()
    calls = []
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        screen._begin_operation("Queued save", lambda *_: calls.append("cancelled"), lambda _: None)
        if stop == "cancel":
            screen.action_cancel_operation()
        else:
            screen._operation_timed_out(screen._operation_generation, "Queued save")
        await wait_until(pilot, lambda: not screen.mutation_pending, interval=0.02)
        assert not screen.mutation_pending and not calls
        screen._begin_operation("Fresh save", lambda *_: calls.append("fresh"), lambda _: None)
        await app.workers.wait_for_complete()
        assert calls == ["fresh"]
