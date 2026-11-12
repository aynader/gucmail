from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from threading import get_ident

import pytest
from textual.widgets import DataTable, Input, Select

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.models import MessageSummary
from gucmail.services.local_search import LocalSearchMode
from gucmail.services.ui_session import SESSION_KEY, SESSION_SCOPE, MailSession, MailSessionStore
from gucmail.state import StateStore
from gucmail.tui.app import ConfirmationScreen, GUCmailApp, PaletteScreen
from gucmail.tui.local_search_screen import LocalSearchScreen


def context_for(root: Path) -> AppContext:
    paths = AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config/config.toml",
        state_database=root / "data/state.db",
        recovery_dir=root / "data/recovery",
        index_database=root / "data/index.db",
        backup_dir=root / "data/backups",
    )
    config = AppConfig(email="session@example.invalid")
    store = ConfigStore(paths)
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def summaries(count: int = 40) -> tuple[MessageSummary, ...]:
    return tuple(
        MessageSummary(
            reference=f"synthetic-{i}", subject=f"Synthetic row {i}", sender=None, received_at=None
        )
        for i in range(count)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"focus": "compose-send"},
        {"focus": []},
        {"folder": ""},
        {"folder": "inbox\x1b[31m"},
        {"folder": "x" * 1_025},
        {"message_row": True},
        {"message_row": -1},
        {"message_row": 200},
        {"message_scroll": 10_001},
        {"folder_scroll": "2"},
        {"search_scope": "similar"},
        {"search_scope": "https://example.invalid"},
        {"search_scope": []},
        {"query": "private phrase"},
        {"pending_send": True},
        {"raw_xml": "<Execute/>"},
    ],
)
def test_session_rejects_unsafe_unknown_or_unbounded_fields(change: dict[str, object]) -> None:
    assert MailSession.parse({**asdict(MailSession()), **change}) is None


def test_session_compare_and_swap_does_not_overwrite_a_newer_app(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    first, stale = MailSessionStore(state), MailSessionStore(state)
    assert first.load() is None and stale.load() is None
    assert first.save(MailSession(message_row=12))
    assert not stale.save(MailSession(message_row=2))
    current = MailSessionStore(state)
    assert current.load() == MailSession(message_row=12)
    assert current.save(MailSession(message_row=13))
    assert first.load() == MailSession(message_row=13)


def test_account_scopes_keep_session_positions_separate(tmp_path: Path) -> None:
    first = MailSessionStore(StateStore(tmp_path / "state.db", account_key="first"))
    second = MailSessionStore(StateStore(tmp_path / "state.db", account_key="second"))
    first.load()
    second.load()
    assert first.save(MailSession(focus="message-filter"))
    assert second.load() is None
    assert second.save(MailSession(focus="folder-tree"))
    assert first.load() == MailSession(focus="message-filter")


def test_session_read_failure_preserves_database_and_disables_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    original = b"synthetic corrupt state -- preserve this file"
    path.write_bytes(original)
    store = MailSessionStore(StateStore(path))
    assert store.load() is None
    assert not store.save(MailSession())
    assert path.read_bytes() == original


def test_malformed_session_is_ignored_without_replaying_actions(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.put_ui_preference(
        SESSION_KEY, {"screen": "confirm-send", "body": "private"}, scope=SESSION_SCOPE
    )
    store = MailSessionStore(state)
    assert store.load() is None
    assert store.save(MailSession())
    assert state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE).value == asdict(MailSession())


async def tab_to(pilot: object, app: GUCmailApp, widget_id: str) -> None:
    for _ in range(12):
        if app.focused is not None and app.focused.id == widget_id:
            return
        await pilot.press("tab")
    pytest.fail(f"Normal Tab navigation did not reach {widget_id}")


async def wait_for_position(app: GUCmailApp) -> None:
    async def completed() -> None:
        while app._pending_session_position is not None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(completed(), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_keyboard_position_survives_restart_without_loading_or_marking_mail(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    first = GUCmailApp(context_for(tmp_path), offline=True)
    async with first.run_test(size=size) as pilot:
        first._render_messages(summaries())
        await pilot.pause()
        await tab_to(pilot, first, "message-table")
        await pilot.press(*(["down"] * 25))
        await pilot.pause()
        table = first.query_one("#message-table", DataTable)
        row, position = table.cursor_row, int(table.scroll_y)
        assert row == 25 and position > 0
        assert first._message_detail is None
    second = GUCmailApp(context_for(tmp_path), offline=True)
    async with second.run_test(size=size) as pilot:
        second._render_messages(summaries())

        await wait_for_position(second)
        await pilot.pause()
        table = second.query_one("#message-table", DataTable)
        assert second.focused.id == "message-table"
        assert table.cursor_row == row and int(table.scroll_y) == position
        assert second._message_detail is None
        assert second._automatic_read_request is None
        assert second.gateway is None


@pytest.mark.asyncio
async def test_restart_clamps_a_smaller_page_and_does_not_restore_old_folder_position(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(MailSession(message_row=35, message_scroll=35))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._render_messages(summaries(3))
        await wait_for_position(app)
        await pilot.pause()
        assert app.query_one("#message-table", DataTable).cursor_row == 2
        assert app.query_one("#message-table", DataTable).scroll_y == 0
        app.ui.folder = "Projects"
        app._render_messages(summaries(40))
        await pilot.pause()
        assert app._pending_session_position is None
        assert app._message_detail is None


@pytest.mark.asyncio
async def test_modal_confirmation_and_body_are_not_restored(tmp_path: Path) -> None:
    first = GUCmailApp(context_for(tmp_path), offline=True)
    async with first.run_test(size=(80, 24)) as pilot:
        await tab_to(pilot, first, "message-filter")
        first.query_one("#message-filter", Input).value = "private unsaved query"
        first.push_screen(
            ConfirmationScreen(
                "Permanent delete synthetic?",
                "Synthetic pending delete; do not execute.",
                confirm_label="Delete permanently",
            )
        )
        await pilot.pause()
        assert isinstance(first.screen, ConfirmationScreen)
    record = first.context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
    assert "private" not in json.dumps(record.value)
    assert set(record.value) == set(asdict(MailSession()))
    second = GUCmailApp(context_for(tmp_path), offline=True)
    async with second.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(second.screen_stack) == 1
        assert second.focused.id == "message-filter"
        assert second.query_one("#message-filter", Input).value == ""
        assert second.gateway is None


@pytest.mark.asyncio
async def test_local_source_choice_is_restored_but_query_and_auto_run_are_not(
    tmp_path: Path,
) -> None:
    first = GUCmailApp(context_for(tmp_path), offline=True)
    async with first.run_test(size=(80, 24)) as pilot:
        first._open_local_search(mode=LocalSearchMode.ATTACHMENTS, query="private search")
        await pilot.pause()
        assert isinstance(first.screen, LocalSearchScreen)
        assert first.screen._active_worker is None
    second = GUCmailApp(context_for(tmp_path), offline=True)
    async with second.run_test(size=(80, 24)) as pilot:
        second._open_local_search()
        await pilot.pause()
        screen = second.screen
        assert isinstance(screen, LocalSearchScreen)
        assert screen.query_one("#local-search-mode", Select).value == "attachments"
        assert screen.query_one("#local-search-query", Input).value == ""
        assert screen._active_worker is None
        screen.configure(mode=LocalSearchMode.SIMILAR, reference="synthetic-message", run=False)
        await pilot.pause()
        assert second._last_search_scope == "attachments"


@pytest.mark.asyncio
async def test_final_session_write_is_awaited_off_the_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    main_thread = get_ident()
    calls: list[int] = []
    save = app._session_store.save

    def checked_save(session: MailSession) -> bool:
        calls.append(get_ident())
        return save(session)

    monkeypatch.setattr(app._session_store, "save", checked_save)
    async with app.run_test(size=(80, 24)) as pilot:
        await tab_to(pilot, app, "message-filter")
        assert calls == []
        app.exit()
        await asyncio.sleep(0)
    assert len(calls) == 1 and calls[0] != main_thread
    assert MailSessionStore(app.context.state).load().focus == "message-filter"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus,size,expected",
    [
        ("preview-scroll", (120, 35), "preview-scroll"),
        ("preview-scroll", (80, 24), "message-table"),
        ("preview-scroll", (70, 20), "message-table"),
        ("folder-tree", (70, 20), "message-table"),
    ],
)
async def test_hidden_restored_pane_falls_back_to_usable_message_list(
    tmp_path: Path, focus: str, size: tuple[int, int], expected: str
) -> None:
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(MailSession(focus=focus))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.focused.id == expected
        assert app.focused in app.screen.focus_chain
        assert app._message_detail is None and app.gateway is None


@pytest.mark.asyncio
async def test_empty_initial_page_does_not_replay_old_position_on_later_refresh(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(MailSession(message_row=35, message_scroll=35))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._render_messages(())
        assert app._pending_session_position is None
        app._render_messages(summaries())
        await pilot.pause()
        assert app.query_one("#message-table", DataTable).cursor_row == 0
        assert app._message_detail is None


@pytest.mark.asyncio
async def test_different_current_folder_does_not_restore_another_folders_position(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(MailSession(folder="Projects", message_row=35, message_scroll=35))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._render_messages(summaries())
        await wait_for_position(app)
        await pilot.pause()
        assert app.ui.folder == "inbox"
        assert app.query_one("#message-table", DataTable).cursor_row == 0
        assert app.query_one("#message-table", DataTable).scroll_y == 0
        assert app._message_detail is None


@pytest.mark.asyncio
async def test_normal_palette_reopening_preserves_source_without_running_search(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(MailSession(search_scope="attachments"))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press(*"Open Local Search")
        await pilot.pause()
        assert app.screen._interpretation.invocation.command_id == "search.search_all_data"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-mode", Select).value == "attachments"
        assert app.screen.query_one("#local-search-query", Input).value == ""
        assert app.screen._active_worker is None
        assert app.gateway is None
