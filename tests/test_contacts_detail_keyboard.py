from __future__ import annotations

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from gucmail.services.contacts import ContactsService
from gucmail.tui.pim_screens import ContactsScreen, ContactsServiceBackend
from test_compose_preferences import wait_for
from test_contacts_display_preferences import ReadOnlyContacts


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_duplicate_review_details_can_be_reached_and_scrolled_with_keyboard(size):
    gateway = ReadOnlyContacts()
    screen = ContactsScreen(ContactsServiceBackend(ContactsService(gateway)))
    app = App()
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await wait_for(pilot, lambda: len(screen._contacts) == 3 and not screen._operation_active)
        button = screen.query_one("#contacts-duplicates", Button)
        button.focus()
        await wait_for(pilot, lambda: button.has_focus)
        await pilot.press("enter")
        await wait_for(pilot, lambda: screen._selected == {"a", "b"})
        calls = len(gateway.calls)
        for _ in range(12):
            await pilot.press("tab")
            if app.focused is not None and app.focused.id == "contacts-detail-pane":
                break
        pane = screen.query_one("#contacts-detail-pane", VerticalScroll)
        assert pane.has_focus
        assert "Maximum score: 80" in str(screen.query_one("#contacts-detail", Static).render())
        assert "shared email address" in str(screen.query_one("#contacts-detail", Static).render())
        if size != (120, 35):
            assert pane.max_scroll_y > 0
        await pilot.press("end")
        await wait_for(pilot, lambda: pane.scroll_y == pane.max_scroll_y)
        await pilot.press("home")
        await wait_for(pilot, lambda: pane.scroll_y == 0)
        await pilot.press("pagedown")
        if pane.max_scroll_y:
            await wait_for(pilot, lambda: pane.scroll_y > 0)
            await pilot.press("pageup")
            await wait_for(pilot, lambda: pane.scroll_y == 0)
        await pilot.press("shift+tab")
        assert app.focused is not pane
        assert screen._selected == {"a", "b"}
        assert len(gateway.calls) == calls and gateway.updates == gateway.deletes == []
