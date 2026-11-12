"""Account-scoped operation history is reviewable offline with pinned actions."""

from pathlib import Path
from threading import Event

import pytest
from textual.widgets import Button, Static

from gucmail.state import StateStore
from gucmail.tui.activity_screen import ActivityScreen
from gucmail.tui.app import AreaScreen, GUCmailApp
from gucmail.tui.area_backends import ActivityAreaBackend
from test_tui import context_for


async def wait(pilot, condition):
    for _ in range(100):
        await pilot.pause(0.03)
        if condition():
            return
    pytest.fail("Activity did not reach its expected state")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (96, 30), (120, 36), (160, 48)])
async def test_restart_review_uses_local_record_and_preserves_uncertain_effects(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    state = context.state
    state.begin_message_operation("a" * 32, "copy", "selection", ("one", "two", "three"))
    state.record_message_target("a" * 32, 0, "completed", result_reference="copied-one")
    state.record_message_target("a" * 32, 1, "dispatching")
    other = StateStore(state.path, account_key="another-account")
    other.begin_message_operation("b" * 32, "delete", "selection", ("private-other",))
    context.state = StateStore(state.path, account_key=state.account_key)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_area("activity")
        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        await wait(pilot, lambda: screen._snapshot is not None and not screen._operation_active)
        events = screen._events
        operation = next(
            item for item in events if item.event_id == "message-operation:" + "a" * 32
        )
        assert "1 of 3 completed" in operation.summary and "1 needs checking" in operation.summary
        assert all(
            "private-other" not in item.detail and "b" * 32 not in item.event_id for item in events
        )
        screen._select_event(operation.event_id)
        await pilot.pause()
        actions = screen.query(
            "#activity-watcher-actions Button, #activity-actions Button, "
            "#activity-footer-actions Button"
        )
        for action in actions:
            if action.display:
                assert screen.region.contains_region(action.region), (action.id, action.region)
                assert action.content_size.height >= 1
        inspect = screen.query_one("#activity-operation-details", Button)
        assert (
            not inspect.disabled and screen.query_one("#activity-retry-delivery", Button).disabled
        )
        assert await pilot.click(inspect)
        await wait(
            pilot,
            lambda: (
                isinstance(app.screen, AreaScreen)
                and "Uncertain: two" in str(app.screen.query_one("#area-content", Static).content)
            ),
        )
        text = str(app.screen.query_one("#area-content", Static).content)
        assert "Completed: one → copied-one" in text and "Not started: three" in text
        assert context.state.get_message_operation("a" * 32)["outcomes"][1]["status"] == "uncertain"
        assert context._gateway is None
        app.screen.action_close()
        await pilot.pause()
        assert app.screen is screen and screen._selected_event_id == operation.event_id


def test_clear_activity_preserves_operation_recovery_records(tmp_path):
    context = context_for(tmp_path)
    context.state.begin_message_operation("a" * 32, "copy", "selection", ("one",))
    backend = ActivityAreaBackend.from_context(context, offline=True)
    plan = backend.plan_clear_activity(cancel=Event(), progress=lambda value: None)
    backend.execute_clear_activity(
        plan, confirmed=True, cancel=Event(), progress=lambda value: None
    )
    assert context.state.get_message_operation("a" * 32) is not None
