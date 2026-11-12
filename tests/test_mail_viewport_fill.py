from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from textual import events
from textual.widgets import DataTable, Input

from gucmail.models import Address, MessageSummary
from gucmail.tui.app import GUCmailApp
from test_tui import FakeGateway, context_for


class ManyMessagesGateway(FakeGateway):
    def __init__(self, count: int = 300) -> None:
        self.count = count
        self.limits: list[int] = []
        self.offsets: list[int] = []

    def list_messages(
        self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
    ) -> list[MessageSummary]:
        self.limits.append(limit)
        self.offsets.append(offset)
        return [
            MessageSummary(
                reference=f"message-{index}",
                subject=f"Synthetic message {index}",
                sender=Address("sender@example.test", "Synthetic sender"),
                received_at=datetime(2026, 10, 1, tzinfo=UTC),
                is_read=False,
            )
            for index in range(offset, min(offset + limit, self.count))
        ]


async def wait_for_mail(pilot, app: GUCmailApp, count: int) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if len(app._messages) >= count and app.ui.busy is None:
            return
    pytest.fail(f"Expected {count} messages; got {len(app._messages)}")


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [24, 70, 240])
async def test_initial_page_fills_viewport_with_bounded_request(
    tmp_path: Path, height: int
) -> None:
    gateway = ManyMessagesGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, height)) as pilot:
        await wait_for_mail(pilot, app, 25)
        table = app.query_one("#message-table", DataTable)
        expected = min(200, max(25, table.content_size.height - table.header_height))
        assert gateway.limits == [expected]
        assert table.row_count == expected


@pytest.mark.asyncio
async def test_resize_expands_once_and_preserves_selection(tmp_path: Path) -> None:
    gateway = ManyMessagesGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for_mail(pilot, app, 25)
        app.ui.selected_reference = "message-5"
        await pilot.resize_terminal(120, 55)
        await pilot.resize_terminal(120, 70)
        await wait_for_mail(pilot, app, 50)
        assert len(gateway.limits) == 2
        assert app.ui.selected_reference == "message-5"
        await pilot.resize_terminal(120, 35)
        await pilot.pause(1)
        assert len(gateway.limits) == 2


@pytest.mark.asyncio
async def test_resize_does_not_reload_short_or_filtered_page(tmp_path: Path) -> None:
    gateway = ManyMessagesGateway(count=10)
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for_mail(pilot, app, 10)
        await pilot.resize_terminal(120, 70)
        await pilot.pause(1)
        assert gateway.limits == [25]

    gateway = ManyMessagesGateway()
    app = GUCmailApp(context_for(tmp_path / "filtered"), gateway=gateway)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for_mail(pilot, app, 25)
        app.query_one("#message-filter", Input).value = "Synthetic"
        await pilot.pause()
        await pilot.resize_terminal(120, 70)
        await pilot.pause(1)
        assert gateway.limits == [25]


@pytest.mark.asyncio
async def test_fill_can_be_disabled(tmp_path: Path) -> None:
    gateway = ManyMessagesGateway()
    context = context_for(tmp_path)
    GUCmailApp(context, gateway=gateway)._settings_service().set("mail.fill_viewport", False)
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(120, 70)) as pilot:
        await wait_for_mail(pilot, app, 25)
        assert gateway.limits == [25]


@pytest.mark.asyncio
async def test_keyboard_loads_more_at_end_and_preserves_selection(tmp_path: Path) -> None:
    gateway = ManyMessagesGateway(count=60)
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for_mail(pilot, app, 25)
        table = app.query_one("#message-table", DataTable)
        table.focus()
        await pilot.press("ctrl+end")
        await wait_for_mail(pilot, app, 50)
        assert gateway.offsets == [0, 25]
        assert app.ui.selected_reference == "message-24"
        await pilot.press("down")
        assert app.ui.selected_reference == "message-25"
        await pilot.press("ctrl+end")
        await wait_for_mail(pilot, app, 60)
        assert gateway.offsets == [0, 25, 50]
        await pilot.press("ctrl+end")
        await pilot.pause(0.3)
        assert gateway.offsets == [0, 25, 50]


@pytest.mark.asyncio
async def test_mouse_wheel_loads_more_from_bottom(tmp_path: Path) -> None:
    gateway = ManyMessagesGateway(count=120)
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 70)) as pilot:
        await wait_for_mail(pilot, app, 25)
        table = app.query_one("#message-table", DataTable)
        first_page = table.row_count
        table.post_message(events.MouseScrollDown(table, 1, 1, 0, 1, 0, False, False, False))
        await wait_for_mail(pilot, app, min(120, first_page * 2))
        assert gateway.offsets == [0, first_page]
        if table.row_count < 120:
            await pilot.pause()
            table.scroll_end(animate=False)
            await pilot.pause()
            table.post_message(events.MouseScrollDown(table, 1, 1, 0, 1, 0, False, False, False))
            await wait_for_mail(pilot, app, 120)
            assert gateway.offsets == [0, first_page, first_page * 2]
        assert table.row_count == 120


@pytest.mark.asyncio
async def test_filter_change_during_load_does_not_replace_filtered_view(tmp_path: Path) -> None:
    started, finish = Event(), Event()

    class SlowGateway(ManyMessagesGateway):
        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ) -> list[MessageSummary]:
            if offset:
                started.set()
                assert finish.wait(5)
            return super().list_messages(
                folder=folder, limit=limit, offset=offset, order_by=order_by
            )

    gateway = SlowGateway(count=60)
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for_mail(pilot, app, 25)
        app.query_one("#message-table", DataTable).focus()
        await pilot.press("ctrl+end")
        for _ in range(50):
            await pilot.pause(0.02)
            if started.is_set():
                break
        assert started.is_set()
        app.query_one("#message-filter", Input).value = "message 2"
        await pilot.pause()
        finish.set()
        for _ in range(50):
            await pilot.pause(0.02)
            if app.ui.busy is None:
                break
        assert app.ui.busy is None
        assert len(app._messages) == 25
        assert app.query_one("#message-table", DataTable).row_count == 6
