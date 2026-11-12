"""Persistent local transport for exercising real services and user interfaces.

All rows are deterministic synthetic fixtures. This adapter simulates server
persistence, not Exchange authentication, permissions, SOAP or network latency.
The audit reports these boundaries separately. Missing methods fail loudly.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from gucmail.errors import ItemNotFoundError, UnsupportedFeatureError
from gucmail.models import (
    Address,
    AttachmentInfo,
    CalendarEvent,
    ContactInfo,
    DraftData,
    FolderInfo,
    Importance,
    ItemSource,
    MessageDetail,
    MessageSummary,
    QuotaInfo,
    QuotaSource,
    SearchQuery,
    Sensitivity,
    TaskInfo,
    to_primitive,
)
from gucmail.services.pim import PimPage


def decode(kind, value):
    data = json.loads(value) if isinstance(value, str) else dict(value)
    for key in (
        "received_at",
        "sent_at",
        "start",
        "end",
        "completed_at",
        "reminder_at",
        "recurrence_id",
    ):
        if data.get(key):
            data[key] = datetime.fromisoformat(data[key])
    for key in ("birthday", "start_date", "due_date"):
        if data.get(key):
            data[key] = date.fromisoformat(data[key])
    for key in ("sender", "organizer"):
        if data.get(key):
            data[key] = Address(**data[key])
    for key in ("emails", "required_attendees", "optional_attendees", "resources"):
        if key in data:
            data[key] = tuple(Address(**item) for item in data[key])
    if "attachments" in data:
        data["attachments"] = tuple(AttachmentInfo(**item) for item in data["attachments"])
    if "categories" in data:
        data["categories"] = tuple(data["categories"])
    if "importance" in data:
        data["importance"] = Importance(data["importance"])
    if "sensitivity" in data:
        data["sensitivity"] = Sensitivity(data["sensitivity"])
    if "source" in data:
        data["source"] = ItemSource(data["source"])
    return {
        "mail": MessageSummary,
        "contact": ContactInfo,
        "event": CalendarEvent,
        "task": TaskInfo,
        "folder": FolderInfo,
    }[kind](**data)


class LocalMailbox:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.calls = deque(maxlen=4096)
        self.counts = Counter()
        self.faults = {}
        self.closed = False
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects (
                    reference TEXT PRIMARY KEY, kind TEXT NOT NULL, folder TEXT NOT NULL,
                    parent TEXT, title TEXT NOT NULL, search_text TEXT NOT NULL,
                    sender TEXT, stamp TEXT, ending TEXT, due TEXT, status TEXT,
                    is_read INTEGER, flagged INTEGER, size INTEGER, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS by_folder ON objects(kind,folder,stamp);
                CREATE INDEX IF NOT EXISTS by_kind_title ON objects(kind,title);
                CREATE INDEX IF NOT EXISTS by_parent ON objects(kind,parent,title);
                CREATE INDEX IF NOT EXISTS by_task_due ON objects(kind,status,due);
                CREATE TABLE IF NOT EXISTS effects (
                    id INTEGER PRIMARY KEY, action TEXT, reference TEXT, result TEXT, stamp TEXT
                );
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def before(self, operation, *, limit=None, offset=None):
        self.counts[operation] += 1
        self.calls.append({"operation": operation, "limit": limit, "offset": offset})
        fault = self.faults.get(operation)
        if fault and fault.get("on_call") and self.counts[operation] != fault["on_call"]:
            fault = None
        if fault:
            if fault.get("delay"):
                time.sleep(fault["delay"])
            if fault.get("error"):
                raise fault["error"]

    @staticmethod
    def row(kind, item, folder=None):
        value = to_primitive(item)
        title = getattr(item, "subject", getattr(item, "display_name", getattr(item, "name", "")))
        if kind == "folder":
            folder = item.path
        folder = folder or getattr(item, "folder_path", None) or f"/{kind.title()}"
        stamp = getattr(item, "received_at", getattr(item, "start", None))
        ending = getattr(item, "end", None)
        due = getattr(item, "due_date", None)
        sender = getattr(item, "sender", None)
        text = " ".join(
            str(value.get(key) or "")
            for key in ("subject", "display_name", "emails", "company", "body", "categories")
        )
        return (
            item.reference,
            kind,
            folder,
            getattr(item, "parent_reference", None),
            title,
            text.casefold(),
            sender.email if sender else "",
            stamp.isoformat() if stamp else "",
            ending.isoformat() if ending else "",
            due.isoformat() if due else "",
            getattr(item, "status", ""),
            int(getattr(item, "is_read", False)),
            int(getattr(item, "flagged", False)),
            getattr(item, "size", 0),
            json.dumps(value, ensure_ascii=False),
        )

    def insert_many(self, kind, items, *, folder=None):
        with self.db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.row(kind, item, folder) for item in items),
            )

    def store(self, kind, item, *, folder=None, action="update"):
        self.insert_many(kind, [item], folder=folder)
        with self.db() as db:
            db.execute(
                "INSERT INTO effects(action,reference,result,stamp) VALUES (?,?,?,?)",
                (
                    action,
                    item.reference,
                    "simulated local persistence",
                    datetime.now(UTC).isoformat(),
                ),
            )
        return item

    def _objects(self, kind, *, where="1=1", values=(), limit=200, offset=0, order="title"):
        if not 1 <= limit <= 200:
            raise ValueError("The synthetic transport enforces a 200-object page bound.")
        with self.db() as db:
            rows = db.execute(
                f"SELECT * FROM objects WHERE kind=? AND {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (kind, *values, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = decode(kind, row["payload"])
            if kind == "mail":
                item = replace(item, folder_path=row["folder"])
            elif kind == "folder":
                item = replace(
                    item, path=row["folder"], name=row["title"], parent_reference=row["parent"]
                )
            result.append(item)
        return result

    def get(self, kind, reference):
        rows = self._objects(kind, where="reference=?", values=(reference,), limit=1)
        if not rows:
            raise ItemNotFoundError("The synthetic item no longer exists.")
        return rows[0]

    def count(self, kind, *, folder=None):
        with self.db() as db:
            return db.execute(
                "SELECT count(*) FROM objects WHERE kind=?" + (" AND folder=?" if folder else ""),
                (kind, folder) if folder else (kind,),
            ).fetchone()[0]

    def close(self):
        self.closed = True

    def status(self):
        self.before("status")
        return {
            "exchange_version": "Local audit simulation",
            "inbox_count": self.count("mail", folder="/Inbox"),
        }

    def get_quota(self):
        self.before("quota")
        return QuotaInfo(
            used_bytes=37 * 1024**2,
            prohibit_send_bytes=92 * 1024**2,
            usage_source=QuotaSource.USER_CONFIGURED,
            prohibit_send_source=QuotaSource.USER_CONFIGURED,
        )

    @staticmethod
    def folder_path(folder):
        return {
            "inbox": "/Inbox",
            "sent": "/Sent Items",
            "sentitems": "/Sent Items",
            "drafts": "/Drafts",
            "trash": "/Deleted Items",
            "junk": "/Junk",
        }.get(folder, folder)

    def list_messages(self, folder="inbox", limit=25, offset=0, order_by="-datetime_received"):
        self.before("list_messages", limit=limit, offset=offset)
        order = {
            "datetime_received": "stamp",
            "received_at": "stamp",
            "subject": "title",
            "sender": "sender",
            "size": "size",
            "datetime_sent": "stamp",
            "sent_at": "stamp",
            "importance": "json_extract(payload,'$.importance')",
        }.get(order_by.lstrip("-"))
        if order is None:
            raise ValueError("Unsupported synthetic sort.")
        return self._objects(
            "mail",
            where="folder=?",
            values=(self.folder_path(folder),),
            limit=limit,
            offset=offset,
            order=order + (" DESC" if order_by.startswith("-") else " ASC"),
        )

    def show_message(self, reference, mark_read=False):
        self.before("show_message")
        summary = self.get("mail", reference)
        if mark_read:
            self.mark_read(reference, True)
            summary = self.get("mail", reference)
        attachment = AttachmentInfo("att_" + reference, "Synthetic notes.txt", 40, "text/plain")
        body = "Hello,\n\nThis is a synthetic academic mailbox record.\n\n" + (
            "Review the attached notes and the next tutorial date.\n"
            * (400 if reference.endswith("000010") else 4)
        )
        return MessageDetail(
            summary=summary,
            to=(Address("student@example.test"),),
            body_text=body,
            attachments=(attachment,) if summary.has_attachments else (),
            raw_headers={"X-Audit": "Synthetic data only"},
        )

    def search_messages(self, query, limit=25, offset=0):
        self.before("search_messages", limit=limit, offset=offset)
        unsupported = (
            "recipient",
            "to",
            "cc",
            "bcc",
            "body",
            "after",
            "before",
            "has_attachments",
            "attachment_name",
            "importance",
            "categories",
            "minimum_size",
            "maximum_size",
            "internet_message_id",
            "fuzzy",
            "exclusions",
            "ranking",
            "thread_response",
            "mailbox_scope",
        )
        defaults = SearchQuery()
        if any(
            getattr(query, name, getattr(defaults, name)) != getattr(defaults, name)
            for name in unsupported
        ):
            raise UnsupportedFeatureError(
                "The audit SQL transport does not model this search predicate; use the production planner/adapter fixture tests. No real provider fallback is allowed."
            )
        clauses = ["1=1"]
        values = []
        for attr, column in (("text", "search_text"), ("subject", "title"), ("sender", "sender")):
            value = getattr(query, attr, None)
            if value:
                clauses.append(column + " LIKE ?")
                values.append("%" + value.casefold() + "%")
        if getattr(query, "folder", None) and not getattr(query, "all_folders", False):
            clauses.append("folder=?")
            values.append(self.folder_path(query.folder))
        for attr, column in (("is_read", "is_read"), ("flagged", "flagged")):
            value = getattr(query, attr, None)
            if value is not None:
                clauses.append(column + "=?")
                values.append(int(value))
        return self._objects(
            "mail",
            where=" AND ".join(clauses),
            values=values,
            limit=limit,
            offset=offset,
            order="stamp DESC",
        )

    def _mail_change(self, reference, **changes):
        item = self.get("mail", reference)
        return self.store("mail", replace(item, **changes))

    def mark_read(self, reference, read=True):
        self.before("mark_read")
        return self._mail_change(reference, is_read=read)

    def flag(self, reference, flagged=True):
        self.before("flag")
        return self._mail_change(reference, flagged=flagged)

    def categorize(self, reference, categories, mode="set"):
        self.before("categorize")
        previous = self.get("mail", reference).categories
        values = (
            tuple(categories)
            if mode == "set"
            else tuple(dict.fromkeys((*previous, *categories)))
            if mode == "add"
            else tuple(c for c in previous if c not in categories)
        )
        return self._mail_change(reference, categories=values)

    def set_importance(self, reference, importance):
        self.before("importance")
        return self._mail_change(reference, importance=importance)

    def move_message(self, reference, destination="trash"):
        self.before("move_message")
        return self._mail_change(reference, folder_path=self.folder_path(destination))

    def copy_message(self, reference, destination):
        self.before("copy_message")
        result = self.store(
            "mail",
            replace(
                self.get("mail", reference),
                reference="msg_" + uuid4().hex,
                folder_path=self.folder_path(destination),
            ),
            action="copy",
        )
        fault = self.faults.get("copy_message", {})
        if fault.get("after_commit_error") and self.counts["copy_message"] == fault.get("on_call"):
            raise fault["after_commit_error"]
        return result

    def delete_message(self, reference, permanent=False):
        self.before("delete_message")
        if not permanent:
            return self.move_message(reference, "trash")
        with self.db() as db:
            db.execute("DELETE FROM objects WHERE reference=? AND kind='mail'", (reference,))

    def raw_message(self, reference):
        detail = self.show_message(reference)
        return (
            "From: sender@example.test\r\nSubject: Synthetic export\r\n\r\n" + detail.body_text
        ).encode()

    def list_folders(self, parent_reference=None, limit=50, offset=0):
        self.before("list_folders", limit=limit, offset=offset)
        return self._objects(
            "folder", where="parent IS ?", values=(parent_reference,), limit=limit, offset=offset
        )

    def get_folder(self, reference):
        self.before("get_folder")
        return self.get("folder", reference)

    def create_folder(self, parent_reference, name):
        self.before("create_folder")
        parent = self.get_folder(parent_reference).path if parent_reference else ""
        return self.store(
            "folder",
            FolderInfo(
                "fld_" + uuid4().hex,
                name,
                parent + "/" + name,
                parent_reference,
                total_count=0,
                unread_count=0,
            ),
            action="create",
        )

    def _relocate_folder(self, reference, parent, name):
        folder = self.get_folder(reference)
        base = self.get_folder(parent).path if parent else ""
        target = base + "/" + name
        with self.db() as db:
            prefix = folder.path + "/"
            db.execute(
                "UPDATE objects SET folder=? || substr(folder,?) WHERE folder=? OR substr(folder,1,?)=?",
                (target, len(folder.path) + 1, folder.path, len(prefix), prefix),
            )
            db.execute(
                "UPDATE objects SET parent=?,title=? WHERE reference=?", (parent, name, reference)
            )
        return self.get_folder(reference)

    def rename_folder(self, reference, new_name):
        self.before("rename_folder")
        current = self.get_folder(reference)
        return self._relocate_folder(reference, current.parent_reference, new_name)

    def move_folder(self, reference, destination_reference):
        self.before("move_folder")
        return self._relocate_folder(
            reference, destination_reference, self.get_folder(reference).name
        )

    def list_contacts(self, folder_reference=None, limit=25):
        self.before("list_contacts", limit=limit)
        return self._objects("contact", limit=limit)

    def search_contacts(self, query, folder_reference=None, limit=25):
        return self.search_contacts_page(
            query, folder_reference=folder_reference, limit=limit
        ).items

    def search_contacts_page(self, query, folder_reference=None, limit=25, offset=0):
        self.before("search_contacts", limit=limit, offset=offset)
        if any(getattr(query, name, None) for name in ("email", "company", "category")):
            raise UnsupportedFeatureError(
                "This audit transport search models text only; production predicate tests use explicit controlled adapters."
            )
        text = getattr(query, "text", query if isinstance(query, str) else "") or ""
        items = self._objects(
            "contact",
            where="search_text LIKE ?",
            values=("%" + text.casefold() + "%",),
            limit=limit,
            offset=offset,
        )
        return PimPage(
            tuple(items), offset, len(items), offset + len(items) if len(items) == limit else None
        )

    def get_contact(self, reference):
        self.before("get_contact")
        return self.get("contact", reference)

    def create_contact(self, plan):
        self.before("create_contact")
        return self.store(
            "contact", replace(plan.contact, reference="con_" + uuid4().hex), action="create"
        )

    def update_contact(self, plan):
        self.before("update_contact")
        return self.store("contact", replace(self.get_contact(plan.reference), **plan.changes))

    def delete_contact(self, plan):
        self.before("delete_contact")
        with self.db() as db:
            db.execute(
                "DELETE FROM objects WHERE reference=? AND kind='contact'", (plan.reference,)
            )

    def list_tasks(self, folder_reference=None, limit=25):
        return self.search_tasks_page(None, folder_reference=folder_reference, limit=limit).items

    def search_tasks(self, query, folder_reference=None, limit=25):
        return self.search_tasks_page(query, folder_reference=folder_reference, limit=limit).items

    def search_tasks_page(self, query, folder_reference=None, limit=25, offset=0):
        self.before("list_tasks", limit=limit, offset=offset)
        if query and getattr(query, "category", None):
            raise UnsupportedFeatureError(
                "The audit transport does not model task category filtering; no real provider fallback is allowed."
            )
        clauses, values = ["1=1"], []
        if query:
            if query.text:
                clauses.append("search_text LIKE ?")
                values.append("%" + query.text.casefold() + "%")
            if query.status:
                clauses.append("status=?")
                values.append(str(query.status))
            if query.exclude_completed:
                clauses.append("status!='completed'")
            for attr, operator in (("due_after", ">="), ("due_before", "<="), ("overdue_at", "<")):
                value = getattr(query, attr, None)
                if value:
                    clauses.append("due != '' AND due" + operator + "?")
                    values.append(value.isoformat())
        items = self._objects(
            "task",
            where=" AND ".join(clauses),
            values=values,
            limit=limit,
            offset=offset,
            order="due,title",
        )
        return PimPage(
            tuple(items), offset, len(items), offset + len(items) if len(items) == limit else None
        )

    def get_task(self, reference):
        self.before("get_task")
        return self.get("task", reference)

    def create_task(self, plan):
        self.before("create_task")
        return self.store(
            "task", replace(plan.task, reference="tsk_" + uuid4().hex), action="create"
        )

    def update_task(self, plan):
        self.before("update_task")
        return self.store("task", replace(self.get_task(plan.reference), **plan.changes))

    def delete_task(self, plan):
        self.before("delete_task")
        with self.db() as db:
            db.execute("DELETE FROM objects WHERE reference=? AND kind='task'", (plan.reference,))

    def list_events(self, *, start, end, calendar_reference=None, limit=25):
        self.before("list_events", limit=limit)
        return self._objects(
            "event",
            where="stamp < ? AND ending > ?",
            values=(end.isoformat(), start.isoformat()),
            limit=limit,
            order="stamp,title",
        )

    def get_event(self, reference):
        self.before("get_event")
        return self.get("event", reference)

    def create_event(self, plan):
        self.before("create_event")
        return self.store(
            "event", replace(plan.event, reference="evt_" + uuid4().hex), action="create"
        )

    def update_event(self, plan):
        self.before("update_event")
        return self.store("event", replace(self.get_event(plan.reference), **plan.changes))

    def delete_event(self, plan):
        self.before("delete_event")
        with self.db() as db:
            db.execute("DELETE FROM objects WHERE reference=? AND kind='event'", (plan.reference,))

    def respond_to_meeting(self, plan):
        self.before("respond_meeting")
        return self.store(
            "event", replace(self.get_event(plan.reference), response_status=plan.response.value)
        )

    def empty_folder(self, reference, permanent=False):
        self.before("empty_folder")
        folder = self.get_folder(reference)
        with self.db() as db:
            if permanent:
                result = db.execute(
                    "DELETE FROM objects WHERE kind='mail' AND folder=?", (folder.path,)
                )
            else:
                result = db.execute(
                    "UPDATE objects SET folder='/Deleted Items' WHERE kind='mail' AND folder=?",
                    (folder.path,),
                )
            return result.rowcount

    def delete_folder(self, reference, permanent=False):
        self.before("delete_folder")
        folder = self.get_folder(reference)
        if not permanent:
            return self._relocate_folder(reference, "fld_role_3", folder.name)
        with self.db() as db:
            prefix = folder.path + "/"
            return db.execute(
                "DELETE FROM objects WHERE folder=? OR substr(folder,1,?)=?",
                (folder.path, len(prefix), prefix),
            ).rowcount

    def list_attachments(self, message_reference, limit=100, offset=0):
        self.before("list_attachments", limit=limit, offset=offset)
        self.get("mail", message_reference)
        return (
            AttachmentInfo(
                reference="att-synthetic-1",
                name="../../notes.txt",
                size=len(b"Synthetic attachment bytes.\n"),
                content_type="text/plain",
            ),
        )[offset : offset + limit]

    def open_attachment(self, message_reference, attachment_reference):
        import io

        self.before("open_attachment")
        self.get("mail", message_reference)
        if attachment_reference != "att-synthetic-1":
            raise ItemNotFoundError("Synthetic attachment does not exist.")
        return io.BytesIO(b"Synthetic attachment bytes.\n")

    def send(self, draft: DraftData):
        self.before("send")
        summary = MessageSummary(
            "msg_" + uuid4().hex,
            draft.subject,
            Address("student@example.test"),
            datetime.now(UTC),
            is_read=True,
            folder_path="/Sent Items",
            source=ItemSource.LOCAL,
        )
        self.store("mail", summary, action="simulated-send")
        return {"reference": summary.reference, "delivery": "simulated local backend only"}

    def reply(self, reference, draft):
        self.get("mail", reference)
        return self.send(draft)

    reply_all = reply
    forward = reply
