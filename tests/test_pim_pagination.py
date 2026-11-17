"""Candidate continuation and offline cross-page search, without a live mailbox."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from exchangelib import Contact, Task
from exchangelib.folders import Contacts, Root, Tasks
from exchangelib.folders.collections import FolderCollection
from textual.app import App
from textual.widgets import Button, DataTable, Input, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.models import ContactInfo, TaskInfo
from gucmail.services.backup import BackupItem, BackupService
from gucmail.services.contacts import ContactQuery, ContactsService
from gucmail.services.pim import PimPage
from gucmail.services.tasks import TaskQuery, TaskService, TaskStatus
from gucmail.tui.pim_screens import (
    ContactsScreen,
    ContactsServiceBackend,
    TasksScreen,
    TasksServiceBackend,
)
from test_acceptance_ews_regressions import projected_gateway as _projected_gateway
from test_cli import _environment
from waiting import wait_until

projected_gateway = _projected_gateway


@pytest.mark.parametrize("kind", ["contacts", "tasks"])
def test_installed_provider_continues_after_empty_200_candidate_window(
    projected_gateway, monkeypatch, kind
):
    gateway = projected_gateway
    folder = (Contacts if kind == "contacts" else Tasks)(
        root=Root(account=gateway.account, id="root"), id=f"synthetic-{kind}"
    )
    monkeypatch.setattr(gateway, f"_{kind}_folder", lambda _: folder)
    rows = [
        Contact(
            account=gateway.account,
            id=f"contact-{n}",
            display_name=f"Contact {n}",
            body="needle" if n == 200 else "other",
        )
        if kind == "contacts"
        else Task(
            account=gateway.account,
            id=f"task-{n}",
            subject=f"Task {n}",
            status="Deferred" if n == 200 else "NotStarted",
            percent_complete=Decimal(0),
        )
        for n in range(201)
    ]
    requests = []
    projections = []

    def find_items(_collection, *args, **kwargs):
        requests.append(kwargs)
        offset = kwargs["offset"]
        assert kwargs["max_items"] <= 200 and kwargs["page_size"] <= 200
        return rows[offset : offset + kwargs["max_items"]]

    def fetch(*, ids, **kwargs):
        projections.append({field.field.name for field in kwargs["only_fields"]})
        return iter(ids)

    monkeypatch.setattr(FolderCollection, "find_items", find_items)
    monkeypatch.setattr(gateway.account, "fetch", fetch)
    service = ContactsService(gateway) if kind == "contacts" else TaskService(gateway)
    query = (
        ContactQuery(text="needle") if kind == "contacts" else TaskQuery(status=TaskStatus.DEFERRED)
    )
    first = service.page(query, limit=50)
    assert first.items == () and first.scanned == first.next_offset == 200
    second = service.page(query, limit=50, offset=first.next_offset)
    assert len(second.items) == second.scanned == 1 and second.next_offset is None
    assert [request["offset"] for request in requests] == [0, 200]
    assert all("attachments" not in projection for projection in projections)
    assert all(("body" in projection) == (kind == "contacts") for projection in projections)


def test_early_result_limit_does_not_skip_prefetched_contact_candidates(
    projected_gateway, monkeypatch
):
    gateway = projected_gateway
    folder = Contacts(root=Root(account=gateway.account, id="root"), id="contacts")
    monkeypatch.setattr(gateway, "_contacts_folder", lambda _: folder)
    rows = [
        Contact(account=gateway.account, id=f"c-{n}", display_name=f"Needle {n}") for n in range(6)
    ]
    offsets = []

    def find_items(_collection, *args, **kwargs):
        offsets.append(kwargs["offset"])
        return rows[kwargs["offset"] : kwargs["offset"] + kwargs["max_items"]]

    monkeypatch.setattr(FolderCollection, "find_items", find_items)
    monkeypatch.setattr(gateway.account, "fetch", lambda *, ids, **_: iter(ids))
    first = gateway.search_contacts_page(
        ContactQuery(text="Needle"), folder_reference=None, limit=1
    )
    second = gateway.search_contacts_page(
        ContactQuery(text="Needle"), folder_reference=None, limit=1, offset=first.next_offset
    )
    assert [first.items[0].display_name, second.items[0].display_name] == ["Needle 0", "Needle 1"]
    assert offsets == [0, 1]


def test_offline_search_filters_before_offset_and_cancels_between_decodes(tmp_path, monkeypatch):
    service = BackupService(tmp_path / "backup")
    service.create(
        tuple(
            BackupItem(
                reference=f"c-{n}",
                kind="contact",
                data={
                    "notes": ("x" * 100_100 + "needle") if n in {2, 4} else "other",
                },
            )
            for n in range(6)
        ),
        backup_id="synthetic",
    )
    archive = service.open_offline("synthetic")
    assert [item.reference for item in archive.list_items(query="needle", offset=1, limit=1)] == [
        "c-4"
    ]
    decoded = []
    original = archive._decode_item

    def decode(*args):
        decoded.append(args[-1].item_key)
        return original(*args)

    monkeypatch.setattr(archive, "_decode_item", decode)
    assert archive.list_items(query="missing", cancelled=lambda: len(decoded) >= 2) == ()
    assert len(decoded) == 2


class PagedGateway:
    def __init__(self):
        self.offsets = []
        self.queries = []

    def search_contacts_page(self, query, *, folder_reference, limit, offset=0):
        self.offsets.append(offset)
        self.queries.append(query)
        return (
            PimPage((), offset, 200, 200, "Local text matching.")
            if offset == 0
            else PimPage((ContactInfo(reference="c-later", display_name="Later match"),), offset, 1)
        )

    def search_tasks_page(self, query, *, folder_reference, limit, offset=0):
        self.offsets.append(offset)
        self.queries.append(query)
        return (
            PimPage((), offset, 200, 200, "Local status matching.")
            if offset == 0
            else PimPage(
                (TaskInfo(reference="t-later", subject="Later match", status="deferred"),),
                offset,
                1,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["contacts", "tasks"])
@pytest.mark.parametrize("size", [(100, 35), (80, 24), (70, 20)])
async def test_ui_empty_page_still_offers_next_and_changed_query_resets_offset(kind, size):
    gateway = PagedGateway()
    screen = (
        ContactsScreen(ContactsServiceBackend(ContactsService(gateway)))
        if kind == "contacts"
        else TasksScreen(TasksServiceBackend(TaskService(gateway)))
    )
    app = App()
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await wait_until(
            pilot,
            lambda: gateway.offsets and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        table = screen.query_one(f"#{kind}-table", DataTable)
        next_button = screen.query_one(f"#{kind}-next", Button)
        assert table.row_count == 0 and not next_button.disabled
        assert next_button.region.bottom <= screen.size.height
        assert next_button.region.y >= 0
        state = screen.query_one("#area-state", Static)
        assert "more on the next page" in str(state.render())
        assert "later candidates" in str(state.tooltip)
        next_button.press()
        await wait_until(
            pilot,
            lambda: table.row_count == 1 and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert table.row_count == 1 and next_button.disabled, (
            gateway.offsets,
            screen.query_one("#area-state", Static).render(),
        )
        assert gateway.offsets[-1] == 200
        assert not screen.query_one(f"#{kind}-prev", Button).disabled
        screen.query_one(f"#{kind}-prev", Button).press()
        await wait_until(
            pilot,
            lambda: gateway.offsets[-1] == 0 and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert table.row_count == 0
        # A changed query must not inherit the previous query's Next offset.
        screen.query_one(f"#{kind}-search", Input).value = "new query"
        next_button.press()
        await wait_until(
            pilot,
            lambda: gateway.queries[-1].text == "new query" and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert gateway.offsets[-1] == 0 and gateway.queries[-1].text == "new query"
        assert screen.query_one(f"#{kind}-prev", Button).disabled


@pytest.mark.parametrize("kind", ["contacts", "tasks"])
def test_cli_page_continuation_is_structured_and_keeps_default_array(tmp_path, monkeypatch, kind):
    gateway = PagedGateway()
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: gateway)
    args = ["--json", kind, "search", "needle", "--page"]
    first = CliRunner().invoke(cli.app, args, env=_environment(tmp_path))
    assert first.exit_code == 0, first.output
    payload = json.loads(first.stdout)
    assert payload["data"]["items"] == [] and payload["data"]["next_offset"] == 200
    second = CliRunner().invoke(cli.app, [*args, "--offset", "200"], env=_environment(tmp_path))
    assert second.exit_code == 0, second.output
    assert len(json.loads(second.stdout)["data"]["items"]) == 1
    plain = CliRunner().invoke(cli.app, [*args[:-1], "--offset", "200"], env=_environment(tmp_path))
    assert plain.exit_code == 0, plain.output
    assert isinstance(json.loads(plain.stdout)["data"], list)


def test_fuzzy_contacts_use_contact_names_and_preserve_specific_filters(tmp_path, monkeypatch):
    gateway = PagedGateway()
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: gateway)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "contacts",
            "search",
            "Later match",
            "--fuzzy",
            "--company",
            "Example",
            "--offset",
            "200",
            "--page",
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert gateway.queries[-1] == ContactQuery(company="Example")
    assert len(json.loads(result.stdout)["data"]["items"]) == 1
