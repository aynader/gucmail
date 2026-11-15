from __future__ import annotations

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static

from gucmail.services.backup import BackupItem, BackupService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.backup_screen import BackupPassphraseScreen
from gucmail.tui.offline_pim_screen import OfflinePimScreen
from test_settings_secondpass import _context
from waiting import wait_until


def _items():
    return tuple(
        BackupItem(
            reference=f"{kind}-{number}",
            kind=kind,
            folder_path=kind.title(),
            data={
                "display_name" if kind == "contact" else "subject": f"Saved {kind} {number}",
                "notes": f"Unique detail {number}",
                "untrusted": "\x1b[31m[bold]<script>bad()</script>",
            },
        )
        for kind in ("calendar", "contact", "task")
        for number in range(3)
    )


def test_offline_archive_zero_limit_and_key_lookup_decode_only_target(tmp_path, monkeypatch):
    service = BackupService(tmp_path / "backups")
    service.create(_items(), backup_id="saved-pim")
    archive = service.open_offline("saved-pim")
    target = archive.list_items(kind="task")[-1]
    decoded = []
    original = archive._decode_item

    def decode(*args):
        decoded.append(args[-1].item_key)
        return original(*args)

    monkeypatch.setattr(archive, "_decode_item", decode)
    assert archive.list_items(limit=0) == ()
    assert decoded == []
    assert archive.get_item(target.item_key).reference == target.reference
    assert decoded == [target.item_key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "area,kind", [("calendar", "calendar"), ("contacts", "contact"), ("tasks", "task")]
)
async def test_offline_pim_reopen_browse_details_filter_paginate_and_resize(
    tmp_path, monkeypatch, area, kind
):
    context = _context(tmp_path)
    BackupService(context.paths.backup_dir).create(_items(), backup_id="saved-pim")
    app = GUCmailApp(context, offline=True)

    def forbidden():
        raise AssertionError("Offline PIM must never resolve an Exchange gateway")

    monkeypatch.setattr(app, "_ensure_gateway", forbidden)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_area(area)
        await wait_until(
            pilot,
            lambda: (
                (isinstance(app.screen, OfflinePimScreen) and app.screen.is_mounted)
                and app.screen._backups
            ),
            interval=0.025,
            pause_first=True,
        )
        screen = app.screen
        assert isinstance(screen, OfflinePimScreen)
        screen.page_size = 2
        screen.query_one("#offline-pim-backup", Select).value = "saved-pim"
        await wait_until(pilot, lambda: len(screen._items) == 2, interval=0.025, pause_first=True)
        table = screen.query_one("#offline-pim-table", DataTable)
        assert table.row_count == 2
        assert {item.kind for item in screen._items.values()} == {kind}
        assert "saved-pim" in str(screen.query_one("#offline-pim-source", Static).render())
        table.focus()
        await pilot.press("down", "up")
        detail = str(screen.query_one("#offline-pim-detail", Static).render())
        assert "Unique detail" in detail
        assert "\x1b" not in detail
        assert '"data": {' not in detail
        raw = screen.query_one("#offline-pim-raw", Button)
        raw.press()
        await pilot.pause()
        assert '"data": {' in str(screen.query_one("#offline-pim-detail", Static).render())
        raw.press()
        await pilot.pause()
        assert '"data": {' not in str(screen.query_one("#offline-pim-detail", Static).render())
        # Search now runs across the archive in a cancellable worker. The match
        # is deliberately beyond the initial two-row page.
        screen.query_one("#offline-pim-filter", Input).value = "detail 2"
        await wait_until(
            pilot,
            lambda: table.row_count == 1 and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert table.row_count == 1
        assert next(iter(screen._items.values())).reference == f"{kind}-2"
        screen.query_one("#offline-pim-filter", Input).value = ""
        await wait_until(
            pilot,
            lambda: table.row_count == 2 and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert table.row_count == 2
        screen.query_one("#offline-pim-next", Button).press()
        await wait_until(pilot, lambda: len(screen._items) == 1, interval=0.025, pause_first=True)
        assert table.row_count == 1
        assert screen.query_one("#offline-pim-next", Button).disabled
        await pilot.resize_terminal(70, 24)
        await pilot.press("escape")
        assert screen.archive is None
        app._open_area(area)
        await wait_until(
            pilot,
            lambda: (
                (isinstance(app.screen, OfflinePimScreen) and app.screen.is_mounted)
                and app.screen._backups
            ),
            interval=0.025,
            pause_first=True,
        )
        assert "saved-pim" in app.screen._backups


@pytest.mark.asyncio
async def test_encrypted_offline_pim_cancel_then_unlock(tmp_path):
    context = _context(tmp_path)
    BackupService(context.paths.backup_dir).create(
        _items(), backup_id="encrypted-pim", passphrase="synthetic-only"
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_area("contacts")
        await wait_until(
            pilot,
            lambda: (
                (isinstance(app.screen, OfflinePimScreen) and app.screen.is_mounted)
                and app.screen._backups
            ),
            interval=0.025,
            pause_first=True,
        )
        screen = app.screen
        select = screen.query_one("#offline-pim-backup", Select)
        select.value = "encrypted-pim"
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, BackupPassphraseScreen) and app.screen.is_mounted,
            interval=0.025,
            pause_first=True,
        )
        assert isinstance(app.screen, BackupPassphraseScreen)
        await pilot.press("escape")
        assert screen.archive is None and not screen._items
        select.value = Select.BLANK
        await pilot.pause()
        select.value = "encrypted-pim"
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, BackupPassphraseScreen) and app.screen.is_mounted,
            interval=0.025,
            pause_first=True,
        )
        app.screen.dismiss("synthetic-only")
        await wait_until(pilot, lambda: screen._items, interval=0.025, pause_first=True)
        assert len(screen._items) == 3
        assert screen.archive.read_only
