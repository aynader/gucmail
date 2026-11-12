"""Users can see each focused field while tabbing and after compact resize."""

import pytest
from textual.app import App
from textual.widgets import Input, Select

from gucmail.tui.pim_screens import CalendarEditorScreen, ContactEditorScreen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["calendar", "contact"])
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_calendar_contact_keyboard_fields_are_visible_and_survive_resize(kind, size):
    app = App()
    editor = (
        CalendarEditorScreen(None, timezone="Africa/Cairo", creating=True)
        if kind == "calendar"
        else ContactEditorScreen(None, creating=True)
    )
    start_id = "calendar-edit-subject" if kind == "calendar" else "contact-edit-name"
    target_id = "calendar-edit-attachments" if kind == "calendar" else "contact-edit-categories"
    fields_id = f"{kind}-editor-fields"
    async with app.run_test(size=size) as pilot:
        app.push_screen(editor)
        await pilot.pause()
        if kind == "calendar":
            editor.query_one("#calendar-edit-repeat", Select).value = "weekly"
            await pilot.pause()
        editor.query_one("#" + start_id, Input).focus()
        await pilot.pause()
        target = editor.query_one("#" + target_id, Input)
        fields = editor.query_one("#" + fields_id)
        for _ in range(50):
            await pilot.press("tab")
            await pilot.pause()
            focused = editor.focused
            if isinstance(focused, Input) and fields in focused.ancestors and focused.visible:
                assert fields.region.contains_region(focused.region), focused.id
            if focused is target:
                break
        await pilot.pause()
        assert editor.focused is target
        assert fields.region.contains_region(target.region)
        target.value = "synthetic retained input"
        await pilot.resize_terminal(70, 20)
        await pilot.pause()
        assert editor.focused is target and target.value == "synthetic retained input"
        assert fields.region.contains_region(target.region)
        await pilot.press("shift+tab", "tab")
        await pilot.pause()
        assert editor.focused is target
        assert fields.region.contains_region(target.region)
        editor.action_cancel()
