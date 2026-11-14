"""Local deletion plans and real Settings consumers over bounded synthetic mail."""

import json
from dataclasses import replace
from threading import Event

import pytest
from rich.text import Text
from textual.widgets import Button, DataTable, Input, Select, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError, ConfirmationRequiredError
from gucmail.models import Address, MessageSummary
from gucmail.services.mail_preferences import (
    DeleteBehavior,
    MailDeletionPreferences,
    conversation_page_positions,
    group_message_page,
)
from gucmail.services.messages import MessageService
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import ConfirmationScreen, GUCmailApp, _AutomaticReadRequest
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_mail_preferences import SortGateway
from test_scheduler import NOW
from test_settings_secondpass import _context

VALUES = {
    "mail.delete_behavior": "move_to_folder",
    "mail.delete_folder": "inbox/Reviewed synthetic mail",
    "mail.group_conversations": True,
}


def settings_for(context, **kwargs):
    return SettingsService(
        context.config_store,
        context=SettingsContext(account_id=context.config.email),
        **kwargs,
    )


def configure(context):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for identifier, value in VALUES.items():
        session.set(identifier, value)
    session.apply()
    return service


def rows():
    return tuple(
        MessageSummary(
            reference,
            subject,
            Address("sender@example.test"),
            NOW,
            conversation_id=conversation,
            is_read=False,
        )
        for reference, subject, conversation in (
            ("first", "Shared topic", "exact-A"),
            ("other", "Shared topic", "exact-B"),
            ("last", "[bold]Hostile[/bold]\x1b[31m subject", "exact-A"),
            ("unknown", "Shared topic", None),
        )
    )


@pytest.mark.asyncio
async def test_rendered_page_selection_is_ready_before_queued_highlights(tmp_path):
    context = _context(tmp_path)
    configure(context)
    app = GUCmailApp(context, offline=True)
    async with app.run_test() as pilot:
        app._render_messages(rows())
        table = app.query_one("#message-table", DataTable)
        assert app.ui.selected_reference == "first"
        table.move_cursor(row=1)
        await wait_for(pilot, lambda: app.ui.selected_reference == "last")
        assert app.ui.selected_reference == "last"
        app._selected_message_references = {"last"}
        app._render_messages((rows()[1], rows()[3]))
        # Commands invoked immediately after a refresh must target the visible
        # cursor rather than the message removed from the new server page.
        assert app.ui.selected_reference == "other"
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == "other"
        assert not app._selected_message_references
        await pilot.pause()
        assert app.ui.selected_reference == "other"
        app._render_messages(())
        assert app.ui.selected_reference is None and table.row_count == 0
        await pilot.pause()
        assert app.ui.selected_reference is None


class MailGateway(SortGateway):
    def __init__(self):
        super().__init__()
        self.rows = rows()
        self.mutations = []
        self.fail_move = False

    def move_message(self, reference, destination):
        self.mutations.append(("move", reference, destination))
        if self.fail_move:
            raise ConfigurationError("Synthetic destination is unavailable; no move occurred.")
        return reference

    def delete_message(self, reference, *, permanent=False):
        self.mutations.append(("delete", reference, permanent))
        return reference


def test_deletion_grouping_stage_cancel_transfer_reset_undo_reopen_account_scope(tmp_path):
    context = _context(tmp_path)
    service = configure(context)
    preference = MailDeletionPreferences.from_settings(service)
    assert preference.destination_for() == VALUES["mail.delete_folder"]
    before = context.paths.config_file.read_bytes()
    session = service.begin()
    session.set("mail.delete_behavior", "deleted_items")
    session.set("mail.group_conversations", False)
    session.cancel()
    assert context.paths.config_file.read_bytes() == before
    restarted = settings_for(context)
    assert MailDeletionPreferences.from_settings(restarted) == preference
    assert restarted.value("mail.group_conversations").effective_value is True
    target = settings_for(_context(tmp_path / "other"))
    target.apply_import(target.plan_import(service.export(setting_ids=tuple(VALUES))))
    assert MailDeletionPreferences.from_settings(target) == preference
    assert target.value("mail.group_conversations").effective_value is True
    service.reset("mail.delete_behavior")
    assert MailDeletionPreferences.from_settings(service).destination_for() is None
    service.undo()
    assert MailDeletionPreferences.from_settings(service) == preference
    service.reset("mail.group_conversations")
    assert service.value("mail.group_conversations").effective_value is False
    service.undo()
    assert service.value("mail.group_conversations").effective_value is True
    first = SettingsService(context.config_store, context=SettingsContext(account_id="A"))
    first.set("mail.delete_behavior", "deleted_items")
    first.set("mail.group_conversations", False)
    second = SettingsService(context.config_store, context=SettingsContext(account_id="B"))
    assert MailDeletionPreferences.from_settings(first).destination_for() is None
    assert MailDeletionPreferences.from_settings(second) == MailDeletionPreferences()
    assert second.value("mail.group_conversations").effective_value is False
    assert MailDeletionPreferences.from_settings(service) == preference
    assert GUCmailApp(context, offline=True)._group_conversations


@pytest.mark.parametrize(
    "identifier,value",
    [
        ("mail.delete_behavior", "permanent"),
        ("mail.delete_behavior", True),
        ("mail.delete_folder", "inbox/evil\nfolder"),
        ("mail.delete_folder", "x" * 4097),
        ("mail.delete_folder", True),
        ("mail.group_conversations", "maybe"),
        ("mail.group_conversations", 1),
    ],
)
def test_invalid_deletion_grouping_preferences_preserve_config(tmp_path, identifier, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(identifier, value)
    assert context.paths.config_file.read_bytes() == before


def test_delete_destination_validation_and_exact_confirmation_before_any_gateway():
    assert MailDeletionPreferences().destination_for() is None
    preference = MailDeletionPreferences(DeleteBehavior.MOVE_TO_FOLDER, "inbox/Chosen")
    assert preference.destination_for(behavior=DeleteBehavior.DELETED_ITEMS) is None
    assert preference.destination_for(folder="inbox/Override") == "inbox/Override"
    with pytest.raises(ConfigurationError):
        preference.destination_for(behavior=DeleteBehavior.DELETED_ITEMS, folder="inbox/Conflict")
    with pytest.raises(ConfigurationError, match="Choose a Delete destination"):
        MailDeletionPreferences(DeleteBehavior.MOVE_TO_FOLDER).destination_for()
    service = MessageService(object())
    plan = service.plan_delete(("first", "first", "last"), destination="inbox/Chosen")
    assert plan.targets == ("first", "last") and plan.operation == "move"
    assert plan.requires_confirmation
    assert not service.execute_mutation(plan, dry_run=True).executed
    other = service.plan_delete(plan.targets, destination="inbox/Other")
    with pytest.raises(ConfirmationRequiredError):
        service.execute_mutation(other, confirmation_scope=plan.confirmation_scope)
    with pytest.raises(ConfigurationError):
        service.plan_delete(("first",), permanent=True, destination="inbox/Chosen")


@pytest.mark.parametrize(
    "options,operation,arguments",
    [
        ([], "move", {"destination": VALUES["mail.delete_folder"]}),
        (["--behavior", "deleted_items"], "delete", {"permanent": False}),
        (["--to", "inbox/Override"], "move", {"destination": "inbox/Override"}),
        (["--permanent"], "delete", {"permanent": True}),
    ],
)
def test_cli_delete_saved_destination_override_and_permanent_dry_run_stay_local(
    tmp_path, monkeypatch, options, operation, arguments
):
    configure(_context(tmp_path))
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Preview must not resolve credentials")
    )
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "messages", "delete", "first", *options, "--dry-run"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["dry_run"] and not data["executed"]
    assert data["plan"]["operation"] == operation
    assert dict(data["plan"]["arguments"]) == arguments
    assert data["plan"]["targets"] == ["first"]


@pytest.mark.parametrize(
    "options",
    [
        ["--permanent", "--to", "inbox/Other"],
        ["--permanent", "--behavior", "deleted_items"],
        ["--behavior", "deleted_items", "--to", "inbox/Other"],
    ],
)
def test_cli_invalid_delete_overrides_fail_before_gateway(tmp_path, monkeypatch, options):
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Invalid request must stay local")
    )
    result = CliRunner().invoke(
        cli.app,
        ["messages", "delete", "first", "--dry-run", *options],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 3, result.output


def test_cli_missing_destination_and_unconfirmed_move_fail_local_then_exact_approved_move(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = settings_for(context)
    service.set("mail.delete_behavior", "move_to_folder")
    monkeypatch.setattr(cli.core, "gateway", lambda *_: pytest.fail("Invalid plan must stay local"))
    command = ["--json", "--non-interactive", "messages", "delete", "first"]
    runner = CliRunner()
    missing = runner.invoke(cli.app, [*command, "--dry-run"], env=_environment(tmp_path))
    assert missing.exit_code == 3 and "destination" in missing.output
    service.set("mail.delete_folder", "inbox/Chosen")
    unconfirmed = runner.invoke(cli.app, command, env=_environment(tmp_path))
    assert unconfirmed.exit_code != 0 and "--yes" in unconfirmed.output
    gateway = MailGateway()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    approved = runner.invoke(cli.app, ["--yes", *command], env=_environment(tmp_path))
    assert approved.exit_code == 0, approved.output
    assert gateway.mutations == [("move", "first", "inbox/Chosen")]


def test_grouping_preserves_real_rows_exact_case_and_unknowns_without_subject_guessing():
    source = rows()
    grouped = group_message_page(source, enabled=True)
    assert [m.reference for m in grouped] == ["first", "last", "other", "unknown"]
    assert grouped[0] is source[0] and grouped[1] is source[2]
    assert conversation_page_positions(grouped) == ((1, 2), (2, 2), None, None)
    assert group_message_page(source, enabled=False) == source
    ambiguous = (
        {"reference": "a", "conversation_id": "ID", "subject": "Same"},
        {"reference": "b", "conversation_id": "id", "subject": "Same"},
        {"reference": "c", "conversation_id": None, "subject": "Same"},
        {"reference": "d", "conversation_id": "", "subject": "Same"},
        {"reference": "e", "conversation_id": 42, "subject": "Same"},
        {"reference": "f", "conversation_id": "   ", "subject": "Same"},
        {"reference": "g", "conversation_id": "ID", "subject": "Different"},
    )
    result = group_message_page(ambiguous, enabled=True)
    assert [m["reference"] for m in result] == ["a", "g", "b", "c", "d", "e", "f"]
    assert conversation_page_positions(result) == ((1, 2), (2, 2), None, None, None, None, None)
    assert all(any(item is original for original in ambiguous) for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_cancel_apply_grouping_keeps_selection_and_delete_review(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    context.config = replace(context.config, email="student@example.test")
    gateway = MailGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app._render_messages(gateway.rows)
        app.ui.selected_reference = "last"
        app._selected_message_references = {"first", "last"}
        app._render_message_rows(app._message_page_rows)
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen

        async def activate(identifier):
            button = screen.query_one(identifier, Button)
            await wait_for(pilot, lambda: not button.has_class("-active") and not button.disabled)
            button.focus()
            await pilot.pause()
            await pilot.press("enter")

        async def stage():
            for identifier, value in VALUES.items():
                assert screen.action_select_setting(identifier)
                await pilot.pause()
                if identifier == "mail.delete_folder":
                    screen.query_one("#settings-editor", Input).value = value
                else:
                    screen.query_one("#settings-choice", Select).value = (
                        "on" if value is True else value
                    )
                await activate("#settings-stage")
                await wait_for(pilot, lambda identifier=identifier: identifier in screen._staged)

        await stage()
        assert not app._group_conversations and not gateway.calls and not gateway.mutations
        assert context.paths.config_file.read_bytes() == before
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged and not screen.mutation_pending)
        assert context.paths.config_file.read_bytes() == before
        await stage()
        await activate("#settings-apply")
        await wait_for(pilot, lambda: app._group_conversations and not screen.mutation_pending)
        assert not gateway.calls and not gateway.mutations
        await pilot.press("escape")
        await pilot.pause()
        table = app.query_one("#message-table", DataTable)
        assert [r.value for r in table.rows] == ["first", "last", "other", "unknown"]
        assert app.ui.selected_reference == "last"
        assert app._selected_message_references == {"first", "last"}
        subject = table.get_cell("last", "subject")
        assert isinstance(subject, Text)
        assert subject.plain == "[2/2] [bold]Hostile[/bold] subject"
        assert "grouped in this page" in str(app.query_one("#message-heading", Static).render())
        table.focus()
        await pilot.press("d")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert VALUES["mail.delete_folder"] in app.screen.detail
        assert "last" in app.screen.detail and "exactly 1" in app.screen.detail
        for identifier in ("#confirm", "#cancel"):
            button = app.screen.query_one(identifier, Button)
            assert button.region.y >= 0 and button.region.bottom <= size[1]
        await pilot.press("escape")
        await pilot.pause()
        assert not gateway.mutations
        assert app.ui.selected_reference == "last" and all(not m.is_read for m in app._messages)
        settings_for(context).set("mail.group_conversations", False)
        app._apply_committed_settings(("mail.group_conversations",))
        assert [r.value for r in table.rows] == ["first", "other", "last", "unknown"]
        assert app.ui.selected_reference == "last"
        assert app._selected_message_references == {"first", "last"}
        assert not gateway.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["deleted_items", "move_to_folder"])
async def test_confirmed_delete_captures_target_and_destination_despite_later_changes(
    tmp_path, monkeypatch, behavior
):
    context = _context(tmp_path)
    service = configure(context)
    service.set("mail.delete_behavior", behavior)
    gateway = MailGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    monkeypatch.setattr(app, "refresh_mailbox", lambda: None)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.query_one("#message-table", DataTable).move_cursor(row=1)
        await pilot.pause()
        assert app.ui.selected_reference == "last"
        app.action_delete()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        service.set("mail.delete_behavior", "move_to_folder")
        service.set("mail.delete_folder", "inbox/Changed after review")
        app.ui.selected_reference = "other"
        app.screen.query_one("#confirm", Button).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: bool(gateway.mutations))
        expected = (
            ("delete", "last", False)
            if behavior == "deleted_items"
            else ("move", "last", VALUES["mail.delete_folder"])
        )
        assert gateway.mutations == [expected]


@pytest.mark.asyncio
async def test_missing_delete_destination_and_failed_move_preserve_ui_without_success(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = settings_for(context)
    service.set("mail.delete_behavior", "move_to_folder")
    gateway = MailGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.query_one("#message-table", DataTable).move_cursor(row=2)
        await pilot.pause()
        assert app.ui.selected_reference == "last"
        app.action_delete()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmationScreen)
        assert "destination" in app._status_activity and not gateway.mutations
        service.set("mail.delete_folder", VALUES["mail.delete_folder"])
        gateway.fail_move = True
        app.action_delete()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        app.screen.query_one("#confirm", Button).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: "uncertain" in app._status_activity)
        assert "Message moved" not in app._status_activity
        assert app.ui.selected_reference == "last" and app._messages == gateway.rows
        assert len(gateway.mutations) == 1 and not gateway.calls


@pytest.mark.asyncio
async def test_grouped_refresh_pagination_and_filters_use_original_server_page(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    configure(context)
    gateway = MailGateway()
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: len(app._visible_messages) == 4 and not app.ui.busy)
        assert gateway.calls[-1] == ("inbox", 25, 0, "-datetime_received")
        assert [m.reference for m in app._message_page_rows] == [m.reference for m in gateway.rows]
        assert [m.reference for m in app._visible_messages] == ["first", "last", "other", "unknown"]
        app.query_one("#message-table", DataTable).move_cursor(row=1)
        # RowHighlighted is queued separately from cursor movement. A generic
        # screen pause may finish before the application consumes that message.
        await wait_for(pilot, lambda: app.ui.selected_reference == "last")
        assert app.ui.selected_reference == "last"
        app._selected_message_references = {"first", "last"}
        app.refresh_mailbox()
        await wait_for(pilot, lambda: len(gateway.calls) == 2 and not app.ui.busy)
        assert app.ui.selected_reference == "last"
        assert app._selected_message_references == {"first", "last"}
        app.query_one("#message-filter", Input).value = "Hostile"
        await pilot.pause()
        assert [m.reference for m in app._visible_messages] == ["last"]
        assert app._selected_message_references == {"last"}
        assert not gateway.mutations and len(gateway.calls) == 2
        app.query_one("#message-filter", Input).value = ""
        await pilot.pause()
        settings_for(context).set("mail.group_conversations", False)
        app._apply_committed_settings(("mail.group_conversations",))
        assert app._visible_messages == gateway.rows
        page = MessageService(gateway).list_messages(folder="inbox", limit=2, offset=1)
        assert gateway.calls[-1] == ("inbox", 2, 1, "-datetime_received")
        assert len(group_message_page(page.items, enabled=True)) == 2
        assert all(not m.is_read for m in app._messages)


@pytest.mark.asyncio
async def test_grouped_read_acknowledgement_survives_regrouping_and_restored_page_order(tmp_path):
    context = _context(tmp_path)
    service = configure(context)
    app = GUCmailApp(context, offline=True)
    async with app.run_test() as pilot:
        source = rows()
        app._render_messages(source)
        await pilot.pause()
        request = _AutomaticReadRequest(1, "last", app.query_one("#preview"), 0, Event())
        app._finish_automatic_read(request, True, None)
        service.set("mail.group_conversations", False)
        app._apply_committed_settings(("mail.group_conversations",))
        assert [m.reference for m in app._visible_messages] == [m.reference for m in source]
        assert [m.reference for m in app._visible_messages if m.is_read] == ["last"]
        service.set("mail.group_conversations", True)
        app._apply_committed_settings(("mail.group_conversations",))
        assert [m.reference for m in app._visible_messages if m.is_read] == ["last"]
        assert all(not m.is_read for m in source)
