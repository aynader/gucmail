"""PIM submissions retain user input until acknowledged, including failed writes."""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from qa.synthetic.mailbox import LocalMailbox
from textual.widgets import Button, Input, Static

from gucmail.errors import ConfigurationError, ErrorContext, NetworkError
from gucmail.models import Address, CalendarEvent, ContactInfo, TaskInfo
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ViewState
from gucmail.tui.pim_screens import (
    CalendarEditorScreen,
    ContactEditorScreen,
    TaskEditorScreen,
)
from test_tui import context_for


async def until(pilot, predicate):
    async with asyncio.timeout(15):
        while not predicate():
            # Await the operation's observable state before asking Pilot to drain
            # the screen. Continuous progress/redraw messages can prevent that
            # drain while the worker is still completing.
            await asyncio.sleep(0.005)
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["contact", "task", "calendar"])
async def test_local_validation_error_clears_after_input_change(tmp_path, area):
    app = GUCmailApp(context_for(tmp_path), offline=True)
    editor = (
        ContactEditorScreen(None)
        if area == "contact"
        else TaskEditorScreen(TaskInfo("tsk-new", "", "not_started"), creating=True)
        if area == "task"
        else CalendarEditorScreen(None, timezone="UTC", creating=True)
    )
    field = "name" if area == "contact" else "subject"
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(editor)
        await pilot.pause()
        widget = editor.query_one(f"#{area}-edit-{field}", Input)
        widget.value = ""
        await pilot.press("ctrl+s")
        status = editor.query_one(f"#{area}-editor-error", Static)
        assert str(status.content)
        widget.value = "Corrected input"
        await pilot.pause()
        assert str(status.content) == ""
        # Editing after an uncertain write must retain its warning and block
        # shortcuts, including when input becomes locally invalid again.
        editor.submission_failed("Synthetic acknowledgment lost", uncertain=True)
        widget.value = ""
        await pilot.press("ctrl+s")
        assert "may have completed" in str(status.content)
        assert app.screen is editor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "area,kind,prefix,field,editor_type",
    [
        ("contacts", "contact", "contact", "name", ContactEditorScreen),
        ("tasks", "task", "task", "subject", TaskEditorScreen),
        ("calendar", "event", "calendar", "subject", CalendarEditorScreen),
    ],
)
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_failed_pim_save_retains_form_and_blocks_uncertain_replay(
    tmp_path, monkeypatch, area, kind, prefix, field, editor_type, lost_ack
):
    context = context_for(tmp_path / "profile")
    gateway = LocalMailbox(tmp_path / "mailbox.sqlite")
    start = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    value = {
        "contact": ContactInfo(
            "ctc-recovery", "Original contact", emails=(Address("a@example.test"),)
        ),
        "task": TaskInfo("tsk-recovery", "Original task", "not_started"),
        "event": CalendarEvent(
            "evt-recovery", "Original event", start, start + timedelta(hours=1), "UTC"
        ),
    }[kind]
    gateway.store(kind, value)
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(120, 36)) as pilot:
        await until(pilot, lambda: not app.ui.busy)
        await pilot.click("#workspace-" + area)
        screen = app.screen
        await until(pilot, lambda: not screen._operation_active and screen._highlighted is not None)
        await pilot.click("#" + area + "-edit")
        await until(pilot, lambda: isinstance(app.screen, editor_type))
        editor = app.screen
        selector = "#" + prefix + "-edit-" + field
        revised = "Revised [bold] input retained"
        editor.query_one(selector, Input).value = revised
        field_widget = editor.query_one(selector, Input)
        field_widget.focus()
        await until(pilot, lambda: field_widget.has_focus)
        field_widget.cursor_position = 7
        action = "update_" + kind
        if lost_ack:
            original = getattr(gateway, action)

            def committed_without_reply(plan):
                original(plan)  # Perform the actual SQL write before losing the reply.
                raise NetworkError(
                    "Synthetic acknowledgment lost",
                    context=ErrorContext(mutation_uncertain=True, retry_safe=False),
                )

            monkeypatch.setattr(gateway, action, committed_without_reply)
        else:
            gateway.faults[action] = {"error": ConfigurationError("Synthetic rejected write")}
        await pilot.press("ctrl+s")
        await until(pilot, lambda: gateway.counts[action] == 1 and not screen._operation_active)
        assert app.screen is editor, "A failed save must retain its editor and the user's input"
        assert editor.query_one(selector, Input).value == revised
        assert field_widget.has_focus and field_widget.cursor_position == 7
        error = editor.query_one("#" + prefix + "-editor-error", Static)
        assert "retained" in str(error.content).casefold()
        save = editor.query_one("#" + prefix + "-edit-save", Button)
        assert save.disabled is lost_ack
        reopened = LocalMailbox(gateway.path)
        persisted = reopened.get(kind, value.reference)
        original_text = value.display_name if kind == "contact" else value.subject
        assert (persisted.display_name if kind == "contact" else persisted.subject) == (
            revised if lost_ack else original_text
        )
        reopened.close()
        if lost_ack:
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert gateway.counts[action] == 1, "Uncertain saves must not be replayed"
            await pilot.press("escape")
        else:
            gateway.faults.clear()
            await pilot.press("ctrl+s")
            await until(pilot, lambda: app.screen is screen and not screen._operation_active)
            assert gateway.counts[action] == 2
            persisted = gateway.get(kind, value.reference)
            assert (persisted.display_name if kind == "contact" else persisted.subject) == revised
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True])
@pytest.mark.parametrize(
    "area,kind,prefix,field,read_method",
    [
        ("contacts", "contact", "contact", "name", "search_contacts_page"),
        ("tasks", "task", "task", "subject", "search_tasks_page"),
        ("calendar", "event", "calendar", "subject", "list_events"),
    ],
)
async def test_create_keeps_editor_while_pending_and_waits_for_acknowledged_selection(
    tmp_path, monkeypatch, area, kind, prefix, field, read_method, overlay
):
    context = context_for(tmp_path / "profile")
    gateway = LocalMailbox(tmp_path / "mailbox.sqlite")
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    dispatched, allow_write, reading, allow_read = (Event() for _ in range(4))
    create = getattr(gateway, "create_" + kind)
    read = getattr(gateway, read_method)

    def delayed_create(plan):
        dispatched.set()
        assert allow_write.wait(10)
        return create(plan)

    def delayed_page(*args, **kwargs):
        if gateway.count(kind):
            reading.set()
            assert allow_read.wait(10)
        return read(*args, **kwargs)

    monkeypatch.setattr(gateway, "create_" + kind, delayed_create)
    monkeypatch.setattr(gateway, read_method, delayed_page)
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: not app.ui.busy)
            await pilot.click("#workspace-" + area)
            screen = app.screen
            await until(pilot, lambda: screen.view_state is ViewState.EMPTY)
            await pilot.click("#" + area + "-create")
            editor = app.screen
            editor.query_one("#" + prefix + "-edit-" + field, Input).value = "New synthetic record"
            await pilot.press("ctrl+s")
            await until(pilot, dispatched.is_set)
            assert app.screen is editor and editor._save_pending
            await pilot.press("ctrl+s", "escape")
            assert app.screen is editor and gateway.count(kind) == 0
            if overlay:
                app.action_palette()
                await until(pilot, lambda: isinstance(app.screen, PaletteScreen))
            allow_write.set()
            if overlay:
                await until(pilot, reading.is_set)
                assert isinstance(app.screen, PaletteScreen)
                await pilot.press("escape")
                await until(pilot, lambda: app.screen is screen)
            else:
                await until(pilot, lambda: reading.is_set() and app.screen is screen)
            assert gateway.count(kind) == 1
            assert screen._targets() == ()
            assert screen.query_one("#" + area + "-edit", Button).disabled
            allow_read.set()
            await until(pilot, lambda: not screen._operation_active)
            assert len(screen._targets()) == 1
            assert gateway.counts["create_" + kind] == 1
    finally:
        allow_write.set()
        allow_read.set()
        context.close()
