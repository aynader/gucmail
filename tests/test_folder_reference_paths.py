"""Folder paths retain hierarchy and identity through direct reads and local moves."""

from types import SimpleNamespace

import pytest
from exchangelib.folders import Folder
from exchangelib.properties import ParentFolderId
from exchangelib.services import FindFolder

import test_mailbox_search_library_boundaries as library_boundaries
from gucmail.config import AppConfig
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    LocalStateError,
    MutationResultUnavailableError,
    NetworkError,
)
from gucmail.ews.adapter import EWSGateway
from gucmail.services.folders import (
    FolderService,
    format_folder_path,
    parse_folder_path,
    validate_folder_name,
)
from gucmail.state import StateStore
from gucmail.tui.app import FolderMoveScreen, FolderNameScreen, GUCmailApp
from test_ews_adapter import FakeFolder
from test_ui_session import context_for


class MutableFolder(FakeFolder):
    def __init__(self, identity, name, children=()):
        super().__init__(identity, name)
        self.children = list(children)
        self.parent = None
        self.folder_class = "IPF.Note"
        for child in self.children:
            child.parent = self
            child.parent_folder_id = SimpleNamespace(id=self.id)

    def move(self, destination):
        super().move(destination)
        self.parent.children.remove(self)
        destination.children.append(self)
        self.parent = destination


def hierarchy(state=None):
    leaf = MutableFolder("leaf", "Notes")
    project = MutableFolder("project", "Projects", [leaf])
    inbox = MutableFolder("inbox", "Posteingang", [project])
    duplicate = MutableFolder("other-leaf", "Notes")
    archive = MutableFolder("archive", "Archive", [duplicate])
    sent = MutableFolder("sent", "Gesendet", [archive])
    root = MutableFolder("mailroot", "Mailbox", [inbox, sent])
    account = SimpleNamespace(
        inbox=inbox, msg_folder_root=root, root=root, protocol=SimpleNamespace(close=lambda: None)
    )
    return EWSGateway(AppConfig(), state=state, account_factory=lambda: account), account


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("/Mailbox/Posteingang/Projects/Notes", "/Mailbox/Posteingang/Projects/Notes"),
        ("Posteingang/Projects/Notes", "/Mailbox/Posteingang/Projects/Notes"),
        ("inbox/Projects/Notes", "/Posteingang/Projects/Notes"),
    ],
)
def test_direct_nested_folder_returns_a_path_that_resolves_in_a_new_gateway(selector, expected):
    gateway, _account = hierarchy()
    selected = gateway.get_folder(selector)
    assert selected.path == expected
    fresh, _fresh_account = hierarchy()
    assert fresh.get_folder(selected.path).ews_id == selected.ews_id == "leaf"


def test_listing_children_of_a_directly_resolved_nested_parent_keeps_the_full_path():
    gateway, _account = hierarchy()
    child = gateway.list_folders("/Mailbox/Posteingang/Projects")[0]
    assert child.path == "/Mailbox/Posteingang/Projects/Notes"
    fresh, _fresh_account = hierarchy()
    assert fresh.get_folder(child.path).ews_id == "leaf"


def test_duplicate_leaf_names_return_distinct_parent_scoped_paths():
    gateway, _account = hierarchy()
    first = gateway.get_folder("/Mailbox/Posteingang/Projects/Notes")
    second = gateway.get_folder("/Mailbox/Gesendet/Archive/Notes")
    assert first.path != second.path
    fresh, _fresh_account = hierarchy()
    assert fresh.get_folder(first.path).ews_id == "leaf"
    assert fresh.get_folder(second.path).ews_id == "other-leaf"


def discovered(gateway):
    root = {row.name: row for row in gateway.list_folders()}
    project = gateway.list_folders(root["Posteingang"].reference)[0]
    leaf = gateway.list_folders(project.reference)[0]
    archive = gateway.list_folders(root["Gesendet"].reference)[0]
    return project, leaf, archive


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("operation", ["move", "rename"])
def test_descendant_reference_keeps_its_current_parent_path_after_a_local_mutation(
    tmp_path, persist, operation
):
    state = StateStore(tmp_path / "state.db") if persist else None
    gateway, _account = hierarchy(state)
    project, leaf, archive = discovered(gateway)
    if operation == "move":
        parent = gateway.move_folder(project.reference, archive.reference)
        expected = "/Mailbox/Gesendet/Archive/Projects/Notes"
    else:
        parent = gateway.rename_folder(project.reference, "研究/2026")
        expected = "/Mailbox/Posteingang/研究\\/2026/Notes"
    assert parent.reference == project.reference
    selected = gateway.get_folder(leaf.reference)
    assert selected.reference == leaf.reference and selected.ews_id == "leaf"
    assert selected.path == expected
    assert gateway.get_folder(selected.path).ews_id == "leaf"


@pytest.mark.parametrize("operation", ["move", "rename"])
def test_persisted_descendant_path_is_updated_before_a_gateway_restart(tmp_path, operation):
    database = tmp_path / "state.db"
    state = StateStore(database)
    gateway, account = hierarchy(state)
    project, leaf, archive = discovered(gateway)
    if operation == "move":
        gateway.move_folder(project.reference, archive.reference)
        expected = "/Mailbox/Gesendet/Archive/Projects/Notes"
    else:
        gateway.rename_folder(project.reference, "研究/2026")
        expected = "/Mailbox/Posteingang/研究\\/2026/Notes"
    restored = StateStore(database)
    assert restored.resolve_reference(leaf.reference, "folder")["folder_path"] == expected
    fresh = EWSGateway(AppConfig(), state=restored, account_factory=lambda: account)
    selected = fresh.get_folder(leaf.reference)
    assert selected.ews_id == "leaf" and selected.reference == leaf.reference
    assert selected.path == expected


@pytest.mark.parametrize("parent_name", ["Ending/", "Ending\\"])
def test_escaped_final_parent_character_does_not_hide_or_remove_the_child_separator(parent_name):
    gateway, account = hierarchy()
    account.inbox.children[0].name = parent_name
    _project, leaf, _archive = discovered(gateway)
    assert gateway._folder_parts(leaf.path) == ("Mailbox", "Posteingang", parent_name, "Notes")
    fresh = EWSGateway(AppConfig(), account_factory=lambda: account)
    assert fresh.get_folder(leaf.path).ews_id == "leaf"


def test_reused_old_path_cannot_redirect_a_persisted_reference_to_another_folder(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.put_reference(
        "folder_selected", "folder", "project", folder_path="/Mailbox/Posteingang/Projects"
    )
    gateway, account = hierarchy(state)
    original = account.inbox.children[0]
    replacement = MutableFolder("replacement", "Projects")
    account.inbox.children[0] = replacement
    replacement.parent = account.inbox
    replacement.parent_folder_id = SimpleNamespace(id=account.inbox.id)
    with pytest.raises(ConflictError, match=r"identity|different folder|changed"):
        gateway.rename_folder("folder_selected", "Changed")
    assert original.saved_fields == replacement.saved_fields == []
    assert original.name == replacement.name == "Projects"
    assert state.resolve_reference("folder_selected", "folder")["ews_id"] == "project"


installed_mailbox = library_boundaries.installed_mailbox


def test_installed_library_direct_nested_path_uses_only_bounded_shallow_name_lookups(
    installed_mailbox, monkeypatch
):
    gateway, account, children, _data, _requests, _details = installed_mailbox
    leaf = Folder(
        root=account.root,
        id="owned-leaf",
        name="Notes",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=children[0].id),
    )
    calls = []

    def find(service, **kwargs):
        payload = service.get_payload(
            **{k: v for k, v in kwargs.items() if k != "max_items"}, page_size=service.page_size
        )
        calls.append((kwargs, payload))
        name = str(kwargs["restriction"].q)
        return iter([children[0] if "Posteingang" in name else leaf])

    monkeypatch.setattr(FindFolder, "call", find)
    selected = gateway.get_folder("/msgfolderroot/Posteingang/Notes")
    assert selected.path == "/msgfolderroot/Posteingang/Notes"
    assert selected.ews_id == leaf.id
    assert len(calls) == 2
    parents = []
    for kwargs, payload in calls:
        assert kwargs["depth"] == "Shallow" and kwargs["max_items"] == 2
        parents.append(
            payload.find(
                "{http://schemas.microsoft.com/exchange/services/2006/messages}ParentFolderIds"
            )[0].get("Id")
        )
    assert parents == ["owned-mail-root", "owned-inbox"]
    assert "_subfolders" not in account.root.__dict__


@pytest.mark.parametrize(
    "old", ["/Mailbox/Project_%'", "/Mailbox/研究\\/2026", "/Mailbox/Ending\\\\"]
)
def test_state_path_relocation_preserves_identifiers_aliases_account_and_component_boundaries(
    tmp_path, old
):
    database = tmp_path / "state.db"
    first = StateStore(database, account_key="first-account")
    other = StateStore(database, account_key="other-account")
    new = "/Mailbox/Archive/研究\\/2027"
    paths = [old, old + "/Notes", old + "/Notes/Deep", old + "Suffix/Notes", "/Elsewhere"]
    for i, path in enumerate(paths):
        kind = "folder" if i == 0 else "message"
        first.put_reference(f"local_{i}", kind, f"remote_{i}", folder_path=path)
        other.put_reference(f"local_{i}", kind, f"remote_{i}", folder_path=path)
    assert first.put_reference("alias_leaf", "message", "remote_1") == "local_1"
    before = [first.resolve_reference(f"local_{i}") for i in range(5)]
    assert first.relocate_reference_folder_paths(old, new) == 3
    assert first.relocate_reference_folder_paths(old, new) == 0
    for i, original in enumerate(before):
        result = first.resolve_reference(f"local_{i}")
        assert result["ews_id"] == original["ews_id"]
        assert result["canonical_ref"] == original["canonical_ref"]
        assert result["folder_path"] == (new + paths[i][len(old) :] if i < 3 else paths[i])
        assert other.resolve_reference(f"local_{i}")["folder_path"] == paths[i]
    assert first.resolve_reference("alias_leaf")["canonical_ref"] == "local_1"
    assert first.resolve_reference("alias_leaf")["folder_path"] == new + "/Notes"


def test_state_path_relocation_rolls_back_every_row_when_a_write_fails(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.put_reference("folder_one", "folder", "remote_one", folder_path="/Old")
    state.put_reference("folder_two", "folder", "remote_two", folder_path="/Old/Child")
    with state.transaction(immediate=True) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_test_update BEFORE UPDATE OF folder_path ON item_references "
            "WHEN OLD.local_ref='folder_two' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(LocalStateError):
        state.relocate_reference_folder_paths("/Old", "/New")
    assert state.resolve_reference("folder_one")["folder_path"] == "/Old"
    assert state.resolve_reference("folder_two")["folder_path"] == "/Old/Child"


@pytest.mark.parametrize("operation", ["move", "rename"])
@pytest.mark.parametrize("failure_point", ["relocate_reference_folder_paths", "put_reference"])
def test_local_path_write_failure_after_remote_acceptance_reports_uncertainty_and_no_retry(
    tmp_path, monkeypatch, operation, failure_point
):
    state = StateStore(tmp_path / "state.db")
    gateway, account = hierarchy(state)
    project, _leaf, archive = discovered(gateway)
    original = account.inbox.children[0]

    def reject(*_args, **_kwargs):
        raise LocalStateError("Synthetic local write failure")

    monkeypatch.setattr(state, failure_point, reject)
    with pytest.raises(MutationResultUnavailableError) as failure:
        if operation == "move":
            gateway.move_folder(project.reference, archive.reference)
        else:
            gateway.rename_folder(project.reference, "Renamed")
    assert failure.value.context.mutation_uncertain
    assert failure.value.context.retry_safe is False
    assert "Refresh" in failure.value.context.suggestion
    if operation == "move":
        assert len(original.move_calls) == 1 and original.parent.name == "Archive"
    else:
        assert original.saved_fields == [["name"]] and original.name == "Renamed"


def test_reference_path_read_failure_cannot_fall_through_to_a_mailbox_mutation(
    tmp_path, monkeypatch
):
    state = StateStore(tmp_path / "state.db")
    state.put_reference("folder_selected", "folder", "project", folder_path="/Mailbox/Projects")
    gateway, _account = hierarchy(state)

    def unavailable(_selector):
        raise NetworkError("Synthetic read failure")

    monkeypatch.setattr(gateway, "_resolve_folder_path", unavailable)
    monkeypatch.setattr(
        gateway, "_construct", lambda *_a, **_k: pytest.fail("Read failure fell through")
    )
    with pytest.raises(NetworkError, match="Synthetic read failure"):
        gateway.rename_folder("folder_selected", "Renamed")


def test_changed_persisted_identity_is_checked_before_using_a_cached_folder(tmp_path):
    state = StateStore(tmp_path / "state.db")
    gateway, _account = hierarchy(state)
    project, _leaf, archive = discovered(gateway)
    state.put_reference(project.reference, "folder", archive.ews_id, folder_path=archive.path)
    selected = gateway.get_folder(project.reference)
    assert selected.ews_id == archive.ews_id and selected.path == archive.path


def test_invalid_known_parent_cycle_fails_without_loading_a_server_hierarchy():
    gateway, _account = hierarchy()
    first = MutableFolder("first", "First")
    second = MutableFolder("second", "Second")
    gateway._remember("folder", first, parent=second)
    gateway._remember("folder", second, parent=first)
    with pytest.raises(ConfigurationError, match=r"cyclic|deep"):
        gateway._folder_path(first)


@pytest.mark.parametrize("name", ["Ending/", "Ending\\", "研究\\/2026"])
def test_shared_path_parser_round_trips_an_escaped_final_folder_name(name):
    components = ("Mailbox", "Posteingang", name)
    path = format_folder_path(components, absolute=True)
    assert parse_folder_path(path) == components


@pytest.mark.parametrize("prefix", ["/Mailbox/Posteingang", "Posteingang", "inbox"])
def test_shared_folder_service_resolves_the_same_direct_path_as_the_gateway(prefix):
    gateway, _account = hierarchy()
    selected = FolderService(gateway).resolve(prefix + "/Projects/Notes")
    assert selected.ews_id == "leaf"
    assert gateway.get_folder(selected.path).ews_id == selected.ews_id


def test_shared_folder_service_accepts_an_escaped_final_slash_name():
    gateway, account = hierarchy()
    account.inbox.children[0].name = "Ending/"
    selected = FolderService(gateway).resolve("Posteingang/Ending\\/")
    assert selected.ews_id == "project"
    assert gateway._folder_parts(selected.path)[-1] == "Ending/"


def test_shared_folder_service_does_not_treat_a_custom_root_named_folder_as_a_namespace():
    gateway, account = hierarchy()
    named = MutableFolder("root-named", "Mailbox")
    account.msg_folder_root.children.append(named)
    named.parent = account.msg_folder_root
    named.parent_folder_id = SimpleNamespace(id=account.msg_folder_root.id)
    service = FolderService(gateway)
    assert service.resolve("Mailbox").ews_id == named.id
    assert service.resolve("/Mailbox/Mailbox").ews_id == named.id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(70, 20), (50, 18)])
@pytest.mark.parametrize("operation", ["rename", "move"])
async def test_folder_name_and_move_prompts_fit_compact_terminals_and_submit_by_keyboard(
    tmp_path, size, operation
):
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    values = []
    try:
        async with app.run_test(size=size) as pilot:
            screen = (
                FolderNameScreen(
                    "Rename folder", "Folder: /Mailbox/Projects", initial_name="Projects"
                )
                if operation == "rename"
                else FolderMoveScreen("/Mailbox/Projects")
            )
            app.push_screen(screen, values.append)
            await pilot.pause()
            box = screen.query_one(
                "#folder-name-box" if operation == "rename" else "#folder-move-box"
            )
            assert box.region.x >= 0 and box.region.right <= size[0]
            assert box.region.y >= 0 and box.region.bottom <= size[1]
            assert app.focused.id == (
                "folder-name-input" if operation == "rename" else "folder-move-destination"
            )
            await pilot.press("end", "ctrl+u", *"Research/2026", "enter")
            await pilot.pause()
            assert values == ["Research/2026"]
    finally:
        context.close()


def test_existing_display_name_codepoints_survive_path_formatting_and_parsing():
    label = "Cafe\u0301"
    path = format_folder_path(("Mailbox", label), absolute=True)
    assert path == "/Mailbox/" + label
    assert parse_folder_path(path) == ("Mailbox", label)


@pytest.mark.parametrize("layer", ["gateway", "service"])
def test_existing_decomposed_unicode_folder_name_resolves_without_rewriting_it(layer):
    gateway, account = hierarchy()
    label = "Cafe\u0301"
    account.inbox.children[0].name = label
    path = "/Mailbox/Posteingang/" + label + "/Notes"
    selected = (
        gateway.get_folder(path) if layer == "gateway" else FolderService(gateway).resolve(path)
    )
    assert selected.ews_id == "leaf" and selected.path == path


def test_new_folder_name_validation_retains_its_explicit_normalization_policy():
    assert validate_folder_name("  Cafe\u0301  ") == "Café"
