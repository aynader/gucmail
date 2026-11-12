from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gucmail.config import AppConfig
from gucmail.errors import NetworkError
from gucmail.ews.adapter import SUMMARY_FIELDS, EWSGateway
from gucmail.ews.mapping import make_local_reference
from gucmail.state import StateStore


class _Protocol:
    def close(self) -> None:
        return None


def _account() -> tuple[Any, Any, Any]:
    inbox = SimpleNamespace(id="folder-inbox", name="Inbox", messages=[])
    trash = SimpleNamespace(id="folder-trash", name="Deleted Items", messages=[])
    root = SimpleNamespace(id="folder-root", name="Root", children=(inbox, trash))
    account = SimpleNamespace(
        inbox=inbox,
        trash=trash,
        msg_folder_root=root,
        root=root,
        protocol=_Protocol(),
    )
    return account, inbox, trash


class _MetadataOnlyMessage:
    id = "ews-metadata-only"
    changekey = "metadata-ck"
    subject = "Metadata only"
    sender = None
    author = None
    datetime_received = datetime(2026, 8, 5, 12, tzinfo=UTC)
    datetime_sent = None
    is_read = False
    has_attachments = False
    importance = "Normal"
    categories: tuple[str, ...] = ()
    size = 128
    conversation_id = None
    gucmail_flag_status = None

    @property
    def text_body(self) -> str:
        raise AssertionError("metadata-only listing probed text_body")

    @property
    def body(self) -> str:
        raise AssertionError("metadata-only listing probed body")


def test_message_list_uses_a_body_free_projection_and_mapper() -> None:
    account, inbox, _trash = _account()
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    captured: list[tuple[str, ...]] = []

    def bounded_items(
        folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: object | None = None,
    ) -> tuple[object, ...]:
        del limit, offset, order_by, filters
        assert folder is inbox
        captured.append(fields)
        return (_MetadataOnlyMessage(),)

    gateway._bounded_items = bounded_items  # type: ignore[method-assign]

    summaries = gateway.list_messages("inbox", limit=1)

    assert captured == [SUMMARY_FIELDS]
    assert not {
        "body",
        "text_body",
        "mime_content",
        "headers",
        "attachments",
        "to_recipients",
        "cc_recipients",
        "bcc_recipients",
        "parent_folder_id",
    }.intersection(SUMMARY_FIELDS)
    assert summaries[0].preview is None


def test_folder_root_transport_failures_are_translated_and_redacted() -> None:
    class BrokenRootAccount:
        protocol = _Protocol()

        @property
        def msg_folder_root(self) -> object:
            raise ConnectionError("private root response")

    gateway = EWSGateway(AppConfig(), account_factory=BrokenRootAccount)

    with pytest.raises(NetworkError) as caught:
        gateway._resolve_folder(None)

    assert "private root response" not in str(caught.value)
    assert "folder root" in str(caught.value).casefold()


class _MovingMessage:
    def __init__(self, remote_id: str, folder: Any) -> None:
        self.id = remote_id
        self.changekey = f"ck-{remote_id}"
        self.folder = folder
        self.subject = "Synthetic move cycle"
        self.sender = None
        self.author = None
        self.datetime_received = datetime(2026, 8, 5, 12, tzinfo=UTC)
        self.datetime_sent = None
        self.is_read = False
        self.has_attachments = False
        self.importance = "Normal"
        self.categories: list[str] = []
        self.size = 256
        self.conversation_id = None
        self.gucmail_flag_status = None
        self.to_recipients: list[Any] = []
        self.cc_recipients: list[Any] = []
        self.bcc_recipients: list[Any] = []
        self.reply_to: list[Any] = []
        self.body = "Synthetic body"
        self.sensitivity = "Normal"
        self.message_id = "<synthetic@example.test>"
        self.in_reply_to = None
        self.references: list[str] = []
        self.headers: list[Any] = []
        self.attachments: list[Any] = []

    def move(self, target: Any) -> None:
        if target.name == "Deleted Items":
            self.id = "ews-deleted"
            self.changekey = "ck-deleted"
        else:
            self.id = "ews-restored"
            self.changekey = "ck-restored"
        self.folder = target
        # exchangelib mutates the item and returns None for ordinary mailbox moves.
        return None


def test_delete_relist_restore_keeps_one_canonical_reference_and_current_object(
    tmp_path: Path,
) -> None:
    account, inbox, trash = _account()
    state = StateStore(tmp_path / "state.sqlite3", account_key="student@example.test")
    gateway = EWSGateway(AppConfig(), state=state, account_factory=lambda: account)
    original = _MovingMessage("ews-inbox", inbox)
    stable_reference = gateway._remember(
        "message",
        original,
        parent=inbox,
        folder_path="/Inbox",
    )

    assert gateway.delete_message(stable_reference) == stable_reference
    deleted_row = state.resolve_reference(stable_reference, "message")
    assert deleted_row is not None
    assert deleted_row["ews_id"] == "ews-deleted"
    assert deleted_row["folder_path"] == "/Deleted Items"

    relisted_deleted = _MovingMessage("ews-deleted", trash)
    trash.messages = [relisted_deleted]
    deleted_page = gateway.list_messages("trash", limit=5)
    assert [item.reference for item in deleted_page] == [stable_reference]
    assert gateway.show_message(deleted_page[0].reference).summary.reference == stable_reference

    assert gateway.move_message(stable_reference, "inbox") == stable_reference
    restored_row = state.resolve_reference(stable_reference, "message")
    assert restored_row is not None
    assert restored_row["ews_id"] == "ews-restored"
    assert restored_row["folder_path"] == "/Inbox"

    # A delayed Deleted Items page must neither re-display nor replace the current
    # restored object in the adapter's canonical in-memory map.
    stale_deleted = _MovingMessage("ews-deleted", trash)
    trash.messages = [stale_deleted]
    assert gateway.list_messages("trash", limit=5) == ()
    assert gateway._memory_object(stable_reference, "message") == (
        "message",
        relisted_deleted,
    )

    relisted_restored = _MovingMessage("ews-restored", inbox)
    inbox.messages = [relisted_restored]
    restored_page = gateway.list_messages("inbox", limit=5)
    assert [item.reference for item in restored_page] == [stable_reference]
    assert len(list(state.iter_rows("item_references"))) == 1
    assert state.reference_health()["ok"] is True


def test_copy_accepts_exchangelib_item_id_objects_not_only_legacy_tuples() -> None:
    account, inbox, _trash = _account()

    class CopySource:
        id = "ews-source"
        changekey = "source-ck"

        @staticmethod
        def copy(_target: object) -> object:
            return SimpleNamespace(id="ews-copy", changekey="copy-ck")

    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: account,
        factory_overrides={"Message": lambda **values: SimpleNamespace(**values)},
    )
    source = CopySource()
    source_reference = gateway._remember("message", source, reference="msg_source")

    copied_reference = gateway.copy_message(source_reference, "inbox")

    assert copied_reference == make_local_reference("message", "ews-copy")
    copied = gateway._memory_object(copied_reference or "", "message")
    assert copied is not None
    assert copied[1].id == "ews-copy"
    assert gateway._parents[("message", copied_reference)] is inbox


def test_field_update_conflict_refreshes_change_key_once_and_retries() -> None:
    """Exchange returns a placeholder change key after IsRead; the next write conflicts."""

    class ErrorChangeKeyRequiredForWriteOperations(Exception):
        pass

    saves: list[tuple[str | None, list[str]]] = []

    class Item:
        id = "item-1"
        changekey = "CQAAAA=="

        def save(self, update_fields: list[str]) -> None:
            saves.append((self.changekey, list(update_fields)))
            if self.changekey == "CQAAAA==":
                raise ErrorChangeKeyRequiredForWriteOperations("ChangeKey is required")

    item = Item()
    fetched: list[Any] = []

    def fetch(ids: Any, only_fields: Any) -> Any:
        fetched.append(list(ids))
        return iter([SimpleNamespace(id="item-1", changekey="fresh-key")])

    account = SimpleNamespace(fetch=fetch, protocol=SimpleNamespace(close=lambda: None))
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    gateway._message_for_update = lambda reference: item  # type: ignore[method-assign]
    gateway._remember = lambda *a, **k: "msg_1"  # type: ignore[method-assign]

    assert gateway.flag("msg_1", flagged=True) == "msg_1"
    assert fetched == [[("item-1", None)]]
    assert saves == [
        ("CQAAAA==", ["gucmail_flag_status"]),
        ("fresh-key", ["gucmail_flag_status"]),
    ]
