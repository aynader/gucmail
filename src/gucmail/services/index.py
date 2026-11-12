"""Independent SQLite FTS5 mailbox index with bounded fuzzy fallback.

The index is a disposable derived view, never the mailbox source of truth. It stores only
fields explicitly supplied by a caller and can be rebuilt or cleared without touching EWS.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import uuid4

from rapidfuzz.fuzz import WRatio

from gucmail.constants import MAX_LOCAL_OFFSET
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    ExitCode,
    GUCmailError,
    LocalStateError,
)
from gucmail.models import MessageDetail, MessageSummary, to_primitive
from gucmail.security import sanitize_terminal
from gucmail.services.semantic import (
    SemanticDocument,
    SemanticSnapshot,
    snapshot_digest,
)

INDEX_SCHEMA_VERSION = 3
MAX_QUERY_LENGTH = 8_192
MAX_FUZZY_CANDIDATES = 5_000
_TOKEN_RE = re.compile(r"[\w@.-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class IndexDocument:
    reference: str
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    body: str = ""
    attachment_names: str = ""
    attachment_text: str = ""
    folder_path: str = ""
    received_at: datetime | None = None
    size: int | None = None
    source: str = "live_server"
    payload: Mapping[str, Any] | None = None

    @classmethod
    def from_value(
        cls,
        value: MessageDetail | MessageSummary | Mapping[str, Any],
        *,
        attachment_text: str = "",
    ) -> IndexDocument:
        if isinstance(value, MessageDetail):
            summary = value.summary
            return cls(
                reference=summary.reference,
                subject=summary.subject,
                sender=summary.sender.email if summary.sender else "",
                recipients=" ".join(item.email for item in (*value.to, *value.cc, *value.bcc)),
                body=value.body_text,
                attachment_names=" ".join(item.name for item in value.attachments),
                attachment_text=attachment_text,
                folder_path=summary.folder_path or "",
                received_at=summary.received_at,
                size=summary.size,
                source=summary.source.value,
                payload=to_primitive(value),
            )
        if isinstance(value, MessageSummary):
            return cls(
                reference=value.reference,
                subject=value.subject,
                sender=value.sender.email if value.sender else "",
                body=value.preview or "",
                folder_path=value.folder_path or "",
                received_at=value.received_at,
                size=value.size,
                source=value.source.value,
                payload=to_primitive(value),
            )
        if not isinstance(value, Mapping):
            raise ConfigurationError("Index documents must be message values or mappings.")
        reference = str(value.get("reference", "")).strip()
        if not reference:
            raise ConfigurationError("An index document needs a reference.")
        received = value.get("received_at")
        if isinstance(received, str):
            try:
                received = datetime.fromisoformat(received.replace("Z", "+00:00"))
            except ValueError:
                received = None
        return cls(
            reference=reference,
            subject=str(value.get("subject", "")),
            sender=str(value.get("sender", "")),
            recipients=str(value.get("recipients", "")),
            body=str(value.get("body", value.get("body_text", ""))),
            attachment_names=str(value.get("attachment_names", "")),
            attachment_text=attachment_text or str(value.get("attachment_text", "")),
            folder_path=str(value.get("folder_path", value.get("folder", ""))),
            received_at=received if isinstance(received, datetime) else None,
            size=int(value["size"]) if value.get("size") is not None else None,
            source=str(value.get("source", "live_server")),
            payload=dict(value),
        )


@dataclass(frozen=True, slots=True)
class IndexHit:
    reference: str
    subject: str
    sender: str
    folder_path: str
    received_at: datetime | None
    size: int | None
    score: float
    snippet: str
    source: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IndexedRecord:
    """One bounded private record read back for local-only analysis.

    This richer shape is deliberately separate from :class:`IndexHit`: bodies and
    attachment text are useful for similarity and duplicate analysis, but must not
    be exposed by ordinary result rendering unless a user explicitly opens details.
    """

    reference: str
    subject: str
    sender: str
    recipients: str
    body: str = field(repr=False)
    attachment_names: str = ""
    attachment_text: str = field(default="", repr=False)
    folder_path: str = ""
    received_at: datetime | None = None
    size: int | None = None
    source: str = "live_server"
    payload: Mapping[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class IndexStats:
    documents: int
    database_bytes: int
    updated_at: datetime | None
    schema_version: int
    fts5: bool


@dataclass(frozen=True, slots=True)
class IndexVerification:
    valid: bool
    integrity: str
    documents: int
    full_text_rows: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class IndexRevision:
    documents: int
    updated_at: str | None
    revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndexReplacement:
    removed: int
    indexed: int


class IndexRebuildCancelled(GUCmailError):
    exit_code = ExitCode.INTERRUPTED


class SearchIndex:
    """Thread-safe local index with one short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.exists():
                    with self._connection() as connection:
                        metadata = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='index_metadata'"
                        ).fetchone()
                        if metadata is not None:
                            row = connection.execute(
                                "SELECT value FROM index_metadata WHERE key='schema_version'"
                            ).fetchone()
                            version = int(row[0]) if row else 0
                            if version > INDEX_SCHEMA_VERSION:
                                raise LocalStateError(
                                    "The local index uses a newer schema; it was retained."
                                )
                            if version == INDEX_SCHEMA_VERSION:
                                return  # Reads must not acquire a schema/write transaction.
                with self._connection() as connection:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS index_metadata (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );
                        CREATE TABLE IF NOT EXISTS documents (
                            reference TEXT PRIMARY KEY,
                            subject TEXT NOT NULL,
                            sender TEXT NOT NULL,
                            recipients TEXT NOT NULL,
                            body TEXT NOT NULL,
                            attachment_names TEXT NOT NULL,
                            attachment_text TEXT NOT NULL,
                            folder_path TEXT NOT NULL,
                            received_at TEXT,
                            size INTEGER,
                            source TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                            reference UNINDEXED,
                            subject,
                            sender,
                            recipients,
                            body,
                            attachment_names,
                            attachment_text,
                            folder_path,
                            tokenize='unicode61 remove_diacritics 2'
                        );
                        """
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    version_row = connection.execute(
                        "SELECT value FROM index_metadata WHERE key='schema_version'"
                    ).fetchone()
                    version = int(version_row[0]) if version_row else 0
                    if version > INDEX_SCHEMA_VERSION:
                        raise LocalStateError(
                            "The local index uses a newer schema; it was retained."
                        )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS document_fts_ids ("
                        "fts_rowid INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE "
                        "REFERENCES documents(reference) ON DELETE CASCADE)"
                    )
                    if version < 2:
                        # FTS reference is UNINDEXED. Map it once to a stable integer
                        # primary key; later updates never scan the full FTS corpus.
                        connection.execute("DELETE FROM document_fts_ids")
                        connection.execute(
                            "INSERT INTO document_fts_ids(reference) "
                            "SELECT reference FROM documents ORDER BY reference"
                        )
                        connection.execute("DELETE FROM documents_fts")
                        connection.execute(
                            "INSERT INTO documents_fts(rowid,reference,subject,sender,recipients,"
                            "body,attachment_names,attachment_text,folder_path) "
                            "SELECT m.fts_rowid,d.reference,d.subject,d.sender,d.recipients,"
                            "d.body,d.attachment_names,d.attachment_text,d.folder_path "
                            "FROM documents d JOIN document_fts_ids m USING(reference)"
                        )
                    if version < 3:
                        # Ranking must not visit full document/body/payload pages
                        # for every match. Keep only filter/order metadata beside
                        # the stable FTS identity, in the same write transaction.
                        columns = {
                            row[1]
                            for row in connection.execute("PRAGMA table_info(document_fts_ids)")
                        }
                        for name, definition in (
                            ("received_at", "TEXT"),
                            ("folder_path", "TEXT NOT NULL DEFAULT ''"),
                            ("sender", "TEXT NOT NULL DEFAULT ''"),
                            ("size", "INTEGER"),
                            ("attachment_names", "TEXT NOT NULL DEFAULT ''"),
                        ):
                            if name not in columns:
                                connection.execute(
                                    f"ALTER TABLE document_fts_ids ADD COLUMN {name} {definition}"
                                )
                        connection.execute(
                            "UPDATE document_fts_ids SET "
                            "(received_at,folder_path,sender,size,attachment_names)="
                            "(SELECT received_at,folder_path,sender,size,attachment_names "
                            "FROM documents WHERE documents.reference=document_fts_ids.reference)"
                        )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS documents_chronological "
                        "ON documents(received_at DESC,reference)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS documents_source_chronological "
                        "ON documents(source,received_at DESC,reference)"
                    )
                    connection.execute(
                        "INSERT INTO index_metadata(key, value) VALUES ('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(INDEX_SCHEMA_VERSION),),
                    )
            except sqlite3.Error as exc:
                raise LocalStateError(
                    f"Could not initialize the local search index: {exc}"
                ) from exc
            except ValueError as exc:
                raise LocalStateError("The local index schema metadata is invalid.") from exc

    @staticmethod
    def _clean(value: object, *, maximum: int = 2_000_000) -> str:
        return sanitize_terminal(value, max_length=maximum)

    def _prepare_row(
        self, document: IndexDocument | MessageDetail | MessageSummary | Mapping[str, Any]
    ) -> tuple[Any, ...]:
        value = (
            document if isinstance(document, IndexDocument) else IndexDocument.from_value(document)
        )
        if not value.reference.strip():
            raise ConfigurationError("An index document needs a reference.")
        fields = (
            self._clean(value.subject, maximum=32_768),
            self._clean(value.sender, maximum=8_192),
            self._clean(value.recipients, maximum=32_768),
            self._clean(value.body),
            self._clean(value.attachment_names, maximum=65_536),
            self._clean(value.attachment_text),
            self._clean(value.folder_path, maximum=32_768),
        )
        received = value.received_at
        if received is not None:
            received = received if received.tzinfo is not None else received.replace(tzinfo=UTC)
        payload = json.dumps(to_primitive(value.payload or {}), ensure_ascii=False, sort_keys=True)
        now = datetime.now(UTC).isoformat()
        return (
            value.reference,
            *fields,
            received.astimezone(UTC).isoformat() if received else None,
            value.size,
            value.source,
            payload,
            now,
        )

    @staticmethod
    def _write_row(connection: sqlite3.Connection, row: tuple[Any, ...]) -> None:
        connection.execute(
            """
            INSERT INTO documents(
                reference, subject, sender, recipients, body, attachment_names,
                attachment_text, folder_path, received_at, size, source,
                payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reference) DO UPDATE SET
                subject=excluded.subject, sender=excluded.sender,
                recipients=excluded.recipients, body=excluded.body,
                attachment_names=excluded.attachment_names,
                attachment_text=excluded.attachment_text,
                folder_path=excluded.folder_path, received_at=excluded.received_at,
                size=excluded.size, source=excluded.source,
                payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            row,
        )
        connection.execute(
            "INSERT INTO document_fts_ids"
            "(reference,received_at,folder_path,sender,size,attachment_names) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(reference) DO UPDATE SET received_at=excluded.received_at,"
            "folder_path=excluded.folder_path,sender=excluded.sender,size=excluded.size,"
            "attachment_names=excluded.attachment_names",
            (row[0], row[8], row[7], row[2], row[9], row[5]),
        )
        fts_rowid = connection.execute(
            "SELECT fts_rowid FROM document_fts_ids WHERE reference=?", (row[0],)
        ).fetchone()[0]
        connection.execute("DELETE FROM documents_fts WHERE rowid=?", (fts_rowid,))
        connection.execute(
            "INSERT INTO documents_fts(rowid,reference,subject,sender,recipients,body,"
            "attachment_names,attachment_text,folder_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fts_rowid, *row[:8]),
        )
        connection.execute(
            "INSERT INTO index_metadata(key, value) VALUES ('updated_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (row[-1],),
        )
        SearchIndex._touch_revision(connection)

    @staticmethod
    def _touch_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO index_metadata(key, value) VALUES ('revision_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (uuid4().hex,),
        )

    def upsert(
        self, document: IndexDocument | MessageDetail | MessageSummary | Mapping[str, Any]
    ) -> None:
        row = self._prepare_row(document)
        self.initialize()
        with self._lock:
            try:
                with self._connection() as connection:
                    self._write_row(connection, row)
            except sqlite3.Error as exc:
                raise LocalStateError(f"Could not update the local search index: {exc}") from exc

    @staticmethod
    def _revision(connection: sqlite3.Connection) -> IndexRevision:
        count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        markers = dict(
            connection.execute(
                "SELECT key, value FROM index_metadata WHERE key IN ('updated_at', 'revision_id')"
            ).fetchall()
        )
        return IndexRevision(count, markers.get("updated_at"), markers.get("revision_id"))

    def revision(self) -> IndexRevision:
        """Read count, timestamp and write identity from one SQLite snapshot."""
        self.initialize()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN")
            return self._revision(connection)

    def replace_many(
        self,
        values: Iterable[IndexDocument | MessageDetail | MessageSummary | Mapping[str, Any]],
        *,
        limit: int = 500,
        expected_revision: IndexRevision | None = None,
        cancel_event: threading.Event | None = None,
        max_prepared_bytes: int = 32 * 1024 * 1024,
    ) -> IndexReplacement:
        """Prepare bounded data first, then atomically replace documents and FTS.

        Input/network/validation failures occur before clearing any existing row.
        Cancellation or SQL failure inside the replacement transaction rolls it back.
        The optional revision is compared after acquiring the cross-process write lock.
        The byte bound counts prepared UTF-8 field/payload data, not OS resident memory.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ConfigurationError("Index replacement limit must be between 1 and 500.")
        if (
            isinstance(max_prepared_bytes, bool)
            or not isinstance(max_prepared_bytes, int)
            or not 1 <= max_prepared_bytes <= 32 * 1024 * 1024
        ):
            raise ConfigurationError("Index replacement data must fit within 32 MiB.")
        if expected_revision is not None and not isinstance(expected_revision, IndexRevision):
            raise ConfigurationError("The index replacement revision is invalid.")

        def cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise IndexRebuildCancelled(
                    "Index rebuild cancelled; the previous index was retained."
                )

        cancelled()
        rows: list[tuple[Any, ...]] = []
        references: set[str] = set()
        total = 0
        for value in values:
            cancelled()
            if len(rows) >= limit:
                raise ConfigurationError(
                    "The index replacement exceeds its reviewed document limit."
                )
            row = self._prepare_row(value)
            if row[0] in references:
                raise ConfigurationError("The index replacement contains duplicate references.")
            references.add(row[0])
            total += sum(len(part.encode("utf-8")) if isinstance(part, str) else 32 for part in row)
            if total > max_prepared_bytes:
                raise ConfigurationError(
                    "The index replacement exceeds its prepared-data byte limit."
                )
            rows.append(row)
        cancelled()
        self.initialize()
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                revision = self._revision(connection)
                if expected_revision is not None and revision != expected_revision:
                    raise ConflictError("The local index changed; review the rebuild again.")
                cancelled()
                connection.execute("DELETE FROM documents_fts")
                connection.execute("DELETE FROM documents")
                now = datetime.now(UTC).isoformat()
                for row in rows:
                    cancelled()
                    self._write_row(connection, (*row[:-1], now))
                cancelled()
                connection.execute(
                    "INSERT INTO index_metadata(key, value) VALUES ('updated_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now,),
                )
                self._touch_revision(connection)
                cancelled()
            return IndexReplacement(revision.documents, len(rows))
        except sqlite3.Error as exc:
            raise LocalStateError(
                "Index replacement failed; its transaction was rolled back."
            ) from exc

    def upsert_many(
        self,
        values: Iterable[IndexDocument | MessageDetail | MessageSummary | Mapping[str, Any]],
        *,
        limit: int = 10_000,
        cancel_event: threading.Event | None = None,
        max_prepared_bytes: int = 32 * 1024 * 1024,
    ) -> int:
        """Commit one bounded batch atomically, including FTS and revision state.

        Producers ingest larger collections in batches. Invalid input, cancellation
        or a failed write retains the previous index rather than a partial batch.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ConfigurationError("Index batch limit must be between 1 and 10000.")
        if (
            isinstance(max_prepared_bytes, bool)
            or not isinstance(max_prepared_bytes, int)
            or not 1 <= max_prepared_bytes <= 32 * 1024 * 1024
        ):
            raise ConfigurationError("An index batch must fit within 32 MiB.")

        def cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise IndexRebuildCancelled(
                    "Index batch cancelled; the previous index was retained."
                )

        cancelled()
        rows: list[tuple[Any, ...]] = []
        prepared_bytes = 0
        for value in islice(values, limit):
            cancelled()
            row = self._prepare_row(value)
            prepared_bytes += sum(
                len(part.encode("utf-8")) if isinstance(part, str) else 32 for part in row
            )
            if prepared_bytes > max_prepared_bytes:
                raise ConfigurationError("The index batch exceeds its prepared-data byte limit.")
            rows.append(row)
        cancelled()
        if not rows:
            return 0
        self.initialize()
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for row in rows:
                    cancelled()
                    self._write_row(connection, row)
                cancelled()
        except sqlite3.Error as exc:
            raise LocalStateError("Index batch failed; its transaction was rolled back.") from exc
        return len(rows)

    def delete(self, reference: str) -> bool:
        self.initialize()
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM documents_fts WHERE rowid=("
                "SELECT fts_rowid FROM document_fts_ids WHERE reference=?)",
                (reference,),
            )
            cursor = connection.execute("DELETE FROM documents WHERE reference=?", (reference,))
            if cursor.rowcount:
                self._touch_revision(connection)
        return cursor.rowcount > 0

    def clear(self) -> int:
        self.initialize()
        with self._lock, self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            connection.execute("DELETE FROM documents_fts")
            connection.execute("DELETE FROM documents")
            connection.execute("DELETE FROM index_metadata WHERE key='updated_at'")
            self._touch_revision(connection)
        return count

    @staticmethod
    def _match_expression(
        query: str,
        *,
        exact: bool = False,
        fields: tuple[str, ...] = (),
    ) -> str:
        if len(query) > MAX_QUERY_LENGTH:
            raise ConfigurationError("Search query is too long.")
        tokens = _TOKEN_RE.findall(query)
        if not tokens:
            raise ConfigurationError("Search query needs at least one searchable term.")
        # Quoting every token prevents FTS operators supplied by a user from changing the plan.
        escaped = tuple(token.replace(chr(34), chr(34) * 2) for token in tokens[:100])
        expression = (
            f'"{" ".join(escaped)}"' if exact else " AND ".join(f'"{token}"' for token in escaped)
        )
        if not fields:
            return expression
        allowed = {
            "subject",
            "sender",
            "recipients",
            "body",
            "attachment_names",
            "attachment_text",
            "folder_path",
        }
        if any(field not in allowed for field in fields):
            raise ConfigurationError("Local search requested an unsupported index field.")
        return "{" + " ".join(fields) + "}: " + expression

    @staticmethod
    def _hit(row: sqlite3.Row, score: float, snippet: str) -> IndexHit:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        received = datetime.fromisoformat(row["received_at"]) if row["received_at"] else None
        return IndexHit(
            reference=str(row["reference"]),
            subject=str(row["subject"]),
            sender=str(row["sender"]),
            folder_path=str(row["folder_path"]),
            received_at=received,
            size=int(row["size"]) if row["size"] is not None else None,
            score=round(float(score), 4),
            snippet=sanitize_terminal(snippet, max_length=1_000),
            source=str(row["source"]),
            payload=payload if isinstance(payload, Mapping) else {},
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> IndexedRecord:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        received = datetime.fromisoformat(row["received_at"]) if row["received_at"] else None
        return IndexedRecord(
            reference=str(row["reference"]),
            subject=str(row["subject"]),
            sender=str(row["sender"]),
            recipients=str(row["recipients"]),
            body=str(row["body"]),
            attachment_names=str(row["attachment_names"]),
            attachment_text=str(row["attachment_text"]),
            folder_path=str(row["folder_path"]),
            received_at=received,
            size=int(row["size"]) if row["size"] is not None else None,
            source=str(row["source"]),
            payload=payload if isinstance(payload, Mapping) else {},
        )

    def semantic_snapshot(
        self, *, limit: int = 500, include_body: bool = False
    ) -> SemanticSnapshot:
        """Read only explicitly selected local text in one bounded, coherent snapshot."""
        if type(limit) is not int or not 2 <= limit <= 2_000:
            raise ConfigurationError("Semantic selection limit must be between 2 and 2000.")
        documents: tuple[SemanticDocument, ...] = ()
        total = 0
        if self.path.exists():
            connection = None
            try:
                connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
                )
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                total = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
                rows = connection.execute(
                    "SELECT reference,substr(subject,1,8192),"
                    "CASE WHEN ? THEN substr(body,1,32768) ELSE '' END,"
                    "substr(folder_path,1,2048),substr(received_at,1,128) "
                    "FROM documents ORDER BY received_at DESC,reference LIMIT ?",
                    (include_body, limit),
                )
                documents = tuple(SemanticDocument(*row) for row in rows)
            except sqlite3.Error as exc:
                raise LocalStateError(
                    "The existing local index cannot be read for semantic search. "
                    "Preserve it and inspect index status; no mailbox access occurred."
                ) from exc
            finally:
                if connection is not None:
                    connection.close()
        return SemanticSnapshot(
            documents,
            snapshot_digest(documents, total=total, limit=limit, include_body=include_body),
            total,
            limit,
            include_body,
        )

    def records(
        self,
        *,
        limit: int = 5_000,
        offset: int = 0,
        source: str | None = None,
    ) -> tuple[IndexedRecord, ...]:
        """Return a deterministic bounded analysis view without an FTS query."""

        if not 1 <= limit <= 10_000 or offset < 0:
            raise ConfigurationError("Index record pagination is outside the safe bounds.")
        self.initialize()
        parameters: list[Any] = []
        where = ""
        if source is not None:
            where = "WHERE source=?"
            parameters.append(source)
        parameters.extend((limit, offset))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM documents {where} "  # noqa: S608 - fixed internal fragment
                    "ORDER BY received_at DESC, reference LIMIT ? OFFSET ?",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise LocalStateError(f"Could not read the local search index: {exc}") from exc
        return tuple(self._record(row) for row in rows)

    def record(self, reference: str) -> IndexedRecord:
        """Read one exact local reference for an explicit similarity operation."""

        value = reference.strip()
        if not value:
            raise ConfigurationError("An indexed message reference is required.")
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE reference=?", (value,)
            ).fetchone()
        if row is None:
            raise ConfigurationError("The selected message is not present in the local index.")
        return self._record(row)

    @contextmanager
    def stream_records(
        self,
        *,
        batch_size: int = 500,
        source: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[Iterator[IndexedRecord]]:
        """Stream a consistent read snapshot with an explicit resource lifetime.

        The context owns the connection even when a consumer stops early. Only
        one bounded SQLite batch is decoded at a time; no deep OFFSET rescans.
        """
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 10_000
        ):
            raise ConfigurationError("Index stream batches must contain 1 to 10000 records.")
        self.initialize()

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise IndexRebuildCancelled("Local index enumeration cancelled.")

        check_cancelled()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN")
                where = "WHERE source=?" if source is not None else ""
                cursor = connection.execute(
                    f"SELECT * FROM documents {where} "  # noqa: S608 - fixed internal fragment
                    "ORDER BY received_at DESC,reference",
                    (source,) if source is not None else (),
                )

                def records() -> Iterator[IndexedRecord]:
                    while True:
                        check_cancelled()
                        rows = cursor.fetchmany(batch_size)
                        if not rows:
                            return
                        for row in rows:
                            check_cancelled()
                            yield self._record(row)

                yield records()
        except sqlite3.Error as exc:
            raise LocalStateError("Could not stream the local search index.") from exc

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        folder: str | None = None,
        fuzzy: bool = False,
        fuzzy_threshold: int = 75,
        exact: bool = False,
        fields: tuple[str, ...] = (),
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        attachment_extension: str | None = None,
    ) -> tuple[IndexHit, ...]:
        if not 1 <= limit <= 500 or not 0 <= offset <= MAX_LOCAL_OFFSET:
            raise ConfigurationError("Index pagination is outside the safe bounds.")
        if not 0 <= fuzzy_threshold <= 100:
            raise ConfigurationError("Fuzzy threshold must be between 0 and 100.")
        if fuzzy and exact:
            raise ConfigurationError("Choose fuzzy matching or exact phrase matching, not both.")
        self.initialize()
        # A filter-only attachment query has no text to score fuzzily. Keep its
        # bounded SQL predicates when fuzzy matching is enabled as a preference.
        if fuzzy and sanitize_terminal(query, max_length=MAX_QUERY_LENGTH).strip():
            return self._fuzzy_search(
                query,
                limit=limit,
                offset=offset,
                folder=folder,
                threshold=fuzzy_threshold,
                fields=fields,
                sender=sender,
                after=after,
                before=before,
                minimum_size=minimum_size,
                maximum_size=maximum_size,
                attachment_extension=attachment_extension,
            )
        normalized_query = sanitize_terminal(query, max_length=MAX_QUERY_LENGTH).strip()
        if not normalized_query and not fields:
            raise ConfigurationError("Search query needs at least one searchable term.")
        expression = (
            self._match_expression(normalized_query, exact=exact, fields=fields)
            if normalized_query
            else None
        )
        parameters: list[Any] = []
        clauses: list[str] = []
        if expression is not None:
            clauses.append("documents_fts MATCH ?")
            parameters.append(expression)
        if folder:
            escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
            clauses.append("d.folder_path LIKE ? ESCAPE '\\'")
        if expression is None and fields and set(fields) <= {"attachment_names", "attachment_text"}:
            clauses.append("(d.attachment_names != '' OR d.attachment_text != '')")
        if sender:
            parameters.append(f"%{sanitize_terminal(sender, max_length=8_192).casefold()}%")
            clauses.append("LOWER(d.sender) LIKE ?")
        if after is not None:
            normalized_after = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
            parameters.append(normalized_after.astimezone(UTC).isoformat())
            clauses.append("d.received_at >= ?")
        if before is not None:
            normalized_before = before if before.tzinfo is not None else before.replace(tzinfo=UTC)
            parameters.append(normalized_before.astimezone(UTC).isoformat())
            clauses.append("d.received_at < ?")
        if minimum_size is not None:
            parameters.append(minimum_size)
            clauses.append("d.size >= ?")
        if maximum_size is not None:
            parameters.append(maximum_size)
            clauses.append("d.size <= ?")
        if attachment_extension:
            extension = sanitize_terminal(attachment_extension, max_length=32).casefold()
            parameters.append(f"%{extension}%")
            clauses.append("LOWER(d.attachment_names) LIKE ?")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        if expression is not None:
            # Filter/rank on narrow metadata. Even selecting a few columns from
            # documents visits its much wider body/payload pages for every hit.
            # Decode full records and produce snippets only for the selected page.
            sql = f"""
                WITH page AS MATERIALIZED (
                    SELECT d.reference, d.received_at, bm25(documents_fts) AS rank
                    FROM documents_fts JOIN document_fts_ids AS d
                        ON d.fts_rowid=documents_fts.rowid{where}
                    ORDER BY rank, d.received_at DESC, d.reference
                    LIMIT ? OFFSET ?
                )
                SELECT d.*, page.rank AS rank,
                       snippet(documents_fts, -1, '[', ']', ' … ', 24) AS match_snippet
                FROM page JOIN documents AS d USING(reference)
                JOIN document_fts_ids AS m USING(reference)
                JOIN documents_fts ON documents_fts.rowid=m.fts_rowid
                WHERE documents_fts MATCH ?
                ORDER BY page.rank, d.received_at DESC, d.reference
            """  # noqa: S608 - fixed internal clauses
        else:
            sql = f"""
                SELECT d.*, 0.0 AS rank, '' AS match_snippet
                FROM documents AS d{where}
                ORDER BY d.received_at DESC, d.reference
                LIMIT ? OFFSET ?
            """  # noqa: S608 - fixed internal clauses
        parameters.extend((limit, offset))
        if expression is not None:
            parameters.append(expression)
        try:
            with self._connection() as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise LocalStateError(f"Local full-text search failed: {exc}") from exc
        return tuple(
            self._hit(row, max(0.0, 100.0 + float(row["rank"])), row["match_snippet"] or "")
            for row in rows
        )

    def _fuzzy_search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        folder: str | None,
        threshold: int,
        fields: tuple[str, ...] = (),
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        attachment_extension: str | None = None,
    ) -> tuple[IndexHit, ...]:
        needle = sanitize_terminal(query, max_length=MAX_QUERY_LENGTH).casefold().strip()
        if not needle:
            raise ConfigurationError("Fuzzy search needs a non-empty query.")
        allowed_fields = {
            "subject",
            "sender",
            "recipients",
            "body",
            "attachment_names",
            "attachment_text",
        }
        if any(field not in allowed_fields for field in fields):
            raise ConfigurationError("Fuzzy search requested an unsupported index field.")
        searched_fields = fields or (
            "subject",
            "sender",
            "recipients",
            "body",
            "attachment_names",
            "attachment_text",
        )
        attachment_only = bool(fields) and set(fields) <= {
            "attachment_names",
            "attachment_text",
        }
        clauses: list[str] = []
        parameters_list: list[Any] = []
        if folder:
            escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("folder_path LIKE ? ESCAPE '\\'")
            parameters_list.append(f"%{escaped}%")
        if attachment_only:
            clauses.append("(attachment_names != '' OR attachment_text != '')")
        if sender:
            clauses.append("LOWER(sender) LIKE ?")
            parameters_list.append(f"%{sanitize_terminal(sender, max_length=8_192).casefold()}%")
        if after is not None:
            normalized_after = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
            clauses.append("received_at >= ?")
            parameters_list.append(normalized_after.astimezone(UTC).isoformat())
        if before is not None:
            normalized_before = before if before.tzinfo is not None else before.replace(tzinfo=UTC)
            clauses.append("received_at < ?")
            parameters_list.append(normalized_before.astimezone(UTC).isoformat())
        if minimum_size is not None:
            clauses.append("size >= ?")
            parameters_list.append(minimum_size)
        if maximum_size is not None:
            clauses.append("size <= ?")
            parameters_list.append(maximum_size)
        if attachment_extension:
            clauses.append("LOWER(attachment_names) LIKE ?")
            parameters_list.append(
                f"%{sanitize_terminal(attachment_extension, max_length=32).casefold()}%"
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT * FROM documents{where} "  # noqa: S608 - fixed internal clauses
            "ORDER BY received_at DESC, reference LIMIT ?"
        )
        parameters = (*parameters_list, MAX_FUZZY_CANDIDATES)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        ranked: list[tuple[float, sqlite3.Row, str]] = []
        for row in rows:
            haystack = " ".join(str(row[name]) for name in searched_fields)
            score = float(WRatio(needle, haystack.casefold()))
            if score >= threshold:
                ranked.append((score, row, haystack[:1_000]))
        ranked.sort(key=lambda value: (-value[0], str(value[1]["reference"])))
        return tuple(
            self._hit(row, score, snippet)
            for score, row, snippet in ranked[offset : offset + limit]
        )

    def stats(self) -> IndexStats:
        self.initialize()
        with self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            row = connection.execute(
                "SELECT value FROM index_metadata WHERE key='updated_at'"
            ).fetchone()
        updated = datetime.fromisoformat(row[0]) if row else None
        return IndexStats(
            documents=count,
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
            updated_at=updated,
            schema_version=INDEX_SCHEMA_VERSION,
            fts5=True,
        )

    def verify(self) -> IndexVerification:
        """Run SQLite integrity checks and compare source and FTS row counts."""

        self.initialize()
        try:
            with self._connection() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                full_text_rows = int(
                    connection.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
                )
                mapped_rows = int(
                    connection.execute("SELECT COUNT(*) FROM document_fts_ids").fetchone()[0]
                )
                missing_mappings = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM documents d "
                        "LEFT JOIN document_fts_ids m USING(reference) "
                        "LEFT JOIN documents_fts f ON f.rowid=m.fts_rowid "
                        "AND f.reference=d.reference "
                        "WHERE f.rowid IS NULL OR m.received_at IS NOT d.received_at "
                        "OR m.folder_path IS NOT d.folder_path OR m.sender IS NOT d.sender "
                        "OR m.size IS NOT d.size OR m.attachment_names IS NOT d.attachment_names"
                    ).fetchone()[0]
                )
                row = connection.execute(
                    "SELECT value FROM index_metadata WHERE key='schema_version'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise LocalStateError(f"Could not verify the local search index: {exc}") from exc
        schema_version = int(row[0]) if row else 0
        return IndexVerification(
            valid=(
                integrity.casefold() == "ok"
                and documents == full_text_rows == mapped_rows
                and missing_mappings == 0
                and schema_version == INDEX_SCHEMA_VERSION
            ),
            integrity=integrity,
            documents=documents,
            full_text_rows=full_text_rows,
            schema_version=schema_version,
        )

    def optimize(self) -> IndexStats:
        """Compact FTS segments and ask SQLite to refresh planner statistics."""

        self.initialize()
        try:
            with self._lock, self._connection() as connection:
                connection.execute("INSERT INTO documents_fts(documents_fts) VALUES ('optimize')")
                connection.execute("PRAGMA optimize")
        except sqlite3.Error as exc:
            raise LocalStateError(f"Could not optimize the local search index: {exc}") from exc
        return self.stats()


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "IndexDocument",
    "IndexHit",
    "IndexStats",
    "IndexVerification",
    "IndexedRecord",
    "SearchIndex",
]
