"""Actual local startup, confirmation, pending status and durable restore contracts."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event

import pytest


async def restore_workflow(root, *, capture=None, size=(80, 24), theme="builtin.default-dark"):
    from qa.synthetic.mailbox import LocalMailbox
    from qa.synthetic.startup import startup_context
    from textual.widgets import Button, DataTable, Static

    from gucmail.commands import CommandInvocation
    from gucmail.models import Address, FolderInfo, MessageSummary
    from gucmail.services.messages import MessageService
    from gucmail.settings import SettingsService
    from gucmail.state import StateStore
    from gucmail.tui.app import ConfirmationScreen, GUCmailApp
    from gucmail.tui.palette_routes import catalog_route
    from gucmail.tui.write_coordinator import (
        PENDING_CHANGE_MESSAGE,
        WriteStateChanged,
        app_write_coordinator,
    )

    context = startup_context(root / "profile")
    context.config = replace(context.config, theme=theme)
    context.config_store.save(context.config)
    SettingsService(context.config_store).set("mail.default_folder", "/Archive")
    gateway = LocalMailbox(root / "mailbox.sqlite")
    gateway.insert_many(
        "folder",
        [
            FolderInfo("f-inbox", "Inbox", "/Inbox", None, 0, 0),
            FolderInfo("f-archive", "Archive", "/Archive", None, 1, 1),
        ],
    )
    reference = "synthetic-held-restore"
    gateway.insert_many(
        "mail",
        [
            MessageSummary(
                reference=reference,
                subject="Synthetic [literal] restore 学生",
                sender=Address("sender@example.test", "Synthetic sender"),
                received_at=datetime(2026, 11, 9, 8, tzinfo=UTC),
                is_read=False,
                folder_path="/Archive",
            )
        ],
    )
    context._gateway = gateway
    started, release, returned = Event(), Event(), Event()
    execute = MessageService.execute_mutation
    patch = pytest.MonkeyPatch()

    def held(service, plan, **kwargs):
        assert plan.operation == "move" and tuple(plan.targets) == (reference,)
        started.set()
        if not release.wait(20):
            raise TimeoutError("Synthetic pre-dispatch mutation hold expired")
        try:
            return execute(service, plan, **kwargs)
        finally:
            returned.set()

    patch.setattr(MessageService, "execute_mutation", held)
    app = GUCmailApp(context, gateway=gateway)
    list_messages = gateway.list_messages
    folders_queried = []

    def observed_list(*args, **kwargs):
        folders_queried.append(kwargs.get("folder", args[0] if args else "inbox"))
        return list_messages(*args, **kwargs)

    gateway.list_messages = observed_list
    phases = []
    try:
        async with app.run_test(size=size) as pilot:
            table = app.query_one("#message-table", DataTable)
            try:
                async with asyncio.timeout(15):
                    while table.row_count != 1 or app.ui.busy:
                        await asyncio.sleep(0.005)
            except TimeoutError:
                diagnostic = {
                    "rows": table.row_count,
                    "busy": app.ui.busy,
                    "ui_folder": getattr(app.ui, "folder", None),
                    "status": app._status_activity,
                    "queries": folders_queried,
                    "provider_counts": dict(gateway.counts),
                    "archive_headers": gateway.count("mail", folder="/Archive"),
                }
                (root / "startup-failure.json").write_text(
                    json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8"
                )
                if capture:
                    capture("startup-failure", app)
                raise
            await pilot.pause()
            if capture:
                capture("configured-default", app)
            app.ui.selected_reference = reference
            app._palette_handlers()["mail.restore"](
                CommandInvocation("mail.restore"), app._palette_context()
            )
            async with asyncio.timeout(15):
                while not (
                    isinstance(app.screen, ConfirmationScreen)
                    and app.screen.query("#confirm")
                    and app.screen.query_one("#confirm", Button).is_mounted
                ):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert reference in app.screen.detail and "inbox" in app.screen.detail
            if capture:
                capture("exact-confirmation", app)
            await pilot.press("y")
            async with asyncio.timeout(15):
                while not started.is_set():
                    await asyncio.sleep(0.005)
            # A request may legitimately remain in flight beyond a 30x50 ms
            # fixture poll window. No effect is claimed before real completion.
            await asyncio.sleep(2.0)
            assert app_write_coordinator(app).pending
            assert gateway.counts["move_message"] == 0
            assert gateway.get("mail", reference).folder_path == "/Archive"
            await pilot.press("tab")
            assert app.focused is not None
            status = str(app.query_one("#status-bar", Static).render())
            assert "Change in progress" in status
            phases.append(
                {
                    "phase": "held-before-dispatch",
                    "pending": True,
                    "move_calls": 0,
                    "source_folder": "/Archive",
                    "visible_status": status,
                }
            )
            if capture:
                capture("held-before-dispatch", app)
            app._catalog_direct(
                catalog_route("mail.set_importance"),
                CommandInvocation.from_mapping("mail.set_importance", {"importance": "high"}),
            )
            async with asyncio.timeout(15):
                while not app._status_activity.startswith(PENDING_CHANGE_MESSAGE):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert gateway.counts["importance"] == 0
            assert "No new action" in str(app.query_one("#status-bar", Static).render())
            if capture:
                capture("overlap-rejected", app)
            release.set()
            async with asyncio.timeout(15):
                while not (
                    returned.is_set()
                    and gateway.counts["move_message"] == 1
                    and not app_write_coordinator(app).pending
                ):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            app.post_message(WriteStateChanged())
            await pilot.pause()
            assert "Change in progress" not in str(app.query_one("#status-bar", Static).render())
            assert gateway.get("mail", reference).folder_path == "/Inbox"
            operations = context.state.list_message_operations(limit=2)
            assert len(operations) == 1
            record = context.state.get_message_operation(operations[0]["operation_id"])
            assert record["outcomes"][0]["status"] == "completed"
            assert record["outcomes"][0]["reference"] == reference
            phases.append(
                {
                    "phase": "completed",
                    "pending": False,
                    "move_calls": 1,
                    "destination_folder": "/Inbox",
                    "durable_status": "completed",
                }
            )
            if capture:
                capture("completed", app)
        state_path = context.paths.state_database
        account = context.state.account_key
    finally:
        release.set()
        patch.undo()
        context.close()
    reopened = LocalMailbox(root / "mailbox.sqlite")
    assert reopened.get("mail", reference).folder_path == "/Inbox"
    assert reopened.count("mail", folder="/Archive") == 0
    assert reopened.count("mail", folder="/Inbox") == 1
    fresh_state = StateStore(state_path, account_key=account)
    assert (
        fresh_state.get_message_operation(operations[0]["operation_id"])["outcomes"][0]["status"]
        == "completed"
    )
    reopened.close()
    return {
        "phases": phases,
        "restart_readback": True,
        "actual_local_services": True,
        "command_entry": "Catalog restore handler plus actual keyboard confirmation",
        "selection_setup": "Explicit synthetic reference; not a full folder-navigation acceptance",
        "live_verified": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size,theme",
    [
        ((80, 24), "builtin.default-dark"),
        ((120, 36), "builtin.default-light"),
        ((70, 20), "builtin.high-contrast-dark"),
    ],
)
async def test_confirmed_restore_stays_pending_until_real_effect_and_restart_readback(
    tmp_path, size, theme
):
    result = await restore_workflow(tmp_path, size=size, theme=theme)
    assert result["restart_readback"]
    assert result["phases"][0]["move_calls"] == 0
    assert result["phases"][1]["durable_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved,legacy,expected",
    [
        (None, None, "/Archive"),
        ("inbox", None, "inbox"),
        (None, "/Legacy", "/Legacy"),
        ("inbox", "/Legacy", "inbox"),
    ],
    ids=["fresh-default", "remembered", "legacy", "remembered-precedes-legacy"],
)
async def test_initial_folder_uses_configured_default_and_preserves_remembered_state(
    tmp_path, saved, legacy, expected
):
    from qa.synthetic.mailbox import LocalMailbox
    from qa.synthetic.startup import startup_context
    from textual.widgets import DataTable

    from gucmail.models import Address, FolderInfo, MessageSummary
    from gucmail.settings import SettingsService
    from gucmail.tui.app import GUCmailApp

    context = startup_context(tmp_path / "profile")
    service = SettingsService(context.config_store)
    service.set("mail.default_folder", "/Archive")
    if saved is not None:
        service.set("mail.last_folder", saved)
    if legacy is not None:
        context.state.initialize()
        with context.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?)",
                ("ui:mail.last_folder", json.dumps(legacy)),
            )
    gateway = LocalMailbox(tmp_path / "mailbox.sqlite")
    folders = ("Inbox", "Archive", "Legacy")
    gateway.insert_many("folder", [FolderInfo("f-" + f, f, "/" + f, None, 1, 1) for f in folders])
    gateway.insert_many(
        "mail",
        [
            MessageSummary(
                reference="message-" + f,
                subject="Synthetic " + f,
                sender=Address("sender@example.test", "Synthetic sender"),
                received_at=datetime(2026, 11, 9, 8, tzinfo=UTC),
                is_read=False,
                folder_path="/" + f,
            )
            for f in folders
        ],
    )
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    try:
        assert app.ui.folder == expected
        async with app.run_test(size=(80, 24)) as pilot:
            table = app.query_one("#message-table", DataTable)
            async with asyncio.timeout(15):
                while table.row_count != 1 or app.ui.busy:
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert app.ui.folder == expected
            assert len(app._message_page_rows) == 1
            assert app._message_page_rows[0].folder_path == gateway.folder_path(expected)
    finally:
        context.close()
