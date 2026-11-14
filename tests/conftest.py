"""Shared test setup.

Two things keep the parallel run (qa/test.py) fast and steady:

* The slowest tests start first, so no worker is left with a 2-minute test at the end.
* Interface tests that wait with "pause a little, check, repeat N times" can be given
  more time on a slow machine with GUCMAIL_TEST_PAUSE_SCALE=2 (or higher). Loops still
  stop as soon as the check passes.
"""

from __future__ import annotations

import os

import pytest
from textual.pilot import Pilot

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


_PAUSE_SCALE = float(os.environ.get("GUCMAIL_TEST_PAUSE_SCALE", "1"))
_original_pause = Pilot.pause


async def _scaled_pause(self: Pilot[object], delay: float | None = None) -> None:
    await _original_pause(self, None if delay is None else delay * _PAUSE_SCALE)


if _PAUSE_SCALE != 1:
    Pilot.pause = _scaled_pause  # type: ignore[method-assign]
