from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Input, Static, TextArea

from gucmail.services.messages import MessageService
from gucmail.services.search import BooleanNode, BooleanOperator, NotNode
from gucmail.state import StateStore
from gucmail.tui.saved_search_screen import (
    SavedSearchEditorScreen,
    SavedSearchNameScreen,
    SavedSearchRunRequested,
    SavedSearchScreen,
    StateSavedSearchBackend,
)


def test_saved_search_backend_crud_metadata_history_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_store = StateStore(path, account_key="student@example.test")
    backend = StateSavedSearchBackend(first_store, timezone="Africa/Cairo")

    created = backend.create(
        "Unread coursework",
        'from:"professor@example.test" AND unread:true AND folder:"Coursework"',
    )
    assert created.name == "Unread coursework"
    assert created.scope == "Coursework"
    assert "Exchange" in created.execution_summary

    updated = backend.update(
        created.name,
        '(subject:"exam" OR subject:"quiz") AND NOT has:attachment',
    )
    plan = backend.preview(updated.query_text)
    assert isinstance(plan.payload["ast"], dict)
    expression = plan.payload["ast"]
    assert expression["type"] == "and"

    renamed = backend.rename(created.name, "Assessment mail")
    duplicate = backend.duplicate(renamed.name, "Assessment mail copy")
    assert [entry.name for entry in backend.list_saved()] == [
        "Assessment mail",
        "Assessment mail copy",
    ]

    first_store.record_search_run(
        query=dict(renamed.payload),
        result_count=7,
        source="saved",
        name=renamed.name,
    )
    refreshed = {entry.name: entry for entry in backend.list_saved()}[renamed.name]
    assert refreshed.last_result_count == 7
    assert refreshed.run_count == 1
    assert backend.list_recent()[0].query_text == renamed.query_text
    assert backend.delete(duplicate.name)

    restarted = StateSavedSearchBackend(
        StateStore(path, account_key="student@example.test"),
        timezone="Africa/Cairo",
    )
    assert [(entry.name, entry.last_result_count) for entry in restarted.list_saved()] == [
        ("Assessment mail", 7)
    ]
    other_account = StateSavedSearchBackend(
        StateStore(path, account_key="other@example.test"),
        timezone="Africa/Cairo",
    )
    assert other_account.list_saved() == ()


def test_message_service_executes_parsed_expression_without_free_text_degradation() -> None:
    captured: dict[str, object] = {}

    class Gateway:
        def search_messages_expression_planned(
            self,
            query: object,
            plan: object,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[object, ...]:
            captured.update(query=query, plan=plan, limit=limit, offset=offset)
            return ()

    page = MessageService(Gateway()).search_expression(  # type: ignore[arg-type]
        '(from:"one@example.test" OR from:"two@example.test") AND NOT subject:"spam"',
        limit=19,
    )

    assert page.items == ()
    assert isinstance(captured["query"], BooleanNode)
    query = captured["query"]
    assert isinstance(query, BooleanNode)
    assert query.operator is BooleanOperator.AND
    assert isinstance(query.children[-1], NotNode)
    assert captured["limit"] == 19


def test_disabled_search_history_keeps_saved_search_run_metadata(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "history-disabled.db", account_key="student@example.test")
    backend = StateSavedSearchBackend(
        state,
        timezone="Africa/Cairo",
        history_enabled=False,
    )
    saved = backend.create("Unread", "unread:true")

    state.record_search_run(
        query=dict(saved.payload),
        result_count=4,
        source="saved",
        name=saved.name,
        keep_history=False,
    )

    refreshed = backend.list_saved()[0]
    assert (refreshed.run_count, refreshed.last_result_count) == (1, 4)
    assert backend.list_recent() == ()
    assert state.list_search_history() == []


class _SavedSearchHarness(App[None]):
    def __init__(self, screen: SavedSearchScreen) -> None:
        super().__init__()
        self.saved_screen = screen
        self.runs: list[SavedSearchRunRequested] = []

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(self.saved_screen)

    def on_saved_search_run_requested(self, event: SavedSearchRunRequested) -> None:
        event.stop()
        self.runs.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rename", "duplicate"])
async def test_escape_cancels_saved_search_name_dialog_without_changes(
    tmp_path: Path, action: str
) -> None:
    state = StateStore(tmp_path / "state.db", account_key="student@example.test")
    backend = StateSavedSearchBackend(state, timezone="Africa/Cairo")
    backend.create("Original", "unread:true")
    screen = SavedSearchScreen(backend)
    app = _SavedSearchHarness(screen)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen.query_one(f"#saved-search-{action}", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, SavedSearchNameScreen)
        app.screen.query_one("#saved-search-name-input", Input).value = "Changed"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen
        assert [(entry.name, entry.query_text) for entry in backend.list_saved()] == [
            ("Original", "unread:true")
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (79, 26)])
async def test_saved_search_screen_crud_filter_run_recent_and_responsive(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    state = StateStore(tmp_path / f"saved-{size[0]}.db", account_key="student@example.test")
    backend = StateSavedSearchBackend(state, timezone="Africa/Cairo")
    backend.create("Unread mail", "unread:true")
    backend.create("Large PDF", 'has:attachment AND attachment:".pdf" AND min-size:1mb')
    state.record_search_run(
        query=dict(backend.list_saved()[0].payload),
        result_count=3,
        source="saved",
        name=backend.list_saved()[0].name,
    )
    screen = SavedSearchScreen(backend)
    app = _SavedSearchHarness(screen)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        table = screen.query_one("#saved-search-table", DataTable)
        assert table.row_count == 2
        assert screen.has_class("narrow") is (size[0] < 96)
        assert screen.query_one("#saved-search-detail").region.height > 0

        screen.query_one("#saved-search-filter", Input).value = "pdf"
        await pilot.pause()
        assert table.row_count == 1
        screen.query_one("#saved-search-filter", Input).value = ""
        await pilot.pause()

        screen.action_run()
        await pilot.pause()
        assert app.runs
        assert app.runs[-1].source == "saved"

        screen.action_toggle_recent()
        await pilot.pause()
        assert table.row_count == 1
        assert "Recent searches" in str(screen.query_one("#saved-search-title", Static).content)
        screen.action_toggle_recent()
        await pilot.pause()

        screen.action_create()
        await pilot.pause()
        assert isinstance(app.screen, SavedSearchEditorScreen)
        editor = app.screen
        editor.query_one("#saved-search-editor-name", Input).value = "Today from Ahmed"
        editor.query_one("#saved-search-editor-query", TextArea).load_text(
            'from:"ahmed@example.test" AND after:today'
        )
        await pilot.pause()
        editor.action_save()
        await pilot.pause()
        assert isinstance(app.screen, SavedSearchScreen)
        assert {entry.name for entry in backend.list_saved()} == {
            "Unread mail",
            "Large PDF",
            "Today from Ahmed",
        }


@pytest.mark.asyncio
async def test_recent_mode_explains_disabled_search_history(tmp_path: Path) -> None:
    backend = StateSavedSearchBackend(
        StateStore(tmp_path / "disabled-screen.db", account_key="student@example.test"),
        timezone="Africa/Cairo",
        history_enabled=False,
    )
    screen = SavedSearchScreen(backend, mode="recent")
    app = _SavedSearchHarness(screen)

    async with app.run_test(size=(79, 26)) as pilot:
        await pilot.pause()
        empty = str(screen.query_one("#saved-search-empty", Static).content)
        assert "disabled" in empty.casefold()
        assert "Search & Indexing" in empty
