"""Persistent shell choices through normal Settings and bounded terminal rendering."""

from dataclasses import replace

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Input, Select, Static

from gucmail.errors import ConfigurationError
from gucmail.services.shell_preferences import fit_header, header_variants
from gucmail.settings import InMemoryRevisionStore, SettingsService
from gucmail.tui.app import AccountStatus, CompactHeader, GUCmailApp
from gucmail.tui.area_common import ViewState
from gucmail.tui.settings_screen import SettingsScreen
from test_compose_preferences import wait_for
from test_settings_secondpass import _context


@pytest.mark.parametrize(
    "key,value",
    [
        ("layout.header_contents", ["account", "account"]),
        ("layout.header_contents", ["sender"]),
        ("layout.header_contents", [True]),
        ("layout.status_bar_contents", []),
        ("layout.status_bar_contents", ["folder", "activity"]),
        ("layout.status_bar_contents", ["activity", "body"]),
        ("layout.header_compactness", "expand"),
    ],
)
def test_invalid_shell_choices_leave_configuration_unchanged(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set(key, value)
    assert context.paths.config_file.read_bytes() == before


def test_shell_preferences_roundtrip_reset_undo_and_export(tmp_path):
    context = _context(tmp_path)
    service = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    session = service.begin()
    session.set("layout.header_contents", "folder,account")
    session.set("layout.header_compactness", "detailed")
    session.set("layout.status_bar_contents", "activity,connection,folder")
    session.apply()
    service.reset("layout.header_compactness")
    assert service.value("layout.header_compactness").effective_value == "adaptive"
    service.undo()
    assert service.value("layout.header_compactness").effective_value == "detailed"
    target = SettingsService(_context(tmp_path / "other").config_store)
    target.apply_import(target.plan_import(service.export()))
    for key in (
        "layout.header_contents",
        "layout.header_compactness",
        "layout.status_bar_contents",
    ):
        assert target.value(key).effective_value == service.value(key).effective_value
    reopened = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert reopened._header_contents == ("folder", "account")
    assert reopened._header_compactness == "detailed"
    assert reopened._status_bar_contents == ("activity", "connection", "folder")


def test_compact_quota_does_not_invent_usage_and_hidden_fields_do_not_return():
    facts = {
        "account": "Connected",
        "storage": "Send threshold 92 MiB (Known GUC Fallback) · usage unknown",
        "monitoring": "Watcher stopped",
        "folder": "Synthetic 界 folder",
    }
    variants = header_variants(facts, ("storage", "account"), compactness="compact")
    assert variants[0] == "Storage details · Connected"
    assert all("Watcher" not in value and "folder" not in value for value in variants)
    assert all("%" not in value for value in variants)
    for width in (1, 5, 20, 70, 140):
        assert cell_len(fit_header(variants, width)) <= width
    assert fit_header(header_variants(facts, ()), 80) == ""
    assert fit_header(header_variants(facts, ("folder",), compactness="detailed"), 5).endswith("…")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_settings_stage_cancel_apply_shell_fields_and_resize_without_network(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Display preferences are local")
    )
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog is not None,
        )
        await pilot.pause()
        screen = app.screen
        before = context.paths.config_file.read_bytes()
        assert screen.action_select_setting("layout.header_contents")
        await pilot.pause()
        screen.query_one("#settings-editor", Input).value = "folder,account"
        screen.query_one("#settings-stage", Button).press()
        await wait_for(
            pilot,
            lambda: "layout.header_contents" in screen._staged and not screen._operation_active,
        )
        assert app._header_contents == ("account", "storage", "monitoring", "folder")
        assert context.paths.config_file.read_bytes() == before
        screen.action_cancel_changes()
        await wait_for(pilot, lambda: not screen._staged and screen.view_state is ViewState.READY)
        await pilot.pause()
        assert context.paths.config_file.read_bytes() == before
        for key, value in (
            ("layout.header_contents", "folder,account"),
            ("layout.header_compactness", "detailed"),
            ("layout.status_bar_contents", "activity,connection,folder"),
        ):
            assert screen.action_select_setting(key)
            await pilot.pause()
            assert screen._selected_setting_id == key
            if key.endswith("compactness"):
                screen.query_one("#settings-choice", Select).value = value
            else:
                screen.query_one("#settings-editor", Input).value = value
            screen.query_one("#settings-stage", Button).press()
            await wait_for(
                pilot,
                lambda key=key: (
                    key in screen._staged
                    and screen.view_state is ViewState.READY
                    and not screen._operation_active
                ),
            )
        screen.query_one("#settings-apply", Button).press()
        await wait_for(
            pilot,
            lambda: (
                app._header_contents == ("folder", "account")
                and screen.view_state is ViewState.READY
            ),
        )
        await pilot.press("escape")
        await pilot.pause()
        app._render_status({"exchange_version": "Synthetic"}, quota_label="Hidden storage")
        header = app.query_one("#account-bar", AccountStatus)
        assert str(header.content) == "Inbox · Connected"
        assert "Hidden storage" not in str(header.tooltip) and "Watcher" not in str(header.tooltip)
        app._set_status("Literal [red] activity\x1b[2J")
        status = app.query_one("#status-bar", Static)
        assert str(status.content) == "Literal [red] activity · Connected · Inbox"
        assert not status._render_markup
        for width in (40, 70, 140):
            await pilot.resize_terminal(width, 20)
            await pilot.pause()
            assert app.query_one(CompactHeader).region.height == 1
            assert cell_len(str(header.content)) <= header.content_size.width
        app._fail_mailbox_refresh(app._mailbox_generation, "Synthetic failure")
        assert "Connected" not in str(header.content)
        assert "Synthetic failure" in str(status.content)
    reopened = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert reopened._header_contents == ("folder", "account")
    assert reopened._header_compactness == "detailed"


@pytest.mark.asyncio
async def test_failed_layout_save_reports_session_only_and_preserves_durable_layout(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("layout.preview_visible", False)
    before = context.paths.config_file.read_bytes()
    app = GUCmailApp(context, offline=True)
    notices = []
    monkeypatch.setattr(app, "notify", lambda text, **kw: notices.append((text, kw)))

    def denied(*args, **kwargs):
        raise OSError("Synthetic write failure")

    monkeypatch.setattr(context.config_store, "save_document", denied)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_reset_layout()
        await pilot.pause()
        assert app._preview_visible
        assert "not saved" in str(app.query_one("#status-bar", Static).content)
        assert notices and notices[-1][1]["severity"] == "warning"
        assert context.paths.config_file.read_bytes() == before
    assert not SettingsService(context.config_store).value("layout.preview_visible").effective_value
