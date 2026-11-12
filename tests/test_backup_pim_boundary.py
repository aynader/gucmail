from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest

from gucmail.config import AppConfig
from gucmail.ews.adapter import EWSGateway
from gucmail.models import CalendarEvent, ContactInfo, FolderInfo, TaskInfo
from gucmail.services.backup import BackupRequest, BackupSelection, BackupService


@pytest.mark.parametrize("cached", [False, True])
def test_folder_create_delete_does_not_trigger_implicit_hierarchy_discovery(monkeypatch, cached):
    from exchangelib.folders import Folder, Root
    from exchangelib.services import CreateFolder, DeleteFolder

    account = SimpleNamespace()
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    root = Root(account=account, id="synthetic-root")
    parent = Folder(root=root, id="synthetic-parent", name="Parent")
    folder = Folder(parent=parent, name="Synthetic")
    root._subfolders = {parent.id: parent} if cached else None

    def forbidden(_self):
        raise AssertionError("Folder mutation unexpectedly enumerated the hierarchy")

    monkeypatch.setattr(Root, "_folders_map", property(forbidden))

    class Create:
        def get(self, *, parent_folder, folders):
            assert parent_folder is parent and folders == [folder]
            return SimpleNamespace(id="synthetic-new", changekey="synthetic-key")

    class Delete:
        def get(self, *, folders, delete_type):
            assert folders == [folder] and delete_type == "MoveToDeletedItems"

    monkeypatch.setattr(CreateFolder, "__new__", lambda *args, **kwargs: Create())
    monkeypatch.setattr(DeleteFolder, "__new__", lambda *args, **kwargs: Delete())
    gateway._save_new_folder(folder, parent)
    assert folder.id == "synthetic-new" and folder.changekey == "synthetic-key"
    if cached:
        assert root._subfolders[folder.id] is folder
    else:
        assert root._subfolders is None
    monkeypatch.setattr(gateway, "_resolve_folder", lambda _reference: folder)
    gateway.delete_folder("synthetic-folder")
    assert folder.id is None
    assert root._subfolders == ({parent.id: parent} if cached else None)


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize(
    "kind,folder_class",
    [("calendar", "IPF.Appointment"), ("contact", "IPF.Contact"), ("task", "IPF.Task")],
)
def test_pim_backup_preserves_selected_folder_content_and_estimates_without_bodies(
    tmp_path, monkeypatch, selected, kind, folder_class
):
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    folder = FolderInfo("custom-folder", "Synthetic", "/Synthetic", None, folder_class=folder_class)
    monkeypatch.setattr(gateway, "_backup_folders", lambda roots: [folder])
    now = datetime.now(UTC)
    values = {
        "calendar": CalendarEvent(
            "event-one",
            "Synthetic",
            now,
            now + timedelta(minutes=15),
            "UTC",
            body="Synthetic event body",
        ),
        "contact": ContactInfo(
            "contact-one",
            "Synthetic",
            notes="Synthetic contact notes",
            phones={"BusinessPhone": "0000000000"},
        ),
        "task": TaskInfo("task-one", "Synthetic", "not_started", body="Synthetic task body"),
    }
    calls = []

    def listing(**kwargs):
        calls.append(kwargs)
        assert kwargs.get("calendar_reference", kwargs.get("folder_reference")) == folder.reference
        return (values[kind],) if kwargs["offset"] == 0 else ()

    if kind == "calendar":
        provider_folder = SimpleNamespace(id="synthetic-calendar")

        def resolve(reference):
            assert reference == folder.reference
            return provider_folder

        def bounded(selected_folder, **kwargs):
            assert selected_folder is provider_folder
            return listing(
                folder_reference=folder.reference,
                offset=kwargs["offset"],
                metadata_only="body" not in kwargs["fields"],
            )

        monkeypatch.setattr(gateway, "_calendar_folder", resolve)
        monkeypatch.setattr(gateway, "_bounded_items", bounded)
        monkeypatch.setattr(gateway, "_map_calendar_item", lambda *a, **kw: values[kind])
    else:
        monkeypatch.setattr(
            gateway, "list_contacts" if kind == "contact" else "list_tasks", listing
        )
    attachment_fetches = []

    def fetch_attachments(reference, kinds, fields):
        assert reference == values[kind].reference
        assert kinds == ("event" if kind == "calendar" else kind,)
        assert fields == ("attachments",)
        attachment_fetches.append(reference)
        return SimpleNamespace(
            attachments=[
                SimpleNamespace(
                    fp=BytesIO(b"Synthetic PIM file"),
                    name="synthetic.txt",
                    content_type="text/plain",
                )
            ]
        )

    monkeypatch.setattr(gateway, "_fetch_item", fetch_attachments)
    request = BackupRequest(
        selection=BackupSelection(
            folders=(folder.reference,) if selected else (), item_kinds=(kind,)
        )
    )
    estimate = gateway.estimate_backup(request)
    assert estimate.item_count == 1
    assert calls and all(call["metadata_only"] for call in calls)
    assert attachment_fetches == []
    calls.clear()
    result = BackupService(tmp_path).create(gateway, request)
    assert calls and all(call.get("metadata_only") is False for call in calls)
    assert result.item_count == 1
    archive = BackupService(tmp_path).open_offline(result.backup_id)
    item = archive.get_item(values[kind].reference)
    assert item.folder_path == folder.path
    field = "notes" if kind == "contact" else "body"
    assert item.data[field] == getattr(values[kind], field)
    assert len(item.attachments) == 1
    assert attachment_fetches == [values[kind].reference]
    assert list(gateway.iter_backup_items(request, frozenset({item.item_key}))) == []
