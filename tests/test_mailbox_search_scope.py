"""Mailbox search must preserve its scope through the provider, not only the UI."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gucmail.commands import CommandInvocation
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, NetworkError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import SearchQuery, SearchRanking
from gucmail.services.messages import MessageService
from gucmail.services.search import build_search_plan
from gucmail.tui.app import GUCmailApp
from gucmail.tui.palette_routes import catalog_route
from test_ui_session import context_for


def remote_ids(gateway, rows):
    return [gateway._id(gateway._memory_object(row.reference, "message")[1]) for row in rows]


def message(identity: str, day: int, *, size: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        id=identity,
        changekey="synthetic-key",
        subject="Synthetic scope term",
        sender=None,
        author=None,
        datetime_received=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=day),
        datetime_sent=None,
        is_read=False,
        has_attachments=False,
        importance="Normal",
        categories=(),
        size=size,
        conversation_id=None,
        gucmail_flag_status=None,
        body="Synthetic body",
    )


def folder(identity: str, name: str, messages=(), children=(), *, kind="IPF.Note"):
    value = SimpleNamespace(
        id=identity,
        changekey="synthetic-folder-key",
        name=name,
        folder_class=kind,
        messages=tuple(messages),
        children=tuple(children),
        parent_folder_id=None,
    )
    for child in value.children:
        child.parent_folder_id = SimpleNamespace(id=identity)
    return value


def mailbox():
    nested = folder("nested", "Onderwijs/2026", (message("nested-item", 6, size=600),))
    inbox = folder("inbox", "Posteingang", (message("inbox-item", 1, size=300),), (nested,))
    sent = folder("sent", "Gesendet", (message("sent-item", 3, size=100),))
    calendar = folder(
        "calendar", "Kalender", (message("non-mail-item", 9),), kind="IPF.Appointment"
    )
    root = folder("root", "Mailbox", children=(inbox, sent, calendar), kind="IPF.Root")
    account = SimpleNamespace(
        inbox=inbox, msg_folder_root=root, root=root, protocol=SimpleNamespace(close=lambda: None)
    )
    return EWSGateway(AppConfig(), account_factory=lambda: account), account


@pytest.mark.asyncio
async def test_all_mail_route_reaches_sent_and_nested_mail_folders(
    tmp_path: Path, monkeypatch
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    queries = []
    monkeypatch.setattr(app, "_search_mail", lambda query, _label: queries.append(query))
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            route = catalog_route("search.search_all_mail")
            app._catalog_search(
                route,
                CommandInvocation.from_mapping(route.command_id, {"query": "Synthetic scope term"}),
            )
            assert len(queries) == 1
            gateway, _account = mailbox()
            page = MessageService(gateway).search_messages(queries[0], limit=5)
            assert remote_ids(gateway, page.items) == ["nested-item", "sent-item", "inbox-item"]
            assert [row.folder_path for row in page.items] == [
                "/Mailbox/Posteingang/Onderwijs\\/2026",
                "/Mailbox/Gesendet",
                "/Mailbox/Posteingang",
            ]
            assert all(not row.is_read for row in page.items)
    finally:
        context.close()


@pytest.mark.parametrize(
    "offset,limit,expected",
    [
        (0, 2, ["nested-item", "sent-item"]),
        (1, 2, ["sent-item", "inbox-item"]),
        (2, 2, ["inbox-item"]),
    ],
)
def test_mailbox_pages_merge_global_order_before_applying_offset(offset, limit, expected):
    gateway, _account = mailbox()
    rows = gateway.search_messages(
        SearchQuery(mailbox_scope=True, subject="Synthetic"), limit=limit, offset=offset
    )
    assert remote_ids(gateway, rows) == expected


@pytest.mark.parametrize(
    "direction,expected",
    [("ascending", ["sent-item", "inbox-item"]), ("descending", ["nested-item", "inbox-item"])],
)
def test_mailbox_size_ranking_merges_each_folder_before_slicing(direction, expected):
    gateway, _account = mailbox()
    query = SearchQuery(mailbox_scope=True, ranking=SearchRanking("size", direction))
    assert remote_ids(gateway, gateway.search_messages(query, limit=2)) == expected


def test_unspecified_scope_keeps_default_folder_behavior():
    gateway, _account = mailbox()
    assert remote_ids(gateway, gateway.search_messages(SearchQuery(subject="Synthetic"))) == [
        "inbox-item"
    ]


@pytest.mark.parametrize("query", [SearchQuery(folder="inbox")])
def test_folder_scope_remains_explicit(query):
    gateway, _account = mailbox()
    assert remote_ids(gateway, gateway.search_messages(query)) == ["inbox-item"]


def test_folder_and_mailbox_scope_conflict_is_rejected_before_account_resolution():
    def forbidden():
        raise AssertionError("Invalid query resolved an account")

    gateway = EWSGateway(AppConfig(), account_factory=forbidden)
    with pytest.raises(ValueError, match="folder"):
        gateway.search_messages(SearchQuery(mailbox_scope=True, folder="inbox"))


def test_mailbox_scope_survives_structured_and_expert_saved_query_planning():
    query = SearchQuery(mailbox_scope=True, subject="Synthetic")
    structured = build_search_plan(query)
    serialized = GUCmailApp._search_query_text(query)
    expert = build_search_plan(serialized)
    assert structured.mailbox_scope and expert.mailbox_scope
    assert structured.folder_scope is None and expert.folder_scope is None
    assert structured.server_filters == expert.server_filters
    assert "scope:mailbox" in serialized
    assert "50" in structured.execution_summary and "200" in structured.execution_summary


@pytest.mark.parametrize(
    "text",
    [
        "NOT scope:mailbox",
        "scope:mailbox OR subject:Synthetic",
        "scope:mailbox AND folder:inbox",
        "scope:invalid",
    ],
)
def test_ambiguous_or_unknown_mailbox_scope_is_never_a_local_residual(text):
    with pytest.raises(ValueError, match=r"scope|folder"):
        build_search_plan(text)


def test_folder_discovery_cap_fails_before_any_message_search(monkeypatch):
    gateway, account = mailbox()
    account.msg_folder_root.children = tuple(folder(f"f{i}", f"Synthetic{i}") for i in range(51))
    monkeypatch.setattr(
        gateway, "_bounded_items", lambda *_a, **_k: pytest.fail("Partial mailbox was searched")
    )
    with pytest.raises(ConfigurationError, match=r"50|folder"):
        gateway.search_messages(SearchQuery(mailbox_scope=True))


def test_mailbox_result_window_is_bounded_before_account_resolution():
    gateway = EWSGateway(
        AppConfig(), account_factory=lambda: pytest.fail("Oversized query resolved account")
    )
    with pytest.raises(ConfigurationError, match=r"200|window"):
        gateway.search_messages(SearchQuery(mailbox_scope=True), limit=2, offset=199)


def test_discovery_failure_cannot_silently_return_inbox_results(monkeypatch):
    gateway, _account = mailbox()
    monkeypatch.setattr(
        gateway,
        "_mailbox_search_folders",
        lambda: (_ for _ in ()).throw(NetworkError("Synthetic offline failure")),
    )
    with pytest.raises(NetworkError):
        gateway.search_messages(SearchQuery(mailbox_scope=True))


def test_opaque_mailbox_scope_flag_is_validated():
    with pytest.raises(ValueError, match="mailbox_scope"):
        build_search_plan(SearchQuery(mailbox_scope="true"))


def test_mailbox_residual_is_checked_after_global_metadata_candidate_selection(monkeypatch):
    gateway, _account = mailbox()
    original = gateway._bounded_items
    projections = []

    def bounded(folder, **kwargs):
        projections.append(kwargs["fields"])
        return original(folder, **kwargs)

    monkeypatch.setattr(gateway, "_bounded_items", bounded)
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True, text="body"), limit=2)
    assert remote_ids(gateway, rows) == ["nested-item", "sent-item"]
    assert projections and all("body" not in fields for fields in projections)


def test_duplicate_identity_is_returned_once_across_folders():
    gateway, account = mailbox()
    account.msg_folder_root.children[1].messages += (account.inbox.messages[0],)
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True), limit=5)
    assert len(rows) == 3
    assert len(set(remote_ids(gateway, rows))) == 3


def test_mailbox_root_items_are_included_with_descendant_mail():
    gateway, account = mailbox()
    account.msg_folder_root.messages = (message("root-item", 4),)
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True), limit=5)
    assert remote_ids(gateway, rows) == ["nested-item", "root-item", "sent-item", "inbox-item"]
    assert rows[1].folder_path == "/Mailbox"


def test_unknown_folder_class_aborts_instead_of_silently_omitting_a_folder(monkeypatch):
    gateway, account = mailbox()
    account.inbox.folder_class = None
    monkeypatch.setattr(
        gateway, "_bounded_items", lambda *_a, **_k: pytest.fail("Incomplete scope was searched")
    )
    with pytest.raises(ConfigurationError, match="class"):
        gateway.search_messages(SearchQuery(mailbox_scope=True))


def test_classless_system_folders_are_skipped_not_fatal():
    """Real GUC mailboxes contain classless system folders (Files, Yammer Root)."""

    gateway, account = mailbox()
    account.msg_folder_root.children[2].folder_class = None  # the calendar-like extra folder
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True))
    assert "inbox-item" in remote_ids(gateway, rows)
