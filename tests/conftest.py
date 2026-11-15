"""Shared test setup.

* The slowest tests start first, so no worker in the parallel run (qa/test.py) is left
  with a 2-minute test at the end.
* Interface tests wait with tests/waiting.py, which waits for a result rather than for a
  fixed number of pauses.
* GUCMAIL_TEST_SLOW_WORKERS=0.2 makes every background worker start and report back
  0.2 seconds late, like a slow PC. Run the suite that way to find tests that read the
  screen before the app has updated it (qa/test.py --slow does this).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from textual.app import App

# Slowest first (measured with --durations). Prefix match on the test id.
SLOW_FIRST = (
    "tests/test_attachment_index_tui.py::test_normal_settings_consent",
    "tests/test_cli.py::test_every_nested_help_page_renders_offline",
    "tests/test_tui.py::test_every_help_palette_route_opens_its_real_interactive_destination",
    "tests/test_contact_operations.py::test_normal_settings_operation_controls",
    "tests/test_restore_planning_cancellation.py::test_backend_cancel_after_third_real_decode",
    "tests/test_branding_secondpass.py::test_all_",
    "tests/test_calendar_operations.py::test_normal_settings_calendar_controls",
    "tests/test_mail_response_preferences.py::test_normal_settings_stage_cancel_apply",
    "tests/test_folder_role_search_scope.py::test_keyboard_preset_scope_replaces",
    "tests/test_tui_visual_regressions.py::test_every_theme_family_applies",
    "tests/test_pim_creation_preferences.py::test_normal_settings_cancel_apply",
    "tests/test_calendar_working_time.py::test_normal_settings_cancel_apply",
    "tests/test_mail_deletion_grouping_preferences.py::test_normal_settings_cancel_apply",
    "tests/test_restore_ui_recovery.py::test_large_plan_uses_bounded_widget_pages",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    def rank(item: pytest.Item) -> int:
        node = item.nodeid.replace("\\", "/")
        for position, prefix in enumerate(SLOW_FIRST):
            if node.startswith(prefix):
                return position
        return len(SLOW_FIRST)

    items.sort(key=rank)  # stable: everything else keeps its normal order


_SLOW_WORKERS = float(os.environ.get("GUCMAIL_TEST_SLOW_WORKERS", "0"))

if _SLOW_WORKERS > 0:
    # Slow down the thread pool itself, so worker functions stay exactly as the app made
    # them. Late start: the screen shows "loading" longer. Late finish: the work is done
    # (a fake server already saw it) but the screen hasn't heard yet. Tests that check
    # the screen must wait for the screen, not for the fake server.
    _original_submit = ThreadPoolExecutor.submit

    def _slow_submit(
        self: ThreadPoolExecutor, fn: Callable[..., object], /, *args: object, **kwargs: object
    ) -> Future[object]:
        def late() -> object:
            time.sleep(_SLOW_WORKERS)
            try:
                return fn(*args, **kwargs)
            finally:
                time.sleep(_SLOW_WORKERS)

        return _original_submit(self, late)

    ThreadPoolExecutor.submit = _slow_submit  # type: ignore[assignment,method-assign]

    # After each hand-over to the screen, the worker carries on a little later. This opens
    # the gap between "the screen got the result" and "the worker finished".
    _original_call_from_thread = App.call_from_thread

    def _slow_call_from_thread(self: App[object], *args: object, **kwargs: object) -> object:
        result: object = _original_call_from_thread(self, *args, **kwargs)  # type: ignore[arg-type]
        time.sleep(_SLOW_WORKERS)
        return result

    App.call_from_thread = _slow_call_from_thread  # type: ignore[assignment,method-assign]
