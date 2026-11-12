from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gucmail.config import AppConfig
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    NetworkError,
    SendOutcomeUncertainError,
    ThrottledError,
)
from gucmail.ews.adapter import (
    CALENDAR_METADATA_FIELDS,
    CONTACT_METADATA_FIELDS,
    TASK_METADATA_FIELDS,
    EWSGateway,
    translate_ews_error,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    DraftData,
    DraftLocation,
    FolderInfo,
    Importance,
    MessageSummary,
    SearchQuery,
)
from gucmail.services.backup import BackupRequest, BackupSelection
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.search import build_search_plan, parse_query
from gucmail.state import StateStore


class FakeProtocol:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def resolve_names(self, *_args, **_kwargs):
        return [
            SimpleNamespace(
                email_address="person@example.test",
                name="Example Person",
                mailbox_type="Mailbox",
            )
        ]

    def get_free_busy_info(self, *_args, **_kwargs):
        return [
            SimpleNamespace(
                view_type="DetailedMerged",
                merged="0022",
                calendar_events=(),
            )
        ]


class FakeMessage:
    def __init__(self) -> None:
        self.id = "ews-message-id"
        self.changekey = "change-key"
        self.is_read = False
        self.categories = ["Existing"]
        self.importance = "Normal"
        self.saved_fields: list[list[str]] = []
        self.moved_to: list[object] = []
        self.delete_calls = 0

    def save(self, *, update_fields: list[str]) -> None:
        self.saved_fields.append(update_fields)

    def move(self, target: object) -> tuple[str, str]:
        self.moved_to.append(target)
        self.id = "replacement-id"
        self.changekey = "replacement-key"
        return self.id, self.changekey

    def delete(self) -> None:
        self.delete_calls += 1


class FakeFolder:
    def __init__(
        self,
        folder_id: str,
        name: str,
        *,
        parent_id: str | None = None,
    ) -> None:
        self.id = folder_id
        self.changekey = f"{folder_id}-change"
        self.name = name
        self.parent_folder_id = SimpleNamespace(id=parent_id) if parent_id else None
        self.is_deletable = True
        self.move_calls: list[object] = []
        self.saved_fields: list[list[str]] = []

    def save(self, *, update_fields: list[str]) -> None:
        self.saved_fields.append(update_fields)

    def move(self, destination: Any) -> None:
        self.move_calls.append(destination)
        self.changekey = "moved-change"
        self.parent_folder_id = SimpleNamespace(id=destination.id)


def _gateway(message: FakeMessage) -> tuple[EWSGateway, object]:
    trash = SimpleNamespace(id="trash-id", name="Deleted Items")
    root = SimpleNamespace(id="root-id", name="Root", children=())
    account = SimpleNamespace(
        trash=trash,
        msg_folder_root=root,
        root=root,
        protocol=FakeProtocol(),
    )
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    gateway._remember("message", message, reference="msg_synthetic")
    return gateway, trash


def test_normal_delete_moves_to_trash_and_permanent_delete_is_explicit() -> None:
    message = FakeMessage()
    gateway, trash = _gateway(message)

    gateway.delete_message("msg_synthetic")
    assert message.moved_to == [trash]
    assert message.delete_calls == 0

    gateway.delete_message("msg_synthetic", permanent=True)
    assert message.delete_calls == 1


def test_folder_move_preserves_local_reference_and_reconciles_parent_path_cache() -> None:
    gateway, _ = _gateway(FakeMessage())
    source = FakeFolder("folder-source", "Projects", parent_id="folder-old-parent")
    destination = FakeFolder("folder-destination", "Archive")
    gateway._remember(
        "folder",
        source,
        reference="folder_synthetic",
        folder_path="/Old/Projects",
    )
    gateway._remember(
        "folder",
        destination,
        reference="folder_destination",
        folder_path="/Archive",
    )
    gateway._folder_paths["folder-child"] = "/Old/Projects/Child"

    moved = gateway.move_folder("folder_synthetic", "folder_destination")

    assert source.move_calls == [destination]
    assert moved.reference == "folder_synthetic"
    assert moved.parent_reference == "folder_destination"
    assert moved.path == "/Archive/Projects"
    assert moved.change_key == "moved-change"
    assert "folder-child" not in gateway._folder_paths
    assert gateway._folder_paths["folder-source"] == "/Archive/Projects"

    moved_to_root = gateway.move_folder("folder_synthetic", None)
    assert source.move_calls[-1] is gateway.account.msg_folder_root
    assert moved_to_root.reference == "folder_synthetic"
    assert moved_to_root.parent_reference
    assert gateway.get_folder(moved_to_root.parent_reference).path == "/Root"
    assert moved_to_root.path == "/Root/Projects"


def test_folder_paths_round_trip_localized_slash_and_backslash_display_names() -> None:
    gateway, _ = _gateway(FakeMessage())
    folder = FakeFolder("folder-unusual", "研究/2026\\Raw")

    path = gateway._folder_path(folder, parent_path="/Projekte")

    assert path == "/Projekte/研究\\/2026\\\\Raw"
    assert gateway._folder_parts(path) == ("Projekte", "研究/2026\\Raw")
    assert gateway._folder_name("研究/2026\\Raw") == "研究/2026\\Raw"


def test_folder_rename_invalidates_selected_and_descendant_cached_paths() -> None:
    gateway, _ = _gateway(FakeMessage())
    parent = FakeFolder("folder-parent", "Parent")
    source = FakeFolder("folder-source", "Old", parent_id=parent.id)
    source.parent = parent
    gateway._remember(
        "folder",
        parent,
        reference="folder_parent",
        folder_path="/Grandparent/Parent",
    )
    gateway._remember(
        "folder",
        source,
        reference="folder_source",
        folder_path="/Grandparent/Parent/Old",
    )
    gateway._folder_paths["folder-child"] = "/Grandparent/Parent/Old/Child"

    renamed = gateway.rename_folder("folder_source", "研究/2026")

    assert source.saved_fields == [["name"]]
    assert renamed.reference == "folder_source"
    assert renamed.path == "/Grandparent/Parent/研究\\/2026"
    assert "folder-child" not in gateway._folder_paths
    assert gateway._folder_paths["folder-source"] == renamed.path


def test_message_updates_use_explicit_field_lists() -> None:
    message = FakeMessage()
    gateway, _ = _gateway(message)

    gateway.mark_read("msg_synthetic", True)
    gateway.categorize("msg_synthetic", ["New"], mode="add")
    gateway.set_importance("msg_synthetic", Importance.HIGH)

    assert message.is_read
    assert message.categories == ["Existing", "New"]
    assert message.importance == "High"
    assert message.saved_fields == [["is_read"], ["categories"], ["importance"]]


def test_directory_and_availability_results_are_provider_neutral() -> None:
    gateway, _ = _gateway(FakeMessage())
    assert gateway.search_directory("Example")[0].email == "person@example.test"

    start = datetime(2026, 8, 5, 9, tzinfo=UTC)
    result = gateway.get_availability(
        [Address("person@example.test")],
        start=start,
        end=start + timedelta(hours=1),
    )
    assert result[0]["attendee"] == Address("person@example.test")
    assert result[0]["merged"] == "0022"


def test_metadata_only_pim_lists_use_body_free_projections() -> None:
    gateway, _ = _gateway(FakeMessage())
    account = gateway.account
    account.calendar = SimpleNamespace()
    account.contacts = SimpleNamespace()
    account.tasks = SimpleNamespace()
    captured: list[tuple[str, ...]] = []

    def bounded_items(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: object | None = None,
    ) -> tuple[object, ...]:
        del limit, offset, order_by, filters
        captured.append(fields)
        return ()

    gateway._bounded_items = bounded_items  # type: ignore[method-assign]
    gateway.list_contacts(folder_reference=None, limit=3)
    gateway.list_tasks(folder_reference=None, limit=3)

    assert captured == [CONTACT_METADATA_FIELDS, TASK_METADATA_FIELDS]
    assert "body" not in CALENDAR_METADATA_FIELDS
    assert "body" not in CONTACT_METADATA_FIELDS
    assert "body" not in TASK_METADATA_FIELDS
    assert "email_addresses" in CONTACT_METADATA_FIELDS


def test_backup_estimate_never_retrieves_body_mime_or_attachment_payload() -> None:
    gateway, _ = _gateway(FakeMessage())
    gateway._backup_folders = lambda _roots: [  # type: ignore[method-assign]
        FolderInfo(
            reference="folder-synthetic",
            name="Synthetic",
            path="Synthetic",
            parent_reference=None,
            folder_class="IPF.Note",
        )
    ]
    gateway.list_messages = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        MessageSummary(
            reference="message-synthetic",
            subject="Synthetic metadata",
            sender=None,
            received_at=None,
            has_attachments=True,
            size=2048,
            folder_path="Synthetic",
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata-only estimate retrieved backup content")

    gateway.show_message = forbidden  # type: ignore[method-assign]
    gateway.raw_message = forbidden  # type: ignore[method-assign]
    gateway.open_attachment = forbidden  # type: ignore[method-assign]
    gateway._backup_message = forbidden  # type: ignore[method-assign]

    estimate = gateway.estimate_backup(
        BackupRequest(selection=BackupSelection(item_kinds=("message",)))
    )
    assert estimate.item_count == 1
    assert estimate.attachment_count == 1
    assert estimate.estimated_bytes >= 2048
    assert estimate.unknown_size_count == 1


def test_attachment_listing_projects_only_metadata_and_primes_explicit_download() -> None:
    message = FakeMessage()
    gateway, _ = _gateway(message)
    attachment = SimpleNamespace(
        attachment_id=SimpleNamespace(id="attachment-id"),
        name="report.pdf",
        size=4_096,
        content_type="application/pdf",
        is_inline=False,
        fp=object(),
    )
    projected = SimpleNamespace(
        id=message.id,
        changekey=message.changekey,
        attachments=(attachment,),
    )
    requested: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    def fetch_only(
        reference: str,
        kinds: tuple[str, ...],
        fields: tuple[str, ...],
    ) -> object:
        requested.append((reference, kinds, fields))
        return projected

    gateway._fetch_item = fetch_only  # type: ignore[method-assign]
    gateway.show_message = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("attachment metadata listing fetched full message detail")
    )

    listed = gateway.list_attachments("msg_synthetic", limit=1)

    assert requested == [("msg_synthetic", ("message", "draft"), ("attachments",))]
    assert listed == (
        AttachmentInfo(
            reference=listed[0].reference,
            name="report.pdf",
            size=4_096,
            content_type="application/pdf",
        ),
    )
    assert gateway._attachment_parents[listed[0].reference] == "msg_synthetic"
    assert gateway._memory_object(listed[0].reference, "attachment") == (
        "attachment",
        attachment,
    )


def test_advanced_search_pushes_body_and_id_and_boundedly_filters_details() -> None:
    gateway, _ = _gateway(FakeMessage())
    folder = object()
    captured: list[tuple[tuple[str, ...], dict[str, object], int]] = []
    candidate = SimpleNamespace(
        id="search-message-id",
        changekey="search-change-key",
        subject="Synthetic search candidate",
        sender=SimpleNamespace(email_address="sender@example.test", name="Sender"),
        datetime_received=datetime(2026, 8, 5, tzinfo=UTC),
        datetime_sent=None,
        is_read=False,
        has_attachments=True,
        importance="Normal",
        categories=(),
        size=2048,
        parent_folder_id=None,
        conversation_id=None,
        gucmail_flag_status=None,
        to_recipients=(SimpleNamespace(email_address="student@example.test", name="Student"),),
        cc_recipients=(),
        bcc_recipients=(),
        reply_to=(),
        body="Synthetic deadline body",
        sensitivity="Normal",
        message_id="<synthetic@example.test>",
        in_reply_to=None,
        references=(),
        headers=(SimpleNamespace(name="X-Synthetic-Route", value="alpha beta"),),
        attachments=(
            SimpleNamespace(
                name="brief.pdf",
                size=100,
                content_type="application/pdf",
                is_inline=False,
                content_id=None,
            ),
        ),
    )

    gateway._resolve_folder = lambda _folder: folder  # type: ignore[method-assign]
    gateway._folder_path = lambda _folder: "Inbox"  # type: ignore[method-assign]

    def bounded_items(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> tuple[object, ...]:
        del offset, order_by
        captured.append((fields, dict(filters or {}), limit))
        return (candidate,)

    gateway._bounded_items = bounded_items  # type: ignore[method-assign]
    detailed = gateway.search_messages(
        SearchQuery(recipient="student@example.test", attachment_name="brief.pdf"),
        limit=5,
    )
    assert [item.reference for item in detailed]
    assert "to_recipients" in captured[-1][0]
    assert "attachments" in captured[-1][0]
    assert "body" not in captured[-1][0]
    assert captured[-1][1]["has_attachments"] is True
    assert captured[-1][2] <= 500

    gateway.search_messages(
        SearchQuery(body="deadline", internet_message_id="<synthetic@example.test>"),
        limit=5,
    )
    assert captured[-1][1]["body__icontains"] == "deadline"
    assert captured[-1][1]["message_id"] == "<synthetic@example.test>"

    expression = parse_query('header:"alpha beta"')
    header_matches = gateway.search_messages_expression_planned(
        expression,
        build_search_plan(expression),
        limit=5,
    )
    assert [item.reference for item in header_matches]
    assert "headers" in captured[-1][0]
    assert captured[-1][1] == {}


def test_stale_session_retry_is_one_shot_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("gucmail.ews.adapter.time.sleep", delays.append)
    gateway, _ = _gateway(FakeMessage())
    protocol = gateway.account.protocol
    gateway._call("prime session", lambda: "ok", read_only=True)
    calls = 0

    def flaky_read() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("synthetic stale session")
        return "recovered"

    assert gateway._call("synthetic read", flaky_read, read_only=True) == "recovered"
    assert calls == 2
    assert protocol.close_calls == 1
    assert gateway.session_generation == 1
    assert delays == [2.0]

    mutation_calls = 0

    def failed_mutation() -> None:
        nonlocal mutation_calls
        mutation_calls += 1
        raise ConnectionError("synthetic mutation transport failure")

    with pytest.raises(NetworkError):
        gateway._call("synthetic mutation", failed_mutation)
    assert mutation_calls == 1
    assert protocol.close_calls == 1


def test_initial_read_send_and_throttle_are_never_automatically_retried() -> None:
    gateway, _ = _gateway(FakeMessage())
    protocol = gateway.account.protocol
    initial_calls = 0

    def initial_failure() -> None:
        nonlocal initial_calls
        initial_calls += 1
        raise ConnectionError("synthetic initial connection failure")

    with pytest.raises(NetworkError):
        gateway._call("initial read", initial_failure, read_only=True)
    assert initial_calls == 1
    assert protocol.close_calls == 0

    gateway._call("prime session", lambda: None, read_only=True)
    send_calls = 0

    def uncertain_send() -> None:
        nonlocal send_calls
        send_calls += 1
        raise ConnectionError("synthetic ambiguous send")

    with pytest.raises(SendOutcomeUncertainError):
        gateway._call(
            "send",
            uncertain_send,
            send_may_have_completed=True,
        )
    assert send_calls == 1

    class ErrorServerBusy(Exception):
        wait = 17

    throttle_calls = 0

    def throttled_read() -> None:
        nonlocal throttle_calls
        throttle_calls += 1
        raise ErrorServerBusy

    with pytest.raises(ThrottledError) as caught:
        gateway._call("throttled read", throttled_read, read_only=True)
    assert throttle_calls == 1
    assert caught.value.retry_after == 17
    assert protocol.close_calls == 0


def test_standard_folder_resolution_translates_transport_failures() -> None:
    class BrokenAccount:
        def __init__(self) -> None:
            self.protocol = FakeProtocol()

        @property
        def inbox(self) -> object:
            raise ConnectionError("private server response")

    gateway = EWSGateway(AppConfig(), account_factory=BrokenAccount)
    with pytest.raises(NetworkError) as caught:
        gateway._standard_folder("inbox")
    assert "private server response" not in str(caught.value)


def test_manual_reconnect_is_lazy_and_advances_the_session_generation() -> None:
    accounts: list[Any] = []

    def factory() -> Any:
        account = SimpleNamespace(protocol=FakeProtocol())
        accounts.append(account)
        return account

    gateway = EWSGateway(AppConfig(), account_factory=factory)
    first = gateway.account
    assert len(accounts) == 1

    gateway.reconnect()

    assert not gateway.connected
    assert gateway.session_generation == 1
    assert first.protocol.close_calls == 1
    assert gateway.account is not first
    assert len(accounts) == 2


class _CanonicalReferenceState:
    def put_reference(self, *_args: Any, **_kwargs: Any) -> str:
        return "msg_canonical"


def test_remember_adopts_the_state_store_canonical_reference() -> None:
    gateway = EWSGateway(
        AppConfig(),
        state=_CanonicalReferenceState(),  # type: ignore[arg-type]
    )
    item = SimpleNamespace(id="ews-canonical-id", changekey="ck-1")
    parent = object()

    reference = gateway._remember(
        "message",
        item,
        reference="msg_derived",
        parent=parent,
    )

    assert reference == "msg_canonical"
    assert gateway._memory_object("msg_canonical", "message") == ("message", item)
    assert gateway._memory_object("msg_derived", "message") is None
    assert gateway._parents[("message", "msg_canonical")] is parent


class _FakeAttachment:
    def __init__(self, *, name: str, content: bytes) -> None:
        self.name = name
        self.content = content
        self.size = len(content)


class _FakeServerDraft:
    next_id = 0

    def __init__(
        self,
        *,
        account: Any,
        folder: Any = None,
        id: str | None = None,
        changekey: str | None = None,
        **values: Any,
    ) -> None:
        self.account = account
        self.folder = folder
        self.id = id
        self.changekey = changekey
        self.attachments: list[Any] = list(values.pop("attachments", ()))
        self.saved_fields: list[list[str] | None] = []
        self.sent = False
        self.is_draft = True
        for name, value in values.items():
            setattr(self, name, value)

    def save(self, *, update_fields: list[str] | None = None) -> _FakeServerDraft:
        self.saved_fields.append(update_fields)
        if self.id is None:
            type(self).next_id += 1
            self.id = f"ews-draft-{type(self).next_id}"
        self.changekey = f"ck-{len(self.saved_fields)}-{len(self.attachments)}"
        return self

    def attach(self, attachments: list[Any]) -> None:
        self.attachments.extend(attachments)
        self.changekey = f"ck-attach-{len(self.attachments)}"

    def send(self, *, save_copy: bool) -> None:
        assert save_copy
        self.sent = True


class _FakeResponse:
    def __init__(self) -> None:
        self.is_read_receipt_requested = False
        self.is_delivery_receipt_requested = False
        self.saved_folder: Any = None

    def save(self, folder: Any) -> Any:
        self.saved_folder = folder
        return SimpleNamespace(id="ews-response-draft", changekey="response-ck")


class _FakeReplySource:
    def __init__(self) -> None:
        self.id = "ews-source"
        self.changekey = "source-ck"
        self.responses: list[_FakeResponse] = []

    def _response(self) -> _FakeResponse:
        response = _FakeResponse()
        self.responses.append(response)
        return response

    def create_reply(self, **_kwargs: Any) -> _FakeResponse:
        return self._response()

    def create_reply_all(self, **_kwargs: Any) -> _FakeResponse:
        return self._response()

    def create_forward(self, **_kwargs: Any) -> _FakeResponse:
        return self._response()


def _compose_gateway() -> tuple[EWSGateway, list[_FakeServerDraft], Any]:
    drafts = SimpleNamespace(id="drafts-folder", name="Drafts")
    protocol = FakeProtocol()
    account = SimpleNamespace(
        drafts=drafts,
        protocol=protocol,
        msg_folder_root=SimpleNamespace(id="root", name="Root", children=()),
        root=SimpleNamespace(id="root", name="Root", children=()),
    )
    created: list[_FakeServerDraft] = []

    def message_factory(**kwargs: Any) -> _FakeServerDraft:
        message = _FakeServerDraft(**kwargs)
        created.append(message)
        return message

    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: account,
        factory_overrides={
            "Body": lambda value: value,
            "HTMLBody": lambda value: value,
            "Mailbox": lambda **values: SimpleNamespace(**values),
            "FileAttachment": lambda **values: _FakeAttachment(**values),
            "Message": message_factory,
        },
    )
    return gateway, created, drafts


def test_server_draft_create_then_update_is_stable_and_attachment_safe(tmp_path: Path) -> None:
    gateway, created, _drafts = _compose_gateway()
    attachment = tmp_path / "notes.txt"
    attachment.write_bytes(b"synthetic attachment")
    local = DraftData(
        reference="local-draft",
        to=[Address("self@example.test")],
        subject="Initial subject",
        body_text="Synthetic body",
        attachment_paths=[attachment],
    )

    synced = gateway.save_server_draft(local)

    assert synced.location is DraftLocation.SERVER
    assert synced.server_id
    assert synced.server_change_key == created[0].changekey
    assert len(created[0].attachments) == 1

    updated = gateway.save_server_draft(
        replace(synced, subject="Updated subject", body_text="Updated body")
    )

    assert updated.server_id == synced.server_id
    assert created[0].subject == "Updated subject"
    assert created[0].saved_fields[1] == [
        "subject",
        "body",
        "to_recipients",
        "cc_recipients",
        "bcc_recipients",
        "importance",
        "is_read_receipt_requested",
        "is_delivery_receipt_requested",
    ]
    assert len(created[0].attachments) == 1
    remote = gateway.get_server_draft(updated.server_id or "")
    assert remote.server_id == updated.server_id
    assert remote.server_change_key == updated.server_change_key
    assert remote.subject == "Updated subject"
    assert remote.body_text == "Updated body"


def test_server_draft_update_rejects_a_stale_change_key() -> None:
    gateway, created, _drafts = _compose_gateway()
    synced = gateway.save_server_draft(DraftData(reference="local-draft", subject="Subject"))

    with pytest.raises(ConflictError):
        gateway.save_server_draft(replace(synced, server_change_key="stale-change-key"))

    assert created[0].saved_fields == [None]


@pytest.mark.parametrize("new_content", [b"other", b"different and larger"])
def test_server_draft_does_not_silently_ignore_a_changed_same_name_attachment(
    tmp_path: Path,
    new_content: bytes,
) -> None:
    gateway, created, _drafts = _compose_gateway()
    attachment = tmp_path / "notes.txt"
    attachment.write_bytes(b"first")
    synced = gateway.save_server_draft(
        DraftData(
            reference="local-draft",
            subject="Subject",
            attachment_paths=[attachment],
        )
    )
    attachment.write_bytes(new_content)

    with pytest.raises(ConflictError) as caught:
        gateway.save_server_draft(synced)

    assert "same name" in str(caught.value).casefold()
    assert len(created[0].attachments) == 1
    assert created[0].saved_fields == [None]


def test_invalid_local_attachment_does_not_create_a_server_draft(tmp_path: Path) -> None:
    gateway, created, _ = _compose_gateway()
    with pytest.raises(ConfigurationError):
        gateway.save_server_draft(
            DraftData(reference="dft-invalid", attachment_paths=[tmp_path / "missing.txt"])
        )
    assert created == []


@pytest.mark.parametrize("operation", ["reply", "reply_all", "forward"])
def test_response_attachments_are_added_to_a_saved_message_draft(
    operation: str,
    tmp_path: Path,
) -> None:
    gateway, created, drafts = _compose_gateway()
    source = _FakeReplySource()
    gateway._remember("message", source, reference="msg_source")
    attachment = tmp_path / "response.txt"
    attachment.write_bytes(b"synthetic response attachment")
    draft = DraftData(
        reference="response-local",
        to=[Address("self@example.test")],
        subject="Synthetic response",
        body_text="Response body",
        attachment_paths=[attachment],
        importance=Importance.HIGH,
        request_read_receipt=True,
        request_delivery_receipt=True,
    )

    result = getattr(gateway, operation)("msg_source", draft)

    assert result["accepted"] is True
    response = source.responses[-1]
    assert response.saved_folder is drafts
    assert response.is_read_receipt_requested
    assert response.is_delivery_receipt_requested
    sent = created[-1]
    assert sent.id == "ews-response-draft"
    assert len(sent.saved_fields) == 1
    assert {"importance", "body", "to_recipients"} <= set(sent.saved_fields[0] or ())
    assert sent.body == draft.body_text
    assert sent.importance == "High"
    assert len(sent.attachments) == 1
    assert sent.sent


@pytest.mark.parametrize("kind", ["reply", "forward"])
def test_saved_response_reopens_edits_and_sends_the_same_server_draft(kind: str) -> None:
    gateway, created, _ = _compose_gateway()
    source = _FakeReplySource()
    gateway._remember("message", source, reference="msg_source")
    draft = DraftData(
        reference="dft-response",
        to=[Address("synthetic@example.test")],
        subject="Synthetic response",
        body_text="Reviewed body with exactly one quote",
        reply_to_reference="msg_source" if kind == "reply" else None,
        forward_of_reference="msg_source" if kind == "forward" else None,
    )
    saved = gateway.save_server_draft(draft)
    assert len(source.responses) == 1
    reopened = gateway.get_server_draft(saved.server_id or "")
    edited = gateway.save_server_draft(replace(reopened, subject="Edited synthetic response"))
    gateway.send(edited)
    assert len(source.responses) == 1
    assert len(created) == 1
    assert created[0].sent
    assert created[0].subject == "Edited synthetic response"
    assert created[0].body == draft.body_text


def test_error_translation_reports_actionable_send_failures_without_raw_details() -> None:
    class ErrorQuotaExceeded(Exception):
        pass

    class ErrorInvalidRecipients(Exception):
        pass

    class ErrorMessageSizeExceeded(Exception):
        pass

    quota = translate_ews_error(ErrorQuotaExceeded("private"), operation="send")
    recipient = translate_ews_error(ErrorInvalidRecipients("private"), operation="send")
    oversized = translate_ews_error(ErrorMessageSizeExceeded("private"), operation="send")
    invalid = translate_ews_error(ValueError("private@example.test"), operation="send")

    assert isinstance(quota, ConflictError)
    assert quota.context.retry_safe is False
    assert isinstance(recipient, ConfigurationError)
    assert isinstance(oversized, ConfigurationError)
    assert "private" not in str(quota)
    assert "private" not in str(recipient)
    assert "private" not in str(oversized)
    assert "private@example.test" not in str(invalid)


def test_bounded_poll_drains_burst_and_preserves_equal_timestamp_boundary() -> None:
    gateway, _ = _gateway(FakeMessage())
    threshold = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
    burst = tuple(
        MessageSummary(
            reference=f"msg-{number}",
            subject=f"Message {number}",
            sender=None,
            received_at=threshold,
            folder_path="Inbox",
        )
        for number in range(5)
    )
    offsets: list[int] = []

    def list_messages(
        folder: str,
        *,
        limit: int,
        offset: int,
        order_by: str,
    ) -> tuple[MessageSummary, ...]:
        assert folder == "inbox"
        assert order_by == "-datetime_received"
        offsets.append(offset)
        return burst[offset : offset + limit]

    gateway.list_messages = list_messages  # type: ignore[method-assign]
    cursor: str | None = threshold.isoformat()
    observed: list[str] = []
    for _ in range(3):
        batch = gateway.poll_new_messages("inbox", cursor, 2)
        observed.extend(message.reference for message in batch["items"])
        cursor = batch["checkpoint"]
        if not batch["more_available"]:
            break

    assert gateway.monitor_strategy == "bounded_poll"
    assert offsets == [0, 1, 3]
    assert observed == [f"msg-{number}" for number in range(5)]
    assert cursor == threshold.isoformat()


@pytest.mark.parametrize("change", ["remove", "insert"])
def test_bounded_poll_rejects_shifted_page_without_advancing_checkpoint(
    tmp_path: Path, change: str
) -> None:
    gateway, _ = _gateway(FakeMessage())
    threshold = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    messages = [
        MessageSummary(
            reference=f"msg-{number}",
            subject="Synthetic",
            sender=None,
            received_at=threshold + timedelta(minutes=number),
            folder_path="Inbox",
        )
        for number in range(20, 15, -1)
    ]
    calls = 0

    def list_messages(
        folder: str, *, limit: int, offset: int, order_by: str
    ) -> tuple[MessageSummary, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "remove":
                messages.pop(0)
            else:
                messages.insert(0, replace(messages[0], reference="newer"))
        return tuple(messages[offset : offset + limit])

    gateway.list_messages = list_messages  # type: ignore[method-assign]
    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(gateway, state, account_id="synthetic", limit=2)
    state.checkpoint(monitor._scope("inbox"), sync_state=threshold.isoformat(), succeeded=True)

    result = monitor.run_once()

    assert not result.succeeded
    assert result.events == ()
    assert monitor.status().folders["inbox"]["sync_state"] == threshold.isoformat()
    assert calls == 2

    # A stable retry begins at the old watermark and sees the remaining items.
    retry = monitor.run_once()
    assert retry.succeeded
    assert {event.message_reference for event in retry.events} == {
        message.reference for message in messages
    }


def test_bounded_poll_restarts_legacy_continuation_and_single_item_pages() -> None:
    gateway, _ = _gateway(FakeMessage())
    threshold = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    messages = tuple(
        MessageSummary(
            reference=f"msg-{number}",
            subject="Synthetic",
            sender=None,
            received_at=threshold,
            folder_path="Inbox",
        )
        for number in range(3)
    )
    calls: list[tuple[int, int]] = []

    def list_messages(
        folder: str, *, limit: int, offset: int, order_by: str
    ) -> tuple[MessageSummary, ...]:
        calls.append((offset, limit))
        return messages[offset : offset + limit]

    gateway.list_messages = list_messages  # type: ignore[method-assign]
    cursor: str | None = (
        'gucmail-poll-v2:{"threshold":"2026-08-05T12:00:00+00:00",'
        '"offset":2,"newest":"2026-08-05T12:00:00+00:00","version":2}'
    )
    seen: list[str] = []
    for _ in range(4):
        batch = gateway.poll_new_messages("inbox", cursor, 1)
        seen.extend(item.reference for item in batch["items"])
        cursor = batch["checkpoint"]
        if not batch["more_available"]:
            break
    assert seen == ["msg-0", "msg-1", "msg-2"]
    assert calls == [(0, 1), (0, 2), (1, 2), (2, 2)]
    assert cursor == threshold.isoformat()
