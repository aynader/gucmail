"""Normal Settings and Expert palette controls for deliberate attachment indexing."""

from dataclasses import replace

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Select, Static

from gucmail.services.attachment_indexing import attachment_index_preferences
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.diagnostics_screen import DiagnosticsScreen
from gucmail.tui.local_search_screen import LocalSearchScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_attachment_index_producer import AttachmentGateway
from test_diagnostics_tui_secondpass import _context
from test_local_ocr_tui import activate, focus, wait
from test_tui import FakeGateway


class KeyboardGateway(AttachmentGateway, FakeGateway):
    pass


async def open_palette(pilot, query, screen_type):
    await pilot.press("ctrl+k")
    await wait(pilot, lambda: isinstance(pilot.app.screen, PaletteScreen))
    await pilot.press(*query)
    await pilot.pause()
    await pilot.press("enter")
    await wait(pilot, lambda: isinstance(pilot.app.screen, screen_type))


async def select_setting(pilot, identifier):
    await pilot.press("ctrl+f")
    await pilot.press("home", "shift+end", *identifier)
    try:
        await wait(pilot, lambda: pilot.app.screen._selected_setting_id == identifier)
    except AssertionError as error:
        screen = pilot.app.screen
        raise AssertionError(
            f"Selected={screen._selected_setting_id}; "
            f"query={screen.query_one('#settings-search', Input).value!r}; "
            f"level={screen.query_one('#settings-level', Select).value}; "
            f"visible={[item.setting_id for item in screen._visible_settings[:5]]}; "
            f"operation={screen._operation_active}; "
            f"status={screen.query_one('#area-state', Static).content}"
        ) from error
    await pilot.press("enter", "enter")
    await wait(pilot, lambda: pilot.app.screen._editor_open)


async def choose(pilot, value):
    selected = await focus(pilot, "#settings-choice")
    assert isinstance(selected, Select)
    await pilot.press("enter", "home")
    for _ in range(len(selected._options)):
        await pilot.press("enter")
        if selected.value == value:
            return
        await pilot.press("enter", "down")
    pytest.fail(f"Visible setting choice could not reach {value}")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_consent_cancel_apply_limit_rebuild_and_stale_policy(tmp_path, size):
    context = _context(tmp_path)
    context.config = replace(context.config, mode="expert")
    context.config_store.save(context.config)
    index = SearchIndex(context.paths.index_database)
    index.upsert(IndexDocument("retained", subject="Existing local record"))
    before = index.revision()
    gateway = KeyboardGateway()
    app = GUCmailApp(context, gateway=gateway)
    settings_service = app._settings_service()
    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: not app.ui.busy and len(app._messages) == 1)
            await open_palette(pilot, "Settings", SettingsScreen)
            settings = app.screen
            await wait(pilot, lambda: settings._catalog is not None)
            await select_setting(pilot, "search.index_attachments")
            await choose(pilot, "on")
            await activate(pilot, "#settings-stage")
            await wait(pilot, lambda: bool(settings._staged) and not settings._operation_active)
            assert not attachment_index_preferences(settings_service).enabled
            assert not any(call[0] in {"metadata", "content"} for call in gateway.calls)
            await pilot.press("ctrl+z")
            await wait(pilot, lambda: not settings._staged and not settings._operation_active)
            assert not attachment_index_preferences(settings_service).enabled
            await select_setting(pilot, "search.index_attachments")
            await choose(pilot, "on")
            await activate(pilot, "#settings-stage")
            await wait(pilot, lambda: bool(settings._staged) and not settings._operation_active)
            await pilot.press("ctrl+s")
            await wait(pilot, lambda: not settings._staged and not settings._operation_active)
            assert attachment_index_preferences(settings_service).enabled
            await select_setting(pilot, "search.attachment_files_per_run")
            await focus(pilot, "#settings-editor")
            await pilot.press("home", "shift+end", "1", "enter")
            await wait(pilot, lambda: bool(settings._staged) and not settings._operation_active)
            await pilot.press("ctrl+s")
            await wait(pilot, lambda: not settings._staged and not settings._operation_active)
            assert attachment_index_preferences(app._settings_service()).files_per_run == 1
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
            initial_reads = len(gateway.calls)
            await open_palette(pilot, "Rebuild index", ConfirmationScreen)
            assert "Attachment content indexing is enabled" in app.screen.detail_text
            assert (
                "1 per run" in app.screen.detail_text
                and "Image OCR is disabled" in app.screen.detail_text
            )
            assert "private local index data" in app.screen.detail_text
            assert len(gateway.calls) == initial_reads and index.revision() == before
            await pilot.press("n")
            await wait(pilot, lambda: isinstance(app.screen, DiagnosticsScreen))
            screen = app.screen
            assert index.revision() == before and not gateway.stream
            await focus(pilot, "#diagnostics-index-limit")
            await pilot.press("home", "shift+end", "1")
            await activate(pilot, "#diagnostics-rebuild-index")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "Bounded metadata limit: 1" in app.screen.detail_text
            await pilot.press("y")
            # The configured extraction deadline is eight seconds. Completion
            # assertions must allow that result (plus guarded child bootstrap),
            # rather than imposing an unrelated five-second performance gate.
            await wait(
                pilot, lambda: app.screen is screen and not screen._operation_active, timeout=15
            )
            assert screen.view_state is ViewState.READY
            assert index.stats().documents == 1 and index.verify().valid
            assert index.search("uniquelysearchableattachment")[0].reference == "synthetic"
            assert gateway.stream.closed
            assert "Attachment files downloaded: 1" in str(
                screen.query_one("#diagnostics-summary", Static).content
            )
            summary = await focus(pilot, "#diagnostics-summary-pane")
            assert isinstance(summary, VerticalScroll)
            await pilot.press("end")
            await pilot.wait_for_scheduled_animations()
            await wait(pilot, lambda: abs(summary.scroll_y - summary.max_scroll_y) < 0.01)
            if size[0] <= 80:
                assert summary.max_scroll_y > 0
                text = screen.query_one("#diagnostics-summary", Static)
                assert text.region.bottom <= summary.content_region.bottom
            await pilot.press("home")
            await pilot.wait_for_scheduled_animations()
            await wait(pilot, lambda: summary.scroll_y == 0)
            downloaded = sum(call[0] == "content" for call in gateway.calls)
            assert downloaded == 1
            await activate(pilot, "#diagnostics-rebuild-index")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            settings_service.set("search.attachment_files_per_run", 2)
            current = index.revision()
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is screen and not screen._operation_active)
            assert screen.view_state is ViewState.ERROR
            assert "settings changed after preview" in str(
                screen.query_one("#area-state", Static).content
            )
            assert index.revision() == current
            assert sum(call[0] == "content" for call in gateway.calls) == downloaded
            await activate(pilot, ".area-close")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
            await open_palette(pilot, "Settings", SettingsScreen)
            settings = app.screen
            await wait(pilot, lambda: settings._catalog is not None)
            await select_setting(pilot, "search.index_attachments")
            await choose(pilot, "off")
            await activate(pilot, "#settings-stage")
            await wait(pilot, lambda: bool(settings._staged) and not settings._operation_active)
            await pilot.press("ctrl+s")
            await wait(pilot, lambda: not settings._staged and not settings._operation_active)
            assert not attachment_index_preferences(app._settings_service()).enabled
            assert index.search("uniquelysearchableattachment"), (
                "Disabling future downloads does not silently erase existing private derived data"
            )
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
            calls = len(gateway.calls)
            await open_palette(pilot, "Search Attachments", LocalSearchScreen)
            search = app.screen
            assert search.query_one("#local-search-mode", Select).value == "attachments"
            await focus(pilot, "#local-search-query")
            await pilot.press("home", "shift+end", *"uniquelysearchableattachment", "enter")
            await wait(pilot, lambda: bool(search._results) and not search._operation_active)
            assert len(search._results) == 1
            result = next(iter(search._results.values()))
            assert result.reference == "synthetic" and result.source == "local-index"
            assert "locally extracted attachment text" in result.explanation
            await focus(pilot, "#local-search-root")
            await pilot.press("end")
            await pilot.wait_for_scheduled_animations()
            assert len(gateway.calls) == calls
    finally:
        context.close()
