"""Exercise the installed EWS queryset and payload builders with no transport."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from exchangelib import Account, Build, EWSDateTime, EWSTimeZone, Message, Version
from exchangelib.folders import Folder, MsgFolderRoot, Root
from exchangelib.properties import ParentFolderId
from exchangelib.services import FindFolder, FindItem

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, ItemNotFoundError
from gucmail.ews.adapter import SUMMARY_FIELDS, EWSGateway
from gucmail.models import SearchQuery

T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"


@pytest.fixture
def installed_mailbox(monkeypatch):
    # Bypass Account.__init__: even version/auth discovery is forbidden here.
    account = Account.__new__(Account)
    account.identity = SimpleNamespace(primary_smtp_address="synthetic@example.test")
    account._version = Version(build=Build(15, 1, 2507, 6))
    account.default_timezone = EWSTimeZone("UTC")
    account.protocol = SimpleNamespace(version=account._version, close=lambda: None)
    root = Root(account=account, id="owned-root", name="Synthetic Root")
    mailroot = MsgFolderRoot(
        root=root, id="owned-mail-root", name="Mailbox", folder_class="IPF.Root"
    )
    inbox = Folder(
        root=root,
        id="owned-inbox",
        name="Posteingang",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=mailroot.id),
    )
    sent = Folder(
        root=root,
        id="owned-sent",
        name="Gesendet",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=mailroot.id),
    )
    account.__dict__.update(root=root, msg_folder_root=mailroot, inbox=inbox)
    children = [inbox, sent]
    requests = []
    details = []
    data = {mailroot.id: []}
    for index, folder in enumerate(children):
        data[folder.id] = [
            Message(
                account=account,
                folder=folder,
                id=f"owned-{index}-{i}",
                changekey="synthetic-key",
                subject="Synthetic",
                datetime_received=EWSDateTime.from_datetime(
                    datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i * 2 + index)
                ),
                is_read=False,
                importance="Normal",
                size=100 + i,
            )
            for i in range(250)
        ]

    def find_folders(service, **kwargs):
        payload = service.get_payload(
            **{k: v for k, v in kwargs.items() if k != "max_items"}, page_size=service.page_size
        )
        requests.append(("FindFolder", kwargs, payload))
        return iter(children[: kwargs["max_items"]])

    def find_items(service, **kwargs):
        payload = service.get_payload(
            **{k: v for k, v in kwargs.items() if k != "max_items"}, page_size=service.page_size
        )
        requests.append(("FindItem", kwargs, payload))
        folder = kwargs["folders"][0]
        rows = sorted(data[folder.id], key=lambda item: item.datetime_received, reverse=True)
        start = kwargs["offset"]
        selected = rows[start : start + kwargs["max_items"]]
        return (
            iter((row.id, row.changekey) for row in selected)
            if kwargs["additional_fields"] is None
            else iter(selected)
        )

    def fetch(*, ids, only_fields, chunk_size):
        ids = list(ids)
        details.append((ids, tuple(only_fields), chunk_size))
        by_id = {row.id: row for rows in data.values() for row in rows}
        for identity, _key in ids:
            row = by_id[identity]
            if "body" in only_fields:
                row.body = "Synthetic body"
            yield row

    monkeypatch.setattr(FindFolder, "call", find_folders)
    monkeypatch.setattr(FindItem, "call", find_items)
    monkeypatch.setattr(account, "fetch", fetch)
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    return gateway, account, children, data, requests, details


def test_real_library_discovers_deep_scope_and_pushes_bounded_filter_pages(installed_mailbox):
    gateway, _account, _children, _data, requests, details = installed_mailbox
    rows = gateway.search_messages(
        SearchQuery(mailbox_scope=True, subject="Synthetic"), limit=3, offset=2
    )
    assert [gateway._memory_object(row.reference, "message")[1].id for row in rows] == [
        "owned-1-248",
        "owned-0-248",
        "owned-1-247",
    ]
    assert len(requests) == 4
    assert all(
        not {"body", "attachments", "mime_content"} & {field.field.name for field in fields}
        for _ids, fields, _size in details
    )
    _, kwargs, payload = requests[0]
    assert kwargs["depth"] == "Deep" and kwargs["max_items"] == 51 and kwargs["offset"] == 0
    assert payload.get("Traversal") == "Deep"
    assert payload.find(M + "IndexedPageFolderView").get("MaxEntriesReturned") == "51"
    for _operation, kwargs, payload in requests[1:]:
        assert kwargs["max_items"] == 5 and kwargs["offset"] == 0
        view = payload.find(M + "IndexedPageItemView")
        assert view.get("MaxEntriesReturned") == "5" and view.get("Offset") == "0"
        assert (
            payload.find(M + "Restriction" + "/" + T + "Contains" + "/" + T + "FieldURI").get(
                "FieldURI"
            )
            == "item:Subject"
        )
        # The library obtains IDs first when summary fields contain complex EWS fields.
        assert kwargs["additional_fields"] is None
    assert len(details) == 3
    assert all(
        {field.field.name for field in fields} == set(SUMMARY_FIELDS)
        for _ids, fields, _size in details
    )
    assert all(not row.is_read for row in rows)


def test_body_residual_fetches_only_global_200_candidates_in_one_batch(installed_mailbox):
    gateway, _account, _children, _data, requests, details = installed_mailbox
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True, text="body"), limit=2)
    assert len(rows) == 2
    body_details = [(ids, fields, size) for ids, fields, size in details if "body" in fields]
    assert len(body_details) == 1 and len(body_details[0][0]) == 200 and body_details[0][2] == 200
    assert "mime_content" not in body_details[0][1]
    # Free text includes attachment names; only the final candidate metadata is fetched.
    assert "attachments" in body_details[0][1]
    assert body_details[0][0][0][0] == "owned-1-249" and body_details[0][0][1][0] == "owned-0-249"
    assert [
        kwargs["max_items"] for operation, kwargs, _payload in requests if operation == "FindItem"
    ] == [200, 200, 200]
    assert all(
        kwargs["additional_fields"] is None
        for operation, kwargs, _payload in requests
        if operation == "FindItem"
    )


@pytest.mark.parametrize("broken", ["missing", "cycle", "duplicate"])
def test_invalid_real_folder_hierarchy_aborts_before_find_item(installed_mailbox, broken):
    gateway, _account, children, _data, requests, _details = installed_mailbox
    if broken == "missing":
        children[0].parent_folder_id = ParentFolderId(id="unreturned-parent")
    elif broken == "cycle":
        children[0].parent_folder_id = ParentFolderId(id=children[1].id)
        children[1].parent_folder_id = ParentFolderId(id=children[0].id)
    else:
        children.append(children[0])
    with pytest.raises(ConfigurationError):
        gateway.search_messages(SearchQuery(mailbox_scope=True))
    assert [operation for operation, _kwargs, _payload in requests] == ["FindFolder"]


@pytest.mark.parametrize("wrong", ["missing", "duplicate", "unexpected"])
def test_detail_identity_failure_never_mislabels_an_item(installed_mailbox, monkeypatch, wrong):
    gateway, account, _children, data, _requests, _details = installed_mailbox

    def fetch(**kwargs):
        ids = list(kwargs["ids"])
        by_id = {row.id: row for rows in data.values() for row in rows}
        rows = [by_id[identity] for identity, _key in ids]
        if "body" not in kwargs["only_fields"]:
            return iter(rows)
        if wrong == "missing":
            return iter(rows[:-1])
        if wrong == "duplicate":
            return iter([rows[0], rows[0], *rows[2:]])
        rows[0].id = "unexpected-id"
        return iter(rows)

    monkeypatch.setattr(account, "fetch", fetch)
    with pytest.raises((ConfigurationError, ItemNotFoundError)):
        gateway.search_messages(SearchQuery(mailbox_scope=True, text="body"), limit=2)
