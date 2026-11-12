"""Regression checks at installed-provider and user-workflow boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from exchangelib import CalendarItem, Message, Task
from exchangelib.folders import Folder, Root
from exchangelib.services import MoveFolder, UpdateFolder

from gucmail.config import AppConfig
from gucmail.ews.adapter import EWSGateway
from gucmail.models import Importance, MessageDetail, MessageSummary, TaskInfo, to_primitive


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("operation", ["rename", "move"])
def test_folder_changes_do_not_discover_hierarchy(monkeypatch, cached, operation):
    account = SimpleNamespace()
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    root = Root(account=account, id="root")
    parent = Folder(root=root, id="parent", name="Parent")
    destination = Folder(root=root, id="destination", name="Destination")
    folder = Folder(parent=parent, id="folder", changekey="before", name="Old")
    root._subfolders = {parent.id: parent, folder.id: folder} if cached else None
    gateway._remember("folder", parent, reference="parent-local", folder_path="/Parent")
    gateway._remember("folder", folder, reference="folder-local", folder_path="/Parent/Old")
    gateway._remember("folder", destination, reference="dest-local", folder_path="/Destination")

    def forbidden(_self):
        raise AssertionError("Implicit whole hierarchy discovery")

    monkeypatch.setattr(Root, "_folders_map", property(forbidden))

    class Update:
        def get(self, *, folders):
            assert folders == [(folder, ["name"])]
            return SimpleNamespace(id="folder", changekey="renamed")

    class Move:
        def get(self, *, folders, to_folder):
            assert folders == [folder] and to_folder is destination
            return SimpleNamespace(id="folder", changekey="moved")

    monkeypatch.setattr(UpdateFolder, "__new__", lambda *a, **kw: Update())
    monkeypatch.setattr(MoveFolder, "__new__", lambda *a, **kw: Move())
    result = (
        gateway.rename_folder("folder-local", "New")
        if operation == "rename"
        else gateway.move_folder("folder-local", "dest-local")
    )
    assert result.reference == "folder-local"
    assert result.path == ("/Parent/New" if operation == "rename" else "/Destination/Old")
    assert result.change_key == ("renamed" if operation == "rename" else "moved")
    assert (root._subfolders is None) is (not cached)


def _restore_gateway(monkeypatch, item_type):
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    created = []
    monkeypatch.setattr(gateway, "_remember", lambda *a, **kw: "restored-local")
    original = gateway._construct

    def construct(class_name, *args, **kwargs):
        if class_name == item_type.__name__:
            kwargs.pop("account", None)
            kwargs.pop("folder", None)
        return original(class_name, *args, **kwargs)

    def save(item, **kwargs):
        created.append((item, kwargs))
        item.id = "synthetic-restored-id"
        item.changekey = "synthetic-restored-key"

    monkeypatch.setattr(gateway, "_construct", construct)
    monkeypatch.setattr(item_type, "save", save)
    return gateway, created


def test_task_restore_keeps_enabled_reminder(monkeypatch):
    gateway, created = _restore_gateway(monkeypatch, Task)
    due = datetime(2030, 1, 1, 9, tzinfo=UTC)
    task = TaskInfo("task", "Synthetic reminder", "not_started", reminder_at=due)
    gateway._restore_task(SimpleNamespace(), to_primitive(task))
    restored, _ = created[0]
    assert restored.reminder_is_set is True
    assert restored.reminder_due_by == due


def test_mail_restore_keeps_html_reply_to_and_inline_relationship(monkeypatch):
    from gucmail.models import Address

    gateway, created = _restore_gateway(monkeypatch, Message)
    summary = MessageSummary("mail", "Synthetic", None, None, importance=Importance.HIGH)
    detail = MessageDetail(
        summary,
        reply_to=(Address("reply@example.test"),),
        body_text="Synthetic image",
        body_html='<p>Synthetic image<img src="cid:picture"></p>',
    )
    attachments = []
    monkeypatch.setattr(Message, "attach", lambda self, values: attachments.extend(values))
    gateway._restore_message(
        SimpleNamespace(
            attachments=(
                {
                    "original_filename": "picture.png",
                    "content": b"synthetic",
                    "content_type": "image/png",
                    "metadata": {"inline": True, "content_id": "picture"},
                },
            )
        ),
        SimpleNamespace(),
        to_primitive(detail),
    )
    restored, _ = created[0]
    assert restored.body.body_type == "HTML"
    assert str(restored.body) == detail.body_html
    assert restored.reply_to[0].email_address == "reply@example.test"
    assert attachments[0].is_inline is True
    assert attachments[0].content_id == "picture"
    assert attachments[0].content_type == "image/png"


@pytest.mark.parametrize("kind,item_type", [("task", Task), ("event", CalendarItem)])
def test_simple_recurrence_restore_uses_existing_typed_mapper(monkeypatch, kind, item_type):
    gateway, created = _restore_gateway(monkeypatch, item_type)
    data = {
        "subject": "Synthetic recurring item",
        "start": "2030-01-01T09:00:00+00:00",
        "end": "2030-01-01T10:00:00+00:00",
        "timezone": "UTC",
        "start_date": "2030-01-01",
        "status": "not_started",
        "recurrence": {
            "schema_version": 1,
            "target": "task" if kind == "task" else "calendar",
            "pattern": {"type": "daily", "interval": 2},
            "boundary": {"type": "numbered", "start": "2030-01-01", "occurrences": 3},
        },
    }
    getattr(gateway, f"_restore_{kind}")(SimpleNamespace(), data)
    restored, save_options = created[0]
    assert restored.recurrence.pattern.interval == 2
    assert restored.recurrence.boundary.number == 3
    if kind == "event":
        assert save_options == {"send_meeting_invitations": "SendToNone"}


def test_mime_restore_saves_original_source_without_duplicate_attachments(monkeypatch):
    gateway, created = _restore_gateway(monkeypatch, Message)
    source = b"From: sender@example.test\r\nSubject: Synthetic MIME\r\n\r\nOriginal body"
    attachments = (
        {"content": source, "metadata": {"role": "message_mime"}},
        {"content": b"data", "original_filename": "file.txt"},
    )
    monkeypatch.setattr(Message, "attach", lambda *a: pytest.fail("Duplicate MIME attachment"))
    data = to_primitive(MessageDetail(MessageSummary("mail", "Synthetic MIME", None, None)))
    gateway._restore_message(SimpleNamespace(attachments=attachments), SimpleNamespace(), data)
    restored, _ = created[0]
    assert restored.mime_content == source
    assert len(created) == 1


@pytest.mark.parametrize("kind,item_type", [("event", CalendarItem), ("task", Task)])
def test_pim_restore_attaches_archived_files_after_save(monkeypatch, kind, item_type):
    gateway, created = _restore_gateway(monkeypatch, item_type)
    attachment = gateway._restore_attachments(
        SimpleNamespace(
            attachments=(
                {
                    "content": b"synthetic",
                    "original_filename": "test.txt",
                    "content_type": "text/plain",
                },
            )
        )
    )[0]
    attached = []
    monkeypatch.setattr(item_type, "attach", lambda self, values: attached.extend(values))
    data = {
        "subject": "Synthetic",
        "start": "2030-01-01T09:00:00+00:00",
        "end": "2030-01-01T10:00:00+00:00",
        "timezone": "UTC",
    }
    getattr(gateway, f"_restore_{kind}")(SimpleNamespace(), data, attachments=[attachment])
    assert len(created) == 1
    assert attached == [attachment]


def test_bad_restore_payload_cannot_create_destination_folder(monkeypatch):
    gateway = EWSGateway(AppConfig(), account_factory=lambda: pytest.fail("Network accessed"))
    monkeypatch.setattr(gateway, "_restore_folder", lambda *a: pytest.fail("Folder created"))
    with pytest.raises(Exception, match="attachment content"):
        gateway.restore_item(
            SimpleNamespace(kind="message", data={}, attachments=({"content": None},)), "Synthetic"
        )


def test_calendar_backup_pages_masters_without_date_horizon(monkeypatch):
    from gucmail.models import FolderInfo

    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    item = CalendarItem(
        id="calendar-master",
        subject="Old series",
        start=datetime(1990, 1, 1, tzinfo=UTC),
        end=datetime(1990, 1, 1, 1, tzinfo=UTC),
    )
    folder = SimpleNamespace(id="calendar")
    monkeypatch.setattr(gateway, "_calendar_folder", lambda _: folder)
    monkeypatch.setattr(gateway, "list_events", lambda **kw: pytest.fail("Expanded CalendarView"))
    calls = []

    def listing(selected, **kwargs):
        calls.append(kwargs)
        return (item,)

    monkeypatch.setattr(gateway, "_bounded_items", listing)
    result = list(
        gateway._iter_backup_pim(
            [FolderInfo("calendar", "Calendar", "/Calendar", None, folder_class="IPF.Appointment")],
            lambda kind: True,
            metadata_only=False,
        )
    )
    assert len(result) == 1 and result[0][1].start.year == 1990
    assert calls[0]["offset"] == 0 and calls[0]["limit"] <= 200
