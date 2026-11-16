"""Wait for the app in tests: check a condition until it holds or time runs out.

Background work in the app (loading, saving, sending) finishes at a different speed on
every machine. Tests therefore wait for a result instead of for a fixed number of
pauses. A passing test returns as soon as the condition holds, so the limit only costs
time when something is really wrong.

Set GUCMAIL_TEST_WAIT_SECONDS to change the limit (default 10 seconds).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

WAIT_SECONDS = float(os.environ.get("GUCMAIL_TEST_WAIT_SECONDS", "10"))


async def wait_until(
    pilot: Any,
    condition: Callable[[], object],
    *,
    interval: float | None = 0.02,
    timeout: float | None = None,
    pause_first: bool = False,
) -> bool:
    """Return True once ``condition()`` is true, or False when the time limit passes.

    ``pilot`` is anything with an async ``pause(seconds)``, usually Textual's Pilot.
    ``pause_first`` lets the app handle a just-sent key or click before the first check.
    The caller asserts afterwards, so a timeout fails with the test's own message.
    """

    limit = max(timeout or 0.0, WAIT_SECONDS)
    deadline = time.monotonic() + limit
    if pause_first:
        await pilot.pause(interval)
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        await pilot.pause(interval)


def rounds(minimum: int = 0) -> Iterator[int]:
    """Count rounds for a hand-written polling loop: ``for _ in rounds(40): ...``.

    Gives at least ``minimum`` rounds, then keeps going until the wait limit passes, so a
    loop that breaks when the app is ready doesn't give up early on a slow machine.
    Prefer ``wait_until`` when the loop only checks one condition.
    """

    deadline = time.monotonic() + WAIT_SECONDS
    count = 0
    while count < minimum or time.monotonic() < deadline:
        yield count
        count += 1


def _settled(button: Any) -> bool:
    """The button can be used and its screen would act on it now.

    Area screens ignore a new change while one of their own, or any change in the app,
    is still being written, so wait for those too.
    """
    from gucmail.tui.write_coordinator import app_write_coordinator

    screen = button.screen
    return (
        not button.disabled
        and not button.has_class("-active")
        and not getattr(screen, "_operation_active", False)
        and not getattr(screen, "_render_pending", False)
        and not getattr(screen, "mutation_pending", False)
        and not app_write_coordinator(button.app).pending
    )


async def press_button(pilot: Any, button: Any) -> None:
    """Press ``button`` with the keyboard once its screen has settled.

    Waits until the button is enabled and its screen has no work running or redraw
    pending, then focuses it and presses Enter. A redraw that finishes late can move the
    focus to another control, so focus is checked again right before the key press.
    """

    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        await wait_until(pilot, lambda: _settled(button))
        button.focus()
        await pilot.pause()
        if button.has_focus and _settled(button):
            await pilot.press("enter")
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"Button {button.id!r} never settled with keyboard focus")


async def click_button(pilot: Any, button: Any) -> None:
    """Click ``button`` with the mouse once its screen has settled and it stops moving.

    When work finishes, a screen often shows or hides other buttons, which moves this
    one. A click aimed at the old position would land on empty space, so the button's
    position must stay the same across a pause before the click.
    """

    deadline = time.monotonic() + WAIT_SECONDS
    seen = None
    while True:
        await wait_until(pilot, lambda: _settled(button))
        region = button.region
        if region.area and region == seen:
            assert await pilot.click(button), f"Click on {button.id!r} landed elsewhere"
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"Button {button.id!r} never stopped moving")
        seen = region
        await pilot.pause(0.05)
