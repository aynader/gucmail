from __future__ import annotations

import asyncio
from importlib.metadata import version
from time import perf_counter

import pytest
from textual import _time
from textual.app import App

from gucmail.tui import runtime


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_their_existing_textual_clock(monkeypatch, platform):
    original = getattr(_time, "win_sleep", None)
    monkeypatch.setattr(runtime.sys, "platform", platform)
    assert not runtime.configure_windows_sleep()
    assert getattr(_time, "win_sleep", None) is original


def test_future_major_version_is_not_patched(monkeypatch):
    original = getattr(_time, "win_sleep", None)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "version", lambda name: "8.0.0")
    assert not runtime.configure_windows_sleep()
    assert getattr(_time, "win_sleep", None) is original


@pytest.mark.asyncio
async def test_actual_textual_sleep_cancels_long_waits_without_executor_threads(monkeypatch):
    assert version("textual").startswith("7.")
    assert runtime.configure_windows_sleep() == (runtime.sys.platform == "win32")
    loop = asyncio.get_running_loop()
    calls = []
    original = loop.run_in_executor

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(loop, "run_in_executor", counted)
    tasks = [asyncio.create_task(_time.sleep(3600)) for _ in range(300)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    for task in tasks:
        task.cancel()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    started = perf_counter()
    await _time.sleep(0.02)
    assert perf_counter() - started >= 0.02
    assert calls == []


@pytest.mark.asyncio
async def test_early_event_loop_wakeup_waits_until_the_actual_deadline(monkeypatch):
    clock = iter([0.0, 0.002, 0.006, 0.01])
    monkeypatch.setattr(runtime, "perf_counter", lambda: next(clock))
    delays = []

    async def early_wakeup(delay):
        delays.append(delay)

    monkeypatch.setattr(runtime.asyncio, "sleep", early_wakeup)
    await runtime.event_loop_sleep(0.01)
    assert delays == [0.01, 0.016, 0.016]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_real_textual_timer_callbacks_and_clean_exit(size):
    class TimerApp(App):
        fired = 0

        def on_mount(self):
            self.set_timer(0.02, self.tick)

        def tick(self):
            self.fired += 1

    for _ in range(3):
        app = TimerApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.08)
            assert app.fired == 1
            await pilot.pause(0.03)
            assert app.fired == 1
