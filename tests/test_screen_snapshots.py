"""Plain-text snapshots of the main screens, so layout changes show up in review.

Run with GUCMAIL_UPDATE_SNAPSHOTS=1 to write new snapshots after an intended change.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gucmail.bootstrap import build_context
from gucmail.models import Address, MessageSummary
from gucmail.tui.app import GUCmailApp
from test_compose_preferences import wait_for
from test_tui import context_for

SNAPSHOTS = Path(__file__).parent / "snapshots"
SIZE = (100, 30)
SENT = datetime(2025, 3, 4, 9, 30, tzinfo=UTC)


def _screen_text(app: GUCmailApp) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join(strip.text.rstrip() for strip in strips).rstrip() + "\n"


def _check(name: str, text: str) -> None:
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("GUCMAIL_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return
    if not path.exists():
        pytest.fail(f"No snapshot {path.name}. Run with GUCMAIL_UPDATE_SNAPSHOTS=1 to write it.")
    expected = path.read_text(encoding="utf-8")
    assert text == expected, f"{name} changed. Set GUCMAIL_UPDATE_SNAPSHOTS=1 if intended."


def _rows() -> tuple[MessageSummary, ...]:
    return (
        MessageSummary(
            "m1",
            "Week 6 lab moved to Thursday",
            Address("mona.adel@example.test", "Mona Adel"),
            SENT,
            is_read=False,
            has_attachments=True,
        ),
        MessageSummary(
            "m2",
            "Re: Thesis draft feedback",
            Address("karim.saleh@example.test", "Karim Saleh"),
            SENT.replace(day=2),
            is_read=True,
            flagged=True,
        ),
        MessageSummary(
            "m3",
            "Library hours during exams",
            Address("library@example.test", "Library"),
            SENT.replace(day=1),
            is_read=True,
        ),
    )


@pytest.mark.asyncio
async def test_sign_in_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CONFIG", "DATA", "CACHE", "LOG"):
        monkeypatch.setenv(f"GUCMAIL_{name}_DIR", str(tmp_path / name.lower()))
    app = GUCmailApp(context=build_context())
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: type(app.screen).__name__ == "SignInScreen")
        await pilot.pause(0.3)
        _check("sign_in", _screen_text(app))


@pytest.mark.asyncio
async def test_inbox_with_mail(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=SIZE) as pilot:
        app._render_messages(_rows())
        app.query_one("#message-table").focus()
        await pilot.pause(0.3)
        _check("inbox", _screen_text(app))


@pytest.mark.asyncio
async def test_settings(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=SIZE) as pilot:
        app._open_area("settings")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "SettingsScreen")
        await wait_for(pilot, lambda: not app.screen._render_pending)
        await pilot.pause(0.3)
        _check("settings", _screen_text(app))


@pytest.mark.asyncio
async def test_help(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("question_mark")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "HelpScreen")
        await pilot.pause(0.3)
        _check("help", _screen_text(app))


def test_list_time_is_short() -> None:
    from gucmail.formatting import list_time

    now = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    cases = {
        datetime(2026, 11, 17, 10, 42, tzinfo=UTC): "10:42",
        datetime(2026, 11, 16, 10, 42, tzinfo=UTC): "Yesterday",
        datetime(2026, 11, 13, 10, 42, tzinfo=UTC): "Fri 10:42",
        datetime(2026, 3, 4, 10, 42, tzinfo=UTC): "4 Mar",
        datetime(2025, 3, 4, 10, 42, tzinfo=UTC): "4 Mar 2025",
    }
    for value, expected in cases.items():
        assert list_time(value, timezone="UTC", now=now) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [100, 80])
async def test_composer(tmp_path: Path, width: int) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.press("c")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "ComposeScreen")
        await pilot.pause(0.3)
        _check(f"composer_{width}", _screen_text(app))


@pytest.mark.asyncio
async def test_send_later_asks_when_and_schedules(tmp_path: Path, monkeypatch) -> None:
    from textual.widgets import Input

    app = GUCmailApp(context_for(tmp_path), offline=True)
    calls: list[str] = []
    monkeypatch.setattr(
        app,
        "schedule_composer_draft",
        lambda screen, payload, *, expression: calls.append(expression),
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("c")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "ComposeScreen")
        app.screen.query_one("#compose-send-later").press()
        await wait_for(pilot, lambda: type(app.screen).__name__ == "DraftScheduleScreen")
        field = app.screen.query_one("#draft-schedule-expression", Input)
        field.value = "in 2 hours"
        await pilot.press("enter")
        await wait_for(pilot, lambda: calls == ["in 2 hours"])
