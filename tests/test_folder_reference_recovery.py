"""Saved folder identities need bounded metadata recovery after external path changes."""

from types import SimpleNamespace

import pytest
from exchangelib.errors import ErrorFolderNotFound
from exchangelib.folders import Folder
from exchangelib.properties import ParentFolderId
from exchangelib.services import FindFolder, GetFolder, UpdateFolder

import test_mailbox_search_library_boundaries as library_boundaries
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, ConflictError, ItemNotFoundError
from gucmail.ews.adapter import FOLDER_FIELDS, EWSGateway
from gucmail.state import StateStore

installed_mailbox = library_boundaries.installed_mailbox
M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
T = "{http://schemas.microsoft.com/exchange/services/2006/types}"


@pytest.fixture
def moved_folder(installed_mailbox, tmp_path, monkeypatch):
    _unused, account, _children, _data, _requests, _details = installed_mailbox
    state = StateStore(tmp_path / "state.db")
    state.put_reference(
        "fld_saved", "folder", "owned-target", folder_path="/msgfolderroot/Old Parent/Old Name"
    )
    parent = Folder(
        root=account.root,
        id="owned-parent",
        name="Archiv",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=account.msg_folder_root.id),
    )
    target = Folder(
        root=account.root,
        id="owned-target",
        changekey="new-key",
        name="Research/2026",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=parent.id),
        total_count=3,
        unread_count=2,
        child_folder_count=1,
    )
    values = {target.id: target, parent.id: parent}
    calls = []
    factories = []

    def factory():
        factories.append("owned-account-reuse")
        return account

    def missing_path(_service, **_kwargs):
        return iter([])

    def metadata(service, **kwargs):
        requested = tuple(kwargs["folders"])
        payload = service.get_payload(**{**kwargs, "folders": requested})
        calls.append((requested[0].id, kwargs, payload))
        return iter([values[requested[0].id]])

    monkeypatch.setattr(FindFolder, "call", missing_path)
    monkeypatch.setattr(GetFolder, "call", metadata)
    gateway = EWSGateway(AppConfig(), state=state, account_factory=factory)
    return gateway, account, state, values, calls, factories


@pytest.mark.parametrize("saved_path", ["/msgfolderroot/Old Parent/Old Name", None])
def test_saved_identity_recovers_current_name_parent_path_and_counts_without_hierarchy_enumeration(
    moved_folder, saved_path
):
    gateway, account, state, _values, calls, factories = moved_folder
    if saved_path is None:
        with state.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE item_references SET folder_path=NULL WHERE local_ref='fld_saved'"
            )
    selected = gateway.get_folder("fld_saved")
    assert selected.name == "Research/2026"
    assert selected.path == "/msgfolderroot/Archiv/Research\\/2026"
    assert selected.ews_id == "owned-target" and selected.reference == "fld_saved"
    assert selected.total_count == 3 and selected.unread_count == 2 and selected.child_count == 1
    assert state.resolve_reference("fld_saved", "folder")["folder_path"] == selected.path
    assert [identity for identity, _kwargs, _payload in calls] == ["owned-target", "owned-parent"]
    assert factories == ["owned-account-reuse"]
    assert "_subfolders" not in account.root.__dict__
    for identity, kwargs, payload in calls:
        assert len(kwargs["folders"]) == 1 and kwargs["shape"] == "IdOnly"
        assert {field.field.name for field in kwargs["additional_fields"]} == {
            "name",
            "folder_class",
            *FOLDER_FIELDS,
        }
        assert payload.find(M + "FolderIds" + "/" + T + "FolderId").get("Id") == identity
        assert payload.find(M + "FolderShape" + "/" + T + "BaseShape").text == "IdOnly"


@pytest.mark.parametrize("problem", ["wrong_identity", "missing_name", "missing_parent", "cycle"])
def test_invalid_recovered_metadata_cannot_replace_a_saved_reference(moved_folder, problem):
    gateway, _account, state, values, _calls, _factories = moved_folder
    target = values["owned-target"]
    if problem == "wrong_identity":
        target.id = "another-folder"
        expected = ConflictError
    elif problem == "missing_name":
        target.name = None
        expected = ConfigurationError
    elif problem == "missing_parent":
        target.parent_folder_id = None
        expected = ConfigurationError
    else:
        values["owned-parent"].parent_folder_id = ParentFolderId(id=target.id)
        expected = ConfigurationError
    before = state.resolve_reference("fld_saved", "folder")
    with pytest.raises(expected):
        gateway.get_folder("fld_saved")
    after = state.resolve_reference("fld_saved", "folder")
    assert after["ews_id"] == before["ews_id"]
    assert after["folder_path"] == before["folder_path"]


@pytest.mark.parametrize("response", ["empty", "duplicate", "not_found"])
def test_missing_or_ambiguous_metadata_is_an_error_before_any_mutation(
    moved_folder, monkeypatch, response
):
    gateway, _account, _state, values, _calls, _factories = moved_folder
    mutations = []

    def broken(_service, **_kwargs):
        return iter(
            []
            if response == "empty"
            else [ErrorFolderNotFound("Synthetic removed folder")]
            if response == "not_found"
            else [values["owned-target"], values["owned-target"]]
        )

    monkeypatch.setattr(GetFolder, "call", broken)
    monkeypatch.setattr(
        UpdateFolder, "get", lambda *_a, **_k: mutations.append("unexpected remote write")
    )
    expected = ConfigurationError if response == "duplicate" else ItemNotFoundError
    with pytest.raises(expected):
        gateway.rename_folder("fld_saved", "Renamed")
    assert not mutations


def test_recovery_depth_is_bounded_before_state_or_mutation_changes(moved_folder):
    gateway, account, state, values, calls, _factories = moved_folder
    for index in range(65):
        identity = "owned-target" if index == 0 else f"owned-parent-{index}"
        values[identity] = Folder(
            root=account.root,
            id=identity,
            name=f"Level{index}",
            folder_class="IPF.Note",
            parent_folder_id=ParentFolderId(id=f"owned-parent-{index + 1}"),
        )
    before = state.resolve_reference("fld_saved", "folder")
    with pytest.raises(ConfigurationError, match=r"depth|deep|limit"):
        gateway.get_folder("fld_saved")
    assert len(calls) <= 64
    assert state.resolve_reference("fld_saved", "folder")["folder_path"] == before["folder_path"]


def test_valid_recovery_precedes_one_sdk_rename_with_current_parent_and_stable_reference(
    moved_folder, monkeypatch
):
    gateway, account, state, _values, calls, factories = moved_folder
    writes = []

    def accepted(_service, **kwargs):
        folder, fields = kwargs["folders"][0]
        writes.append((folder.id, folder.name, fields))
        assert [identity for identity, _args, _payload in calls] == ["owned-target", "owned-parent"]
        return SimpleNamespace(id=folder.id, changekey="renamed-key")

    monkeypatch.setattr(UpdateFolder, "get", accepted)
    selected = gateway.rename_folder("fld_saved", "Renamed")
    assert writes == [("owned-target", "Renamed", ["name"])]
    assert selected.reference == "fld_saved" and selected.ews_id == "owned-target"
    assert selected.name == "Renamed" and selected.path == "/msgfolderroot/Archiv/Renamed"
    assert selected.change_key == "renamed-key"
    assert state.resolve_reference("fld_saved", "folder")["folder_path"] == selected.path
    assert factories == ["owned-account-reuse"] and "_subfolders" not in account.root.__dict__
