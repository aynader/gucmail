from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from textual.app import App
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from gucmail.commands import CommandInvocation
from gucmail.errors import LocalStateError
from gucmail.help import HelpRepository
from gucmail.help.tutorials import (
    LESSONS,
    LessonProgress,
    TutorialProgress,
    check_exercise,
)
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp, HelpScreen
from gucmail.tui.tutorial_screen import TutorialScreen


def lesson(name):
    return next(item for item in LESSONS if item.lesson_id == name)


def test_all_required_lessons_have_packaged_guides_and_local_exercises():
    assert {item.lesson_id for item in LESSONS} == {
        "navigation",
        "first-message",
        "palette",
        "natural-language",
        "scheduling",
        "notifications",
        "backup",
    }
    repository = HelpRepository.load_default()
    for item in LESSONS:
        assert repository.get(item.help_page)
        assert len(item.steps) >= 2
        assert all(step.instruction and step.title for step in item.steps)


def test_composition_uses_real_validation_without_a_gateway_or_state(monkeypatch):
    from gucmail.services.compose import ComposeService

    def forbidden(*_args, **_kwargs):
        pytest.fail("Tutorial attempted transmission or draft storage")

    monkeypatch.setattr(ComposeService, "execute_send", forbidden)
    monkeypatch.setattr(ComposeService, "save_draft", forbidden)
    step = lesson("first-message").steps[0]
    result = check_exercise(step, value="Synthetic subject", body="Synthetic body")
    assert result.passed and "Nothing was saved or sent" in result.detail
    assert not check_exercise(step, value="", body="Synthetic body").passed
    assert not check_exercise(step, value="Synthetic subject", body="").passed
    assert not check_exercise(step, value="x" * 501, body="Synthetic body").passed
    assert not check_exercise(step, value="Synthetic subject", body="x" * 2001).passed


def test_command_practice_checks_real_extracted_fields_and_never_executes(monkeypatch):
    from gucmail.commands import CommandExecutor

    monkeypatch.setattr(CommandExecutor, "execute", lambda *_a, **_kw: pytest.fail("Dispatch"))
    palette = lesson("palette").steps[0]
    natural = lesson("natural-language").steps[0]
    assert check_exercise(palette, value="open help").passed
    assert not check_exercise(palette, value="delete these messages").passed
    result = check_exercise(natural, value="show unread mail from learner@example.invalid")
    assert result.passed and "Sender: learner@example.invalid" in result.detail
    assert "Preview only" in result.detail
    assert not check_exercise(natural, value="show read mail from learner@example.invalid").passed
    assert not check_exercise(natural, value="show unread mail from other@example.invalid").passed
    hostile = check_exercise(palette, value="delete \x1b[31mthese\x1b[0m")
    assert not hostile.passed and "\x1b" not in hostile.detail


def test_time_practice_exposes_absolute_zone_offset_and_rejects_ambiguous_or_past():
    step = lesson("scheduling").steps[0]
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    result = check_exercise(step, value="tomorrow 10 AM", timezone="UTC", now=now)
    assert result.passed and "2026-10-02T10:00:00+00:00" in result.detail
    assert "No job was created" in result.detail
    assert not check_exercise(step, value="03/04/2027", now=now).passed
    assert not check_exercise(step, value="2020-01-01", now=now).passed
    assert not check_exercise(step, value="tomorrow", timezone="Invalid/Zone", now=now).passed


def test_progress_resumes_isolated_lessons_and_accounts_without_practice_text(tmp_path):
    store = StateStore(tmp_path / "state.db", account_key="account-a")
    progress = TutorialProgress(store)
    progress.save(lesson("navigation"), LessonProgress(1))
    progress.save(lesson("palette"), LessonProgress(1, True))
    reloaded = TutorialProgress(StateStore(tmp_path / "state.db", account_key="account-a"))
    assert reloaded.load(lesson("navigation")) == LessonProgress(1)
    assert reloaded.load(lesson("palette")) == LessonProgress(1, True)
    assert reloaded.load(lesson("first-message")) == LessonProgress()
    other = TutorialProgress(StateStore(tmp_path / "state.db", account_key="account-b"))
    assert other.load(lesson("palette")) == LessonProgress()
    reloaded.save(lesson("palette"), LessonProgress())
    assert reloaded.load(lesson("navigation")) == LessonProgress(1)
    records = store.list_ui_preferences(scope="help.tutorials")
    assert all(set(record.value) == {"version", "step", "completed"} for record in records)


@pytest.mark.parametrize(
    "value",
    [
        {"version": 1, "step": -1, "completed": True},
        {"version": 1, "step": 500, "completed": True},
        {"version": 1, "step": True},
        {"version": 2, "step": 1, "completed": True},
        "unexpected shape",
    ],
)
def test_bad_progress_cannot_hide_or_complete_a_lesson(tmp_path, value):
    store = StateStore(tmp_path / "state.db")
    store.put_ui_preference("navigation", value, scope="help.tutorials")
    assert TutorialProgress(store).load(lesson("navigation")) == LessonProgress()


def test_progress_storage_failure_leaves_lessons_usable_in_memory(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")

    def failed(*_args, **_kwargs):
        raise LocalStateError("Synthetic unavailable database")

    monkeypatch.setattr(store, "get_ui_preference", failed)
    monkeypatch.setattr(store, "put_ui_preference", failed)
    progress = TutorialProgress(store)
    assert progress.load(lesson("navigation")) == LessonProgress()
    progress.save(lesson("navigation"), LessonProgress(1, True))
    assert progress.load(lesson("navigation")) == LessonProgress(1, True)
    assert "session" in progress.notice
    with pytest.raises(ValueError):
        progress.save(lesson("navigation"), LessonProgress(0, True))


@pytest.mark.parametrize(
    "column,value", [("value_json", "broken JSON"), ("updated_at", "invalid timestamp")]
)
def test_corrupt_sqlite_progress_does_not_prevent_lesson_start(tmp_path, column, value):
    store = StateStore(tmp_path / "state.db")
    store.put_ui_preference("navigation", {"version": 1, "step": 1}, scope="help.tutorials")
    statements = {
        "value_json": "UPDATE ui_preferences SET value_json=?",
        "updated_at": "UPDATE ui_preferences SET updated_at=?",
    }
    with store.transaction() as connection:
        connection.execute(statements[column], (value,))
    progress = TutorialProgress(store)
    assert progress.load(lesson("navigation")) == LessonProgress()
    assert "session" in progress.notice
    progress.save(lesson("navigation"), LessonProgress(1))
    assert TutorialProgress(store).load(lesson("navigation")) == LessonProgress(1)


def test_raw_sqlite_lock_error_falls_back_to_session_progress(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database locked")

    monkeypatch.setattr(store, "get_ui_preference", locked)
    monkeypatch.setattr(store, "put_ui_preference", locked)
    progress = TutorialProgress(store)
    assert progress.load(lesson("palette")) == LessonProgress()
    progress.save(lesson("palette"), LessonProgress(1, True))
    assert progress.load(lesson("palette")) == LessonProgress(1, True)


async def pass_current(screen, pilot):
    step = screen._lesson.steps[screen._position]
    if step.exercise == "navigation":
        screen.query_one("#tutorial-navigation", DataTable).focus()
        await pilot.press("home", "down", "enter")
    elif step.exercise == "choice":
        screen.query_one("#tutorial-choice", Select).value = step.correct
        await pilot.pause()
        assert await pilot.click("#tutorial-check")
    else:
        field = screen.query_one("#tutorial-input", Input)
        field.value = {
            "compose": "Synthetic subject",
            "palette": "open help",
            "natural-language": "show unread mail from learner@example.invalid",
            "time": "tomorrow 10 AM",
        }[step.exercise]
        if step.exercise == "compose":
            screen.query_one("#tutorial-body", TextArea).load_text("Synthetic body")
        field.focus()
        await pilot.pause()
        await pilot.press("enter")
    await pilot.pause()
    assert screen._passed


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [item.lesson_id for item in LESSONS])
async def test_each_lesson_completes_and_restarts_through_its_controls(name, tmp_path):
    store = StateStore(tmp_path / "state.db")
    screen = TutorialScreen(TutorialProgress(store), initial_lesson=name)
    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_next()  # Neither a skip nor Next without a valid check completes a step.
        assert screen._position == 0 and not screen._completed
        for position in range(len(screen._lesson.steps)):
            assert screen._position == position
            await pass_current(screen, pilot)
            assert await pilot.click("#tutorial-next")
            await pilot.pause()
        assert screen._completed
        assert TutorialProgress(store).load(lesson(name)) == LessonProgress(1, True)
        assert await pilot.click("#tutorial-restart")
        await pilot.pause()
        assert screen._position == 0 and not screen._completed
        assert TutorialProgress(store).load(lesson(name)) == LessonProgress()
        await pilot.press("escape")
        assert app.screen is not screen


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_compact_actions_resume_and_changed_input_invalidates_check(size, tmp_path):
    store = StateStore(tmp_path / "state.db")
    screen = TutorialScreen(TutorialProgress(store), initial_lesson="first-message")
    app = App()
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        for button in screen.query("#tutorial-actions Button"):
            assert button.region.y >= 0 and button.region.bottom <= screen.region.bottom
            assert button.region.right <= screen.region.right
        await pass_current(screen, pilot)
        screen.query_one("#tutorial-body", TextArea).load_text("")
        await pilot.pause()
        assert screen.query_one("#tutorial-next", Button).disabled
        screen.action_next()
        assert screen._position == 0
        await pass_current(screen, pilot)
        assert await pilot.click("#tutorial-next")
        await pilot.pause()
        assert screen._position == 1
        await pilot.press("escape")
        resumed = TutorialScreen(TutorialProgress(store), initial_lesson="first-message")
        await app.push_screen(resumed)
        await pilot.pause()
        assert resumed._position == 1 and not resumed._completed
        assert "Step 2" in str(resumed.query_one("#tutorial-progress", Static).render())


@pytest.mark.asyncio
async def test_help_and_palette_open_lessons_offline_and_guide_returns_to_progress(tmp_path):
    from test_tui import context_for

    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)

    def no_connection():
        pytest.fail("Tutorial tried to authenticate")

    app._ensure_gateway = no_connection
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._open_help(initial_page_id="tutorial.backup")
        await pilot.pause()
        assert await pilot.click("#help-tutorials")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TutorialScreen) and screen._lesson.lesson_id == "backup"
        assert await pilot.click("#tutorial-guide")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.navigator.current.page_id == "tutorial.backup"
        await pilot.press("escape")
        assert app.screen is screen
        await pilot.press("escape", "escape")
        app._palette_handlers()["help.tutorials"](
            CommandInvocation("help.tutorials"), app._palette_context()
        )
        await pilot.pause()
        assert isinstance(app.screen, TutorialScreen)
        assert app.screen._lesson.lesson_id == "navigation"
