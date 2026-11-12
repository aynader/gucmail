"""Recurrence fields remain reachable during keyboard navigation and resize."""

from datetime import date

import pytest
from textual.app import App
from textual.widgets import Input, Select

from gucmail.models import TaskInfo
from gucmail.tui.pim_screens import TaskEditorScreen


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_repeating_task_fields_stay_visible_after_tab_reverse_and_resize(size):
    app = App()
    task = TaskInfo(
        "synthetic:task",
        "Keyboard recurrence review",
        "not_started",
        start_date=date(2026, 10, 9),
    )
    async with app.run_test(size=size) as pilot:
        editor = TaskEditorScreen(task)
        app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#task-edit-repeat", Select).value = "weekly"
        await pilot.pause()
        editor.query_one("#task-edit-subject", Input).focus()
        await pilot.pause()
        target = editor.query_one("#task-repeat-count", Input)
        fields = editor.query_one("#task-editor-fields")
        for _ in range(20):
            await pilot.press("tab")
            if editor.focused is target:
                break
        await pilot.pause()
        assert editor.focused is target
        assert fields.region.contains_region(target.region)
        target.value = "7"
        await pilot.press("shift+tab", "shift+tab", "tab", "tab")
        await pilot.pause()
        assert editor.focused is target
        assert fields.region.contains_region(target.region)
        await pilot.resize_terminal(70, 20)
        await pilot.pause()
        assert editor.focused is target and target.value == "7"
        assert fields.region.contains_region(target.region)
        assert editor._value().recurrence["boundary"]["occurrences"] == 7
        editor.action_cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_pending_scroll_animation_cannot_hide_the_current_task_field(size):
    app = App()
    task = TaskInfo(
        "synthetic:task",
        "Review retained focus",
        "not_started",
        start_date=date(2026, 10, 9),
    )
    async with app.run_test(size=size) as pilot:
        editor = TaskEditorScreen(task)
        app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#task-edit-repeat", Select).value = "weekly"
        await pilot.pause()
        target = editor.query_one("#task-repeat-count", Input)
        fields = editor.query_one("#task-editor-fields")
        target.focus()
        await pilot.pause()
        assert fields.region.contains_region(target.region)
        # An earlier field can have queued a scroll that starts after this focus.
        fields.animate("scroll_y", 0, duration=0.2, delay=0.1)
        editor.set_focus(target)
        await pilot.pause()
        assert not app.animator.is_being_animated(fields, "scroll_y")
        # Observe beyond the original delayed animation's full lifetime.
        # Textual's completion event stays unset when a queued timer is stopped.
        await pilot.pause(0.4)
        await pilot.pause()
        assert editor.focused is target
        assert fields.region.contains_region(target.region)
        editor.action_cancel()
