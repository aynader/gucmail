"""Acceptance regressions using real exchangelib projections without any network."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from exchangelib import Account, Configuration, Credentials, FileAttachment, Message, Version
from exchangelib.attachments import AttachmentId
from exchangelib.folders import Folder, Root
from exchangelib.folders.collections import FolderCollection
from exchangelib.transport import NTLM
from exchangelib.version import Build

from gucmail.config import AppConfig
from gucmail.errors import ConflictError
from gucmail.ews.adapter import SUMMARY_FIELDS, EWSGateway
from gucmail.models import Importance
from gucmail.state import StateStore


@pytest.mark.parametrize(
    "enabled,minutes,expected",
    [
        (False, 15, None),
        (False, 0, None),
        (True, 0, 0),
        (True, 30, 30),
    ],
)
def test_calendar_reminder_mapping_respects_exchange_enabled_flag(enabled, minutes, expected):
    from datetime import UTC, datetime, timedelta

    from exchangelib import CalendarItem

    from gucmail.ews.mapping import map_calendar_event

    start = datetime(2030, 1, 1, 9, tzinfo=UTC)
    item = CalendarItem(
        subject="Synthetic reminder regression",
        start=start,
        end=start + timedelta(minutes=15),
        reminder_is_set=enabled,
        reminder_minutes_before_start=minutes,
    )
    assert map_calendar_event(item).reminder_minutes == expected


def test_task_complete_and_reopen_never_write_server_managed_completion_date(
    projected_gateway: EWSGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from exchangelib import Task
    from exchangelib.services import UpdateItem

    from gucmail.services.tasks import TaskService

    item = Task(
        account=projected_gateway.account,
        id="synthetic-task",
        changekey="key",
        subject="Synthetic task",
        status="NotStarted",
        percent_complete=Decimal(0),
    )
    reference = projected_gateway._remember("task", item)
    payloads = []
    server_completion = datetime(2026, 9, 25, 9, tzinfo=UTC)

    def save(value, *, update_fields):
        payloads.append(
            UpdateItem(account=value.account).get_payload(
                items=[(value, update_fields)],
                message_disposition="SaveOnly",
                conflict_resolution="AutoResolve",
                send_meeting_invitations_or_cancellations="SendToNone",
                suppress_read_receipts=True,
            )
        )

    def fetch(account, *, ids, only_fields):
        assert ids == [(item.id, item.changekey)]
        assert "complete_date" in only_fields
        current = projected_gateway._objects[("task", reference)]
        return [
            Task(
                account=account,
                id=item.id,
                changekey=item.changekey,
                subject=current.subject,
                status=current.status,
                percent_complete=current.percent_complete,
                complete_date=server_completion if current.status == "Completed" else None,
            )
        ]

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    service = TaskService(projected_gateway)
    completed = service.complete(reference)
    assert completed.status == "completed" and completed.completed_at == server_completion
    reopened = service.reopen(reference)
    assert reopened.status == "not_started" and reopened.completed_at is None
    assert reopened.percent_complete == 0
    assert len(payloads) == 2
    assert all(not payload.xpath('//*[local-name()="CompleteDate"]') for payload in payloads)


def test_partial_contact_update_preserves_display_name_in_actual_ews_payload(
    projected_gateway: EWSGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exchangelib import Contact
    from exchangelib.services import UpdateItem

    from gucmail.services.contacts import ContactUpdatePlan

    item = Contact(
        account=projected_gateway.account,
        id="synthetic-contact",
        changekey="key",
        display_name="Synthetic stale cached name",
        company_name="Synthetic original",
    )
    reference = projected_gateway._remember("contact", item)
    payloads = []

    def fetch(account, *, ids, only_fields):
        assert ids == [(item.id, item.changekey)]
        assert "display_name" in only_fields and "item_class" in only_fields
        return [
            Contact(
                account=account,
                id=item.id,
                changekey="fresh-key",
                display_name="Synthetic explicit name",
                company_name="Synthetic current company",
                item_class="IPM.Contact",
            )
        ]

    def save(value, *, update_fields):
        payloads.append(
            UpdateItem(account=value.account).get_payload(
                items=[(value, update_fields)],
                message_disposition="SaveOnly",
                conflict_resolution="AutoResolve",
                send_meeting_invitations_or_cancellations="SendToNone",
                suppress_read_receipts=True,
            )
        )

    monkeypatch.setattr(Contact, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    projected_gateway.update_contact(ContactUpdatePlan(reference, {"company": "Synthetic edit"}))
    assert payloads[0].xpath('//*[local-name()="DisplayName"]/text()') == [
        "Synthetic explicit name"
    ]
    assert payloads[0].xpath('//*[local-name()="CompanyName"]/text()') == ["Synthetic edit"]


@pytest.fixture
def projected_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EWSGateway:
    def no_network(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("This regression test must not access the network")

    monkeypatch.setattr("requests.sessions.Session.send", no_network)
    monkeypatch.setattr(Account, "fetch", no_network)
    account = Account(
        primary_smtp_address="synthetic@example.test",
        config=Configuration(
            service_endpoint="https://ews.example.test/EWS/Exchange.asmx",
            credentials=Credentials("synthetic", "synthetic-test-only"),
            auth_type=NTLM,
            version=Version(build=Build(15, 1, 2507, 6)),
        ),
        autodiscover=False,
    )
    return EWSGateway(
        AppConfig(), state=StateStore(tmp_path / "state.db"), account_factory=lambda: account
    )


def test_resolve_observes_identity_and_change_key_written_by_another_process(
    projected_gateway: EWSGateway,
) -> None:
    gateway = projected_gateway
    assert gateway.state is not None
    item = Message(account=gateway.account, id="before-move", changekey="before-key")
    gateway._remember("message", item, reference="msg_synthetic", folder_path="/Inbox")
    other_process = StateStore(gateway.state.path)
    other_process.put_reference(
        "msg_synthetic", "message", "after-move", change_key="after-key", folder_path="/Trash"
    )

    _kind, resolved = gateway._resolve_item("msg_synthetic", "message")

    assert resolved.id == "after-move"
    assert resolved.changekey == "after-key"
    other_process.put_reference("msg_synthetic", "message", "after-move", change_key="newer-key")
    assert gateway._resolve_item("msg_synthetic", "message")[1].changekey == "newer-key"


def test_draft_and_message_list_keep_one_identity_through_move_and_stale_refresh(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.db")
    assert state.put_reference("drf-first", "draft", "before", change_key="key") == "drf-first"
    assert state.put_reference("msg-list", "message", "before", change_key="key") == "drf-first"
    state.put_reference("msg-list", "message", "after", change_key="new", folder_path="/Trash")
    reopened = StateStore(state.path)
    assert reopened.resolve_reference("drf-first", "draft")["ews_id"] == "after"
    assert reopened.resolve_reference("msg-list", "message")["ews_id"] == "after"
    assert reopened.put_reference("msg-old-page", "message", "before") == "drf-first"
    assert reopened.resolve_reference("msg-old-page")["ews_id"] == "after"
    assert reopened.reference_health()["ok"]


def test_existing_duplicate_mail_kinds_are_repaired_without_losing_either_reference(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.db")
    state.put_reference("drf-original", "draft", "before", change_key="key")
    # Realistic older state: the same Exchange Message was listed via a different
    # presentation, before mail-family canonicalization was implemented.
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO item_references SELECT account_key, 'msg-duplicate', 'message', "
            "ews_id, change_key, folder_path, created_at, updated_at FROM item_references "
            "WHERE local_ref='drf-original'"
        )
        connection.execute(
            "INSERT INTO item_reference_remote_ids SELECT account_key, 'message', ews_id, "
            "'msg-duplicate', is_current, first_seen_at, last_seen_at "
            "FROM item_reference_remote_ids "
            "WHERE canonical_ref='drf-original'"
        )
    state.put_reference("msg-duplicate", "message", "after", change_key="new")
    assert state.resolve_reference("drf-original", "draft")["ews_id"] == "after"
    assert state.resolve_reference("msg-duplicate", "message")["ews_id"] == "after"
    assert state.reference_health()["ok"]


def test_exchange_dates_support_standard_timezone_conversion_in_every_domain() -> None:
    from datetime import UTC, date, datetime

    from exchangelib import EWSDateTime, EWSTimeZone

    from gucmail.ews.mapping import map_calendar_event, map_contact, map_message_summary, map_task

    stamp = EWSDateTime(2026, 9, 7, 12, tzinfo=EWSTimeZone("Africa/Cairo"))
    message = map_message_summary(SimpleNamespace(datetime_received=stamp, datetime_sent=stamp))
    event = map_calendar_event(SimpleNamespace(start=stamp, end=stamp, recurrence_id=stamp))
    task = map_task(
        SimpleNamespace(
            start_date=stamp,
            due_date=stamp,
            complete_date=stamp,
            reminder_due_by=stamp,
            reminder_is_set=True,
        )
    )
    contact = map_contact(SimpleNamespace(birthday=stamp))
    for value in (
        message.received_at,
        message.sent_at,
        event.start,
        event.end,
        event.recurrence_id,
        task.completed_at,
        task.reminder_at,
    ):
        assert type(value) is datetime
        assert value.astimezone(UTC).hour == 9
    for value in (task.start_date, task.due_date, contact.birthday):
        assert type(value) is date


def test_long_session_provider_cache_is_bounded_and_does_not_retain_bodies(
    projected_gateway: EWSGateway,
) -> None:
    gateway = projected_gateway
    gateway.object_cache_limit = 8
    for number in range(40):
        value = Message(account=gateway.account, id=f"remote-{number}", body="synthetic body")
        gateway._remember("message", value, reference=f"msg-{number}")
        assert value.body == "synthetic body"
    assert len(gateway._objects) == 8
    assert all(item.body is None for item in gateway._objects.values())
    assert gateway._resolve_item("msg-0", "message")[1].id == "remote-0"


def test_cacheless_reconnect_retains_bounded_identity_snapshots(
    projected_gateway: EWSGateway,
) -> None:
    account = projected_gateway.account
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    gateway.object_cache_limit = 2
    gateway.memory_reference_limit = 5
    for number in range(8):
        gateway._remember(
            "message",
            Message(account=account, id=f"remote-{number}"),
            reference=f"msg-{number}",
            folder_path="/Inbox",
        )
    assert len(gateway._memory_references) == 5
    gateway.reconnect()
    assert not gateway._objects
    assert gateway._resolve_item("msg-4", "message")[1].id == "remote-4"
    assert gateway._state_reference("msg-4", "message")[1]["folder_path"] == "/Inbox"


def test_attachment_cache_strips_parent_body_and_stream_still_uses_same_account(
    projected_gateway: EWSGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exchangelib.services import GetAttachment

    gateway = projected_gateway
    item = Message(account=gateway.account, id="synthetic-parent", body="private synthetic body")
    attachment = FileAttachment(
        name="tiny.txt",
        size=4,
        parent_item=item,
        attachment_id=AttachmentId(id="synthetic-attachment"),
    )
    reference = gateway._remember("message", item)
    gateway._remember("attachment", attachment, reference="att-synthetic", parent=item)
    gateway._attachment_parents["att-synthetic"] = reference
    cached = gateway._memory_object("att-synthetic", "attachment")[1]
    assert cached.parent_item.body is None
    assert gateway._parents[("attachment", "att-synthetic")].body is None

    def stream(service: Any, attachment_id: Any) -> Any:
        assert service.account is gateway.account
        assert attachment_id.id == "synthetic-attachment"
        yield b"tiny"

    monkeypatch.setattr(GetAttachment, "stream_file_content", stream)
    with gateway.open_attachment(reference, "att-synthetic") as handle:
        assert handle.read() == b"tiny"


@pytest.mark.parametrize("mode,expected", [("add", ["Keep", "New"]), ("remove", ["Keep"])])
def test_category_merge_fetches_existing_values_after_restart(
    projected_gateway: EWSGateway,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: list[str],
) -> None:
    gateway = projected_gateway
    assert gateway.state is not None
    gateway.state.put_reference("msg_synthetic", "message", "remote-id", change_key="old-key")
    fields_requested: list[tuple[str, ...]] = []
    updates: list[list[str]] = []

    def fetch(*, ids: Any, only_fields: tuple[str, ...]) -> list[Message]:
        fields_requested.append(only_fields)
        return [
            Message(
                account=gateway.account,
                id=ids[0][0],
                changekey="current-key",
                categories=["Keep", "New"],
            )
        ]

    def save(item: Message, *, update_fields: list[str]) -> None:
        assert update_fields == ["categories"]
        updates.append(list(item.categories))
        item.changekey = "saved-key"

    monkeypatch.setattr(gateway.account, "fetch", fetch)
    monkeypatch.setattr(Message, "save", save)
    gateway.categorize("msg_synthetic", ["New"], mode=mode)

    assert fields_requested == [("categories",)]
    assert updates == [expected]
    assert gateway.state.resolve_reference("msg_synthetic")["change_key"] == "saved-key"


@pytest.mark.parametrize("operation", ["mark_read", "flag", "set_importance"])
def test_field_update_persists_returned_change_key(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    gateway = projected_gateway
    assert gateway.state is not None
    gateway._bindings()
    gateway.state.put_reference("msg_synthetic", "message", "remote-id", change_key="old-key")

    def save(item: Message, **_kwargs: Any) -> None:
        item.changekey = "saved-key"

    monkeypatch.setattr(Message, "save", save)
    value = Importance.HIGH if operation == "set_importance" else True
    getattr(gateway, operation)("msg_synthetic", value)
    assert gateway.state.resolve_reference("msg_synthetic")["change_key"] == "saved-key"


def test_attachment_metadata_never_reads_lazy_content() -> None:
    class MetadataOnly:
        name = "synthetic.txt"
        size = 4

        @property
        def content(self) -> bytes:
            pytest.fail("Attachment identity unexpectedly downloaded content")

    assert EWSGateway._attachment_identity(MetadataOnly()) == ("synthetic.txt", 4)


def test_server_draft_can_be_imported_with_real_exchange_timestamp(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import datetime

    from exchangelib import EWSDateTime, EWSTimeZone

    from gucmail.services.drafts import DraftService

    gateway = projected_gateway
    gateway.state.put_reference("draft_timestamp", "draft", "remote-id", change_key="key")
    monkeypatch.setattr(
        gateway.account,
        "fetch",
        lambda **_: [
            Message(
                account=gateway.account,
                id="remote-id",
                changekey="key",
                subject="Synthetic",
                is_draft=True,
                last_modified_time=EWSDateTime(2026, 9, 7, tzinfo=EWSTimeZone("UTC")),
            )
        ],
    )
    monkeypatch.setattr(type(gateway.account), "drafts", property(lambda _: SimpleNamespace()))
    remote = gateway.get_server_draft("draft_timestamp")
    saved = DraftService(gateway.state, tmp_path / "recovery").create(remote)
    assert saved.subject == "Synthetic"
    assert type(remote.updated_at) is datetime


def test_message_reader_preserves_draft_kind_and_stored_folder_after_restart(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = projected_gateway
    assert gateway.state is not None
    gateway.state.put_reference(
        "draft_synthetic", "draft", "remote-id", change_key="key", folder_path="/Drafts"
    )
    monkeypatch.setattr(
        gateway.account,
        "fetch",
        lambda **_: [
            Message(account=gateway.account, id="remote-id", changekey="key", subject="Synthetic")
        ],
    )

    detail = gateway.show_message("draft_synthetic")

    assert detail.summary.reference == "draft_synthetic"
    assert detail.summary.folder_path == "/Drafts"
    assert gateway.state.resolve_reference("draft_synthetic")["item_kind"] == "draft"


def test_explicit_reconnect_drops_objects_bound_to_old_account(
    projected_gateway: EWSGateway,
) -> None:
    gateway = projected_gateway
    previous = gateway.account
    gateway._remember(
        "message",
        Message(account=previous, id="remote-id", changekey="key"),
        reference="msg_synthetic",
    )
    replacement = SimpleNamespace()
    gateway._account_factory = lambda: replacement
    gateway.reconnect()
    assert gateway._memory_object("msg_synthetic", "message") is None


def test_verified_attachment_upload_survives_restart_without_downloading_content(
    projected_gateway: EWSGateway,
) -> None:
    gateway = projected_gateway
    remote = FileAttachment(
        name="synthetic.txt", size=4, attachment_id=AttachmentId(id="remote-attachment")
    )
    local = FileAttachment(name="synthetic.txt", content=b"test")
    item = SimpleNamespace(id="draft-remote", attachments=[remote])
    gateway._record_attachment_uploads(item, [local])
    assert gateway.state is not None
    restarted = EWSGateway(AppConfig(), state=StateStore(gateway.state.path))

    # The remote FileAttachment has no account/content; fetching .content would
    # raise and violate the fixture's network guard.
    assert restarted._pending_attachments(item, [local]) == []
    with pytest.raises(ConflictError):
        restarted._pending_attachments(
            item, [FileAttachment(name="synthetic.txt", content=b"edit")]
        )


def test_server_draft_attachment_listing_and_delete_keep_its_reference_kind(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = projected_gateway
    assert gateway.state is not None
    gateway.state.put_reference("draft_synthetic", "draft", "before-delete", change_key="key")
    monkeypatch.setattr(
        gateway.account,
        "fetch",
        lambda **_: [Message(account=gateway.account, id="before-delete", changekey="key")],
    )
    assert gateway.list_attachments("draft_synthetic") == ()
    monkeypatch.setattr(gateway, "_resolve_folder", lambda _: SimpleNamespace(id="trash-folder"))
    monkeypatch.setattr(gateway, "_folder_path", lambda _: "/Trash")
    monkeypatch.setattr(Message, "move", lambda *_: ("after-delete", "new-key"))
    assert gateway.delete_message("draft_synthetic") == "draft_synthetic"
    assert gateway.state.resolve_reference("draft_synthetic")["item_kind"] == "draft"
    assert gateway.state.resolve_reference("draft_synthetic")["ews_id"] == "after-delete"


def test_message_pagination_bounds_the_actual_server_query_and_skips_preceding_pages(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = projected_gateway
    gateway._bindings()
    root = Root(account=gateway.account, id="root-synthetic")
    folder = Folder(root=root, id="folder-synthetic", name="Synthetic")
    requests: list[dict[str, Any]] = []

    def find_items(_collection: Any, *_args: Any, **kwargs: Any) -> list[Message]:
        requests.append(kwargs)
        return [Message(account=gateway.account, id="synthetic-item", changekey="key")]

    monkeypatch.setattr(FolderCollection, "find_items", find_items)
    monkeypatch.setattr(gateway.account, "fetch", lambda *, ids, **_: iter(ids))
    assert len(gateway._bounded_items(folder, fields=SUMMARY_FIELDS, limit=3, offset=400)) == 1
    assert len(requests) == 1
    assert requests[0]["page_size"] == 3
    assert requests[0]["max_items"] == 3
    assert requests[0]["offset"] == 400


def test_folder_pagination_passes_limits_and_offset_to_find_folder(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = projected_gateway
    root = Root(account=gateway.account, id="root-synthetic", name="Root")
    folder = Folder(root=root, id="parent-synthetic", name="Parent")
    requests: list[dict[str, Any]] = []

    def find_folders(_collection: Any, **kwargs: Any) -> list[Folder]:
        requests.append(kwargs)
        return [Folder(root=root, id="child-synthetic", name="Child")]

    monkeypatch.setattr(gateway, "_resolve_folder", lambda _: folder)
    monkeypatch.setattr(FolderCollection, "find_folders", find_folders)
    assert len(gateway.list_folders(parent_reference="folder-parent", limit=3, offset=20)) == 1
    assert len(requests) == 1
    assert requests[0]["page_size"] == 3
    assert requests[0]["max_items"] == 3
    assert requests[0]["offset"] == 20
    assert requests[0]["depth"] == "Shallow"


@pytest.mark.parametrize("operation", ["read", "update"])
def test_sent_item_cannot_be_reopened_or_overwritten_as_a_draft(
    projected_gateway: EWSGateway, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from gucmail.errors import ConflictError
    from gucmail.models import DraftData

    gateway = projected_gateway
    gateway.state.put_reference("draft_sent", "draft", "remote-sent", change_key="key")
    message = Message(account=gateway.account, id="remote-sent", changekey="key", is_draft=False)
    monkeypatch.setattr(gateway.account, "fetch", lambda **_: [message])
    with pytest.raises(ConflictError, match="not an unsent draft"):
        if operation == "read":
            gateway.get_server_draft("draft_sent")
        else:
            gateway.save_server_draft(DraftData(reference="draft_local", server_id="draft_sent"))


def test_task_search_restricts_server_before_limit_and_requests_no_body(
    projected_gateway, monkeypatch
):
    from datetime import date
    from decimal import Decimal

    from exchangelib import Task
    from exchangelib.folders import Tasks
    from exchangelib.restriction import Restriction
    from lxml import etree

    from gucmail.services.tasks import TaskQuery

    gateway = projected_gateway
    root = Root(account=gateway.account, id="root-synthetic")
    folder = Tasks(root=root, id="tasks-synthetic")
    monkeypatch.setattr(gateway, "_tasks_folder", lambda _: folder)
    requests = []

    def find_items(collection, *args, **kwargs):
        requests.append(kwargs)
        restriction = args[0].to_xml(
            folders=[folder], version=gateway.account.version, applies_to=Restriction.ITEMS
        )
        xml = etree.tostring(restriction).decode()
        assert "task:DueDate" in xml and "item:Body" in xml
        assert 'ContainmentMode="FullString"' in xml
        return [
            Task(
                account=gateway.account,
                id="match-beyond-first-page",
                changekey="key",
                subject="Synthetic match",
                status="NotStarted",
                percent_complete=Decimal(0),
            )
        ]

    monkeypatch.setattr(FolderCollection, "find_items", find_items)
    fetched = []

    def fetch(*, ids, **kwargs):
        fetched.append(kwargs)
        return iter(ids)

    monkeypatch.setattr(gateway.account, "fetch", fetch)
    rows = gateway.search_tasks(
        TaskQuery(
            text="Synthetic", due_before=date(2026, 9, 11), category="Work", exclude_completed=True
        ),
        folder_reference=None,
        limit=3,
    )
    assert len(rows) == 1 and len(requests) == 1
    assert requests[0]["max_items"] == requests[0]["page_size"] == 3
    assert requests[0]["additional_fields"] is None
    assert all(field.field.name != "body" for field in fetched[0]["only_fields"])


def test_contact_filters_restrict_server_before_limit_without_fetching_notes(
    projected_gateway, monkeypatch
):
    from exchangelib import Contact
    from exchangelib.folders import Contacts
    from exchangelib.indexed_properties import EmailAddress
    from exchangelib.restriction import Restriction
    from lxml import etree

    from gucmail.services.contacts import ContactQuery

    gateway = projected_gateway
    folder = Contacts(root=Root(account=gateway.account, id="root-synthetic"), id="contacts-test")
    monkeypatch.setattr(gateway, "_contacts_folder", lambda _: folder)
    requests = []
    projections = []

    def find_items(collection, *args, **kwargs):
        requests.append(kwargs)
        xml = etree.tostring(
            args[0].to_xml(
                folders=[folder], version=gateway.account.version, applies_to=Restriction.ITEMS
            )
        ).decode()
        assert "contacts:CompanyName" in xml and "item:Categories" in xml
        for label in ("EmailAddress1", "EmailAddress2", "EmailAddress3"):
            assert f'FieldIndex="{label}"' in xml
        return [
            Contact(
                account=gateway.account,
                id="match-beyond-original-page",
                changekey="key",
                display_name="Synthetic contact",
                email_addresses=[EmailAddress(label="EmailAddress3", email="test@example.test")],
                company_name="Synthetic company",
                categories=["Work"],
            )
        ]

    def fetch(*, ids, **kwargs):
        projections.append(kwargs["only_fields"])
        return iter(ids)

    monkeypatch.setattr(FolderCollection, "find_items", find_items)
    monkeypatch.setattr(gateway.account, "fetch", fetch)
    result = gateway.search_contacts(
        ContactQuery(email="example.test", company="synthetic", category="work"),
        folder_reference=None,
        limit=3,
    )
    assert len(result) == len(requests) == 1
    assert result[0].emails[0].email == "test@example.test"
    assert requests[0]["max_items"] == requests[0]["page_size"] == 3
    assert all(field.field.name != "body" for field in projections[0])


@pytest.mark.parametrize("kind", ["event", "calendar", "contact", "task"])
@pytest.mark.parametrize("provider_returns_identity", [True, False])
def test_pim_soft_delete_preserves_reference_across_restart(
    projected_gateway, monkeypatch, kind, provider_returns_identity
):
    from exchangelib import CalendarItem, Contact, Task

    gateway = projected_gateway
    provider_type = {
        "event": CalendarItem,
        "calendar": CalendarItem,
        "contact": Contact,
        "task": Task,
    }[kind]
    item = provider_type(account=gateway.account, id="before-delete", changekey="old-key")
    reference = gateway._remember(kind, item)
    trash = SimpleNamespace(id="trash-folder")
    monkeypatch.setattr(type(gateway.account), "trash", property(lambda _: trash))
    monkeypatch.setattr(gateway, "_folder_path", lambda _: "/Deleted Items")

    def move(value, target):
        assert target is trash
        assert value.id == "before-delete"
        if provider_returns_identity:
            return "after-delete", "new-key"
        value.id = "after-delete"
        value.changekey = "new-key"
        return None

    monkeypatch.setattr(provider_type, "move", move)
    if kind == "contact":

        def fetch(account, *, ids, only_fields):
            assert ids == [("before-delete", "old-key")]
            assert "item_class" in only_fields
            return [
                Contact(
                    account=account,
                    id="before-delete",
                    changekey="old-key",
                    item_class="IPM.Contact",
                )
            ]

        monkeypatch.setattr(Account, "fetch", fetch)
    plan = SimpleNamespace(
        reference=reference, scope="occurrence", send_cancellation=False, hard_delete=False
    )
    getattr(gateway, "delete_event" if kind in {"event", "calendar"} else f"delete_{kind}")(plan)
    row = gateway.state.resolve_reference(reference, kind)
    assert row["ews_id"] == "after-delete"
    assert row["change_key"] == "new-key"
    assert row["folder_path"] == "/Deleted Items"
    restarted = EWSGateway(
        AppConfig(), state=StateStore(gateway.state.path), account_factory=lambda: gateway.account
    )
    resolved_kind, restored = restarted._resolve_item(reference, kind)
    assert resolved_kind == kind
    assert restored.id == "after-delete" and restored.changekey == "new-key"


def test_calendar_series_delete_keeps_master_and_occurrence_references_distinct(
    projected_gateway, monkeypatch
):
    from exchangelib import CalendarItem

    gateway = projected_gateway
    occurrence = CalendarItem(
        account=gateway.account, id="occurrence", changekey="key", type="Occurrence"
    )
    master = CalendarItem(
        account=gateway.account, id="master", changekey="key", type="RecurringMaster"
    )
    occurrence_ref = gateway._remember("event", occurrence)
    master_ref = gateway._remember("event", master)
    trash = SimpleNamespace(id="trash-folder")
    monkeypatch.setattr(type(gateway.account), "trash", property(lambda _: trash))
    monkeypatch.setattr(gateway, "_folder_path", lambda _: "/Deleted Items")
    monkeypatch.setattr(CalendarItem, "recurring_master", lambda _: master)
    monkeypatch.setattr(CalendarItem, "refresh", lambda _: None)

    def move(item, target):
        assert item.id == "master" and target is trash
        return "moved-master", "new-key"

    monkeypatch.setattr(CalendarItem, "move", move)
    gateway.delete_event(
        SimpleNamespace(reference=occurrence_ref, scope="series", send_cancellation=False)
    )
    assert gateway.state.resolve_reference(master_ref)["ews_id"] == "moved-master"
    assert gateway.state.resolve_reference(occurrence_ref)["ews_id"] == "occurrence"
