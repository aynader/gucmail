"""Expert-mode keyboard index rebuild, refusal and concurrent-edit recovery."""

from dataclasses import replace

import pytest
from textual.widgets import Input, Static

from gucmail.errors import ConfigurationError
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.diagnostics_screen import DiagnosticsScreen
from test_diagnostics_tui_secondpass import MetadataGateway, _context
from test_local_ocr_tui import activate, wait


class TrackedMetadataGateway(MetadataGateway):
    def __init__(self):
        super().__init__()
        self.reads = []

    def list_messages(self, **kwargs):
        self.reads.append(kwargs)
        return super().list_messages(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_palette_rebuild_decline_accept_and_stale_review_preserves_concurrent_edit(
    tmp_path, size
):
    context = _context(tmp_path)
    context.config = replace(context.config, mode="expert")
    context.config_store.save(context.config)
    index = SearchIndex(context.paths.index_database)
    index.upsert(IndexDocument("retained", subject="Preserved reference"))
    revision = index.revision()
    gateway = TrackedMetadataGateway()
    app = GUCmailApp(context, gateway=gateway)
    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: not app.ui.busy and len(app._messages) == 2)
            initial_reads = len(gateway.reads)
            await pilot.press("ctrl+k")
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            app.screen.query_one("#palette-input", Input).value = "Rebuild index"
            await pilot.pause()
            await pilot.press("enter")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "atomic replacement" in app.screen.detail_text
            assert "Message bodies and attachments are not downloaded" in app.screen.detail_text
            assert len(gateway.reads) == initial_reads and index.revision() == revision
            await pilot.press("n")
            await wait(pilot, lambda: isinstance(app.screen, DiagnosticsScreen))
            screen = app.screen
            assert index.record("retained").subject == "Preserved reference"
            assert "Current disposable index rows to replace: 1" in str(
                screen.query_one("#diagnostics-summary", Static).content
            )
            screen.query_one("#diagnostics-index-limit", Input).value = "2"
            await pilot.pause()
            await activate(pilot, "#diagnostics-rebuild-index")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "Bounded metadata limit: 2" in app.screen.detail_text
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is screen and not screen._operation_active)
            assert screen.view_state is ViewState.READY
            assert "Index rebuilt: 1 removed, 2 indexed" in str(
                screen.query_one("#area-state", Static).content
            )
            assert index.stats().documents == 2 and index.verify().valid
            assert "New indexed metadata records: 2" in str(
                screen.query_one("#diagnostics-summary", Static).content
            )
            with pytest.raises(ConfigurationError, match="not present"):
                index.record("retained")
            assert index.search("One")
            assert len(gateway.reads) == initial_reads + 1 and gateway.reads[-1]["limit"] == 2
            await activate(pilot, "#diagnostics-rebuild-index")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            SearchIndex(index.path).upsert(IndexDocument("inbox-one", subject="Concurrent edit"))
            changed_revision = index.revision()
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is screen and not screen._operation_active)
            assert screen.view_state is ViewState.ERROR
            assert "changed" in str(screen.query_one("#area-state", Static).content).casefold()
            assert index.revision() == changed_revision and index.verify().valid
            assert index.record("inbox-one").subject == "Concurrent edit"
            assert len(gateway.reads) == initial_reads + 1
            await activate(pilot, ".area-close")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
    finally:
        context.close()


@pytest.mark.asyncio
async def test_expert_palette_gate_tracks_saved_applied_cancelled_reset_and_restart(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    try:
        async with app.run_test() as pilot:
            service = app._settings_service()
            assert not app._palette_context().expert_mode
            session = service.begin()
            session.set("advanced.mode", "expert")
            assert not app._palette_context().expert_mode
            session.cancel()
            assert not app._palette_context().expert_mode
            service.set("advanced.mode", "expert")
            app._apply_committed_settings(("advanced.mode",))
            assert app._palette_context().expert_mode and context.config.mode == "expert"
            restarted = GUCmailApp(
                replace(context, config=context.config_store.load()), offline=True
            )
            assert restarted.context.config.mode == "expert"
            service.reset("advanced.mode")
            app._apply_committed_settings(("advanced.mode",))
            assert not app._palette_context().expert_mode
            await pilot.pause()
    finally:
        context.close()
