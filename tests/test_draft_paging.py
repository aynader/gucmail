"""Bounded persisted draft reads, Unicode filtering and actual CLI/TUI continuation."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from textual.widgets import Button, Collapsible, DataTable, Input, Static
from typer.testing import CliRunner

import gucmail.cli as cli
import gucmail.services.drafts as draft_module
from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.models import Address, DraftData, DraftLocation
from gucmail.services.drafts import DraftService, draft_content_hash, draft_to_dict
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from test_tui import context_for


def populate(service, count):
    """Bulk seed synthetic persisted records, then exercise production readers."""
    service.state.initialize()
    now = datetime(2026, 10, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        updated = now - timedelta(minutes=index // 2)  # Stable tie ordering matters.
        draft = DraftData(
            reference=f"dft-{index:05d}",
            subject=f"Course {index:05d} Straße [red] طالب",
            body_text="Local body " + "x" * 2048,
            to=[Address(f"student{index}@example.test")],
            cc=[Address("研究@example.test")],
            bcc=[Address("hidden@example.test")],
            updated_at=updated,
            location=DraftLocation.LOCAL if index % 2 == 0 else DraftLocation.SERVER,
        )
        rows.append(
            (
                draft.reference,
                json.dumps(draft_to_dict(draft)),
                1,
                draft.location.value,
                draft_content_hash(draft),
                now.isoformat(),
                updated.isoformat(),
            )
        )
    with service.state.transaction(immediate=True) as connection:
        connection.executemany(
            "INSERT INTO local_drafts(reference,draft_json,revision,state,content_hash,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            rows,
        )


def test_5000_drafts_only_deserializes_returned_page_and_reopens(tmp_path, monkeypatch):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    populate(service, 5_000)
    original = draft_module.draft_from_dict
    decoded = []

    def tracked(value):
        decoded.append(value["reference"])
        return original(value)

    monkeypatch.setattr(draft_module, "draft_from_dict", tracked)
    first = service.list_page(limit=25)
    last = service.list_page(limit=25, offset=4975)
    assert first.total == last.total == 5000
    assert first.next_offset == 25 and last.next_offset is None
    assert len(decoded) == 50
    assert [item.reference for item in first.items] == [f"dft-{i:05d}" for i in range(25)]
    assert [item.reference for item in last.items] == [f"dft-{i:05d}" for i in range(4975, 5000)]
    reopened = DraftService(StateStore(service.state.path), tmp_path / "recovery")
    assert reopened.list_page(limit=25, offset=4975) == last
    with service.state.connection() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT draft_json FROM local_drafts "
            "ORDER BY updated_at DESC, reference LIMIT 25 OFFSET 4975"
        ).fetchall()
    assert any("idx_local_drafts_recent" in str(tuple(row)) for row in plan)


@pytest.mark.parametrize(
    "query", ["STRASSE", "研究", "hidden@example.test", "[red]", "local body", "%", "x' OR 1=1 --"]
)
def test_draft_page_matches_legacy_literal_search(tmp_path, query):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    populate(service, 12)
    expected = service.search(query)
    page = service.list_page(query=query, limit=100)
    assert list(page.items) == expected
    assert page.total == len(expected)
    filtered = service.list_page(query=query, locations=(DraftLocation.SERVER,), limit=3)
    expected = [item for item in expected if item.location is DraftLocation.SERVER]
    assert list(filtered.items) == expected[:3] and filtered.total == len(expected)


def test_draft_page_bounds_cancellation_age_and_corruption(tmp_path):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    populate(service, 8)
    for options in (
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"offset": 1 << 70},
        {"query": "x" * 4097},
    ):
        with pytest.raises(ConfigurationError):
            service.list_page(**options)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(LocalStateError, match="cancelled"):
        service.list_page(cancel=cancelled)
    page = service.list_page(updated_before=datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
    assert page.total == 4
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE local_drafts SET draft_json='invalid' WHERE reference='dft-00000'"
        )
    with pytest.raises(LocalStateError):
        service.list_page(query="course")
    # A corrupt unrelated page does not force deserialization during normal paging.
    assert len(service.list_page(offset=2).items) == 6


def test_v13_draft_paging_upgrade_preserves_content_and_revisions(tmp_path):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    saved = service.create(DraftData("dft-upgrade", subject="Before", body_text="Unsaved work"))
    saved = service.edit(saved.reference, subject="After")
    revisions = service.revisions(saved.reference)
    with service.state.transaction() as connection:
        connection.execute("DROP INDEX idx_local_drafts_recent")
        connection.execute("DROP INDEX idx_local_drafts_location_recent")
        connection.execute("DELETE FROM schema_migrations WHERE version=14")
    reopened = DraftService(StateStore(service.state.path), tmp_path / "recovery")
    assert reopened.list_page().items == (saved,)
    assert reopened.revisions(saved.reference) == revisions
    assert reopened.read_recovery(saved.reference) == saved


def test_cancellation_interrupts_sql_scan_and_releases_snapshot(tmp_path):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    populate(service, 200)

    class CancelDuringScan(Event):
        checks = 0

        def is_set(self):
            self.checks += 1
            if self.checks == 3:
                self.set()
            return super().is_set()

    cancel = CancelDuringScan()
    with pytest.raises(LocalStateError, match="cancelled"):
        service.list_page(query="absent substring", cancel=cancel)
    assert cancel.checks >= 3
    assert service.list_page().total == 200


def test_cli_draft_pages_keep_json_items_contract(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    service = DraftService(context.state, context.paths.recovery_dir)
    populate(service, 57)
    monkeypatch.setattr(cli.core, "build_context", lambda **kwargs: context)
    runner = CliRunner()
    first = runner.invoke(cli.app, ["--json", "--non-interactive", "drafts", "list"])
    second = runner.invoke(
        cli.app,
        ["--json", "--non-interactive", "drafts", "list", "--limit", "25", "--offset", "25"],
    )
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    a, b = json.loads(first.output)["data"], json.loads(second.output)["data"]
    assert len(a) == len(b) == 25 and a[0]["reference"] == "dft-00000"
    assert b[0]["reference"] == "dft-00025"
    bad = runner.invoke(cli.app, ["drafts", "list", "--offset", "-1"])
    assert bad.exit_code == 2


async def settled(pilot, screen):
    async with asyncio.timeout(15):
        while (
            screen._operation_active or screen.query_one("#draft-table", DataTable).row_count == 0
        ):
            await pilot.pause(0.01)
    await pilot.pause()


@pytest.mark.asyncio
async def test_tui_draft_continuation_refresh_filter_and_shrinking_last_page(tmp_path):
    context = context_for(tmp_path)
    service = DraftService(context.state, context.paths.recovery_dir)
    populate(service, 57)
    backend = ServiceDraftAttachmentBackend(service, online=False)
    screen = DraftAttachmentScreen(backend, page_size=25, offline=True)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await settled(pilot, screen)
        table = screen.query_one("#draft-table", DataTable)
        assert table.row_count == 25 and screen._draft_next_offset == 25
        await pilot.click("#draft-next")
        await settled(pilot, screen)
        assert screen._draft_offset == 25 and "dft-00025" in screen._drafts
        table.move_cursor(row=5)
        await pilot.pause()
        selected = screen.selected_draft_reference
        screen.action_refresh()
        await settled(pilot, screen)
        assert screen.selected_draft_reference == selected
        await pilot.click("#draft-next")
        await settled(pilot, screen)
        assert table.row_count == 7 and screen._draft_offset == 50
        assert screen.query_one("#draft-next", Button).disabled
        with service.state.transaction() as connection:
            connection.execute("DELETE FROM local_drafts WHERE reference >= 'dft-00050'")
        screen.action_refresh()
        await settled(pilot, screen)
        assert screen._draft_offset == 25 and table.row_count == 25
        screen.query_one("#draft-filter", Input).value = "00003"
        screen.action_refresh()
        await settled(pilot, screen)
        assert screen._draft_offset == 0 and tuple(screen._drafts) == ("dft-00003",)
        assert screen.query_one("#draft-previous", Button).disabled
        with service.state.transaction() as connection:
            connection.execute(
                "UPDATE local_drafts SET draft_json='invalid' WHERE reference='dft-00000'"
            )
        screen.action_refresh()
        async with asyncio.timeout(15):
            while screen._operation_active:
                await asyncio.sleep(0.005)
        await pilot.pause()
        assert not screen._drafts and table.row_count == 0
        assert screen.selected_draft_reference is None
        assert screen.query_one("#draft-open", Button).disabled
        assert "could not be loaded" in str(screen.query_one("#draft-detail", Static).content)


@pytest.mark.asyncio
async def test_compact_drafts_keep_paging_and_core_actions_visible(tmp_path):
    context = context_for(tmp_path)
    service = DraftService(context.state, context.paths.recovery_dir)
    populate(service, 57)
    screen = DraftAttachmentScreen(
        ServiceDraftAttachmentBackend(service, online=False),
        page_size=25,
        offline=True,
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await settled(pilot, screen)
        details = screen.query_one("#draft-details", Collapsible)
        assert details.collapsed
        search = screen.query_one("#draft-filter", Input)
        assert search.content_region.height == 1
        assert "Search subject" in search.render_line(0).text
        search.focus()
        await pilot.pause()
        assert search.content_region.height == 1
        for selector in ("#draft-next", "#draft-create", "#draft-open", "#draft-delete"):
            button = screen.query_one(selector, Button)
            assert button.region.height and screen.region.contains_region(button.region), {
                "control": selector,
                "region": button.region,
            }
        await pilot.click("#draft-details CollapsibleTitle")
        await pilot.pause()
        assert not details.collapsed
        screen.query_one("#draft-details CollapsibleTitle").focus()
        await pilot.press("enter")
        assert details.collapsed
        await pilot.press("enter")
        assert not details.collapsed
        await pilot.resize_terminal(82, 25)
        assert not details.collapsed, "A same-layout resize must preserve the user's disclosure"
        await pilot.resize_terminal(120, 36)
        assert not details.collapsed
