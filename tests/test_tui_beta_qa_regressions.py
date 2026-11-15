"""Regressions found while driving the mounted TUI against synthetic data (beta QA)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual.widgets import DataTable

from gucmail.models import Address, MessageSummary
from gucmail.tui.app import GUCmailApp
from test_tui import context_for
from waiting import wait_until


class PagedGateway:
    """Synthetic folder with more messages than one bounded page."""

    TOTAL = 120

    def __init__(self) -> None:
        self.offsets: list[int] = []

    def status(self):  # type: ignore[no-untyped-def]
        return {"exchange_version": "Synthetic Exchange", "inbox_count": self.TOTAL}

    def get_quota(self):  # type: ignore[no-untyped-def]
        return None

    def list_messages(  # type: ignore[no-untyped-def]
        self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
    ):
        self.offsets.append(offset)
        start = datetime(2026, 8, 5, 9, tzinfo=UTC)
        return [
            MessageSummary(
                reference=f"m-{index:04}",
                subject=f"Synthetic message {index:04}",
                sender=Address("teacher@example.test", "Course Team"),
                received_at=start - timedelta(minutes=index),
            )
            for index in range(offset, min(offset + limit, self.TOTAL))
        ]


async def _rows(pilot, table: DataTable, app: GUCmailApp, minimum: int = 1) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(15):
        while table.row_count < minimum or app.ui.busy:
            await asyncio.sleep(0.02)
    await pilot.pause()


def test_message_list_home_and_end_move_to_first_and_last_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = PagedGateway()
        app = GUCmailApp(context_for(tmp_path), gateway=gateway)
        async with app.run_test(size=(80, 24)) as pilot:
            table = app.query_one("#message-table", DataTable)
            await _rows(pilot, table, app)
            loaded = table.row_count
            table.focus()
            await pilot.press("end")
            await pilot.pause()
            assert table.cursor_row == loaded - 1
            # End on the last loaded row requests the next bounded page.
            await pilot.press("end")
            await _rows(pilot, table, app, minimum=loaded + 1)
            assert table.row_count > loaded
            await pilot.press("home")
            await pilot.pause()
            assert table.cursor_row == 0

    asyncio.run(scenario())


class _CancelledScheduler:
    def __init__(self, state: str) -> None:
        self.state = state

    def run_reference_if_due(self, reference: str):  # type: ignore[no-untyped-def]
        return None

    def get(self, reference: str):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from gucmail.models import SendState

        return SimpleNamespace(message=SimpleNamespace(state=SendState(self.state)))


def test_undo_send_countdown_replaces_stale_queued_status_after_cancel(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        app = GUCmailApp(context_for(tmp_path), gateway=PagedGateway())
        async with app.run_test(size=(100, 30)) as pilot:
            table = app.query_one("#message-table", DataTable)
            await _rows(pilot, table, app)
            monkeypatch.setattr(app, "_scheduler_service", lambda: _CancelledScheduler("cancelled"))
            app._set_status("Queued with a 10-second Undo Send delay. Cancel in Scheduled Mail.")
            app._finish_undo_delay("sch-synthetic")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = str(app.query_one("#status-bar").render())
            assert "Queued" not in status
            assert "cancelled" in status and "Nothing was sent" in status

    asyncio.run(scenario())


def test_scheduled_and_saved_search_actions_are_reachable_on_small_terminals(
    tmp_path: Path,
) -> None:
    from textual.widgets import Button

    from gucmail.tui.saved_search_screen import SavedSearchScreen
    from gucmail.tui.scheduled_screen import ScheduledScreen

    async def scenario() -> None:
        app = GUCmailApp(context_for(tmp_path), gateway=PagedGateway())
        async with app.run_test(size=(80, 24)) as pilot:
            table = app.query_one("#message-table", DataTable)
            await _rows(pilot, table, app)
            for area, screen_type in (
                ("scheduled", ScheduledScreen),
                ("saved_searches", SavedSearchScreen),
            ):
                app._open_area(area)
                async with asyncio.timeout(10):
                    while not isinstance(app.screen, screen_type):
                        await asyncio.sleep(0.02)
                await pilot.pause()
                for size in ((80, 24), (70, 20)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    for button in app.screen.query(Button):
                        if not button.display or button.disabled:
                            continue
                        button.focus()
                        # Focus scrolls the widget into view with a short animation.
                        await wait_until(
                            pilot,
                            lambda: app.screen.region.contains_region(button.region),  # noqa: B023 - wait_until runs it right away
                            interval=0.02,
                            pause_first=True,
                        )
                        assert app.screen.region.contains_region(button.region), (
                            area,
                            size,
                            button.id or str(button.label),
                            button.region,
                        )
                app.pop_screen()
                await pilot.pause()

    asyncio.run(scenario())


def test_palette_unavailable_command_shows_the_actual_reason(tmp_path: Path) -> None:
    from gucmail.commands import CommandInvocation
    from gucmail.nlp import InterpretationKind, NaturalLanguageResult, ParsedEntities
    from gucmail.tui.app import PaletteSubmission

    async def scenario() -> None:
        app = GUCmailApp(context_for(tmp_path), gateway=PagedGateway(), offline=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            invocation = CommandInvocation("calendar.open_today")
            app._execute_palette_submission(
                PaletteSubmission(
                    invocation,
                    NaturalLanguageResult(
                        "open today",
                        "open today",
                        InterpretationKind.COMMAND,
                        invocation,
                        (),
                        ParsedEntities(),
                        "synthetic",
                    ),
                )
            )
            await pilot.pause()
            status = str(app.query_one("#status-bar").render())
            assert status.startswith("Command unavailable: ")
            assert "Details omitted" not in status
            assert "CommandUnavailableError" not in status

    asyncio.run(scenario())


def test_mail_folder_tree_hides_only_explicit_non_mail_folder_classes() -> None:
    from gucmail.tui.app import _is_mail_folder_class

    assert _is_mail_folder_class("IPF.Note")
    assert _is_mail_folder_class("IPF.Note.OutlookHomepage")
    assert _is_mail_folder_class(None)  # unknown class: never hide possible mail
    for hidden in ("IPF.Appointment", "IPF.Contact", "IPF.Contact.RecipientCache",
                   "IPF.Task", "IPF.StickyNote", "IPF.Journal", "IPF.Configuration"):
        assert not _is_mail_folder_class(hidden), hidden
