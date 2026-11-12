"""Initial data must size columns before being presented as a usable page."""

import asyncio

import pytest
from qa.synthetic.mailbox import LocalMailbox
from textual.widgets import DataTable

from gucmail.models import Address, ContactInfo, TaskInfo
from gucmail.tui.app import GUCmailApp
from test_tui import context_for


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["contacts", "tasks"])
async def test_first_populated_pim_columns_measure_data(tmp_path, area):
    context = context_for(tmp_path / "profile")
    gateway = LocalMailbox(tmp_path / "mailbox.sqlite")
    label = "University student 00001 [red] literal"
    for index in range(30):
        gateway.store(
            "contact" if area == "contacts" else "task",
            ContactInfo(f"con-{index}", label, emails=(Address("student@example.test"),))
            if area == "contacts"
            else TaskInfo(f"tsk-{index}", label, "not_started"),
        )
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_area(area)
        screen = app.screen
        await pilot.pause()
        table = screen.query_one("#" + area + "-table", DataTable)
        async with asyncio.timeout(15):
            while screen._operation_active or table.row_count == 0:
                await pilot.pause(0.01)
        await pilot.pause()
        column = table.columns["name" if area == "contacts" else "subject"]
        assert column.content_width >= len(label), {
            "width": column.content_width,
            "pending": table._require_update_dimensions,
            "new_rows": len(table._new_rows),
            "values": table.get_row_at(0),
        }
        assert label in table.render_line(table.header_height).text, {
            "line": table.render_line(table.header_height).text,
            "columns": [
                (str(key.value), value.content_width) for key, value in table.columns.items()
            ],
        }
