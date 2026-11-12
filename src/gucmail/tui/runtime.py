"""Process-local compatibility fixes for explicitly supported Textual versions."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from functools import lru_cache
from importlib.metadata import version
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from textual.color import Color
    from textual.style import Style


def configure_styles_cache() -> bool:
    """Keep Textual 7.5.0's background memoization independent of widget lifetimes.

    The installed method caches ``(self, colors)`` although its calculation uses
    only colors. Its global LRU consequently owns closed widgets' StylesCache
    instances and their rendered strips. Keep the original pure calculation and
    1024-entry bound, with colors as the key. Other versions are left untouched.
    """
    if version("textual") != "7.5.0":
        return False
    from textual._styles_cache import StylesCache

    previous = StylesCache.get_inner_outer
    if getattr(previous, "_gucmail_color_keys", False):
        return True
    original = getattr(previous, "__wrapped__", None)
    if not callable(original):
        return False
    calculate = cast("Callable[[None, Color, Color], tuple[Style, Style]]", original)

    @lru_cache(maxsize=1024)
    def by_colors(base: Color, background: Color) -> tuple[Style, Style]:
        return calculate(None, base, background)

    def get_inner_outer(_cache: StylesCache, base: Color, background: Color) -> tuple[Style, Style]:
        return by_colors(base, background)

    # Preserve cache inspection/clearing for diagnostics, without retaining the
    # original instance-keyed LRU or changing installed dependency files.
    previous.cache_clear()
    adapted = cast(Any, get_inner_outer)
    adapted.cache_info = by_colors.cache_info
    adapted.cache_clear = by_colors.cache_clear
    adapted._gucmail_color_keys = True
    cast(Any, StylesCache).get_inner_outer = get_inner_outer
    return True


async def event_loop_sleep(seconds: float) -> None:
    """Yield to the loop and preserve the requested high-resolution deadline.

    Windows loop clocks may allow early callbacks within their clock resolution.
    Compare against Textual's perf_counter clock so a due-message or operation
    timer cannot fire early simply because of this compatibility change.
    Cancellation propagates naturally; no executor or native wait is allocated.
    """
    if seconds <= 0:
        await asyncio.sleep(0)
        return
    deadline = perf_counter() + seconds
    await asyncio.sleep(seconds)
    while (remaining := deadline - perf_counter()) > 0:
        # A sub-clock-resolution delay can be promoted immediately on a busy
        # Windows loop. Yield for a full clock tick rather than spinning while
        # perf_counter still reports time remaining.
        await asyncio.sleep(max(remaining, 0.016))


def configure_windows_sleep() -> bool:
    """Keep cancellable UI sleeps on the event loop, without native waiter threads.

    Textual7's Windows sleeper can close native handles while its executor wait
    is pending. Standard asyncio sleeps sacrifice sub-millisecond animation
    precision in exchange for cancellation without a pending native wait.
    The application-supported Textual major version alone is adapted. No
    installed dependency files or asyncio policies are changed.
    """
    if sys.platform != "win32" or not version("textual").startswith("7."):
        return False
    from textual import _time

    if not hasattr(_time, "win_sleep"):
        return False
    _time.win_sleep = event_loop_sleep
    return True
