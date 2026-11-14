"""Saved message order at shared CLI/TUI boundaries, never live mailbox tests."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import Button, DataTable, Select
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError
from gucmail.models import Address, MessageDetail, MessageSummary
from gucmail.services.mail_preferences import MailSortPreferences
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ViewState
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_settings_secondpass import _context
from test_tui import FakeGateway


class SortGateway(FakeGateway):
    def __init__(self):
        self.calls = []
        now = datetime(2026, 10, 2, tzinfo=UTC)
        self.rows = (
            MessageSummary("first", "Alpha", Address("one@example.test"), now, size=500),
            MessageSummary(
                "second", "Beta", Address("two@example.test"), now + timedelta(days=1), size=200
            ),
        )

    def list_messages(self, *, folder, limit, offset=0, order_by="-datetime_received"):
        self.calls.append((folder, limit, offset, order_by))
        field = {"datetime_received": "received_at", "datetime_sent": "sent_at"}.get(
            order_by.lstrip("-"), order_by.lstrip("-")
        )
        values = sorted(
            self.rows, key=lambda m: getattr(m, field), reverse=order_by.startswith("-")
        )
        return values[offset : offset + limit]

    def show_message(self, reference, *, mark_read=False):
        assert not mark_read
        return MessageDetail(next(m for m in self.rows if m.reference == reference), body_text="")


def test_sort_preferences_transfer_reset_undo_restart_and_account_isolation(tmp_path):
    context = _context(tmp_path)
    service = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    assert MailSortPreferences.from_settings(service).order_by == "-datetime_received"
    session = service.begin()
    session.set("mail.sort_field", "size")
    session.set("mail.sort_direction", "ascending")
    session.apply()
    assert (
        MailSortPreferences.from_settings(SettingsService(context.config_store)).order_by == "size"
    )
    exported = service.export()
    target = SettingsService(_context(tmp_path / "target").config_store)
    target.apply_import(target.plan_import(exported))
    assert MailSortPreferences.from_settings(target).order_by == "size"
    service.reset("mail.sort_direction")
    assert MailSortPreferences.from_settings(service).order_by == "-size"
    service.undo()
    assert MailSortPreferences.from_settings(service).order_by == "size"
    first = SettingsService(
        context.config_store, context=SettingsContext(account_id="first-account")
    )
    first.set("mail.sort_field", "subject")
    second = SettingsService(
        context.config_store, context=SettingsContext(account_id="second-account")
    )
    assert MailSortPreferences.from_settings(first).order_by == "subject"
    assert MailSortPreferences.from_settings(second).order_by == "size"
    restarted = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert restarted._mail_sort_order == "size"


@pytest.mark.parametrize(
    "identifier,value",
    [("mail.sort_field", "body"), ("mail.sort_direction", "reverse"), ("mail.sort_field", True)],
)
def test_invalid_sort_preferences_preserve_configuration(tmp_path, identifier, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set(identifier, value)
    assert context.paths.config_file.read_bytes() == before


def test_cli_saved_order_explicit_override_offset_and_invalid_order_before_gateway(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    session = SettingsService(context.config_store).begin()
    session.set("mail.sort_field", "subject")
    session.set("mail.sort_direction", "ascending")
    session.apply()
    gateway = SortGateway()
    runner = CliRunner()
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: gateway)
    result = runner.invoke(
        cli.app,
        ["--json", "messages", "list", "--limit", "1", "--offset", "1"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert gateway.calls == [("inbox", 1, 1, "subject")]
    assert json.loads(result.output)["data"]["items"][0]["reference"] == "second"
    result = runner.invoke(
        cli.app, ["--json", "messages", "list", "--order-by=-size"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert gateway.calls[-1][-1] == "-size"
    assert (
        MailSortPreferences.from_settings(SettingsService(context.config_store)).order_by
        == "subject"
    )
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Invalid order must stay local")
    )
    invalid = runner.invoke(
        cli.app, ["messages", "list", "--order-by=body"], env=_environment(tmp_path)
    )
    assert invalid.exit_code == 3, invalid.output
    too_large = runner.invoke(
        cli.app, ["messages", "list", "--limit", "201"], env=_environment(tmp_path)
    )
    assert too_large.exit_code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_settings_stage_cancel_apply_refreshes_bounded_server_order_and_restart(
    tmp_path, size
):
    context = _context(tmp_path)
    gateway = SortGateway()
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=size) as pilot:
        await wait_for(pilot, lambda: bool(gateway.calls) and len(app._messages) == 2)
        assert gateway.calls[-1] == ("inbox", app._mailbox_page_limit(), 0, "-datetime_received")
        assert [m.reference for m in app._messages] == ["second", "first"]
        app._open_area("settings")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog is not None,
        )
        screen = app.screen
        await pilot.pause()
        before = context.paths.config_file.read_bytes()
        calls = len(gateway.calls)
        assert screen.action_select_setting("mail.sort_field")
        await pilot.pause()
        screen.query_one("#settings-choice", Select).value = "subject"
        screen.query_one("#settings-stage", Button).focus()
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: "mail.sort_field" in screen._staged and not screen._operation_active
        )
        assert context.paths.config_file.read_bytes() == before
        assert len(gateway.calls) == calls
        screen.action_cancel_changes()
        await wait_for(pilot, lambda: not screen._staged and screen.view_state is ViewState.READY)
        for identifier, value in [
            ("mail.sort_field", "subject"),
            ("mail.sort_direction", "ascending"),
        ]:
            assert screen.action_select_setting(identifier)
            await pilot.pause()
            screen.query_one("#settings-choice", Select).value = value
            stage = screen.query_one("#settings-stage", Button)
            await wait_for(pilot, lambda stage=stage: not stage.has_class("-active"))
            stage.focus()
            await pilot.press("enter")
            await wait_for(
                pilot,
                lambda identifier=identifier: (
                    identifier in screen._staged
                    and not screen._operation_active
                    and screen.view_state is ViewState.READY
                ),
            )
        selected = app.ui.selected_reference
        await pilot.press("ctrl+s")
        await wait_for(
            pilot,
            lambda: (
                screen.view_state is ViewState.READY
                and gateway.calls[-1][-1] == "subject"
                and [m.reference for m in app._messages] == ["first", "second"]
                and not app.ui.busy
            ),
        )
        assert app.ui.selected_reference == selected
        assert gateway.calls[-1] == ("inbox", app._mailbox_page_limit(), 0, "subject")
        await pilot.press("escape")
        await pilot.pause()
        table = app.query_one("#message-table", DataTable)
        assert [r.value for r in table.rows] == ["first", "second"]
    restarted = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert restarted._mail_sort_order == "subject"
