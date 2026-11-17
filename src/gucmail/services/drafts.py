"""Durable local drafts, crash recovery, and attachment integrity checks.

The mailbox remains the authority for server drafts.  This module deliberately
keeps an independent local copy so a failed server save or editor crash cannot
destroy a composition.  Recovery files are replaced atomically and SQLite
updates use optimistic revision checks.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from gucmail.constants import MAX_LOCAL_OFFSET
from gucmail.errors import ConfigurationError, ConflictError, ItemNotFoundError, LocalStateError
from gucmail.models import Address, DraftData, DraftLocation, Importance
from gucmail.security import safe_filename, sha256_file
from gucmail.state import StateStore, iso, utc_now

RECOVERY_FORMAT_VERSION = 1
DEFAULT_REVISION_RETENTION = 50


class AttachmentIntegrityError(ConflictError):
    """A scheduled or recovered attachment no longer matches its manifest."""


class AttachmentDiagnosticStatus(StrEnum):
    """Machine-readable local attachment validation result."""

    OK = "ok"
    INVALID_MANIFEST = "invalid_manifest"
    MISSING = "missing"
    NOT_A_FILE = "not_a_file"
    SIZE_CHANGED = "size_changed"
    CONTENT_CHANGED = "content_changed"
    TOTAL_TOO_LARGE = "total_too_large"


@dataclass(frozen=True, slots=True)
class AttachmentDiagnostic:
    path: str
    status: AttachmentDiagnosticStatus
    message: str
    expected_size: int | None = None
    actual_size: int | None = None

    @property
    def valid(self) -> bool:
        return self.status is AttachmentDiagnosticStatus.OK


@dataclass(frozen=True, slots=True)
class DraftRevision:
    reference: str
    revision: int
    content_hash: str
    created_at: datetime
    draft: DraftData


@dataclass(frozen=True, slots=True)
class DraftRecord:
    draft: DraftData
    content_hash: str
    created_at: datetime
    updated_at: datetime
    state: DraftLocation


@dataclass(frozen=True, slots=True)
class DraftPage:
    items: tuple[DraftData, ...]
    offset: int
    limit: int
    total: int
    content_hashes: tuple[str, ...] = ()

    @property
    def next_offset(self) -> int | None:
        following = self.offset + len(self.items)
        return following if self.items and following < self.total else None


@dataclass(frozen=True, slots=True)
class AttachmentManifestEntry:
    path: str
    name: str
    size: int
    modified_ns: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "modified_ns": self.modified_ns,
            "sha256": self.sha256,
        }


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _address_to_dict(address: Address) -> dict[str, str | None]:
    return {
        "email": address.email,
        "name": address.name,
        "mailbox_type": address.mailbox_type,
    }


def draft_to_dict(draft: DraftData) -> dict[str, Any]:
    """Serialize a draft without depending on dataclass implementation details."""

    return {
        "reference": draft.reference,
        "to": [_address_to_dict(item) for item in draft.to],
        "cc": [_address_to_dict(item) for item in draft.cc],
        "bcc": [_address_to_dict(item) for item in draft.bcc],
        "subject": draft.subject,
        "body_text": draft.body_text,
        "body_html": draft.body_html,
        "attachment_paths": [str(path) for path in draft.attachment_paths],
        "importance": draft.importance.value,
        "request_read_receipt": draft.request_read_receipt,
        "request_delivery_receipt": draft.request_delivery_receipt,
        "save_sent_copy": draft.save_sent_copy,
        "location": draft.location.value,
        "revision": draft.revision,
        "updated_at": (
            draft.updated_at.replace(tzinfo=UTC).isoformat()
            if draft.updated_at is not None and draft.updated_at.tzinfo is None
            else draft.updated_at.astimezone(UTC).isoformat()
            if draft.updated_at is not None
            else None
        ),
        "server_id": draft.server_id,
        "server_change_key": draft.server_change_key,
        "reply_to_reference": draft.reply_to_reference,
        "forward_of_reference": draft.forward_of_reference,
    }


def _addresses(value: object) -> list[Address]:
    if not isinstance(value, list):
        return []
    result: list[Address] = []
    for raw in value:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("email"), str):
            raise LocalStateError("Draft recovery data contains an invalid address.")
        result.append(
            Address(
                email=raw["email"],
                name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                mailbox_type=(
                    raw.get("mailbox_type") if isinstance(raw.get("mailbox_type"), str) else None
                ),
            )
        )
    return result


def draft_from_dict(data: Mapping[str, Any]) -> DraftData:
    """Deserialize validated local draft JSON into the public domain model."""

    try:
        reference = data["reference"]
        if not isinstance(reference, str) or not reference:
            raise ValueError("reference must be a non-empty string")
        updated_raw = data.get("updated_at")
        updated_at = datetime.fromisoformat(updated_raw) if isinstance(updated_raw, str) else None
        raw_paths = data.get("attachment_paths", [])
        if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
            raise ValueError("attachment_paths must be strings")
        save_sent_copy = data.get("save_sent_copy", True)
        if type(save_sent_copy) is not bool:
            raise ValueError("save_sent_copy must be a boolean")
        return DraftData(
            reference=reference,
            to=_addresses(data.get("to", [])),
            cc=_addresses(data.get("cc", [])),
            bcc=_addresses(data.get("bcc", [])),
            subject=str(data.get("subject", "")),
            body_text=str(data.get("body_text", "")),
            body_html=data.get("body_html") if isinstance(data.get("body_html"), str) else None,
            attachment_paths=[Path(item) for item in raw_paths],
            importance=Importance(str(data.get("importance", Importance.NORMAL.value))),
            request_read_receipt=bool(data.get("request_read_receipt", False)),
            request_delivery_receipt=bool(data.get("request_delivery_receipt", False)),
            save_sent_copy=save_sent_copy,
            location=DraftLocation(str(data.get("location", DraftLocation.LOCAL.value))),
            revision=int(data.get("revision", 1)),
            updated_at=updated_at,
            server_id=data.get("server_id") if isinstance(data.get("server_id"), str) else None,
            server_change_key=(
                data.get("server_change_key")
                if isinstance(data.get("server_change_key"), str)
                else None
            ),
            reply_to_reference=(
                data.get("reply_to_reference")
                if isinstance(data.get("reply_to_reference"), str)
                else None
            ),
            forward_of_reference=(
                data.get("forward_of_reference")
                if isinstance(data.get("forward_of_reference"), str)
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalStateError(f"Draft recovery data is invalid: {exc}") from exc


def _content_dict(draft: DraftData) -> dict[str, Any]:
    data = draft_to_dict(draft)
    # True is the historical implicit policy. Omit it to preserve actual prior
    # draft/recovery/scheduler hashes; an explicit False must remain hash-bound.
    if draft.save_sent_copy is True:
        data.pop("save_sent_copy", None)
    # These values describe local persistence or the current Exchange copy, not
    # message content.  In particular, Exchange changes the ChangeKey after an
    # in-place save; including it would falsely report every successful sync as
    # a content conflict.
    for key in (
        "revision",
        "updated_at",
        "location",
        "server_id",
        "server_change_key",
    ):
        data.pop(key, None)
    return data


def _storage_dict(draft: DraftData) -> dict[str, Any]:
    """Return every persisted field except save bookkeeping."""

    data = draft_to_dict(draft)
    data.pop("revision", None)
    data.pop("updated_at", None)
    return data


def draft_content_hash(draft: DraftData) -> str:
    payload = json.dumps(
        _content_dict(draft), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_attachment_manifest(
    paths: Iterable[Path | str],
    *,
    maximum_bytes: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Hash local attachments so unattended sends cannot use changed content."""

    result: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except OSError as exc:
            raise AttachmentIntegrityError(f"Attachment is not available: {path}") from exc
        if not resolved.is_file():
            raise AttachmentIntegrityError(f"Attachment is not a regular file: {resolved}")
        normalized = os.path.normcase(str(resolved))
        if normalized in seen:
            continue
        seen.add(normalized)
        total += stat.st_size
        if maximum_bytes is not None and total > maximum_bytes:
            raise AttachmentIntegrityError(
                f"Attachments total {total} bytes, exceeding the configured {maximum_bytes} bytes."
            )
        result.append(
            AttachmentManifestEntry(
                path=str(resolved),
                name=safe_filename(resolved.name),
                size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                sha256=sha256_file(resolved),
            ).as_dict()
        )
    return tuple(result)


def validate_attachment_manifest(
    manifest: Iterable[Mapping[str, Any]],
    *,
    maximum_bytes: int | None = None,
) -> None:
    """Raise before sending if any attachment was replaced, edited, or removed."""

    diagnostics = inspect_attachment_manifest(manifest, maximum_bytes=maximum_bytes)
    invalid = next((item for item in diagnostics if not item.valid), None)
    if invalid is not None:
        raise AttachmentIntegrityError(invalid.message)


def inspect_attachment_manifest(
    manifest: Iterable[Mapping[str, Any]],
    *,
    maximum_bytes: int | None = None,
) -> tuple[AttachmentDiagnostic, ...]:
    """Return complete missing/changed-file diagnostics without failing at the first file.

    Scheduled-send and recovery screens need to explain *which* paths require
    attention.  The older validator remains the fail-fast compatibility API and
    delegates to this bounded diagnostic pass.
    """

    total = 0
    result: list[AttachmentDiagnostic] = []
    for entry in manifest:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            result.append(
                AttachmentDiagnostic(
                    "<invalid>",
                    AttachmentDiagnosticStatus.INVALID_MANIFEST,
                    "Attachment manifest contains an invalid path.",
                )
            )
            continue
        path = Path(raw_path)
        try:
            stat = path.stat()
        except OSError:
            result.append(
                AttachmentDiagnostic(
                    raw_path,
                    AttachmentDiagnosticStatus.MISSING,
                    f"Scheduled attachment is missing: {path}",
                    expected_size=(
                        int(entry["size"]) if isinstance(entry.get("size"), int) else None
                    ),
                )
            )
            continue
        if not path.is_file():
            result.append(
                AttachmentDiagnostic(
                    raw_path,
                    AttachmentDiagnosticStatus.NOT_A_FILE,
                    f"Scheduled attachment is not a file: {path}",
                )
            )
            continue
        expected_size = entry.get("size")
        if not isinstance(expected_size, int) or stat.st_size != expected_size:
            result.append(
                AttachmentDiagnostic(
                    raw_path,
                    AttachmentDiagnosticStatus.SIZE_CHANGED,
                    f"Scheduled attachment changed size: {path}",
                    expected_size=expected_size if isinstance(expected_size, int) else None,
                    actual_size=stat.st_size,
                )
            )
            continue
        total += stat.st_size
        if maximum_bytes is not None and total > maximum_bytes:
            result.append(
                AttachmentDiagnostic(
                    raw_path,
                    AttachmentDiagnosticStatus.TOTAL_TOO_LARGE,
                    f"Attachments exceed the configured {maximum_bytes}-byte limit.",
                    expected_size=expected_size,
                    actual_size=stat.st_size,
                )
            )
            continue
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or sha256_file(path) != expected_hash:
            result.append(
                AttachmentDiagnostic(
                    raw_path,
                    AttachmentDiagnosticStatus.CONTENT_CHANGED,
                    f"Scheduled attachment content changed: {path}",
                    expected_size=expected_size,
                    actual_size=stat.st_size,
                )
            )
            continue
        result.append(
            AttachmentDiagnostic(
                raw_path,
                AttachmentDiagnosticStatus.OK,
                f"Attachment is available and unchanged: {path}",
                expected_size=expected_size,
                actual_size=stat.st_size,
            )
        )
    return tuple(result)


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class DraftService:
    """Revisioned draft persistence with a separate crash-recovery journal."""

    def __init__(
        self,
        state: StateStore,
        recovery_directory: Path,
        *,
        maximum_attachment_bytes: int | None = None,
        revision_retention: int = DEFAULT_REVISION_RETENTION,
    ) -> None:
        if revision_retention < 1:
            raise ValueError("revision_retention must be positive")
        self.state = state
        self.recovery_directory = recovery_directory
        self.maximum_attachment_bytes = maximum_attachment_bytes
        self.revision_retention = revision_retention

    def recovery_path(self, reference: str) -> Path:
        stem = safe_filename(reference, fallback="draft")[:80]
        suffix = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:12]
        return self.recovery_directory / f"{stem}-{suffix}.json"

    def create(self, draft: DraftData | None = None, **values: Any) -> DraftData:
        if draft is None:
            reference = str(values.pop("reference", f"dft-{uuid.uuid4().hex}"))
            draft = DraftData(reference=reference, **values)
        draft.revision = 1
        return self._save(draft, create_only=True, expected_revision=None, expected_hash=None)

    def save(
        self,
        draft: DraftData,
        *,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> DraftData:
        """Save using optimistic concurrency; a stale ``draft.revision`` conflicts."""

        if expected_revision is None:
            expected_revision = draft.revision
        return self._save(
            draft,
            create_only=False,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
        )

    def autosave(
        self,
        draft: DraftData,
        *,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> DraftData:
        return self.save(
            draft,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
        )

    def _save(
        self,
        draft: DraftData,
        *,
        create_only: bool,
        expected_revision: int | None,
        expected_hash: str | None,
    ) -> DraftData:
        if not draft.reference.strip():
            raise LocalStateError("A draft reference may not be empty.")
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM local_drafts WHERE reference=?", (draft.reference,)
            ).fetchone()
            if create_only and row is not None:
                raise ConflictError(f"Draft already exists: {draft.reference}")
            if row is None and not create_only:
                raise ItemNotFoundError(f"Draft not found: {draft.reference}")
            if row is not None:
                current_revision = int(row["revision"])
                current_hash = str(row["content_hash"])
                if expected_revision is not None and expected_revision != current_revision:
                    raise ConflictError(
                        f"Draft {draft.reference} changed from revision {expected_revision} "
                        f"to {current_revision}; reload it before saving."
                    )
                if expected_hash is not None and expected_hash != current_hash:
                    raise ConflictError(
                        f"Draft {draft.reference} content changed; reload it before saving."
                    )
                revision = current_revision + 1
                created_at = str(row["created_at"])
            else:
                revision = 1
                created_at = iso()

            now = utc_now()
            saved = draft_from_dict(draft_to_dict(draft))
            saved.revision = revision
            saved.updated_at = now
            content_hash = draft_content_hash(saved)
            if row is not None:
                existing = draft_from_dict(json.loads(str(row["draft_json"])))
                if _storage_dict(existing) == _storage_dict(saved):
                    self.write_recovery(existing)
                    return existing

            payload = json.dumps(draft_to_dict(saved), ensure_ascii=False, sort_keys=True)
            self.write_recovery(saved)
            now_text = iso(now)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO local_drafts(
                        reference, draft_json, revision, server_id, server_change_key,
                        state, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        saved.reference,
                        payload,
                        revision,
                        saved.server_id,
                        saved.server_change_key,
                        saved.location.value,
                        content_hash,
                        created_at,
                        now_text,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE local_drafts SET draft_json=?, revision=?, server_id=?,
                        server_change_key=?, state=?, content_hash=?, updated_at=?
                    WHERE reference=? AND revision=?
                    """,
                    (
                        payload,
                        revision,
                        saved.server_id,
                        saved.server_change_key,
                        saved.location.value,
                        content_hash,
                        now_text,
                        saved.reference,
                        int(row["revision"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(f"Draft changed concurrently: {saved.reference}")
            connection.execute(
                """
                INSERT INTO draft_revisions(
                    draft_reference, revision, draft_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (saved.reference, revision, payload, content_hash, now_text),
            )
            self._prune_revisions_in_transaction(connection, saved.reference)
        return saved

    def _prune_revisions_in_transaction(self, connection: Any, reference: str) -> int:
        cursor = connection.execute(
            """
            DELETE FROM draft_revisions
            WHERE draft_reference=? AND revision NOT IN (
                SELECT revision FROM draft_revisions
                WHERE draft_reference=? ORDER BY revision DESC LIMIT ?
            )
            """,
            (reference, reference, self.revision_retention),
        )
        return int(cursor.rowcount)

    def prune_revisions(self, reference: str, *, keep_latest: int | None = None) -> int:
        """Apply a bounded revision-retention policy to one draft."""

        if not self.exists(reference):
            raise ItemNotFoundError(f"Draft not found: {reference}")
        keep = self.revision_retention if keep_latest is None else keep_latest
        if keep < 1:
            raise ValueError("keep_latest must be positive")
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM draft_revisions
                WHERE draft_reference=? AND revision NOT IN (
                    SELECT revision FROM draft_revisions
                    WHERE draft_reference=? ORDER BY revision DESC LIMIT ?
                )
                """,
                (reference, reference, keep),
            )
        return int(cursor.rowcount)

    def write_recovery(self, draft: DraftData) -> Path:
        # A missing/changed attachment must not prevent recovery of the text.
        # This is a recovery copy, never authority to send unchecked files.
        try:
            manifest = build_attachment_manifest(
                draft.attachment_paths, maximum_bytes=self.maximum_attachment_bytes
            )
            attachment_review_required = False
        except (AttachmentIntegrityError, OSError):
            manifest = ()
            attachment_review_required = True
        data = draft_to_dict(draft)
        envelope = {
            "attachment_review_required": attachment_review_required,
            "format_version": RECOVERY_FORMAT_VERSION,
            "saved_at": iso(),
            "content_hash": draft_content_hash(draft),
            "attachment_manifest": manifest,
            "draft": data,
        }
        path = self.recovery_path(draft.reference)
        try:
            _atomic_write_json(path, envelope)
        except OSError as exc:
            raise LocalStateError(f"Could not write draft recovery copy: {exc}") from exc
        return path

    def read_recovery(
        self,
        reference: str,
        *,
        validate_attachments: bool = False,
    ) -> DraftData:
        path = self.recovery_path(reference)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ItemNotFoundError(f"No recovery copy exists for draft: {reference}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalStateError(f"Draft recovery copy is unreadable: {path}") from exc
        if not isinstance(parsed, dict) or parsed.get("format_version") != RECOVERY_FORMAT_VERSION:
            raise LocalStateError(f"Draft recovery copy has an unsupported format: {path}")
        raw_draft = parsed.get("draft")
        if not isinstance(raw_draft, Mapping):
            raise LocalStateError(f"Draft recovery copy contains no valid draft: {path}")
        draft = draft_from_dict(raw_draft)
        if draft.reference != reference:
            raise LocalStateError("Draft recovery reference does not match its filename.")
        expected = parsed.get("content_hash")
        if not isinstance(expected, str) or expected != draft_content_hash(draft):
            raise LocalStateError("Draft recovery content hash does not match its payload.")
        if validate_attachments:
            if parsed.get("attachment_review_required"):
                raise AttachmentIntegrityError(
                    "Recovery preserved the draft, but its attachments need review before sending."
                )
            raw_manifest = parsed.get("attachment_manifest", [])
            if not isinstance(raw_manifest, list):
                raise LocalStateError("Draft recovery attachment manifest is invalid.")
            validate_attachment_manifest(
                (item for item in raw_manifest if isinstance(item, Mapping)),
                maximum_bytes=self.maximum_attachment_bytes,
            )
        return draft

    def remove_recovery(self, reference: str) -> bool:
        path = self.recovery_path(reference)
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise LocalStateError(f"Could not remove draft recovery copy: {exc}") from exc
        return existed

    def get_record(self, reference: str) -> DraftRecord:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_drafts WHERE reference=?", (reference,)
            ).fetchone()
        if row is None:
            raise ItemNotFoundError(f"Draft not found: {reference}")
        try:
            draft = draft_from_dict(json.loads(str(row["draft_json"])))
            return DraftRecord(
                draft=draft,
                content_hash=str(row["content_hash"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                state=DraftLocation(str(row["state"])),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise LocalStateError(f"Stored draft is corrupt: {reference}") from exc

    def get(self, reference: str) -> DraftData:
        return self.get_record(reference).draft

    def list(self, *, location: DraftLocation | None = None) -> list[DraftData]:
        self.state.initialize()
        query = "SELECT draft_json FROM local_drafts"
        parameters: tuple[Any, ...] = ()
        if location is not None:
            query += " WHERE state=?"
            parameters = (location.value,)
        query += " ORDER BY updated_at DESC, reference"
        with self.state.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result: list[DraftData] = []
        for row in rows:
            try:
                result.append(draft_from_dict(json.loads(str(row["draft_json"]))))
            except (json.JSONDecodeError, LocalStateError) as exc:
                raise LocalStateError("A stored draft is corrupt.") from exc
        return result

    list_drafts = list

    def list_page(
        self,
        *,
        query: str = "",
        locations: Iterable[DraftLocation] = (),
        updated_before: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
        cancel: threading.Event | None = None,
    ) -> DraftPage:
        """Read one consistent, bounded page; never deserialize skipped drafts.

        Substring search keeps the existing Unicode-casefold semantics for
        subject, plain body and recipient emails. SQLite evaluates the predicate;
        only returned records become domain objects. Search may scan the local
        corpus, but does not collect it in Python or the presentation widget.
        """
        if not 1 <= limit <= 1_000:
            raise ConfigurationError("Draft limit must be 1-1000.")
        if not 0 <= offset <= MAX_LOCAL_OFFSET:
            raise ConfigurationError(f"Draft offset must be between 0 and {MAX_LOCAL_OFFSET}.")
        needle = query.casefold().strip()
        if len(needle) > 4_096:
            raise ConfigurationError("Draft search must be at most 4096 characters.")
        if cancel is not None and cancel.is_set():
            raise LocalStateError("Loading drafts was cancelled.")
        predicates: list[str] = []
        parameters: list[Any] = []
        try:
            choices = tuple(dict.fromkeys(DraftLocation(value).value for value in locations))
        except ValueError as exc:
            raise ConfigurationError("The draft location filter is invalid.") from exc
        if choices:
            predicates.append("state IN (" + ",".join("?" for _ in choices) + ")")
            parameters.extend(choices)
        if updated_before is not None:
            predicates.append("updated_at < ?")
            parameters.append(iso(_aware_utc(updated_before)))
        if needle:
            predicates.append("""
                instr(gucmail_casefold(
                    coalesce(json_extract(draft_json, '$.subject'), '') || char(10) ||
                    coalesce(json_extract(draft_json, '$.body_text'), '') || char(10) ||
                    coalesce((SELECT group_concat(json_extract(value, '$.email'), ' ')
                        FROM (
                            SELECT value FROM json_each(local_drafts.draft_json, '$.to')
                            UNION ALL
                            SELECT value FROM json_each(local_drafts.draft_json, '$.cc')
                            UNION ALL
                            SELECT value FROM json_each(local_drafts.draft_json, '$.bcc')
                        )), '')
                ), ?) > 0
            """)
            parameters.append(needle)
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        self.state.initialize()
        try:
            with self.state.transaction() as connection:
                connection.create_function("gucmail_casefold", 1, str.casefold, deterministic=True)
                if cancel is not None:
                    connection.set_progress_handler(lambda: int(cancel.is_set()), 1_000)
                total = int(
                    connection.execute(
                        "SELECT count(*) FROM local_drafts" + where,  # noqa: S608
                        parameters,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT draft_json,content_hash FROM local_drafts"  # noqa: S608
                    + where
                    + " ORDER BY updated_at DESC, reference LIMIT ? OFFSET ?",
                    (*parameters, limit, offset),
                ).fetchall()
                items = tuple(draft_from_dict(json.loads(row[0])) for row in rows)
                if cancel is not None and cancel.is_set():
                    raise LocalStateError("Loading drafts was cancelled.")
                connection.set_progress_handler(None, 0)
        except sqlite3.Error as exc:
            if cancel is not None and cancel.is_set():
                raise LocalStateError("Loading drafts was cancelled.") from exc
            raise LocalStateError(
                "The local draft page could not be read; check cache health."
            ) from exc
        except (json.JSONDecodeError, TypeError) as exc:
            raise LocalStateError("A stored draft is corrupt.") from exc
        return DraftPage(items, offset, limit, total, tuple(str(row[1]) for row in rows))

    def search(self, query: str) -> builtins.list[DraftData]:
        needle = query.casefold().strip()
        if not needle:
            return self.list()
        result: list[DraftData] = []
        for draft in self.list():
            addresses = " ".join(address.email for address in (*draft.to, *draft.cc, *draft.bcc))
            haystack = f"{draft.subject}\n{draft.body_text}\n{addresses}".casefold()
            if needle in haystack:
                result.append(draft)
        return result

    def edit(
        self,
        reference: str,
        *,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
        **changes: Any,
    ) -> DraftData:
        record = self.get_record(reference)
        allowed = {
            "to",
            "cc",
            "bcc",
            "subject",
            "body_text",
            "body_html",
            "attachment_paths",
            "importance",
            "request_read_receipt",
            "request_delivery_receipt",
            "save_sent_copy",
            "location",
            "server_id",
            "server_change_key",
            "reply_to_reference",
            "forward_of_reference",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise LocalStateError(f"Unknown draft field(s): {', '.join(sorted(unknown))}")
        updated = replace(record.draft, **changes)
        return self.save(
            updated,
            expected_revision=(
                record.draft.revision if expected_revision is None else expected_revision
            ),
            expected_hash=expected_hash,
        )

    def duplicate(self, reference: str, *, new_reference: str | None = None) -> DraftData:
        source = self.get(reference)
        copied = replace(
            source,
            reference=new_reference or f"dft-{uuid.uuid4().hex}",
            revision=1,
            updated_at=None,
            location=DraftLocation.LOCAL,
            server_id=None,
            server_change_key=None,
        )
        return self.create(copied)

    def revisions(self, reference: str) -> builtins.list[DraftRevision]:
        self.state.initialize()
        with self.state.connection() as connection:
            rows = connection.execute(
                """
                SELECT revision, draft_json, content_hash, created_at
                FROM draft_revisions WHERE draft_reference=? ORDER BY revision DESC
                """,
                (reference,),
            ).fetchall()
        if not rows and not self.exists(reference):
            raise ItemNotFoundError(f"Draft not found: {reference}")
        return [
            DraftRevision(
                reference=reference,
                revision=int(row["revision"]),
                content_hash=str(row["content_hash"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                draft=draft_from_dict(json.loads(str(row["draft_json"]))),
            )
            for row in rows
        ]

    def exists(self, reference: str) -> bool:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM local_drafts WHERE reference=?", (reference,)
            ).fetchone()
        return row is not None

    def recover(self, reference: str, *, overwrite: bool = False) -> DraftData:
        recovered = self.read_recovery(reference)
        if not self.exists(reference):
            recovered.revision = 1
            return self.create(recovered)
        current = self.get_record(reference)
        if draft_content_hash(recovered) == current.content_hash:
            return current.draft
        if not overwrite:
            raise ConflictError(
                f"Recovery copy and stored draft differ for {reference}; "
                "explicit overwrite is required."
            )
        recovered.revision = current.draft.revision
        return self.save(recovered, expected_revision=current.draft.revision)

    def recovery_references(self) -> builtins.list[str]:
        """Return valid recovery references oldest first, newest last."""
        if not self.recovery_directory.exists():
            return []
        references: list[tuple[int, str]] = []
        for path in sorted(self.recovery_directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                draft = raw.get("draft") if isinstance(raw, dict) else None
                reference = draft.get("reference") if isinstance(draft, dict) else None
                if isinstance(reference, str):
                    references.append((path.stat().st_mtime_ns, reference))
            except (OSError, json.JSONDecodeError):
                continue
        return [reference for _modified, reference in sorted(references)]

    def conflicts(self) -> builtins.list[DraftData]:
        return self.list(location=DraftLocation.CONFLICTED)

    def delete(
        self,
        reference: str,
        *,
        remove_recovery: bool = True,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> bool:
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT revision,content_hash FROM local_drafts WHERE reference=?", (reference,)
            ).fetchone()
            if row is None:
                return False
            if (expected_revision is not None and int(row["revision"]) != expected_revision) or (
                expected_hash is not None and row["content_hash"] != expected_hash
            ):
                raise ConflictError("This draft changed after review; refresh and review it again.")
            cursor = connection.execute("DELETE FROM local_drafts WHERE reference=?", (reference,))
            if cursor.rowcount:
                connection.execute(
                    "DELETE FROM metadata WHERE key IN (?, ?)",
                    (_draft_sync_key(reference), _draft_session_index_key(reference)),
                )
            # Keep the writer lock through file cleanup. Otherwise another
            # session can recreate this reference and its new recovery file can
            # be removed by the preceding delete. Cleanup failure rolls back SQL.
            if remove_recovery:
                self.remove_recovery(reference)
        return True

    def finish_sent(self, reference: str, *, expected_revision: int) -> bool:
        """Remove a sent local copy, preserving concurrent edits and their recovery."""

        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT revision FROM local_drafts WHERE reference=?", (reference,)
            ).fetchone()
            if row is not None and int(row["revision"]) != expected_revision:
                return False
            connection.execute("DELETE FROM local_drafts WHERE reference=?", (reference,))
            connection.execute(
                "DELETE FROM metadata WHERE key IN (?, ?)",
                (_draft_sync_key(reference), _draft_session_index_key(reference)),
            )
            self.remove_recovery(reference)
        return True


class DraftSyncState(StrEnum):
    """Authority relationship between the local recovery copy and Exchange draft."""

    LOCAL_ONLY = "local_only"
    SERVER_ONLY = "server_only"
    SYNCHRONIZED = "synchronized"
    LOCAL_DIRTY = "local_dirty"
    SERVER_DIRTY = "server_dirty"
    CONFLICTED = "conflicted"
    SERVER_MISSING = "server_missing"


@dataclass(frozen=True, slots=True)
class ServerDraftSnapshot:
    """Complete server draft returned by an update-in-place gateway."""

    draft: DraftData
    server_id: str
    server_change_key: str | None
    modified_at: datetime | None = None
    client_reference: str | None = None


class ServerDraftGateway(Protocol):
    """Server-draft boundary with an explicit create/update-in-place contract.

    ``save_server_draft`` must create only when ``draft.server_id`` is absent.
    When it is present, the gateway must address that exact Exchange item using
    ``server_change_key`` and return the same stable local ``reference`` with the
    post-save ID and ChangeKey. It must never create a replacement silently.
    """

    def save_server_draft(self, draft: DraftData) -> DraftData: ...

    def get_server_draft(self, server_id: str) -> DraftData: ...


@dataclass(frozen=True, slots=True)
class DraftSyncMetadata:
    reference: str
    state: DraftSyncState
    local_content_hash: str
    base_content_hash: str | None = None
    server_content_hash: str | None = None
    server_id: str | None = None
    server_change_key: str | None = None
    last_synced_at: datetime | None = None
    server_modified_at: datetime | None = None
    conflict_reason: str | None = None
    conflict_detected_at: datetime | None = None
    remote_draft: Mapping[str, Any] | None = None

    @property
    def conflicted(self) -> bool:
        return self.state is DraftSyncState.CONFLICTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "state": self.state.value,
            "local_content_hash": self.local_content_hash,
            "base_content_hash": self.base_content_hash,
            "server_content_hash": self.server_content_hash,
            "server_id": self.server_id,
            "server_change_key": self.server_change_key,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "server_modified_at": (
                self.server_modified_at.isoformat() if self.server_modified_at else None
            ),
            "conflict_reason": self.conflict_reason,
            "conflict_detected_at": (
                self.conflict_detected_at.isoformat() if self.conflict_detected_at else None
            ),
            "remote_draft": dict(self.remote_draft) if self.remote_draft is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DraftSyncMetadata:
        def parsed_time(name: str) -> datetime | None:
            raw = data.get(name)
            return datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None

        reference = data.get("reference")
        local_hash = data.get("local_content_hash")
        if not isinstance(reference, str) or not isinstance(local_hash, str):
            raise LocalStateError("Stored draft synchronization metadata is invalid.")
        remote = data.get("remote_draft")
        return cls(
            reference=reference,
            state=DraftSyncState(str(data.get("state"))),
            local_content_hash=local_hash,
            base_content_hash=(
                str(data["base_content_hash"]) if data.get("base_content_hash") else None
            ),
            server_content_hash=(
                str(data["server_content_hash"]) if data.get("server_content_hash") else None
            ),
            server_id=str(data["server_id"]) if data.get("server_id") else None,
            server_change_key=(
                str(data["server_change_key"]) if data.get("server_change_key") else None
            ),
            last_synced_at=parsed_time("last_synced_at"),
            server_modified_at=parsed_time("server_modified_at"),
            conflict_reason=(str(data["conflict_reason"]) if data.get("conflict_reason") else None),
            conflict_detected_at=parsed_time("conflict_detected_at"),
            remote_draft=dict(remote) if isinstance(remote, Mapping) else None,
        )


_SYNC_TRANSITIONS: Mapping[DraftSyncState, frozenset[DraftSyncState]] = {
    DraftSyncState.LOCAL_ONLY: frozenset(
        {
            DraftSyncState.LOCAL_ONLY,
            DraftSyncState.LOCAL_DIRTY,
            DraftSyncState.SYNCHRONIZED,
            DraftSyncState.CONFLICTED,
        }
    ),
    DraftSyncState.SERVER_ONLY: frozenset(
        {
            DraftSyncState.SERVER_ONLY,
            DraftSyncState.SYNCHRONIZED,
            DraftSyncState.SERVER_DIRTY,
            DraftSyncState.CONFLICTED,
        }
    ),
    DraftSyncState.SYNCHRONIZED: frozenset(
        {
            DraftSyncState.LOCAL_DIRTY,
            DraftSyncState.SERVER_DIRTY,
            DraftSyncState.CONFLICTED,
            DraftSyncState.SERVER_MISSING,
            DraftSyncState.SYNCHRONIZED,
        }
    ),
    DraftSyncState.LOCAL_DIRTY: frozenset(
        {
            DraftSyncState.SYNCHRONIZED,
            DraftSyncState.CONFLICTED,
            DraftSyncState.SERVER_MISSING,
            DraftSyncState.LOCAL_DIRTY,
        }
    ),
    DraftSyncState.SERVER_DIRTY: frozenset(
        {DraftSyncState.SYNCHRONIZED, DraftSyncState.CONFLICTED, DraftSyncState.SERVER_DIRTY}
    ),
    DraftSyncState.CONFLICTED: frozenset(
        {DraftSyncState.LOCAL_DIRTY, DraftSyncState.SYNCHRONIZED, DraftSyncState.CONFLICTED}
    ),
    DraftSyncState.SERVER_MISSING: frozenset(
        {
            DraftSyncState.SERVER_MISSING,
            DraftSyncState.LOCAL_ONLY,
            DraftSyncState.SYNCHRONIZED,
            DraftSyncState.CONFLICTED,
        }
    ),
}


def _draft_sync_key(reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"draft.sync.{digest}"


def _draft_session_index_key(reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return f"draft.sessions.{digest}"


def _copy_draft_with_reference(draft: DraftData, reference: str) -> DraftData:
    copied = draft_from_dict(draft_to_dict(draft))
    copied.reference = reference
    return copied


class DraftSyncCoordinator:
    """Synchronize drafts while preserving one stable local identity."""

    def __init__(self, drafts: DraftService, gateway: ServerDraftGateway) -> None:
        self.drafts = drafts
        self.gateway = gateway

    def metadata(self, reference: str) -> DraftSyncMetadata:
        draft = self.drafts.get(reference)
        key = _draft_sync_key(reference)
        self.drafts.state.initialize()
        with self.drafts.state.connection() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is not None:
            try:
                raw = json.loads(str(row[0]))
                if not isinstance(raw, Mapping):
                    raise ValueError("metadata is not an object")
                parsed = DraftSyncMetadata.from_dict(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LocalStateError(
                    f"Draft synchronization metadata is corrupt: {reference}"
                ) from exc
            if parsed.reference != reference:
                raise LocalStateError("Draft synchronization identity does not match its key.")
            return parsed
        local_hash = draft_content_hash(draft)
        if draft.server_id is None:
            state = DraftSyncState.LOCAL_ONLY
            base_hash = None
        elif draft.location in {DraftLocation.SYNCHRONIZED, DraftLocation.SERVER}:
            state = DraftSyncState.SYNCHRONIZED
            base_hash = local_hash
        else:
            state = DraftSyncState.LOCAL_DIRTY
            base_hash = None
        return DraftSyncMetadata(
            reference=reference,
            state=state,
            local_content_hash=local_hash,
            base_content_hash=base_hash,
            server_content_hash=base_hash,
            server_id=draft.server_id,
            server_change_key=draft.server_change_key,
        )

    def _store(self, metadata: DraftSyncMetadata, *, previous: DraftSyncState | None) -> None:
        if previous is not None and metadata.state not in _SYNC_TRANSITIONS[previous]:
            raise ConflictError(
                f"Invalid draft synchronization transition: {previous.value} -> "
                f"{metadata.state.value}."
            )
        payload = json.dumps(metadata.as_dict(), ensure_ascii=False, sort_keys=True)
        with self.drafts.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_draft_sync_key(metadata.reference), payload),
            )

    def mark_local_dirty(self, reference: str) -> DraftSyncMetadata:
        draft = self.drafts.get(reference)
        current = self.metadata(reference)
        state = (
            DraftSyncState.LOCAL_DIRTY
            if current.server_id is not None
            else DraftSyncState.LOCAL_ONLY
        )
        updated = replace(
            current,
            state=state,
            local_content_hash=draft_content_hash(draft),
            conflict_reason=None,
            conflict_detected_at=None,
            remote_draft=None,
        )
        self._store(updated, previous=current.state)
        return updated

    def save_local(self, draft: DraftData, *, expected_revision: int | None = None) -> DraftData:
        saved = self.drafts.save(draft, expected_revision=expected_revision)
        self.mark_local_dirty(saved.reference)
        return saved

    def import_server_draft(
        self,
        snapshot: ServerDraftSnapshot,
        *,
        reference: str | None = None,
    ) -> DraftData:
        """Import once, then reuse the same local reference even if Exchange rotates IDs."""

        self.drafts.state.initialize()
        with self.drafts.state.connection() as connection:
            row = connection.execute(
                "SELECT reference FROM local_drafts WHERE server_id=?", (snapshot.server_id,)
            ).fetchone()
        existing_reference = str(row[0]) if row is not None else None
        stable_reference = existing_reference or reference or snapshot.client_reference
        stable_reference = stable_reference or f"dft-{uuid.uuid4().hex}"
        if existing_reference:
            self.observe_server(existing_reference, snapshot)
            return self.drafts.get(existing_reference)
        candidate = _copy_draft_with_reference(snapshot.draft, stable_reference)
        candidate.server_id = snapshot.server_id
        candidate.server_change_key = snapshot.server_change_key
        candidate.location = DraftLocation.SYNCHRONIZED
        candidate.revision = 1
        candidate.updated_at = None
        saved = self.drafts.create(candidate)
        content_hash = draft_content_hash(saved)
        metadata = DraftSyncMetadata(
            reference=saved.reference,
            state=DraftSyncState.SYNCHRONIZED,
            local_content_hash=content_hash,
            base_content_hash=content_hash,
            server_content_hash=content_hash,
            server_id=snapshot.server_id,
            server_change_key=snapshot.server_change_key,
            last_synced_at=utc_now(),
            server_modified_at=_aware_utc(snapshot.modified_at),
        )
        self._store(metadata, previous=None)
        return saved

    def sync_to_server(self, reference: str) -> DraftData:
        local = self.drafts.get(reference)
        current = self.metadata(reference)
        if current.state is DraftSyncState.CONFLICTED:
            raise ConflictError("Resolve the draft conflict before saving it to Exchange.")
        outgoing = _copy_draft_with_reference(local, reference)
        outgoing.server_id = current.server_id or local.server_id
        outgoing.server_change_key = current.server_change_key or local.server_change_key
        try:
            returned = self.gateway.save_server_draft(outgoing)
            snapshot = self._snapshot_from_gateway(returned, reference)
        except ConflictError as exc:
            server_id = current.server_id or local.server_id
            if server_id:
                with suppress(Exception):
                    conflict_snapshot = self._snapshot_from_gateway(
                        self.gateway.get_server_draft(server_id), reference
                    )
                    self._mark_conflict(reference, conflict_snapshot, reason=str(exc))
            raise
        self._validate_snapshot(snapshot, reference)
        returned_draft = _copy_draft_with_reference(snapshot.draft, reference)
        # EWS stores message content, not the client's future SendItem policy.
        returned_draft.save_sent_copy = local.save_sent_copy
        local_hash = draft_content_hash(local)
        remote_hash = draft_content_hash(returned_draft)
        if local_hash != remote_hash:
            self._mark_conflict(
                reference,
                snapshot,
                reason="Exchange returned content different from the saved local draft.",
            )
            raise ConflictError("The server draft differs from the local draft after saving.")
        updated = _copy_draft_with_reference(local, reference)
        updated.server_id = snapshot.server_id
        updated.server_change_key = snapshot.server_change_key
        updated.location = DraftLocation.SYNCHRONIZED
        saved = self.drafts.save(updated, expected_revision=local.revision)
        saved_hash = draft_content_hash(saved)
        metadata = DraftSyncMetadata(
            reference=reference,
            state=DraftSyncState.SYNCHRONIZED,
            local_content_hash=saved_hash,
            base_content_hash=saved_hash,
            server_content_hash=remote_hash,
            server_id=snapshot.server_id,
            server_change_key=snapshot.server_change_key,
            last_synced_at=utc_now(),
            server_modified_at=_aware_utc(snapshot.modified_at),
        )
        self._store(metadata, previous=current.state)
        return saved

    def observe_server(self, reference: str, snapshot: ServerDraftSnapshot) -> DraftSyncMetadata:
        """Merge a fetched server snapshot or persist a three-way conflict."""

        self._validate_snapshot(snapshot, reference)
        local = self.drafts.get(reference)
        current = self.metadata(reference)
        remote = _copy_draft_with_reference(snapshot.draft, reference)
        remote.save_sent_copy = local.save_sent_copy
        local_hash = draft_content_hash(local)
        remote_hash = draft_content_hash(remote)
        base_hash = current.base_content_hash
        if local_hash == remote_hash:
            merged = _copy_draft_with_reference(local, reference)
            merged.server_id = snapshot.server_id
            merged.server_change_key = snapshot.server_change_key
            merged.location = DraftLocation.SYNCHRONIZED
            saved = self.drafts.save(merged, expected_revision=local.revision)
            updated = DraftSyncMetadata(
                reference=reference,
                state=DraftSyncState.SYNCHRONIZED,
                local_content_hash=draft_content_hash(saved),
                base_content_hash=remote_hash,
                server_content_hash=remote_hash,
                server_id=snapshot.server_id,
                server_change_key=snapshot.server_change_key,
                last_synced_at=utc_now(),
                server_modified_at=_aware_utc(snapshot.modified_at),
            )
            self._store(updated, previous=current.state)
            return updated
        if base_hash is None:
            return self._mark_conflict(
                reference,
                snapshot,
                reason="No common synchronization base exists for differing draft copies.",
            )
        local_changed = local_hash != base_hash
        server_changed = remote_hash != base_hash
        if local_changed and server_changed:
            return self._mark_conflict(
                reference,
                snapshot,
                reason="Both the local and Exchange drafts changed since the last sync.",
            )
        if server_changed:
            remote.server_id = snapshot.server_id
            remote.server_change_key = snapshot.server_change_key
            remote.location = DraftLocation.SYNCHRONIZED
            remote.revision = local.revision
            saved = self.drafts.save(remote, expected_revision=local.revision)
            updated = DraftSyncMetadata(
                reference=reference,
                state=DraftSyncState.SYNCHRONIZED,
                local_content_hash=draft_content_hash(saved),
                base_content_hash=remote_hash,
                server_content_hash=remote_hash,
                server_id=snapshot.server_id,
                server_change_key=snapshot.server_change_key,
                last_synced_at=utc_now(),
                server_modified_at=_aware_utc(snapshot.modified_at),
            )
            self._store(updated, previous=current.state)
            return updated
        updated = replace(
            current,
            state=DraftSyncState.LOCAL_DIRTY,
            local_content_hash=local_hash,
            server_content_hash=remote_hash,
            server_id=snapshot.server_id,
            server_change_key=snapshot.server_change_key,
            server_modified_at=_aware_utc(snapshot.modified_at),
        )
        self._store(updated, previous=current.state)
        return updated

    def _mark_conflict(
        self,
        reference: str,
        snapshot: ServerDraftSnapshot,
        *,
        reason: str,
    ) -> DraftSyncMetadata:
        local = self.drafts.get(reference)
        current = self.metadata(reference)
        remote = _copy_draft_with_reference(snapshot.draft, reference)
        remote.save_sent_copy = local.save_sent_copy
        conflicted = _copy_draft_with_reference(local, reference)
        conflicted.location = DraftLocation.CONFLICTED
        conflicted.server_id = snapshot.server_id
        conflicted.server_change_key = snapshot.server_change_key
        saved = self.drafts.save(conflicted, expected_revision=local.revision)
        metadata = DraftSyncMetadata(
            reference=reference,
            state=DraftSyncState.CONFLICTED,
            local_content_hash=draft_content_hash(saved),
            base_content_hash=current.base_content_hash,
            server_content_hash=draft_content_hash(remote),
            server_id=snapshot.server_id,
            server_change_key=snapshot.server_change_key,
            last_synced_at=current.last_synced_at,
            server_modified_at=_aware_utc(snapshot.modified_at),
            conflict_reason=reason,
            conflict_detected_at=utc_now(),
            remote_draft=draft_to_dict(remote),
        )
        self._store(metadata, previous=current.state)
        return metadata

    def resolve_conflict(self, reference: str, *, use: str) -> DraftData:
        current = self.metadata(reference)
        if current.state is not DraftSyncState.CONFLICTED:
            raise ConflictError("The draft has no unresolved synchronization conflict.")
        local = self.drafts.get(reference)
        normalized = use.strip().casefold().replace("-", "_")
        if normalized in {"local", "keep_local"}:
            chosen = _copy_draft_with_reference(local, reference)
            chosen.location = DraftLocation.LOCAL
            saved = self.drafts.save(chosen, expected_revision=local.revision)
            metadata = replace(
                current,
                state=DraftSyncState.LOCAL_DIRTY,
                local_content_hash=draft_content_hash(saved),
                base_content_hash=current.server_content_hash,
                conflict_reason=None,
                conflict_detected_at=None,
                remote_draft=None,
            )
        elif normalized in {"server", "use_server"}:
            if current.remote_draft is None:
                raise LocalStateError("The conflicting server draft snapshot is unavailable.")
            chosen = draft_from_dict(current.remote_draft)
            chosen.save_sent_copy = local.save_sent_copy
            chosen.reference = reference
            chosen.server_id = current.server_id
            chosen.server_change_key = current.server_change_key
            chosen.location = DraftLocation.SYNCHRONIZED
            chosen.revision = local.revision
            saved = self.drafts.save(chosen, expected_revision=local.revision)
            chosen_hash = draft_content_hash(saved)
            metadata = replace(
                current,
                state=DraftSyncState.SYNCHRONIZED,
                local_content_hash=chosen_hash,
                base_content_hash=chosen_hash,
                server_content_hash=chosen_hash,
                last_synced_at=utc_now(),
                conflict_reason=None,
                conflict_detected_at=None,
                remote_draft=None,
            )
        else:
            raise ValueError("Conflict resolution must be 'local' or 'server'.")
        self._store(metadata, previous=current.state)
        return saved

    @staticmethod
    def _validate_snapshot(snapshot: ServerDraftSnapshot, reference: str) -> None:
        if not snapshot.server_id.strip():
            raise LocalStateError("The server draft response contains no Exchange item ID.")
        if snapshot.client_reference is not None and snapshot.client_reference != reference:
            raise ConflictError("The server draft response belongs to another local draft.")

    @staticmethod
    def _snapshot_from_gateway(draft: DraftData, reference: str) -> ServerDraftSnapshot:
        if draft.reference != reference:
            raise ConflictError("The server draft response changed the stable local reference.")
        if not draft.server_id:
            raise LocalStateError("The server draft response contains no Exchange item ID.")
        return ServerDraftSnapshot(
            draft=draft,
            server_id=draft.server_id,
            server_change_key=draft.server_change_key,
            client_reference=reference,
        )
