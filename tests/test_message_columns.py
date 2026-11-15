"""Column schema, Settings persistence and actual mounted list consumers."""

import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from gucmail.errors import ConfigurationError
from gucmail.models import Address, MessageSummary
from gucmail.services.message_columns import (
    DEFAULT_COLUMNS,
    columns_value,
    fitted_columns,
    parse_columns,
)
from gucmail.settings import InMemoryRevisionStore, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.message_columns_preview import MessageColumnsPreviewScreen
from gucmail.tui.message_columns_screen import MessageColumnsScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_settings_secondpass import _context
from waiting import rounds


def test_column_preferences_export_import_reset_undo_and_reopen(tmp_path):
    context = _context(tmp_path)
    service = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    choices = (replace(DEFAULT_COLUMNS[2], width=42), *DEFAULT_COLUMNS[:2], *DEFAULT_COLUMNS[3:])
    value = columns_value(choices)
    service.set("layout.message_columns", value)
    assert (
        SettingsService(context.config_store).value("layout.message_columns").effective_value
        == value
    )
    exported = service.export()
    assert exported["settings"]["layout.message_columns"] == value
    service.reset("layout.message_columns")
    assert service.value("layout.message_columns").effective_value == columns_value()
    service.undo()
    assert service.value("layout.message_columns").effective_value == value
    other = SettingsService(_context(tmp_path / "other").config_store)
    other.apply_import(other.plan_import(exported))
    assert other.value("layout.message_columns").effective_value == value
    # Caller mutation cannot change the registry's defaults or persisted configuration.
    value[0]["width"] = 119
    assert service.value("layout.message_columns").effective_value[0]["width"] == 42
    assert DEFAULT_COLUMNS[2].width is None


@pytest.mark.parametrize(
    "invalid", ["duplicate", "missing", "unknown", "bool_width", "zero", "huge", "flags", "compact"]
)
def test_invalid_columns_are_rejected_without_configuration_write(tmp_path, invalid):
    rows = columns_value()
    if invalid == "duplicate":
        rows[-1] = copy.deepcopy(rows[0])
    elif invalid == "missing":
        rows.pop()
    elif invalid == "unknown":
        rows[0]["id"] = "password"
    elif invalid == "bool_width":
        rows[0]["width"] = True
    elif invalid == "zero":
        rows[0]["width"] = 0
    elif invalid == "huge":
        rows[0]["width"] = 121
    elif invalid == "flags":
        rows[0]["visible"] = "false"
    else:
        for row in rows:
            row["compact"] = False
    context = _context(tmp_path)
    original = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set("layout.message_columns", rows)
    assert context.paths.config_file.read_bytes() == original


def test_default_adaptive_columns_and_explicit_horizontal_scroll_widths():
    compact = fitted_columns(DEFAULT_COLUMNS, 50, 1)
    assert [column.id for column, _ in compact] == ["state", "sender", "subject"]
    assert sum(width for _, width in compact) + 6 == 50
    wide = fitted_columns(DEFAULT_COLUMNS, 115, 1)
    assert [column.id for column, _ in wide] == ["state", "sender", "subject", "received", "size"]
    assert sum(width for _, width in wide) + 10 == 115
    custom = tuple(replace(c, width=120) if c.id == "subject" else c for c in DEFAULT_COLUMNS)
    assert dict((c.id, size) for c, size in fitted_columns(custom, 50, 1))["subject"] == 120


async def wait_for(pilot, condition):
    for _ in rounds(100):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError("Mounted workflow did not reach the expected state")


async def open_column_preview(app, pilot, *, width):
    app._open_area("settings")
    await wait_for(pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog)
    settings = app.screen
    assert settings.action_select_setting("layout.message_columns")
    settings.query_one("#settings-managed", Button).press()
    await wait_for(pilot, lambda: isinstance(app.screen, MessageColumnsScreen))
    editor = app.screen
    table = editor.query_one("#columns-table", DataTable)
    await wait_for(pilot, lambda: table.row_count == 11)
    table.move_cursor(row=2)
    await pilot.pause()
    assert editor.selected == "subject"
    editor.query_one("#column-width", Input).value = str(width)
    editor.query_one("#column-compact", Checkbox).value = True
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: "layout.message_columns" in settings._staged)
    await wait_for(pilot, lambda: not settings.mutation_pending)
    settings.query_one("#settings-preview", Button).press()
    await wait_for(pilot, lambda: isinstance(app.screen, MessageColumnsPreviewScreen))
    preview = app.screen
    await wait_for(
        pilot,
        lambda: (
            bool(preview.query("#column-preview-table"))
            and len(preview.query_one("#column-preview-table", DataTable).columns)
            == len(preview.columns)
        ),
    )
    await pilot.pause()
    return settings, preview


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_live_column_preview_and_cancel_restore_exact_runtime_and_file(tmp_path, size):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    messages = (
        MessageSummary("first", "Literal [red] label", Address("synthetic@example.test"), None),
        MessageSummary("second", "Selected", Address("other@example.test"), None),
    )
    async with app.run_test(size=size) as pilot:
        app._render_message_rows(messages)
        table = app.query_one("#message-table", DataTable)
        table.move_cursor(row=1)
        await wait_for(pilot, lambda: app.ui.selected_reference == "second")
        original_columns = app._message_columns
        original_file = context.paths.config_file.read_bytes()
        settings, preview = await open_column_preview(app, pilot, width=42)
        assert app._message_columns[2].width == 42
        assert (
            table.columns[next(key for key in table.columns if key.value == "subject")].width == 42
        )
        await wait_for(pilot, lambda: preview.query_one(DataTable).row_count == 2)
        copied = preview.query_one("#column-preview-table", DataTable)
        subject_index = next(i for i, key in enumerate(copied.columns) if key.value == "subject")
        assert copied.get_row("first")[subject_index].plain == "Literal [red] label"
        assert copied.cursor_row == 1
        assert preview.query_one("#column-preview-cancel", Button).region.bottom <= size[1]
        assert context.paths.config_file.read_bytes() == original_file
        await pilot.press("escape")
        await wait_for(pilot, lambda: not settings._preview and not settings.mutation_pending)
        assert app._message_columns == original_columns
        assert app.ui.selected_reference == "second"
        assert context.paths.config_file.read_bytes() == original_file


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["normal", "forced_unmount"])
async def test_leaving_settings_reverts_a_column_preview(tmp_path, close):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        original_columns = app._message_columns
        original_file = context.paths.config_file.read_bytes()
        settings, preview = await open_column_preview(app, pilot, width=37)
        preview.query_one("#column-preview-edit", Button).press()
        await wait_for(pilot, lambda: app.screen is settings)
        assert app._message_columns[2].width == 37
        if close == "normal":
            settings.action_close()
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert app._message_columns[2].width == 37
            assert context.paths.config_file.read_bytes() == original_file
            await pilot.press("escape")
            await wait_for(pilot, lambda: app.screen is settings)
            assert settings._preview and app._message_columns[2].width == 37
            settings.action_close()
            await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
        else:
            app.pop_screen()
        await wait_for(pilot, lambda: not settings.is_attached)
        assert app._message_columns == original_columns
        assert context.paths.config_file.read_bytes() == original_file


@pytest.mark.asyncio
async def test_replacing_preview_then_apply_keeps_only_durable_new_columns(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        settings, preview = await open_column_preview(app, pilot, width=37)
        preview.query_one("#column-preview-edit", Button).press()
        await wait_for(pilot, lambda: app.screen is settings)
        prior_token = settings._preview.token
        value = columns_value()
        value[2]["width"] = 52
        settings._staged["layout.message_columns"] = value
        settings.query_one("#settings-preview", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, MessageColumnsPreviewScreen))
        await wait_for(pilot, lambda: bool(app.screen.query("#column-preview-apply")))
        await pilot.pause()
        assert settings._preview.token != prior_token
        assert list(settings.backend._previews) == [settings._preview.token]
        assert app._message_columns[2].width == 52
        app.screen.query_one("#column-preview-apply", Button).press()
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        assert not settings.backend._previews
        assert app._message_columns[2].width == 52
        assert (
            SettingsService(context.config_store).value("layout.message_columns").effective_value
            == value
        )
        settings.action_close()
        await wait_for(pilot, lambda: not settings.is_attached)
        assert app._message_columns[2].width == 52
        assert GUCmailApp(context, offline=True)._message_columns[2].width == 52


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_guided_columns_reject_width_and_cancel_without_dismissing(size):
    app = App()
    results = []
    screen = MessageColumnsScreen(columns_value())
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen, results.append)
        await pilot.pause()
        screen.query_one("#column-width", Input).value = "0"
        await pilot.press("ctrl+s")
        assert app.screen is screen
        assert "1-120" in str(screen.query_one("#columns-error", Static).content)
        assert screen.query_one("#column-stage", Button).region.bottom <= size[1]
        await pilot.press("escape")
        assert results == [None]


@pytest.mark.asyncio
async def test_actual_settings_guided_apply_reflows_preserves_selection_and_restart(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    messages = (
        MessageSummary(
            "first",
            "Literal [red] subject",
            Address("one@example.test"),
            datetime(2026, 10, 1, tzinfo=UTC),
            has_attachments=True,
            folder_path="Synthetic/Inbox",
            conversation_id="synthetic-conversation",
        ),
        MessageSummary("second", "Selected row", Address("two@example.test"), None),
    )
    async with app.run_test(size=(120, 35)) as pilot:
        app._render_message_rows(messages)
        app.query_one("#message-table", DataTable).move_cursor(row=1)
        await wait_for(pilot, lambda: app.ui.selected_reference == "second")
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen
        assert settings.action_select_setting("layout.message_columns")
        settings.query_one("#settings-managed", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, MessageColumnsScreen))
        editor = app.screen
        table = editor.query_one("#columns-table", DataTable)
        await wait_for(pilot, lambda: table.row_count == 11)
        table.move_cursor(row=2)
        await pilot.pause()
        assert editor.selected == "subject"
        editor.query_one("#column-width", Input).value = "42"
        await pilot.press("alt+up", "alt+up")
        assert editor.columns[0].id == "subject"
        table.move_cursor(row=5)
        await pilot.pause()
        assert editor.selected == "attachments"
        editor.query_one("#column-visible", Checkbox).value = True
        editor.query_one("#column-compact", Checkbox).value = True
        await pilot.press("ctrl+s")
        await wait_for(
            pilot, lambda: app.screen is settings and "layout.message_columns" in settings._staged
        )
        assert app._message_columns == DEFAULT_COLUMNS  # staging is reversible
        settings.query_one("#settings-apply", Button).press()
        await wait_for(pilot, lambda: app._message_columns[0].id == "subject")
        await pilot.press("escape")
        await pilot.pause()
        rendered = app.query_one("#message-table", DataTable)
        assert next(iter(rendered.columns)).value == "subject"
        assert next(iter(rendered.columns.values())).width == 42
        assert rendered.get_row("first")[0].plain == "Literal [red] subject"
        assert "Yes" in map(str, rendered.get_row("first"))
        assert app.ui.selected_reference == "second"
        await pilot.resize_terminal(70, 20)
        await pilot.pause()
        assert [key.value for key in rendered.columns] == [
            "subject",
            "state",
            "sender",
            "attachments",
        ]
        assert app.ui.selected_reference == "second"
    reopened = GUCmailApp(_context(tmp_path), offline=True)
    assert reopened._message_columns == app._message_columns
    assert parse_columns(columns_value(app._message_columns)) == app._message_columns
