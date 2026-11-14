"""Tests against the shape of the real GUC server, not an idealized Exchange.

Three bugs in 1.1 only appeared on the live account. Each has a test here (or is named
here with where its test lives) that fails if its fix is removed:

1. Search All Folders stopped at classless system folders (Files, Yammer Root).
   -> test_whole_guc_mailbox_search_* below.
2. Flag/categorize failed on a stale change key.
   -> tests/test_ews_adapter_core_regressions.py (ErrorChangeKeyRequiredForWriteOperations).
3. Directory search and free/busy crashed because the protocol had no EWS version.
   -> test_directory_and_free_busy_* below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gucmail.config import AppConfig
from gucmail.ews.adapter import EWSGateway
from gucmail.models import Address, SearchQuery
from test_mailbox_search_scope import folder, message, remote_ids

TREE = json.loads(
    (Path(__file__).parent / "fixtures" / "server" / "guc_folder_tree.json").read_text(
        encoding="utf-8"
    )
)


def _build(node: dict, path: str, counter: list[int]) -> SimpleNamespace:
    identity = path.strip("/").replace("/", ".") or "root"
    items = []
    for _ in range(node.get("messages", 0)):
        counter[0] += 1
        items.append(message(f"{identity}-{counter[0]}", counter[0]))
    children = [
        _build(child, f"{path}/{child['name']}", counter) for child in node.get("children", ())
    ]
    return folder(identity, node["name"], items, children, kind=node["class"])


def guc_mailbox() -> tuple[EWSGateway, SimpleNamespace]:
    root = _build(TREE["root"], "", [0])
    by_name = {child.name: child for child in root.children}
    account = SimpleNamespace(
        inbox=by_name["Inbox"],
        msg_folder_root=root,
        root=root,
        protocol=SimpleNamespace(close=lambda: None),
    )
    return EWSGateway(AppConfig(), account_factory=lambda: account), account


def test_whole_guc_mailbox_search_skips_system_folders_and_finds_all_mail():
    gateway, _account = guc_mailbox()
    rows = gateway.search_messages(SearchQuery(mailbox_scope=True), limit=50)
    found = set(remote_ids(gateway, rows))
    # Every message in a real mail folder, including nested and Archive...
    assert found == {"Inbox-1", "Inbox-2", "Inbox.Folder 1-3", "Sent Items-4", "Archive-5"}
    # ...and nothing from calendar, contacts, Yammer or the classless folders.
    assert all(row.folder_path.split("/")[2] in {"Inbox", "Sent Items", "Archive"} for row in rows)


def test_whole_guc_mailbox_search_still_refuses_a_classless_standard_folder():
    gateway, account = guc_mailbox()
    account.inbox.folder_class = None
    with pytest.raises(Exception, match="class"):
        gateway.search_messages(SearchQuery(mailbox_scope=True))


class _Protocol:
    def __init__(self) -> None:
        self.config = SimpleNamespace(version=None)
        self.seen_versions: list[object] = []

    def resolve_names(self, *_args, **_kwargs):
        self.seen_versions.append(self.config.version)
        if self.config.version is None:
            raise AttributeError("'NoneType' object has no attribute 'build'")  # what GUC did
        return []

    def get_free_busy_info(self, *_args, **_kwargs):
        self.seen_versions.append(self.config.version)
        if self.config.version is None:
            raise AttributeError("'NoneType' object has no attribute 'build'")
        return iter(())

    def close(self) -> None:
        pass


def _versionless_gateway() -> tuple[EWSGateway, _Protocol]:
    protocol = _Protocol()
    account = SimpleNamespace(protocol=protocol, version="Exchange2016-negotiated")
    return EWSGateway(AppConfig(), account_factory=lambda: account), protocol


def test_directory_and_free_busy_set_the_negotiated_version_first():
    gateway, protocol = _versionless_gateway()
    assert gateway.search_directory("Example") == ()
    start = datetime(2026, 11, 16, 9, tzinfo=UTC)
    gateway.get_availability(
        [Address(email="someone@guc.edu.eg")], start=start, end=start + timedelta(hours=2)
    )
    assert protocol.seen_versions == ["Exchange2016-negotiated"] * 2
