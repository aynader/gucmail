"""Discovered display paths must remain usable when the local reference cache is absent."""

import pytest
from exchangelib.services import FindFolder

import test_mailbox_search_library_boundaries as library_boundaries
from test_mailbox_search_scope import folder, mailbox

installed_mailbox = library_boundaries.installed_mailbox


@pytest.mark.parametrize("name", ["Posteingang", "Gesendet", "Kalender"])
def test_discovered_absolute_folder_path_roundtrips_without_a_reference_lookup(name):
    gateway, _account = mailbox()
    original = next(row for row in gateway.list_folders() if row.name == name)
    reconstructed, _fresh_account = mailbox()
    resolved = reconstructed.get_folder(original.path)
    assert resolved.ews_id == original.ews_id
    assert resolved.name == original.name


def test_nested_path_preserves_literal_slash_and_root_prefix():
    gateway, _account = mailbox()
    parent = next(row for row in gateway.list_folders() if row.name == "Posteingang")
    original = gateway.list_folders(parent.reference)[0]
    assert original.path == "/Mailbox/Posteingang/Onderwijs\\/2026"
    reconstructed, _fresh_account = mailbox()
    assert reconstructed.get_folder(original.path).ews_id == original.ews_id


def test_relative_folder_named_like_root_is_still_a_child():
    gateway, account = mailbox()
    child = folder("named-like-root", "Mailbox")
    account.msg_folder_root.children += (child,)
    child.parent_folder_id = type(account.inbox.parent_folder_id)(id=account.msg_folder_root.id)
    assert gateway.get_folder("Mailbox").ews_id == child.id
    assert gateway.get_folder("/Mailbox/Mailbox").ews_id == child.id
    assert gateway.get_folder("/Mailbox").ews_id == account.msg_folder_root.id


def test_relative_localized_and_escaped_paths_still_work():
    gateway, account = mailbox()
    assert gateway.get_folder("Posteingang").ews_id == account.inbox.id
    assert gateway.get_folder("Posteingang/Onderwijs\\/2026").ews_id == account.inbox.children[0].id


@pytest.mark.parametrize("prefix", ["msgfolderroot", "Mailbox"])
def test_real_library_root_prefixed_path_uses_one_bounded_name_lookup(
    installed_mailbox,
    monkeypatch,
    prefix,
):
    gateway, _account, children, _data, _requests, _details = installed_mailbox
    captured = []

    def by_name(service, **kwargs):
        payload = service.get_payload(
            **{k: v for k, v in kwargs.items() if k != "max_items"}, page_size=service.page_size
        )
        captured.append((kwargs, payload))
        return iter([children[0]])

    monkeypatch.setattr(FindFolder, "call", by_name)
    result = gateway.get_folder(f"/{prefix}/Posteingang")
    assert result.ews_id == children[0].id and result.name == "Posteingang"
    assert len(captured) == 1
    kwargs, payload = captured[0]
    assert kwargs["depth"] == "Shallow" and kwargs["max_items"] == 2
    assert (
        payload.find(
            "{http://schemas.microsoft.com/exchange/services/2006/messages}ParentFolderIds"
        )[0].get("Id")
        == "owned-mail-root"
    )
    assert "Posteingang" in str(kwargs["restriction"].q)
