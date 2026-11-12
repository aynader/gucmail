"""Normal palette/setup/search controls at three terminal sizes; synthetic local data."""

import asyncio

import pytest
from tests.test_tui import context_for
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.semantic_model import LocalTopicModel
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.local_search_screen import LocalSearchScreen
from gucmail.tui.semantic_screen import SemanticScreen
from test_semantic_search import CHUNKS, FIXTURE


async def wait(pilot, condition):
    for _ in range(200):
        await pilot.pause(0.025)
        if condition():
            return
    state = pilot.app.screen.query("#area-state")
    assert condition(), (
        f"Synthetic local UI did not reach its expected state: {type(pilot.app.screen).__name__}; "
        f"{str(state.first(Static).content) if state else ''}"
    )


async def activate(app, pilot, selector):
    button = app.screen.query_one(selector, Button)
    for _ in range(50):
        if button.has_focus:
            break
        await pilot.press("tab")
    await wait(pilot, lambda: button.has_focus and app.screen.region.contains_region(button.region))
    assert not button.disabled
    await wait(pilot, lambda: not button.has_class("-active"))
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_palette_consent_preview_build_query_stale_clear_keyboard(
    tmp_path, monkeypatch, size
):
    from gucmail.services import semantic as module

    monkeypatch.setattr(
        module,
        "fit_in_worker",
        lambda *args, **kwargs: LocalTopicModel.from_bytes(FIXTURE.read_bytes()),
    )
    context = context_for(tmp_path)
    index = SearchIndex(context.paths.index_database)
    index.upsert_many(IndexDocument(f"m-{i}", subject=text) for i, text in enumerate(CHUNKS))
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Private semantic UI accessed Exchange")
    )
    try:
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+k")
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            app.screen.query_one("#palette-input", Input).value = "Semantic search"
            await pilot.pause()
            await pilot.press("enter")
            await wait(pilot, lambda: isinstance(app.screen, LocalSearchScreen))
            search = app.screen
            await activate(app, pilot, "#local-search-semantic-setup")
            await wait(
                pilot,
                lambda: isinstance(app.screen, SemanticScreen) and not app.screen._operation_active,
            )
            setup = app.screen
            assert not setup.service.store.enabled
            assert not setup.service.store.path.exists()
            await activate(app, pilot, "#semantic-enable")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("n")
            await wait(pilot, lambda: app.screen is setup)
            assert not setup.service.store.enabled
            await activate(app, pilot, "#semantic-enable")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is setup and not setup._operation_active)
            assert setup.service.store.enabled
            setup.query_one("#semantic-dimensions", Input).value = "2"
            await activate(app, pilot, "#semantic-preview")
            await wait(pilot, lambda: not setup._operation_active and setup._preview is not None)
            assert setup._preview["selected_documents"] == 8
            assert setup._preview["include_body"] is False
            assert not setup.service.store.path.exists()
            await activate(app, pilot, "#semantic-build")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "indexed bodies: False" in app.screen.detail_text
            await pilot.press("n")
            await wait(pilot, lambda: app.screen is setup)
            assert not setup.service.store.path.exists()
            await activate(app, pilot, "#semantic-build")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is setup and not setup._operation_active)
            assert setup.service.status()["documents"] == 8
            await activate(app, pilot, ".area-close")
            await wait(pilot, lambda: app.screen is search)
            search.query_one("#local-search-query", Input).value = "car"
            await activate(app, pilot, "#local-search-run")
            await wait(pilot, lambda: not search._operation_active)
            assert search.query_one("#local-search-table", DataTable).row_count >= 3
            result = next(row for row in search._results.values() if row.reference == "m-2")
            assert (
                result.source == "local-semantic" and "lexical overlap 0.000" in result.explanation
            )
            table = search.query_one("#local-search-table", DataTable)
            for _ in range(50):
                if table.has_focus:
                    break
                await pilot.press("tab")
            await wait(pilot, lambda: table.has_focus)
            await pilot.press("home", "down", "down")
            await wait(
                pilot,
                lambda: (
                    "Reference: m-2"
                    in str(search.query_one("#local-search-detail", Static).content)
                ),
            )
            assert "lexical overlap 0.000" in str(
                search.query_one("#local-search-detail", Static).content
            )
            details = search.query_one("#local-search-detail-pane", VerticalScroll)
            await pilot.press("tab")
            await wait(pilot, lambda: details.has_focus)
            if size[0] < 92:
                assert details.max_scroll_y > 0, (
                    details.size,
                    details.virtual_size,
                    search.query_one("#local-search-detail", Static).region,
                )
            await pilot.press("end")
            await pilot.pause()
            if size[0] < 92:
                try:
                    await wait(pilot, lambda: details.scroll_y > 0)
                except AssertionError as exc:
                    raise AssertionError(
                        (
                            details.size,
                            details.virtual_size,
                            details.max_scroll_y,
                            details.scroll_target_y,
                            details._allow_scroll,
                            details._bindings.key_to_bindings.get("end"),
                            app.focused,
                        )
                    ) from exc
                assert details.scroll_y > 0
            index.delete("m-2")
            await activate(app, pilot, "#local-search-run")
            await wait(
                pilot,
                lambda: (
                    not search._operation_active
                    and "changed" in str(search.query_one("#area-state", Static).content)
                ),
            )
            assert "changed" in str(search.query_one("#area-state", Static).content)
            assert search.query_one("#local-search-table", DataTable).row_count == 0
            assert search.query_one("#local-search-open", Button).disabled
            await activate(app, pilot, "#local-search-semantic-setup")
            await wait(
                pilot,
                lambda: isinstance(app.screen, SemanticScreen) and not app.screen._operation_active,
            )
            setup = app.screen
            assert setup.service.status()["stale"]
            await activate(app, pilot, "#semantic-clear")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is setup and not setup._operation_active)
            assert not setup.service.status()["built"]
            assert index.stats().documents == 7
            await activate(app, pilot, ".area-close")
            await wait(pilot, lambda: app.screen is search)
            await activate(app, pilot, ".area-close")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
    finally:
        context.close()
        await asyncio.sleep(0)
