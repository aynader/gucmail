"""Provider-neutral mailbox backup, restore planning and offline archive access.

This module never imports ``exchangelib`` and never opens a network session.
Callers may stream synthetic or live-adapter-provided :class:`BackupItem`
instances into :class:`BackupService`; every other operation is filesystem-only.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import heapq
import json
import os
import tempfile
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from gucmail import __version__
from gucmail.backup_format import (
    INCOMPLETE_MANIFEST_FILENAME,
    MANIFEST_FILENAME,
    BackupManifest,
    ItemDescriptor,
    KeyMaterial,
    ObjectDescriptor,
    atomic_write_bytes,
    canonical_json_bytes,
    decrypt_object,
    encrypt_object,
    load_json,
    new_encryption_metadata,
    object_aad,
    object_relative_path,
    read_manifest,
    resolve_relative_path,
    sha256_bytes,
    unencrypted_metadata,
    utc_text,
    validate_backup_id,
    verify_manifest_integrity,
    write_manifest,
)
from gucmail.errors import (
    AuthenticationError,
    BackupIntegrityError,
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ErrorContext,
    ExitCode,
    GUCmailError,
    ItemNotFoundError,
    LocalStateError,
    MutationResultUnavailableError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.models import to_primitive
from gucmail.security import safe_filename, unique_download_path
from gucmail.services.restore_journal import RestoreJournal, RestoreTargetIntent

_MAX_OBJECT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CHAIN_DEPTH = 128
_LOCK_FILENAME = ".backup-writer.lock"
_PARTIAL_SUFFIX = ".partial"
_TRASH_DIRECTORY = ".trash"
_JSON_MEDIA_TYPE = "application/vnd.gucmail.backup-record+json"
_MAX_RESTORE_ATTACHMENT_BYTES = 100 * 1024 * 1024


class BackupKind(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    SELECTED_FOLDERS = "selected-folders"
    SELECTED_MESSAGES = "selected-messages"
    SEARCH_RESULTS = "search-results"
    CLUSTER = "cluster"
    CALENDAR = "calendar"
    CONTACTS = "contacts"
    TASKS = "tasks"
    RULES = "rules"
    USER_CONFIGURATION = "user-configuration"


@dataclass(frozen=True, slots=True)
class BackupSelection:
    folders: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    search: str | None = None
    cluster: str | None = None
    item_kinds: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "folders": list(self.folders),
            "messages": list(self.messages),
            "search": self.search,
            "cluster": self.cluster,
            "item_kinds": list(self.item_kinds),
        }

    @classmethod
    def from_value(cls, value: BackupSelection | Mapping[str, Any] | None) -> BackupSelection:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("Backup selection must be a mapping or BackupSelection.")

        def strings(name: str) -> tuple[str, ...]:
            raw = value.get(name, ())
            if isinstance(raw, str):
                return (raw,)
            if not isinstance(raw, Sequence) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"Backup selection {name} must contain strings.")
            return tuple(raw)

        return cls(
            folders=strings("folders"),
            messages=strings("messages"),
            search=str(value["search"]) if value.get("search") is not None else None,
            cluster=str(value["cluster"]) if value.get("cluster") is not None else None,
            item_kinds=strings("item_kinds"),
        )


@dataclass(frozen=True, slots=True)
class BackupOptions:
    compression: bool = True
    max_concurrency: int = 1
    checkpoint_every: int = 10

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 16:
            raise ValueError("Backup concurrency must be between 1 and 16.")
        if self.checkpoint_every < 1:
            raise ValueError("Backup checkpoint frequency must be positive.")


@dataclass(frozen=True, slots=True)
class BackupRequest:
    kind: BackupKind = BackupKind.FULL
    selection: BackupSelection = field(default_factory=BackupSelection)
    exclusions: tuple[str, ...] = ()
    sync_state: Mapping[str, Any] = field(default_factory=dict)
    parent_backup_id: str | None = None
    source_label: str = "mailbox"
    options: BackupOptions = field(default_factory=BackupOptions)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, BackupKind):
            object.__setattr__(self, "kind", BackupKind(self.kind))
        if not isinstance(self.selection, BackupSelection):
            object.__setattr__(self, "selection", BackupSelection.from_value(self.selection))
        if self.parent_backup_id is not None:
            validate_backup_id(self.parent_backup_id)
        if not self.source_label.strip():
            raise ValueError("Backup source label cannot be empty.")
        if (
            self.kind
            in {BackupKind.SELECTED_MESSAGES, BackupKind.SEARCH_RESULTS, BackupKind.CLUSTER}
            and not self.selection.messages
        ):
            raise ValueError("A selected backup requires resolved message references.")
        if (self.selection.search or self.selection.cluster) and not self.selection.messages:
            raise ValueError("Resolve search or cluster results to message references first.")


@dataclass(frozen=True, slots=True)
class BackupAttachment:
    original_filename: str
    data: bytes | bytearray | memoryview | Path = field(repr=False)
    content_type: str = "application/octet-stream"
    normalized_filename: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: BackupAttachment | Mapping[str, Any] | Path) -> BackupAttachment:
        if isinstance(value, cls):
            return value
        if isinstance(value, Path):
            return cls(value.name, value)
        if not isinstance(value, Mapping):
            raise TypeError("Backup attachments must be BackupAttachment, Path, or mapping values.")
        data = value.get("data", value.get("content", value.get("path")))
        if isinstance(data, str) and value.get("path") is not None:
            data = Path(data)
        if not isinstance(data, (bytes, bytearray, memoryview, Path)):
            raise ValueError("Backup attachment data is missing or unsupported.")
        original = value.get("original_filename", value.get("filename", value.get("name")))
        if not isinstance(original, str) or not original:
            original = data.name if isinstance(data, Path) else "attachment"
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("Backup attachment metadata must be a mapping.")
        normalized = value.get("normalized_filename")
        return cls(
            original_filename=original,
            data=data,
            content_type=str(value.get("content_type", "application/octet-stream")),
            normalized_filename=str(normalized) if normalized is not None else None,
            metadata=dict(metadata),
        )


@dataclass(frozen=True, slots=True)
class BackupItem:
    reference: str
    kind: str
    data: Any = field(repr=False)
    folder_path: str | None = None
    attachments: tuple[BackupAttachment, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.reference:
            raise ValueError("Backup items require a stable reference.")
        if not self.kind:
            raise ValueError("Backup items require a kind.")


@dataclass(frozen=True, slots=True)
class BackupEstimate:
    item_count: int
    attachment_count: int
    estimated_bytes: int
    unknown_size_count: int
    encrypted: bool
    note: str = (
        "Estimate uses available local metadata; server-side MIME and attachment sizes may vary."
    )


@dataclass(frozen=True, slots=True)
class BackupProgress:
    backup_id: str
    phase: str
    processed_items: int
    stored_objects: int
    logical_bytes: int
    stored_bytes: int
    current_reference: str | None = None


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_id: str
    path: Path
    kind: str
    state: str
    item_count: int
    object_count: int
    logical_bytes: int
    stored_bytes: int
    encrypted: bool
    parent_backup_id: str | None
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class BackupSummary:
    backup_id: str
    path: Path
    kind: str
    state: str
    created_at: str | None
    completed_at: str | None
    item_count: int
    object_count: int
    stored_bytes: int
    encrypted: bool
    parent_backup_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationReport:
    backup_id: str
    path: Path
    valid: bool
    complete: bool
    encrypted: bool
    mode: str
    checked_objects: int
    checked_bytes: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    verified_at: str = ""

    def raise_for_error(self) -> None:
        if not self.valid:
            raise BackupIntegrityError("; ".join(self.errors) or "Backup verification failed.")


@dataclass(frozen=True, slots=True)
class PrunePlan:
    candidates: tuple[str, ...]
    retained: tuple[str, ...]
    protected_ancestors: tuple[str, ...]
    potential_bytes: int
    repository_fingerprint: str
    confirmation_token: str
    policy: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PruneResult:
    plan: PrunePlan
    moved_to_trash: tuple[Path, ...]
    bytes_moved: int
    note: str = "Backups were moved to recoverable trash; disk space is not reclaimed until purge."


@dataclass(frozen=True, slots=True)
class RestorePlanEntry:
    item_key: str
    source_reference: str
    kind: str
    destination_folder: str
    action: str
    duplicate_reason: str | None = None
    warnings: tuple[str, ...] = ()
    recovery_token: str | None = None
    recovery_status: str | None = None
    recovery_operation_id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class RestorePlan:
    backup_id: str
    manifest_digest: str
    chain_digest: str
    destination_root: str
    entries: tuple[RestorePlanEntry, ...]
    create_count: int
    duplicate_count: int
    unsupported_count: int
    by_kind: Mapping[str, int]
    by_destination: Mapping[str, int]
    warnings: tuple[str, ...]
    confirmation_token: str
    requires_confirmation: bool = True
    server_mutations_performed: bool = False
    blocked_count: int = 0
    allow_completed_duplicates: bool = False


@dataclass(frozen=True, slots=True)
class RestoreItemResult:
    source_reference: str
    kind: str
    destination_folder: str
    succeeded: bool
    restored_reference: str | None = None
    error_type: str | None = None
    mutation_uncertain: bool = False
    recovery_write_failed: bool = False


@dataclass(frozen=True, slots=True)
class RestoreResult:
    plan: RestorePlan
    attempted_count: int
    restored_count: int
    failed_count: int
    duplicate_count: int
    unsupported_count: int
    stopped_early: bool
    results: tuple[RestoreItemResult, ...]
    server_mutations_performed: bool
    mutation_uncertain: bool = False
    cancelled: bool = False
    interrupted: bool = False
    operation_id: str | None = None
    journal_available: bool = False
    recovery_write_failed: bool = False
    blocked_count: int = 0


@dataclass(frozen=True, slots=True)
class OfflineItem:
    item_key: str
    reference: str
    kind: str
    folder_path: str | None
    data: Any = field(repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attachments: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    backup_id: str = ""
    source: str = "backup"
    offline: bool = True
    stale: bool = True


@dataclass(frozen=True, slots=True)
class OfflineSearchResult:
    item: OfflineItem
    score: float
    matched_text: str


class BackupCancelledError(GUCmailError):
    """A clean cancellation that retains a resumable checkpoint."""

    exit_code = ExitCode.INTERRUPTED

    def __init__(self, backup_id: str, resume_path: Path) -> None:
        super().__init__(
            f"Backup {backup_id} was cancelled safely and can be resumed from {resume_path}."
        )
        self.backup_id = backup_id
        self.resume_path = resume_path


class BackupReadCancelledError(GUCmailError):
    """A cancelled archive read is neither corruption nor a partial restore."""

    exit_code = ExitCode.INTERRUPTED

    def __init__(self) -> None:
        super().__init__(
            "Backup reading was cancelled. No mailbox changes were made by this read.",
            context=ErrorContext(retry_safe=True, mutation_uncertain=False),
        )


@runtime_checkable
class BackupSource(Protocol):
    """Minimal adapter boundary for a live, cached, search, or synthetic source."""

    def iter_backup_items(
        self,
        request: BackupRequest,
        completed_item_keys: frozenset[str],
    ) -> Iterable[BackupItem | Mapping[str, Any] | Any]: ...


@runtime_checkable
class BackupEstimateSource(Protocol):
    """Metadata-only estimate boundary.

    Implementations must not retrieve message bodies, MIME content, or attachment
    payloads.  Unknown attachment sizes are represented by ``unknown_size_count``.
    """

    def estimate_backup(self, request: BackupRequest) -> BackupEstimate | Mapping[str, Any]: ...


@runtime_checkable
class RestoreTarget(Protocol):
    """Intentionally narrow target interface for future plan execution adapters."""

    def restore_item(self, item: OfflineItem, destination_folder: str) -> str: ...


ProgressCallback = Callable[[BackupProgress], None]
CancellationCheck = Callable[[], bool] | Any


class BackupService:
    """Create immutable backups and open them without initializing Exchange."""

    def __init__(
        self,
        root: Path | str,
        state: Any | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.state = state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or self._default_id
        if self.root.exists() and self.root.is_symlink():
            raise BackupIntegrityError("Backup repository root cannot be a symbolic link.")
        self.root.mkdir(parents=True, exist_ok=True)

    def _default_id(self) -> str:
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid4().hex[:12]}"

    @staticmethod
    def item_key(kind: str, reference: str) -> str:
        return sha256_bytes(f"{kind}\0{reference}".encode())

    def estimate(
        self,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        request: BackupRequest | None = None,
        *,
        encrypted: bool = False,
    ) -> BackupEstimate:
        effective = request or BackupRequest()
        provider_estimate = getattr(source, "estimate_backup", None)
        if callable(provider_estimate):
            result = provider_estimate(effective)
            if isinstance(result, BackupEstimate):
                encryption_overhead = (
                    (result.item_count + result.attachment_count) * 28
                    if encrypted and not result.encrypted
                    else 0
                )
                return replace(
                    result,
                    estimated_bytes=result.estimated_bytes + encryption_overhead,
                    encrypted=encrypted,
                )
            if isinstance(result, Mapping):
                item_count = int(result.get("item_count", 0))
                attachment_count = int(result.get("attachment_count", 0))
                estimated_bytes = int(result.get("estimated_bytes", result.get("size_bytes", 0)))
                if encrypted:
                    estimated_bytes += (item_count + attachment_count) * 28
                return BackupEstimate(
                    item_count=item_count,
                    attachment_count=attachment_count,
                    estimated_bytes=estimated_bytes,
                    unknown_size_count=int(result.get("unknown_size_count", 0)),
                    encrypted=encrypted,
                )
            raise TypeError("Backup source estimate must be BackupEstimate or a mapping.")

        if isinstance(source, BackupSource):
            raise UnsupportedFeatureError(
                "This backup provider does not expose the metadata-only estimate contract. "
                "Refusing to enumerate backup content because that may retrieve bodies or "
                "attachment payloads."
            )

        item_count = 0
        attachment_count = 0
        estimated_bytes = 0
        unknown = 0
        for raw in self._iter_source(source, effective, frozenset()):
            item = self._coerce_item(raw)
            if self._excluded(item, effective.exclusions):
                continue
            item_count += 1
            estimated_bytes += self._estimate_value(item.data)
            estimated_bytes += self._estimate_value(item.metadata)
            for attachment in item.attachments:
                attachment_count += 1
                try:
                    estimated_bytes += self._blob_size(attachment.data)
                except OSError:
                    unknown += 1
        # AEAD tags, nonces, descriptors and canonical record wrappers.
        overhead = item_count * 512 + attachment_count * 256
        if encrypted:
            overhead += (item_count + attachment_count) * 28
        return BackupEstimate(
            item_count, attachment_count, estimated_bytes + overhead, unknown, encrypted
        )

    estimate_backup = estimate

    def create(
        self,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        request: BackupRequest | None = None,
        *,
        kind: BackupKind | str | None = None,
        selection: BackupSelection | Mapping[str, Any] | None = None,
        exclusions: Sequence[str] = (),
        sync_state: Mapping[str, Any] | None = None,
        parent_backup_id: str | None = None,
        backup_id: str | None = None,
        passphrase: str | bytes | None = None,
        compression: bool | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> BackupResult:
        if request is not None and any(
            value is not None
            for value in (kind, selection, sync_state, parent_backup_id, compression)
        ):
            raise ValueError("Pass either BackupRequest or individual backup options, not both.")
        effective = request or BackupRequest(
            kind=BackupKind(kind or BackupKind.FULL),
            selection=BackupSelection.from_value(selection),
            exclusions=tuple(exclusions),
            sync_state=dict(sync_state or {}),
            parent_backup_id=parent_backup_id,
            options=BackupOptions(compression=True if compression is None else compression),
        )
        effective = self._bind_source(effective, source)
        identifier = validate_backup_id(backup_id or self._id_factory())
        final_path = self.root / identifier
        staging_path = self.root / f"{identifier}{_PARTIAL_SUFFIX}"
        if final_path.exists() or staging_path.exists():
            raise ConflictError(f"Backup destination already exists: {identifier}")

        with self._writer_lock("create"):
            staging_path.mkdir(parents=False)
            if passphrase is None:
                encryption = unencrypted_metadata()
                keys = None
            else:
                encryption, keys = new_encryption_metadata(passphrase)
            created = utc_text(self._clock())
            manifest = BackupManifest(
                backup_id=identifier,
                application_version=__version__,
                kind=effective.kind.value,
                state="incomplete",
                created_at=created,
                completed_at=None,
                selection={**effective.selection.to_dict(), "source_label": effective.source_label},
                exclusions=effective.exclusions,
                encryption=encryption,
                compression="zlib" if effective.options.compression else "none",
                parent_backup_id=effective.parent_backup_id,
                sync_state=dict(effective.sync_state),
                resume={
                    "resumable": True,
                    "processed_item_keys": [],
                    "checkpoint_every": effective.options.checkpoint_every,
                    "options_fingerprint": self._request_fingerprint(effective),
                },
                objects={},
                items=(),
                statistics=self._statistics({}, ()),
                warnings=(
                    "Exchange does not provide a globally atomic mailbox snapshot; "
                    "items may change while a live adapter is collecting them.",
                    *(
                        (
                            "This incremental uses a complete source rescan compared with its "
                            "parent. It emits changed/new records and deletion tombstones, but "
                            "does not claim native Exchange change tracking.",
                        )
                        if effective.sync_state.get("strategy") == "full_snapshot_parent_diff"
                        else ()
                    ),
                ),
                integrity={},
            )
            manifest = write_manifest(
                staging_path / INCOMPLETE_MANIFEST_FILENAME,
                manifest,
                manifest_key=keys.manifest_key if keys else None,
            )
            return self._continue_create(
                source,
                effective,
                manifest,
                keys,
                staging_path,
                final_path,
                progress=progress,
                cancelled=cancelled,
            )

    create_backup = create

    def incremental_request(
        self,
        parent: str | Path,
        *,
        passphrase: str | bytes | None = None,
        sync_state: Mapping[str, Any] | None = None,
        selection: BackupSelection | Mapping[str, Any] | None = None,
        exclusions: Sequence[str] | None = None,
        compression: bool = True,
    ) -> BackupRequest:
        """Plan a delta within the parent's exact scope, without reading mailbox data.

        Search and cluster selections store a frozen set of resolved references. They
        are not re-evaluated during a delta. Changing scope requires a new base archive.
        """
        parent_path, manifest, _keys = self._open_manifest(parent, passphrase=passphrase)
        if manifest.state != "complete":
            raise BackupIntegrityError("Incremental backup parent is not complete.")
        if parent_path.parent.resolve() != self.root.resolve():
            raise BackupIntegrityError("Incremental parent must belong to this backup repository.")
        inherited = BackupSelection.from_value(manifest.selection)
        chosen = inherited if selection is None else BackupSelection.from_value(selection)
        chosen_exclusions = manifest.exclusions if exclusions is None else tuple(exclusions)
        if _selection_scope(chosen) != _selection_scope(inherited) or set(chosen_exclusions) != set(
            manifest.exclusions
        ):
            raise BackupIntegrityError(
                "Incremental scope differs from its parent. Keep the parent's folders, "
                "references, content types and exclusions, or create a new base backup."
            )
        archive = self.open_offline(parent, passphrase=passphrase, verify=False)
        for _path, ancestor, _keys in archive._chain():
            if _selection_scope(BackupSelection.from_value(ancestor.selection)) != (
                _selection_scope(inherited)
            ) or set(ancestor.exclusions) != set(manifest.exclusions):
                raise BackupIntegrityError(
                    "This older archive chain changed scope between backups. It remains "
                    "available offline; create a new base before taking more incrementals."
                )
        return BackupRequest(
            kind=BackupKind.INCREMENTAL,
            selection=chosen,
            exclusions=chosen_exclusions,
            source_label=str(manifest.selection.get("source_label", "mailbox")),
            parent_backup_id=manifest.backup_id,
            sync_state={
                **dict(sync_state or {}),
                "source_identity": manifest.sync_state.get("source_identity"),
                "strategy": "full_snapshot_parent_diff",
                "native_change_tracking": False,
                "snapshot_complete": True,
                "deletion_tracking": "parent_tombstones",
                "selection_mode": "fixed_references" if chosen.messages else "folder_scope",
            },
            options=BackupOptions(compression=compression),
        )

    @staticmethod
    def _bind_source(
        request: BackupRequest,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
    ) -> BackupRequest:
        identity = getattr(source, "backup_source_identity", None)
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise BackupIntegrityError("Backup source identity must be a nonempty string.")
        expected = request.sync_state.get("source_identity")
        if expected and expected != identity:
            raise BackupIntegrityError(
                "The backup belongs to a different mailbox source, or its source identity "
                "cannot be verified. Select the original account or create a new base backup."
            )
        if identity is None:
            return request
        return replace(request, sync_state={**request.sync_state, "source_identity": identity})

    def incremental(
        self,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        parent: str | Path,
        *,
        sync_state: Mapping[str, Any],
        passphrase: str | bytes | None = None,
        backup_id: str | None = None,
        selection: BackupSelection | Mapping[str, Any] | None = None,
        exclusions: Sequence[str] | None = None,
        compression: bool = True,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> BackupResult:
        request = self.incremental_request(
            parent,
            passphrase=passphrase,
            sync_state=sync_state,
            selection=selection,
            exclusions=exclusions,
            compression=compression,
        )
        request = self._bind_source(request, source)
        parent_archive = self.open_offline(parent, passphrase=passphrase, verify=True)
        return self.create(
            _SnapshotDiffSource(self, source, parent_archive),
            request,
            backup_id=backup_id,
            passphrase=passphrase,
            progress=progress,
            cancelled=cancelled,
        )

    create_incremental = incremental

    def resume(
        self,
        identifier: str | Path,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        *,
        passphrase: str | bytes | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> BackupResult:
        staging_path = self._resolve_partial(identifier)
        manifest, keys = self._read_manifest_auto(
            staging_path / INCOMPLETE_MANIFEST_FILENAME, passphrase
        )
        if manifest.state != "incomplete" or not manifest.resume.get("resumable"):
            raise BackupIntegrityError("Backup checkpoint is not resumable.")
        selection_value = manifest.selection
        request = BackupRequest(
            kind=BackupKind(manifest.kind),
            selection=BackupSelection.from_value(selection_value),
            exclusions=manifest.exclusions,
            sync_state=manifest.sync_state,
            parent_backup_id=manifest.parent_backup_id,
            source_label=str(selection_value.get("source_label", "mailbox")),
            options=BackupOptions(
                compression=manifest.compression == "zlib",
                checkpoint_every=int(manifest.resume.get("checkpoint_every", 10)),
            ),
        )
        if manifest.resume.get("options_fingerprint") != self._request_fingerprint(request):
            raise BackupIntegrityError("Backup resume options no longer match the checkpoint.")
        # Do not resume against another account, even if item references happen to overlap.
        self._bind_source(request, source)
        effective_source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any] = source
        if (
            request.kind is BackupKind.INCREMENTAL
            and request.sync_state.get("strategy") == "full_snapshot_parent_diff"
        ):
            if request.parent_backup_id is None:
                raise BackupIntegrityError("Incremental checkpoint has no parent backup.")
            parent_archive = self.open_offline(
                request.parent_backup_id,
                passphrase=passphrase,
                verify=True,
            )
            effective_source = _SnapshotDiffSource(self, source, parent_archive)
        final_path = self.root / manifest.backup_id
        if final_path.exists():
            raise ConflictError(f"Completed backup already exists: {manifest.backup_id}")
        with self._writer_lock("resume"):
            return self._continue_create(
                effective_source,
                request,
                manifest,
                keys,
                staging_path,
                final_path,
                progress=progress,
                cancelled=cancelled,
            )

    resume_backup = resume

    def _continue_create(
        self,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        request: BackupRequest,
        manifest: BackupManifest,
        keys: KeyMaterial | None,
        staging_path: Path,
        final_path: Path,
        *,
        progress: ProgressCallback | None,
        cancelled: CancellationCheck | None,
    ) -> BackupResult:
        objects = dict(manifest.objects)
        item_descriptors = list(manifest.items)
        completed = {str(value) for value in manifest.resume.get("processed_item_keys", [])}
        known_items = {item.item_key for item in item_descriptors}
        since_checkpoint = 0

        def checkpoint() -> None:
            nonlocal manifest, since_checkpoint
            manifest = replace(
                manifest,
                objects=dict(objects),
                items=tuple(item_descriptors),
                resume={
                    **manifest.resume,
                    "resumable": True,
                    "processed_item_keys": sorted(completed),
                    "updated_at": utc_text(self._clock()),
                },
                statistics=self._statistics(objects, item_descriptors),
                integrity={},
            )
            manifest = write_manifest(
                staging_path / INCOMPLETE_MANIFEST_FILENAME,
                manifest,
                manifest_key=keys.manifest_key if keys else None,
            )
            since_checkpoint = 0

        try:
            for raw in self._iter_source(source, request, frozenset(completed)):
                if self._is_cancelled(cancelled):
                    checkpoint()
                    raise BackupCancelledError(manifest.backup_id, staging_path)
                item = self._coerce_item(raw)
                if self._excluded(item, request.exclusions):
                    continue
                key = self.item_key(item.kind, item.reference)
                if key in completed:
                    continue
                if key in known_items:
                    raise ConflictError(
                        f"Backup source returned duplicate item reference {item.reference!r}."
                    )
                descriptor = self._store_item(
                    item,
                    key,
                    staging_path,
                    manifest.backup_id,
                    request.options,
                    keys,
                    objects,
                )
                item_descriptors.append(descriptor)
                known_items.add(key)
                completed.add(key)
                since_checkpoint += 1
                statistics = self._statistics(objects, item_descriptors)
                if progress:
                    progress(
                        BackupProgress(
                            backup_id=manifest.backup_id,
                            phase="collecting",
                            processed_items=statistics["item_count"],
                            stored_objects=statistics["object_count"],
                            logical_bytes=statistics["logical_bytes"],
                            stored_bytes=statistics["stored_bytes"],
                            current_reference=item.reference,
                        )
                    )
                if since_checkpoint >= request.options.checkpoint_every:
                    checkpoint()
            if self._is_cancelled(cancelled):
                checkpoint()
                raise BackupCancelledError(manifest.backup_id, staging_path)
        except BaseException:
            if since_checkpoint:
                with contextlib.suppress(Exception):
                    checkpoint()
            raise

        completed_manifest = replace(
            manifest,
            state="complete",
            completed_at=utc_text(self._clock()),
            objects=dict(objects),
            items=tuple(item_descriptors),
            resume={
                "resumable": False,
                "processed_items": len(item_descriptors),
                "completed_at": utc_text(self._clock()),
            },
            statistics=self._statistics(objects, item_descriptors),
            integrity={},
        )
        completed_manifest = write_manifest(
            staging_path / MANIFEST_FILENAME,
            completed_manifest,
            manifest_key=keys.manifest_key if keys else None,
        )
        # Read-back validation happens before publishing the directory.
        reloaded = BackupManifest.from_dict(load_json(staging_path / MANIFEST_FILENAME))
        verify_manifest_integrity(
            reloaded,
            manifest_key=keys.manifest_key if keys else None,
        )
        with contextlib.suppress(FileNotFoundError):
            (staging_path / INCOMPLETE_MANIFEST_FILENAME).unlink()
        os.replace(staging_path, final_path)
        final_statistics = completed_manifest.statistics
        if progress:
            progress(
                BackupProgress(
                    backup_id=completed_manifest.backup_id,
                    phase="complete",
                    processed_items=int(final_statistics["item_count"]),
                    stored_objects=int(final_statistics["object_count"]),
                    logical_bytes=int(final_statistics["logical_bytes"]),
                    stored_bytes=int(final_statistics["stored_bytes"]),
                )
            )
        self._record_state(completed_manifest, final_path)
        return self._result(final_path, completed_manifest)

    def _store_item(
        self,
        item: BackupItem,
        item_key: str,
        root: Path,
        backup_id: str,
        options: BackupOptions,
        keys: KeyMaterial | None,
        objects: dict[str, ObjectDescriptor],
    ) -> ItemDescriptor:
        dependencies: list[str] = []

        def externalize(value: Any) -> Any:
            if isinstance(value, (bytes, bytearray, memoryview)):
                payload = bytes(value)
                descriptor = self._store_object(
                    payload,
                    kind="binary",
                    media_type="application/octet-stream",
                    root=root,
                    backup_id=backup_id,
                    options=options,
                    keys=keys,
                    objects=objects,
                )
                dependencies.append(descriptor.content_id)
                return {
                    "$gucmail_object": descriptor.content_id,
                    "media_type": descriptor.media_type,
                    "size": descriptor.logical_size,
                }
            if is_dataclass(value) and not isinstance(value, type):
                return externalize(to_primitive(value))
            if isinstance(value, Enum):
                return externalize(value.value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): externalize(child) for key, child in value.items()}
            if isinstance(value, Sequence) and not isinstance(value, str):
                return [externalize(child) for child in value]
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            raise TypeError(f"Unsupported backup value type: {type(value).__name__}")

        attachments: list[dict[str, Any]] = []
        for attachment in item.attachments:
            data = self._read_blob(attachment.data)
            descriptor = self._store_object(
                data,
                kind="attachment",
                media_type=attachment.content_type or "application/octet-stream",
                root=root,
                backup_id=backup_id,
                options=options,
                keys=keys,
                objects=objects,
            )
            dependencies.append(descriptor.content_id)
            normalized = attachment.normalized_filename or safe_filename(
                attachment.original_filename
            )
            attachments.append(
                {
                    "original_filename": attachment.original_filename,
                    "normalized_filename": normalized,
                    "content_type": attachment.content_type,
                    "size": descriptor.logical_size,
                    "sha256": descriptor.sha256,
                    "object": descriptor.content_id,
                    "metadata": externalize(attachment.metadata),
                }
            )

        record = {
            "record_schema": 1,
            "item_key": item_key,
            "reference": item.reference,
            "kind": item.kind,
            "folder_path": item.folder_path,
            "deleted": item.deleted,
            "metadata": externalize(item.metadata),
            "data": externalize(item.data),
            "attachments": attachments,
        }
        record_bytes = canonical_json_bytes(record)
        record_descriptor = self._store_object(
            record_bytes,
            kind="record",
            media_type=_JSON_MEDIA_TYPE,
            root=root,
            backup_id=backup_id,
            options=options,
            keys=keys,
            objects=objects,
        )
        return ItemDescriptor(
            item_key=item_key,
            kind=item.kind,
            record_object=record_descriptor.content_id,
            folder_path=item.folder_path,
            deleted=item.deleted,
            dependency_objects=tuple(dict.fromkeys(dependencies)),
        )

    def _store_object(
        self,
        logical: bytes,
        *,
        kind: str,
        media_type: str,
        root: Path,
        backup_id: str,
        options: BackupOptions,
        keys: KeyMaterial | None,
        objects: dict[str, ObjectDescriptor],
    ) -> ObjectDescriptor:
        logical_digest = sha256_bytes(logical)
        content_id = logical_digest
        existing = objects.get(content_id)
        if existing is not None:
            path = resolve_relative_path(root, existing.path)
            if (
                not path.is_file()
                or path.is_symlink()
                or self._sha256_path(path) != existing.stored_sha256
            ):
                raise BackupIntegrityError(
                    f"Previously stored backup object is missing or damaged: {content_id}"
                )
            return existing

        stored = logical
        compression = "none"
        if options.compression and logical:
            compressed = zlib.compress(logical, level=6)
            if len(compressed) < len(logical):
                stored = compressed
                compression = "zlib"
        encryption = "none"
        nonce: str | None = None
        if keys is not None:
            stored, nonce = encrypt_object(
                stored,
                keys.encryption_key,
                aad=object_aad(backup_id, content_id),
            )
            encryption = "AES-256-GCM"
        descriptor = ObjectDescriptor(
            content_id=content_id,
            sha256=logical_digest,
            stored_sha256=sha256_bytes(stored),
            path=object_relative_path(content_id),
            kind=kind,
            media_type=media_type,
            logical_size=len(logical),
            stored_size=len(stored),
            compression=compression,
            encryption=encryption,
            nonce=nonce,
        )
        path = resolve_relative_path(root, descriptor.path)
        if path.exists():
            if path.is_symlink() or self._sha256_path(path) != descriptor.stored_sha256:
                raise BackupIntegrityError(f"Conflicting content-addressed object: {content_id}")
        else:
            atomic_write_bytes(path, stored)
        objects[content_id] = descriptor
        return descriptor

    def inspect(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None = None,
        allow_incomplete: bool = False,
    ) -> BackupManifest:
        _, manifest, _ = self._open_manifest(
            identifier, passphrase=passphrase, allow_incomplete=allow_incomplete
        )
        return manifest

    inspect_backup = inspect

    def list(
        self,
        *,
        include_incomplete: bool = False,
        limit: int | None = None,
        inspect_corrupt_sizes: bool = True,
    ) -> tuple[BackupSummary, ...]:
        if limit is not None and not 1 <= limit <= 10_000:
            raise ValueError("backup list limit must be between 1 and 10000")

        def candidates() -> Iterator[Path]:
            for path in self.root.iterdir():
                try:
                    if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                        continue
                    incomplete = path.name.endswith(_PARTIAL_SUFFIX)
                    if incomplete and not include_incomplete:
                        continue
                    manifest_path = path / (
                        INCOMPLETE_MANIFEST_FILENAME if incomplete else MANIFEST_FILENAME
                    )
                    if manifest_path.is_file() and not manifest_path.is_symlink():
                        yield path
                except OSError:
                    continue

        def path_order(path: Path) -> tuple[int, str]:
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            return modified, path.name.casefold()

        paths: Iterable[Path]
        if limit is None:
            paths = candidates()
        else:
            paths = heapq.nlargest(limit, candidates(), key=path_order)
        summaries: list[BackupSummary] = []
        for path in paths:
            incomplete = path.name.endswith(_PARTIAL_SUFFIX)
            manifest_path = path / (
                INCOMPLETE_MANIFEST_FILENAME if incomplete else MANIFEST_FILENAME
            )
            try:
                manifest = BackupManifest.from_dict(load_json(manifest_path))
                statistics = manifest.statistics
                summaries.append(
                    BackupSummary(
                        backup_id=manifest.backup_id,
                        path=path,
                        kind=manifest.kind,
                        state=manifest.state,
                        created_at=manifest.created_at,
                        completed_at=manifest.completed_at,
                        item_count=int(statistics.get("item_count", 0)),
                        object_count=int(statistics.get("object_count", 0)),
                        stored_bytes=int(statistics.get("stored_bytes", 0)),
                        encrypted=manifest.encrypted,
                        parent_backup_id=manifest.parent_backup_id,
                    )
                )
            except Exception as exc:
                summaries.append(
                    BackupSummary(
                        backup_id=path.name.removesuffix(_PARTIAL_SUFFIX),
                        path=path,
                        kind="unknown",
                        state="corrupt",
                        created_at=None,
                        completed_at=None,
                        item_count=0,
                        object_count=0,
                        stored_bytes=(self._directory_size(path) if inspect_corrupt_sizes else 0),
                        encrypted=False,
                        error=str(exc),
                    )
                )
        return tuple(sorted(summaries, key=lambda item: item.created_at or "", reverse=True))

    list_backups = list

    def verify(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None = None,
        full: bool = True,
        allow_incomplete: bool = False,
        detect_extra: bool = True,
        cancelled: CancellationCheck | None = None,
    ) -> VerificationReport:
        self._check_read_cancelled(cancelled)
        path = self._resolve_backup(identifier, allow_incomplete=allow_incomplete)
        checked_objects = 0
        checked_bytes = 0
        errors: list[str] = []
        warnings: list[str] = []
        manifest: BackupManifest | None = None
        try:
            manifest_path = path / (
                INCOMPLETE_MANIFEST_FILENAME
                if path.name.endswith(_PARTIAL_SUFFIX)
                else MANIFEST_FILENAME
            )
            manifest, keys = self._read_manifest_auto(manifest_path, passphrase)
            self._check_read_cancelled(cancelled)
            if manifest.state != "complete":
                errors.append("Backup is incomplete and is not safe to restore.")
            record_bindings = {}
            for item in manifest.items:
                self._check_read_cancelled(cancelled)
                record_bindings[item.record_object] = item.item_key
            if len(record_bindings) != len(manifest.items):
                errors.append("Multiple manifest items share one canonical record object.")
            expected_paths: set[Path] = set()
            for content_id, descriptor in manifest.objects.items():
                self._check_read_cancelled(cancelled)
                try:
                    object_path = resolve_relative_path(path, descriptor.path)
                    expected_paths.add(object_path.resolve())
                    if object_path.is_symlink():
                        raise BackupIntegrityError("Object is a symbolic link.")
                    stat = object_path.stat()
                    if stat.st_size != descriptor.stored_size:
                        raise BackupIntegrityError(
                            f"Stored size mismatch ({stat.st_size} != {descriptor.stored_size})."
                        )
                    stored_digest = self._sha256_path(object_path, cancelled=cancelled)
                    checked_bytes += stat.st_size
                    if stored_digest != descriptor.stored_sha256:
                        raise BackupIntegrityError("Stored SHA-256 mismatch.")
                    if full:
                        logical = self._read_logical_object(
                            path, manifest, descriptor, keys, cancelled=cancelled,
                        )
                        if descriptor.kind == "record":
                            expected_item_key = record_bindings.get(descriptor.content_id)
                            if expected_item_key is None:
                                raise BackupIntegrityError(
                                    "Canonical record is not bound to a manifest item."
                                )
                            self._validate_record(
                                logical,
                                descriptor,
                                expected_item_key=expected_item_key,
                            )
                    checked_objects += 1
                except (OSError, BackupIntegrityError) as exc:
                    errors.append(f"Object {content_id}: {exc}")
            if detect_extra:
                object_root = path / "objects"
                if object_root.exists():
                    for candidate in object_root.rglob("*.gmo"):
                        self._check_read_cancelled(cancelled)
                        if candidate.is_symlink():
                            errors.append(
                                f"Unexpected symbolic link: {candidate.relative_to(path)}"
                            )
                        elif candidate.resolve() not in expected_paths:
                            warnings.append(f"Unreferenced object: {candidate.relative_to(path)}")
            actual = self._statistics(manifest.objects, manifest.items, cancelled=cancelled)
            self._check_read_cancelled(cancelled)
            for key in ("item_count", "object_count", "logical_bytes", "stored_bytes"):
                if int(manifest.statistics.get(key, -1)) != actual[key]:
                    errors.append(f"Manifest statistic {key} does not match referenced data.")
        except BackupReadCancelledError:
            raise
        except Exception as exc:
            errors.append(str(exc))
        self._check_read_cancelled(cancelled)
        backup_id = manifest.backup_id if manifest else path.name.removesuffix(_PARTIAL_SUFFIX)
        return VerificationReport(
            backup_id=backup_id,
            path=path,
            valid=not errors,
            complete=bool(manifest and manifest.state == "complete"),
            encrypted=bool(manifest and manifest.encrypted),
            mode="full" if full else "quick",
            checked_objects=checked_objects,
            checked_bytes=checked_bytes,
            errors=tuple(errors),
            warnings=tuple(warnings),
            verified_at=utc_text(self._clock()),
        )

    verify_backup = verify

    def open_offline(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None = None,
        verify: bool = True,
        cancelled: CancellationCheck | None = None,
    ) -> OfflineArchive:
        self._check_read_cancelled(cancelled)
        path, manifest, keys = self._open_manifest(identifier, passphrase=passphrase)
        self._check_read_cancelled(cancelled)
        if verify:
            report = self.verify(path, passphrase=passphrase, full=True, cancelled=cancelled)
            report.raise_for_error()
        self._check_read_cancelled(cancelled)
        return OfflineArchive(
            self, path, manifest, keys, passphrase=passphrase, cancelled=cancelled,
        )

    open_archive = open_offline

    def export(
        self,
        identifier: str | Path,
        destination: Path | str,
        *,
        passphrase: str | bytes | None = None,
        overwrite: bool = False,
    ) -> Path:
        archive = self.open_offline(identifier, passphrase=passphrase, verify=True)
        target = Path(destination)
        if target.exists() and not overwrite:
            raise ConflictError(f"Export destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".partial", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
                for path, manifest, _ in archive._chain():
                    prefix = safe_filename(manifest.backup_id)
                    bundle.write(path / MANIFEST_FILENAME, f"{prefix}/{MANIFEST_FILENAME}")
                    for object_descriptor in manifest.objects.values():
                        object_path = resolve_relative_path(path, object_descriptor.path)
                        bundle.write(object_path, f"{prefix}/{object_descriptor.path}")
                metadata = {
                    "format": "gucmail-backup-export",
                    "format_version": 1,
                    "head_backup_id": archive.manifest.backup_id,
                    "included_backups": [manifest.backup_id for _, manifest, _ in archive._chain()],
                    "encrypted": archive.manifest.encrypted,
                    "privacy_notice": archive.manifest.privacy_notice,
                }
                bundle.writestr("export.json", canonical_json_bytes(metadata) + b"\n")
            # Windows requires a writable descriptor for fsync even though ZipFile has
            # already closed and flushed the archive.
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            return target
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink()

    export_backup = export

    def prune(
        self,
        *,
        keep_last: int = 3,
        older_than: datetime | None = None,
        apply: bool = False,
        dry_run: bool | None = None,
        confirmation_token: str | None = None,
    ) -> PrunePlan | PruneResult:
        if keep_last < 1:
            raise ValueError("Retention must keep at least one completed backup.")
        if dry_run is not None:
            apply = not dry_run
        repository = self._prune_repository()
        complete = [item for item in repository if item.state == "complete"]
        complete.sort(key=lambda item: item.created_at or "", reverse=True)
        retained_ids = {item.backup_id for item in complete[:keep_last]}
        cutoff = utc_text(older_than) if older_than is not None else None
        candidates = {
            item.backup_id
            for index, item in enumerate(complete)
            if index >= keep_last and (cutoff is None or (item.created_at or "") < cutoff)
        }
        parent_by_id = {item.backup_id: item.parent_backup_id for item in complete}
        protected: set[str] = set()
        # An interrupted incremental still needs its entire parent chain to resume.
        incomplete = [item for item in repository if item.state == "incomplete"]
        roots = [*retained_ids, *(item.backup_id for item in incomplete)]
        parent_by_id.update({item.backup_id: item.parent_backup_id for item in incomplete})
        for backup_id in roots:
            parent = parent_by_id.get(backup_id)
            depth = 0
            while parent is not None:
                depth += 1
                if depth > _MAX_CHAIN_DEPTH:
                    raise BackupIntegrityError("Backup parent chain is excessively deep.")
                if parent in protected:
                    break
                protected.add(parent)
                parent = parent_by_id.get(parent)
        candidates -= protected
        retained_ids.update(protected)
        ordered_candidates = tuple(
            item.backup_id for item in complete if item.backup_id in candidates
        )
        ordered_retained = tuple(
            item.backup_id for item in complete if item.backup_id not in candidates
        )
        fingerprint = self._repository_fingerprint(repository)
        core = {
            "candidates": list(ordered_candidates),
            "retained": list(ordered_retained),
            "fingerprint": fingerprint,
            "keep_last": keep_last,
            "older_than": cutoff,
        }
        token = sha256_bytes(canonical_json_bytes(core))[:16]
        potential = sum(self._directory_size(self.root / item) for item in ordered_candidates)
        plan = PrunePlan(
            candidates=ordered_candidates,
            retained=ordered_retained,
            protected_ancestors=tuple(sorted(protected)),
            potential_bytes=potential,
            repository_fingerprint=fingerprint,
            confirmation_token=token,
            policy={"keep_last": keep_last, "older_than": cutoff},
        )
        if not apply:
            return plan
        if confirmation_token != plan.confirmation_token:
            raise ConfirmationRequiredError(
                "Pruning requires the confirmation token from the current prune plan."
            )
        return self.execute_prune(plan, confirmation_token=confirmation_token)

    def execute_prune(self, plan: PrunePlan, *, confirmation_token: str) -> PruneResult:
        if confirmation_token != plan.confirmation_token:
            raise ConfirmationRequiredError("Prune confirmation token does not match the plan.")
        moved: list[Path] = []
        bytes_moved = 0
        trash_root = (
            self.root
            / _TRASH_DIRECTORY
            / (self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
        )
        with self._writer_lock("prune"):
            # Check after acquiring the same lock used by create/resume. Otherwise
            # a newly completed or interrupted incremental can acquire a parent
            # between the check and the move.
            current = self._prune_repository()
            if self._repository_fingerprint(current) != plan.repository_fingerprint:
                raise ConflictError("Backup repository changed after the prune plan was created.")
            trash_root.mkdir(parents=True, exist_ok=False)
            try:
                for backup_id in plan.candidates:
                    source = self._resolve_backup(backup_id)
                    if source.parent.resolve() != self.root.resolve():
                        raise BackupIntegrityError("Prune target escapes the backup repository.")
                    # The current prune plan is bound to the manifest-derived
                    # repository fingerprint. Encrypted manifests cannot be
                    # authenticated here without retaining a passphrase, so
                    # pruning remains a recoverable move to repository trash.
                    BackupManifest.from_dict(load_json(source / MANIFEST_FILENAME))
                    size = self._directory_size(source)
                    target = trash_root / backup_id
                    os.replace(source, target)
                    moved.append(target)
                    bytes_moved += size
            except BaseException:
                # Restore already-moved directories before surfacing a failed prune.
                for target in reversed(moved):
                    with contextlib.suppress(OSError):
                        os.replace(target, self.root / target.name)
                raise
        return PruneResult(plan, tuple(moved), bytes_moved)

    def _prune_repository(self) -> tuple[BackupSummary, ...]:
        """Require readable checkpoints before deciding which ancestors are safe to move."""
        summaries = self.list(include_incomplete=True)
        if any(item.state not in {"complete", "incomplete"} for item in summaries):
            raise BackupIntegrityError(
                "Cannot safely prune while a backup manifest is corrupt or has an unknown state."
            )
        known_paths = {item.path for item in summaries}
        if any(
            path.is_dir()
            and not path.is_symlink()
            and path.name.endswith(_PARTIAL_SUFFIX)
            and path not in known_paths
            for path in self.root.iterdir()
        ):
            raise BackupIntegrityError(
                "Cannot safely prune while an incomplete backup has no readable manifest."
            )
        return summaries

    def plan_restore(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None = None,
        destination_root: str = "Restored by GUC Mail CLI",
        selected_references: Collection[str] | None = None,
        existing_references: Collection[str] = (),
        allow_completed_duplicates: bool = False,
        cancelled: CancellationCheck | None = None,
    ) -> RestorePlan:
        self._check_read_cancelled(cancelled)
        archive = self.open_offline(
            identifier, passphrase=passphrase, verify=True, cancelled=cancelled,
        )
        archive._pin_chain()
        chain_digest = archive._chain_digest()
        journal = RestoreJournal(self.state) if self.state is not None else None
        selected = set(selected_references) if selected_references is not None else None
        existing = set(existing_references)
        entries: list[RestorePlanEntry] = []
        warnings: set[str] = {
            "Restore cannot recreate historical EWS item IDs or change keys exactly.",
            "The plan performs no server mutations and must be explicitly confirmed before use.",
            "Duplicate protection covers this profile's recorded operations. "
            "Older or other-profile restores may be untracked.",
        }
        supported = {"message", "email", "draft", "calendar", "event", "contact", "task"}
        with (journal.snapshot() if journal else contextlib.nullcontext(None)) as lookup:
            for item in archive.iter_items():
                self._check_read_cancelled(cancelled)
                if (
                    selected is not None
                    and item.reference not in selected
                    and item.item_key not in selected
                ):
                    continue
                item_warnings: list[str] = []
                if any(
                    (attachment.get("metadata") or {}).get("role") == "ews_native_item"
                    for attachment in item.attachments
                ):
                    native_warning = (
                        "This PIM item uses an opaque Exchange stream and requires UploadItems "
                        "on Exchange 2010 SP1 or newer. Restore creates a new item; original "
                        "mailbox relationships and server compatibility are not guaranteed."
                    )
                    item_warnings.append(native_warning)
                    warnings.add(native_warning)
                duplicate_reason: str | None = None
                if item.reference in existing or item.item_key in existing:
                    action = "skip_duplicate"
                    duplicate_reason = "stable reference already exists"
                elif item.kind.casefold() not in supported:
                    action = "unsupported"
                    item_warnings.append(
                        "This item kind requires a separate high-friction restore workflow."
                    )
                else:
                    action = "create"
                destination = self._restore_destination(destination_root, item)
                if item.kind.casefold() == "draft":
                    destination = f"{safe_filename(destination_root)}/Restored Drafts"
                    item_warnings.append("Restored draft remains unsent and unscheduled.")
                    warnings.add(
                        "Restored drafts are save-only and are never scheduled automatically."
                    )
                recovery = (
                    lookup(chain_digest, item.item_key, destination) if lookup else None
                )
                if action == "create" and recovery is not None:
                    if recovery.status != "completed":
                        action = "review_required"
                        item_warnings.append(
                            "A previous restore to this destination is unconfirmed. "
                            "Do not repeat it. "
                            f"Review restore operation {recovery.operation_id}."
                        )
                    elif not allow_completed_duplicates:
                        action = "skip_duplicate"
                        duplicate_reason = f"Recorded completed restore {recovery.reference}"
                    else:
                        item_warnings.append(
                            "Another copy of an already completed restore was explicitly requested."
                        )
                entries.append(
                    RestorePlanEntry(
                        item_key=item.item_key,
                        source_reference=item.reference,
                        kind=item.kind,
                        destination_folder=destination,
                        action=action,
                        duplicate_reason=duplicate_reason,
                        warnings=tuple(item_warnings),
                        recovery_token=recovery.token if recovery else None,
                        recovery_status=recovery.status if recovery else None,
                        recovery_operation_id=recovery.operation_id if recovery else None,
                        title=self._restore_title(item),
                    )
                )
        self._check_read_cancelled(cancelled)
        counts = Counter(entry.action for entry in entries)
        by_kind = Counter(entry.kind for entry in entries)
        by_destination = Counter(entry.destination_folder for entry in entries)
        manifest_digest = self._read_digest(archive.manifest.to_dict(), cancelled)
        serialized_entries = []
        for entry in entries:
            self._check_read_cancelled(cancelled)
            serialized_entries.append(asdict(entry))
        core = {
            "backup_id": archive.manifest.backup_id,
            "manifest_digest": manifest_digest,
            "chain_digest": chain_digest,
            "destination_root": destination_root,
            "entries": serialized_entries,
            "allow_completed_duplicates": allow_completed_duplicates,
        }
        confirmation_token = self._read_digest(core, cancelled)[:16]
        self._check_read_cancelled(cancelled)
        return RestorePlan(
            backup_id=archive.manifest.backup_id,
            manifest_digest=manifest_digest,
            chain_digest=chain_digest,
            destination_root=destination_root,
            entries=tuple(entries),
            create_count=counts["create"],
            duplicate_count=counts["skip_duplicate"],
            unsupported_count=counts["unsupported"],
            by_kind=dict(by_kind),
            by_destination=dict(by_destination),
            warnings=tuple(sorted(warnings)),
            confirmation_token=confirmation_token,
            blocked_count=counts["review_required"],
            allow_completed_duplicates=allow_completed_duplicates,
        )

    restore_plan = plan_restore

    def restore(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None = None,
        confirmed: bool = False,
        target: RestoreTarget | None = None,
        confirmation_token: str | None = None,
        destination_root: str = "Restored by GUC Mail CLI",
        selected_references: Collection[str] | None = None,
        existing_references: Collection[str] = (),
        allow_completed_duplicates: bool = False,
        continue_on_error: bool = False,
        cancelled: CancellationCheck | None = None,
    ) -> RestorePlan | RestoreResult:
        """Return a plan by default or execute through an explicit provider target.

        Execution performs no automatic retries. A provider error may represent an
        ambiguous mutation, so the exact failed item is surfaced for manual review.
        """

        plan = self.plan_restore(
            identifier,
            passphrase=passphrase,
            destination_root=destination_root,
            selected_references=selected_references,
            existing_references=existing_references,
            allow_completed_duplicates=allow_completed_duplicates,
        )
        if not confirmed and target is None:
            return plan
        if not confirmed or confirmation_token != plan.confirmation_token:
            raise ConfirmationRequiredError(
                "Restore requires explicit confirmation of the current restore plan."
            )
        if plan.create_count and (target is None or not isinstance(target, RestoreTarget)):
            raise UnsupportedFeatureError(
                "Restore execution requires a provider adapter implementing RestoreTarget."
            )

        archive = self.open_offline(identifier, passphrase=passphrase, verify=True)
        archive._pin_chain()
        if archive._chain_digest() != plan.chain_digest:
            raise ConfirmationRequiredError(
                "The backup chain changed after restore planning. Review a new restore plan."
            )
        entries = [entry for entry in plan.entries if entry.action == "create"]
        journal = RestoreJournal(self.state) if self.state is not None else None
        operation_id = uuid4().hex if journal is not None and entries else None
        if operation_id is not None and journal is not None:
            try:
                journal.begin(
                    operation_id, plan.backup_id, plan.chain_digest,
                    (RestoreTargetIntent(
                        entry.item_key, entry.source_reference, entry.destination_folder,
                        entry.recovery_token,
                    ) for entry in entries),
                    allow_completed_duplicates=plan.allow_completed_duplicates,
                )
            except Exception as exc:
                raise LocalStateError(
                    "Restore did not start because its recovery intent could not be recorded. "
                    "Check local storage before trying again."
                ) from exc
        descriptors = archive._merged_descriptors()
        results: list[RestoreItemResult] = []
        stopped_early = False
        provider_attempted = False
        was_cancelled = False
        interrupted = False
        recovery_write_failed = False
        for ordinal, entry in enumerate(entries):
            if self._is_cancelled(cancelled):
                was_cancelled = stopped_early = True
                break
            provider_called = False
            claimed = acknowledged = False
            try:
                # Decode inside the per-item boundary so a later local read
                # failure preserves successful earlier server creations. Build
                # the descriptor index once; do not re-scan the archive per item.
                item = archive._decode_item(*descriptors[entry.item_key])
                hydrated = self._hydrate_restore_attachments(archive, item)
                if self._is_cancelled(cancelled):
                    was_cancelled = stopped_early = True
                    break
                if operation_id is not None and journal is not None:
                    journal.dispatch(operation_id, ordinal)
                    claimed = True
                if self._is_cancelled(cancelled):
                    if claimed and operation_id is not None and journal is not None:
                        journal.cancel_before_provider(operation_id, ordinal)
                    was_cancelled = stopped_early = True
                    break
                assert target is not None
                provider_called = True
                provider_attempted = True
                restored_reference = target.restore_item(
                    hydrated,
                    entry.destination_folder,
                )
                if (
                    not isinstance(restored_reference, str)
                    or not restored_reference.strip()
                    or restored_reference != restored_reference.strip()
                    or len(restored_reference) > 8192
                    or any(ord(char) < 32 or ord(char) == 127 for char in restored_reference)
                ):
                    raise MutationResultUnavailableError(
                        "The restore provider did not return a usable local reference.",
                        suggestion="Inspect the destination before attempting this item again.",
                    )
                acknowledged = True
                if operation_id is not None and journal is not None:
                    journal.finish(
                        operation_id, ordinal, "completed", result_reference=restored_reference,
                    )
            except (Exception, KeyboardInterrupt) as exc:
                interrupted = isinstance(exc, KeyboardInterrupt)
                if acknowledged:
                    recovery_write_failed = True
                    results.append(RestoreItemResult(
                        source_reference=entry.source_reference,
                        kind=entry.kind,
                        destination_folder=entry.destination_folder,
                        succeeded=True,
                        restored_reference=restored_reference,
                        error_type=type(exc).__name__,
                        recovery_write_failed=True,
                    ))
                else:
                    definite = isinstance(exc, (
                        AuthenticationError, ConfigurationError, ConflictError, ItemNotFoundError,
                        ThrottledError, UnsupportedFeatureError,
                    )) or (isinstance(exc, GUCmailError) and exc.context.retry_safe is True)
                    uncertain = provider_called and (
                        not definite
                        or (isinstance(exc, GUCmailError) and exc.context.mutation_uncertain)
                    )
                    record_failed = False
                    if claimed and operation_id is not None and journal is not None:
                        try:
                            journal.finish(
                                operation_id, ordinal, "uncertain" if uncertain else "rejected",
                                error_type=type(exc).__name__,
                            )
                        except Exception:
                            # The already committed dispatch marker remains protected.
                            record_failed = recovery_write_failed = True
                    results.append(RestoreItemResult(
                        source_reference=entry.source_reference,
                        kind=entry.kind,
                        destination_folder=entry.destination_folder,
                        succeeded=False,
                        error_type=type(exc).__name__,
                        mutation_uncertain=uncertain,
                        recovery_write_failed=record_failed,
                    ))
                if interrupted or not continue_on_error or recovery_write_failed:
                    stopped_early = True
                    break
            else:
                results.append(
                    RestoreItemResult(
                        source_reference=entry.source_reference,
                        kind=entry.kind,
                        destination_folder=entry.destination_folder,
                        succeeded=True,
                        restored_reference=restored_reference,
                    )
                )
        if was_cancelled and operation_id is not None and journal is not None:
            try:
                journal.cancel_unstarted(operation_id)
            except Exception:
                recovery_write_failed = True
        restored_count = sum(result.succeeded for result in results)
        return RestoreResult(
            plan=plan,
            attempted_count=len(results),
            restored_count=restored_count,
            failed_count=len(results) - restored_count,
            duplicate_count=plan.duplicate_count,
            unsupported_count=plan.unsupported_count,
            stopped_early=stopped_early,
            results=tuple(results),
            # The provider may create a destination folder before the item save.
            # Local archive failures before provider entry cannot mutate Exchange.
            server_mutations_performed=provider_attempted,
            mutation_uncertain=any(result.mutation_uncertain for result in results),
            cancelled=was_cancelled,
            interrupted=interrupted,
            operation_id=operation_id,
            journal_available=journal is not None,
            recovery_write_failed=recovery_write_failed,
            blocked_count=plan.blocked_count,
        )

    @staticmethod
    def _restore_title(item: OfflineItem) -> str | None:
        sources = [item.metadata]
        if isinstance(item.data, Mapping):
            sources.append(item.data)
            if isinstance(item.data.get("summary"), Mapping):
                sources.append(item.data["summary"])
        for source in sources:
            for key in ("subject", "display_name", "title", "name"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value[:500]
        return None

    @staticmethod
    def _hydrate_restore_attachments(
        archive: OfflineArchive,
        item: OfflineItem,
    ) -> OfflineItem:
        hydrated: list[Mapping[str, Any]] = []
        total = 0
        for attachment in item.attachments:
            selector = str(attachment.get("object", ""))
            content = archive.read_attachment_bytes(
                item.reference,
                selector,
                max_bytes=_MAX_RESTORE_ATTACHMENT_BYTES - total,
            )
            total += len(content)
            hydrated.append({**dict(attachment), "content": content})
        return replace(item, attachments=tuple(hydrated))

    def _open_manifest(
        self,
        identifier: str | Path,
        *,
        passphrase: str | bytes | None,
        allow_incomplete: bool = False,
    ) -> tuple[Path, BackupManifest, KeyMaterial | None]:
        path = self._resolve_backup(identifier, allow_incomplete=allow_incomplete)
        incomplete = path.name.endswith(_PARTIAL_SUFFIX)
        manifest_path = path / (INCOMPLETE_MANIFEST_FILENAME if incomplete else MANIFEST_FILENAME)
        manifest, keys = self._read_manifest_auto(manifest_path, passphrase)
        if manifest.state != "complete" and not allow_incomplete:
            raise BackupIntegrityError(
                "Backup is incomplete; resume or discard it before browsing."
            )
        return path, manifest, keys

    @staticmethod
    def _read_manifest_auto(
        path: Path, passphrase: str | bytes | None
    ) -> tuple[BackupManifest, KeyMaterial | None]:
        raw = load_json(path)
        encryption = raw.get("encryption")
        encrypted = isinstance(encryption, Mapping) and encryption.get("enabled") is True
        return read_manifest(path, passphrase=passphrase if encrypted else None)

    def _resolve_backup(self, identifier: str | Path, *, allow_incomplete: bool = False) -> Path:
        raw = Path(identifier)
        path = raw if raw.is_absolute() or raw.parent != Path(".") else self.root / raw
        if path.is_file() and path.name in {MANIFEST_FILENAME, INCOMPLETE_MANIFEST_FILENAME}:
            path = path.parent
        if not path.exists() and allow_incomplete:
            partial = self.root / f"{raw.name}{_PARTIAL_SUFFIX}"
            if partial.exists():
                path = partial
        if not path.is_dir():
            raise ItemNotFoundError(f"Backup not found: {identifier}")
        if path.is_symlink():
            raise BackupIntegrityError("Backup directory cannot be a symbolic link.")
        manifest_name = (
            INCOMPLETE_MANIFEST_FILENAME
            if path.name.endswith(_PARTIAL_SUFFIX)
            else MANIFEST_FILENAME
        )
        if not (path / manifest_name).is_file():
            raise BackupIntegrityError(f"Backup manifest is missing: {path}")
        return path

    def _resolve_partial(self, identifier: str | Path) -> Path:
        raw = Path(identifier)
        candidates = [raw] if raw.is_absolute() or raw.parent != Path(".") else []
        candidates.extend(
            [
                self.root / raw,
                self.root / f"{raw.name.removesuffix(_PARTIAL_SUFFIX)}{_PARTIAL_SUFFIX}",
            ]
        )
        for candidate in candidates:
            if candidate.is_file() and candidate.name == INCOMPLETE_MANIFEST_FILENAME:
                candidate = candidate.parent
            if candidate.is_dir() and (candidate / INCOMPLETE_MANIFEST_FILENAME).is_file():
                if candidate.is_symlink():
                    raise BackupIntegrityError("Backup checkpoint cannot be a symbolic link.")
                return candidate
        raise ItemNotFoundError(f"Resumable backup not found: {identifier}")

    @contextlib.contextmanager
    def _writer_lock(self, operation: str) -> Iterator[None]:
        path = self.root / _LOCK_FILENAME
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ConflictError(
                f"Another backup writer holds {path.name}; inspect it before removing a stale lock."
            ) from exc
        try:
            message = canonical_json_bytes(
                {"pid": os.getpid(), "operation": operation, "started_at": utc_text(self._clock())}
            )
            os.write(descriptor, message)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    @staticmethod
    def _iter_source(
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        request: BackupRequest,
        completed: frozenset[str],
    ) -> Iterable[BackupItem | Mapping[str, Any] | Any]:
        if isinstance(source, BackupSource):
            return source.iter_backup_items(request, completed)
        if isinstance(source, (str, bytes, bytearray, Mapping)):
            raise TypeError("Backup source must be an iterable of items, not a scalar value.")
        return source

    @classmethod
    def _coerce_item(cls, raw: Any) -> BackupItem:
        if isinstance(raw, BackupItem):
            return raw
        if isinstance(raw, Mapping):
            reference = raw.get("reference", raw.get("local_ref", raw.get("id")))
            if not isinstance(reference, str) or not reference:
                raise ValueError("Backup mapping item requires reference, local_ref, or id.")
            kind = str(raw.get("kind", raw.get("item_kind", "message")))
            attachments_raw = raw.get("attachments", ())
            if attachments_raw is None:
                attachments_raw = ()
            if not isinstance(attachments_raw, Sequence) or isinstance(attachments_raw, str):
                raise ValueError("Backup item attachments must be a sequence.")
            attachments = tuple(BackupAttachment.from_value(item) for item in attachments_raw)
            if "data" in raw:
                data = raw["data"]
            elif "content" in raw:
                data = raw["content"]
            else:
                control = {
                    "reference",
                    "local_ref",
                    "id",
                    "kind",
                    "item_kind",
                    "folder_path",
                    "folder",
                    "attachments",
                    "metadata",
                    "deleted",
                }
                data = {str(key): value for key, value in raw.items() if key not in control}
            metadata = raw.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise ValueError("Backup item metadata must be a mapping.")
            return BackupItem(
                reference=reference,
                kind=kind,
                data=data,
                folder_path=str(raw.get("folder_path", raw.get("folder")))
                if raw.get("folder_path", raw.get("folder")) is not None
                else None,
                attachments=attachments,
                metadata=dict(metadata),
                deleted=bool(raw.get("deleted", False)),
            )
        reference = getattr(raw, "reference", None)
        if reference is None:
            summary = getattr(raw, "summary", None)
            reference = getattr(summary, "reference", None)
        if not isinstance(reference, str) or not reference:
            raise TypeError(f"Cannot infer a backup reference from {type(raw).__name__}.")
        return BackupItem(
            reference=reference,
            kind=type(raw).__name__.casefold(),
            data=to_primitive(raw),
            folder_path=getattr(raw, "folder_path", None),
        )

    @staticmethod
    def _excluded(item: BackupItem, patterns: Sequence[str]) -> bool:
        values = (item.reference, item.kind, item.folder_path or "")
        return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns for value in values)

    @staticmethod
    def _blob_size(data: bytes | bytearray | memoryview | Path) -> int:
        return data.stat().st_size if isinstance(data, Path) else len(data)

    @staticmethod
    def _read_blob(data: bytes | bytearray | memoryview | Path) -> bytes:
        if isinstance(data, Path):
            if data.is_symlink():
                raise BackupIntegrityError(
                    "Refusing to back up an attachment through a symbolic link."
                )
            size = data.stat().st_size
            if size > _MAX_OBJECT_BYTES:
                raise BackupIntegrityError(f"Backup object exceeds {_MAX_OBJECT_BYTES} bytes.")
            return data.read_bytes()
        return bytes(data)

    @classmethod
    def _estimate_value(cls, value: Any) -> int:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return len(value)
        if isinstance(value, Path):
            with contextlib.suppress(OSError):
                return value.stat().st_size
            return 0
        try:
            primitive = to_primitive(value)
            return len(canonical_json_bytes(cls._estimate_json_value(primitive)))
        except (TypeError, BackupIntegrityError):
            return len(str(value).encode("utf-8", errors="replace"))

    @classmethod
    def _estimate_json_value(cls, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"$binary_size": len(value)}
        if isinstance(value, Mapping):
            return {str(key): cls._estimate_json_value(child) for key, child in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, str):
            return [cls._estimate_json_value(child) for child in value]
        return value

    @staticmethod
    def _statistics(
        objects: Mapping[str, ObjectDescriptor], items: Sequence[ItemDescriptor],
        *, cancelled: CancellationCheck | None = None,
    ) -> dict[str, int]:
        logical_bytes = stored_bytes = attachment_count = deleted_count = 0
        for descriptor in objects.values():
            if cancelled is not None:
                BackupService._check_read_cancelled(cancelled)
            logical_bytes += descriptor.logical_size
            stored_bytes += descriptor.stored_size
            attachment_count += descriptor.kind == "attachment"
        for item in items:
            if cancelled is not None:
                BackupService._check_read_cancelled(cancelled)
            deleted_count += item.deleted
        return {
            "item_count": len(items),
            "object_count": len(objects),
            "logical_bytes": logical_bytes,
            "stored_bytes": stored_bytes,
            "deleted_item_count": deleted_count,
            "attachment_object_count": attachment_count,
        }

    @staticmethod
    def _sha256_path(
        path: Path, *, chunk_size: int = 1024 * 1024,
        cancelled: CancellationCheck | None = None,
    ) -> str:
        BackupService._check_read_cancelled(cancelled)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                BackupService._check_read_cancelled(cancelled)
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        BackupService._check_read_cancelled(cancelled)
        return digest.hexdigest()

    @staticmethod
    def _read_logical_object(
        root: Path,
        manifest: BackupManifest,
        descriptor: ObjectDescriptor,
        keys: KeyMaterial | None,
        *,
        cancelled: CancellationCheck | None = None,
    ) -> bytes:
        BackupService._check_read_cancelled(cancelled)
        path = resolve_relative_path(root, descriptor.path)
        if path.is_symlink():
            raise BackupIntegrityError("Backup object cannot be a symbolic link.")
        try:
            stat = path.stat()
        except OSError as exc:
            raise BackupIntegrityError(
                f"Backup object is missing: {descriptor.content_id}"
            ) from exc
        if stat.st_size != descriptor.stored_size:
            raise BackupIntegrityError("Backup object stored size does not match its manifest.")
        if stat.st_size > _MAX_OBJECT_BYTES:
            raise BackupIntegrityError("Backup object exceeds the safe supported size.")
        stored = path.read_bytes()
        BackupService._check_read_cancelled(cancelled)
        if sha256_bytes(stored) != descriptor.stored_sha256:
            raise BackupIntegrityError("Backup object stored SHA-256 verification failed.")
        if descriptor.encryption == "AES-256-GCM":
            if keys is None or descriptor.nonce is None:
                raise BackupIntegrityError("Encrypted backup object is locked.")
            stored = decrypt_object(
                stored,
                keys.encryption_key,
                aad=object_aad(manifest.backup_id, descriptor.content_id),
                nonce=descriptor.nonce,
            )
        elif descriptor.encryption != "none":
            raise BackupIntegrityError("Unsupported object encryption algorithm.")
        BackupService._check_read_cancelled(cancelled)
        if descriptor.compression == "zlib":
            try:
                decompressor = zlib.decompressobj()
                logical = decompressor.decompress(stored, descriptor.logical_size + 1)
                if len(logical) > descriptor.logical_size or decompressor.unconsumed_tail:
                    raise BackupIntegrityError(
                        "Compressed backup object exceeds its declared size."
                    )
                logical += decompressor.flush(descriptor.logical_size - len(logical) + 1)
            except zlib.error as exc:
                raise BackupIntegrityError("Compressed backup object is damaged.") from exc
        elif descriptor.compression == "none":
            logical = stored
        else:
            raise BackupIntegrityError("Unsupported object compression algorithm.")
        if len(logical) != descriptor.logical_size:
            raise BackupIntegrityError("Backup object logical size does not match its manifest.")
        BackupService._check_read_cancelled(cancelled)
        if sha256_bytes(logical) != descriptor.sha256 or descriptor.content_id != descriptor.sha256:
            raise BackupIntegrityError("Backup object logical SHA-256 verification failed.")
        BackupService._check_read_cancelled(cancelled)
        return logical

    @staticmethod
    def _validate_record(
        logical: bytes,
        descriptor: ObjectDescriptor,
        *,
        expected_item_key: str,
    ) -> Mapping[str, Any]:
        try:
            value = json.loads(logical.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError("Canonical backup record is not valid UTF-8 JSON.") from exc
        if not isinstance(value, Mapping) or value.get("record_schema") != 1:
            raise BackupIntegrityError("Canonical backup record schema is invalid.")
        item_key = value.get("item_key")
        if not isinstance(item_key, str):
            raise BackupIntegrityError("Canonical backup record has no item key.")
        if item_key != expected_item_key:
            raise BackupIntegrityError("Canonical record is not bound to its manifest item.")
        return value

    @staticmethod
    def _request_fingerprint(request: BackupRequest) -> str:
        payload = {
            "kind": request.kind.value,
            "selection": request.selection.to_dict(),
            "exclusions": list(request.exclusions),
            "parent_backup_id": request.parent_backup_id,
            "source_label": request.source_label,
            "compression": request.options.compression,
        }
        return sha256_bytes(canonical_json_bytes(payload))

    @staticmethod
    def _is_cancelled(value: CancellationCheck | None) -> bool:
        if value is None:
            return False
        if callable(value):
            return bool(value())
        checker = getattr(value, "is_set", None)
        return bool(checker()) if callable(checker) else bool(value)

    @staticmethod
    def _check_read_cancelled(value: CancellationCheck | None) -> None:
        if BackupService._is_cancelled(value):
            raise BackupReadCancelledError()

    @staticmethod
    def _read_digest(value: Any, cancelled: CancellationCheck | None) -> str:
        # Retain the exact existing canonical representation and token contract.
        # JSON/crypto library calls are checked at their boundaries; file hashing
        # and explicit item/descriptor loops additionally check each chunk/item.
        BackupService._check_read_cancelled(cancelled)
        result = sha256_bytes(canonical_json_bytes(value))
        BackupService._check_read_cancelled(cancelled)
        return result

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        with contextlib.suppress(OSError):
            for item in path.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    with contextlib.suppress(OSError):
                        total += item.stat().st_size
        return total

    @staticmethod
    def _repository_fingerprint(summaries: Sequence[BackupSummary]) -> str:
        payload = [
            {
                "id": item.backup_id,
                "created_at": item.created_at,
                "stored_bytes": item.stored_bytes,
                "parent": item.parent_backup_id,
                "state": item.state,
            }
            for item in sorted(summaries, key=lambda current: current.backup_id)
        ]
        return sha256_bytes(canonical_json_bytes(payload))

    @staticmethod
    def _restore_destination(root: str, item: OfflineItem) -> str:
        safe_root = safe_filename(root, fallback="Restored by GUC Mail CLI")
        if not item.folder_path:
            return f"{safe_root}/Unfiled"
        segments = [
            safe_filename(segment, fallback="Folder")
            for segment in item.folder_path.replace("\\", "/").split("/")
        ]
        return "/".join([safe_root, *segments])

    @staticmethod
    def _result(path: Path, manifest: BackupManifest) -> BackupResult:
        statistics = manifest.statistics
        return BackupResult(
            backup_id=manifest.backup_id,
            path=path,
            kind=manifest.kind,
            state=manifest.state,
            item_count=int(statistics["item_count"]),
            object_count=int(statistics["object_count"]),
            logical_bytes=int(statistics["logical_bytes"]),
            stored_bytes=int(statistics["stored_bytes"]),
            encrypted=manifest.encrypted,
            parent_backup_id=manifest.parent_backup_id,
            manifest=manifest,
        )

    def _record_state(self, manifest: BackupManifest, path: Path) -> None:
        # StateStore is optional and deliberately non-authoritative.  Avoid
        # depending on a particular migration/API while still supporting a
        # future registry hook.
        hook = getattr(self.state, "record_backup", None)
        if callable(hook):
            hook(manifest=manifest, path=path)


class OfflineArchive:
    """Read-only, filesystem-backed view over a complete backup chain."""

    def __init__(
        self,
        service: BackupService,
        path: Path,
        manifest: BackupManifest,
        keys: KeyMaterial | None,
        *,
        passphrase: str | bytes | None,
        cancelled: CancellationCheck | None = None,
    ) -> None:
        self._service = service
        self.path = path
        self.manifest = manifest
        self._keys = keys
        self._passphrase = passphrase
        self._read_cancelled = cancelled
        self._pinned_chain: tuple[tuple[Path, BackupManifest, KeyMaterial | None], ...] | None = (
            None
        )
        self.offline = True
        self.read_only = True
        self.server_mutations_available = False

    @property
    def backup_date(self) -> str:
        return self.manifest.completed_at or self.manifest.created_at

    @property
    def stale(self) -> bool:
        return True

    @property
    def source_notice(self) -> str:
        return (
            f"Offline mode — source backup {self.manifest.backup_id} from {self.backup_date}; "
            "data may be stale and server mutations are unavailable."
        )

    def _chain(self) -> tuple[tuple[Path, BackupManifest, KeyMaterial | None], ...]:
        self._service._check_read_cancelled(self._read_cancelled)
        if self._pinned_chain is not None:
            return self._pinned_chain
        chain: list[tuple[Path, BackupManifest, KeyMaterial | None]] = [
            (self.path, self.manifest, self._keys)
        ]
        seen = {self.manifest.backup_id}
        current = self.manifest
        while current.parent_backup_id is not None:
            self._service._check_read_cancelled(self._read_cancelled)
            if len(chain) >= _MAX_CHAIN_DEPTH:
                raise BackupIntegrityError("Backup parent chain is excessively deep.")
            parent_id = current.parent_backup_id
            if parent_id in seen:
                raise BackupIntegrityError("Backup parent chain contains a cycle.")
            parent_path = self._service.root / parent_id
            if not parent_path.is_dir():
                raise BackupIntegrityError(f"Incremental backup parent is missing: {parent_id}")
            parent, keys = self._service._read_manifest_auto(
                parent_path / MANIFEST_FILENAME, self._passphrase
            )
            self._service._check_read_cancelled(self._read_cancelled)
            if parent.state != "complete":
                raise BackupIntegrityError(f"Incremental backup parent is incomplete: {parent_id}")
            if parent.backup_id != parent_id:
                raise BackupIntegrityError(
                    "Incremental backup parent identifier does not match path."
                )
            chain.append((parent_path, parent, keys))
            seen.add(parent_id)
            current = parent
        chain.reverse()
        return tuple(chain)

    def _pin_chain(self) -> None:
        """Use one manifest snapshot for an entire restore plan or execution."""
        if self._pinned_chain is None:
            self._pinned_chain = self._chain()

    def _chain_digest(self) -> str:
        """Bind a restore confirmation to every manifest supplying its content."""
        manifests = []
        for _, manifest, _ in self._chain():
            self._service._check_read_cancelled(self._read_cancelled)
            manifests.append(manifest.to_dict())
        return self._service._read_digest(manifests, self._read_cancelled)

    def _merged_descriptors(
        self,
    ) -> dict[str, tuple[Path, BackupManifest, KeyMaterial | None, ItemDescriptor]]:
        merged: dict[str, tuple[Path, BackupManifest, KeyMaterial | None, ItemDescriptor]] = {}
        for path, manifest, keys in self._chain():
            for descriptor in manifest.items:
                self._service._check_read_cancelled(self._read_cancelled)
                if descriptor.deleted:
                    merged.pop(descriptor.item_key, None)
                else:
                    merged[descriptor.item_key] = (path, manifest, keys, descriptor)
        return merged

    def iter_items(
        self,
        *,
        kind: str | None = None,
        kinds: Collection[str] | None = None,
        folder: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        query: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[OfflineItem]:
        self._service._check_read_cancelled(self._read_cancelled)
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("Offline pagination values cannot be negative.")
        if kind is not None and kinds is not None:
            raise ValueError("Pass kind or kinds, not both.")
        folded_kinds = {value.casefold() for value in kinds} if kinds is not None else None
        if limit == 0:
            return
        yielded = 0
        skipped = 0
        needle = query.strip().casefold()
        for path, manifest, keys, descriptor in self._merged_descriptors().values():
            self._service._check_read_cancelled(self._read_cancelled)
            if cancelled is not None and cancelled():
                return
            if kind is not None and descriptor.kind.casefold() != kind.casefold():
                continue
            if folded_kinds is not None and descriptor.kind.casefold() not in folded_kinds:
                continue
            if (
                folder is not None
                and (descriptor.folder_path or "").casefold() != folder.casefold()
            ):
                continue
            item = None
            if needle:
                item = self._decode_item(path, manifest, keys, descriptor)
                if needle not in self._search_text(item).casefold():
                    continue
            if skipped < offset:
                skipped += 1
                continue
            yield item if item is not None else self._decode_item(path, manifest, keys, descriptor)
            yielded += 1
            if limit is not None and yielded >= limit:
                break
        self._service._check_read_cancelled(self._read_cancelled)

    def list_items(self, **filters: Any) -> tuple[OfflineItem, ...]:
        return tuple(self.iter_items(**filters))

    def list_messages(
        self, *, folder: str | None = None, offset: int = 0, limit: int | None = None
    ) -> tuple[OfflineItem, ...]:
        return tuple(
            self.iter_items(
                kinds={"message", "email", "messagesummary", "messagedetail"},
                folder=folder,
                offset=offset,
                limit=limit,
            )
        )

    def get_item(self, reference: str) -> OfflineItem:
        descriptors = self._merged_descriptors()
        if reference in descriptors:
            return self._decode_item(*descriptors[reference])
        for item in self.iter_items():
            if item.reference == reference:
                return item
        raise ItemNotFoundError(f"Offline backup item not found: {reference}")

    get = get_item

    def folders(self) -> tuple[dict[str, Any], ...]:
        counts: Counter[str] = Counter()
        kinds: dict[str, Counter[str]] = {}
        for item in self.iter_items():
            folder = item.folder_path or "Unfiled"
            counts[folder] += 1
            kinds.setdefault(folder, Counter())[item.kind] += 1
        return tuple(
            {
                "path": path,
                "item_count": counts[path],
                "kinds": dict(kinds[path]),
                "source": "backup",
                "offline": True,
            }
            for path in sorted(counts, key=str.casefold)
        )

    browse_folders = folders

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        folder: str | None = None,
        fuzzy: bool = False,
        minimum_score: float = 70.0,
        limit: int = 100,
    ) -> tuple[OfflineSearchResult, ...]:
        needle = query.strip().casefold()
        if not needle:
            raise ValueError("Offline search query cannot be empty.")
        if limit < 1 or limit > 10_000:
            raise ValueError("Offline search limit must be between 1 and 10000.")

        def candidates() -> Iterator[OfflineSearchResult]:
            for item in self.iter_items(kind=kind, folder=folder):
                haystack = self._search_text(item)
                folded = haystack.casefold()
                if needle in folded:
                    score = 100.0
                elif fuzzy:
                    score = self._fuzzy_score(needle, folded)
                    if score < minimum_score:
                        continue
                else:
                    continue
                yield OfflineSearchResult(item, score, haystack[:500])

        # ``nsmallest`` retains at most ``limit`` candidates while preserving the
        # deterministic best-score/reference ordering.
        return tuple(
            heapq.nsmallest(
                limit,
                candidates(),
                key=lambda result: (-result.score, result.item.reference.casefold()),
            )
        )

    def iter_search_documents(self) -> Iterator[dict[str, Any]]:
        for item in self.iter_items():
            yield {
                "reference": item.reference,
                "item_key": item.item_key,
                "kind": item.kind,
                "folder": item.folder_path,
                "text": self._search_text(item),
                "source": "backup",
                "backup_id": item.backup_id,
                "offline": True,
                "stale": True,
            }

    def read_attachment_bytes(
        self,
        item_reference: str,
        attachment: str,
        *,
        max_bytes: int = _MAX_RESTORE_ATTACHMENT_BYTES,
    ) -> bytes:
        """Read one verified attachment with an explicit in-memory size ceiling."""

        if max_bytes < 0:
            raise BackupIntegrityError("Attachment restore size limit was exceeded.")
        item = self.get_item(item_reference)
        selected: Mapping[str, Any] | None = None
        for candidate in item.attachments:
            if attachment in {
                str(candidate.get("object", "")),
                str(candidate.get("original_filename", "")),
                str(candidate.get("normalized_filename", "")),
            }:
                selected = candidate
                break
        if selected is None:
            raise ItemNotFoundError(f"Attachment not found in offline item: {attachment}")
        content_id = str(selected["object"])
        owner = self._object_owner(content_id)
        if owner is None:
            raise BackupIntegrityError("Attachment object is not reachable from the backup chain.")
        path, manifest, keys, descriptor = owner
        if descriptor.logical_size > max_bytes:
            raise BackupIntegrityError(
                "Attachment exceeds the bounded restore memory limit for one item."
            )
        return self._service._read_logical_object(path, manifest, descriptor, keys)

    def extract_attachment(
        self,
        item_reference: str,
        attachment: str,
        destination: Path | str,
        *,
        overwrite: bool = False,
    ) -> Path:
        item = self.get_item(item_reference)
        selected: Mapping[str, Any] | None = None
        for candidate in item.attachments:
            if attachment in {
                str(candidate.get("object", "")),
                str(candidate.get("original_filename", "")),
                str(candidate.get("normalized_filename", "")),
            }:
                selected = candidate
                break
        if selected is None:
            raise ItemNotFoundError(f"Attachment not found in offline item: {attachment}")
        logical = self.read_attachment_bytes(
            item_reference,
            str(selected["object"]),
            max_bytes=_MAX_RESTORE_ATTACHMENT_BYTES,
        )
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        filename = str(selected.get("normalized_filename") or selected.get("original_filename"))
        target = unique_download_path(directory, filename, overwrite=overwrite)
        atomic_write_bytes(target, logical)
        return target

    def export_item(
        self,
        reference: str,
        destination: Path | str,
        *,
        overwrite: bool = False,
    ) -> Path:
        item = self.get_item(reference)
        target = Path(destination)
        if target.is_dir() or target.suffix == "":
            target.mkdir(parents=True, exist_ok=True)
            target = target / f"{safe_filename(item.reference, fallback='item')}.json"
        if target.exists() and not overwrite:
            raise ConflictError(f"Export destination already exists: {target}")
        payload = {
            "source": "backup",
            "offline": True,
            "backup_id": item.backup_id,
            "item": {
                "item_key": item.item_key,
                "reference": item.reference,
                "kind": item.kind,
                "folder_path": item.folder_path,
                "metadata": item.metadata,
                "data": item.data,
                "attachments": list(item.attachments),
            },
        }
        atomic_write_bytes(target, canonical_json_bytes(payload) + b"\n")
        return target

    def _decode_item(
        self,
        path: Path,
        manifest: BackupManifest,
        keys: KeyMaterial | None,
        descriptor: ItemDescriptor,
    ) -> OfflineItem:
        self._service._check_read_cancelled(self._read_cancelled)
        object_descriptor = manifest.objects.get(descriptor.record_object)
        if object_descriptor is None:
            raise BackupIntegrityError("Offline item record object is missing from manifest.")
        logical = self._service._read_logical_object(
            path, manifest, object_descriptor, keys, cancelled=self._read_cancelled,
        )
        record = self._service._validate_record(
            logical,
            object_descriptor,
            expected_item_key=descriptor.item_key,
        )
        if record.get("item_key") != descriptor.item_key:
            raise BackupIntegrityError("Offline item key does not match its record.")
        attachments = record.get("attachments", [])
        metadata = record.get("metadata", {})
        if not isinstance(attachments, list) or not all(
            isinstance(value, Mapping) for value in attachments
        ):
            raise BackupIntegrityError("Offline item attachment metadata is invalid.")
        if not isinstance(metadata, Mapping):
            raise BackupIntegrityError("Offline item metadata is invalid.")
        self._service._check_read_cancelled(self._read_cancelled)
        return OfflineItem(
            item_key=descriptor.item_key,
            reference=str(record.get("reference", "")),
            kind=str(record.get("kind", descriptor.kind)),
            folder_path=str(record["folder_path"])
            if record.get("folder_path") is not None
            else None,
            data=record.get("data"),
            metadata=dict(metadata),
            attachments=tuple(dict(value) for value in attachments),
            backup_id=manifest.backup_id,
        )

    def _object_owner(
        self, content_id: str
    ) -> tuple[Path, BackupManifest, KeyMaterial | None, ObjectDescriptor] | None:
        for path, manifest, keys in reversed(self._chain()):
            descriptor = manifest.objects.get(content_id)
            if descriptor is not None:
                return path, manifest, keys, descriptor
        return None

    @classmethod
    def _search_text(cls, item: OfflineItem) -> str:
        values: list[str] = [item.reference, item.kind, item.folder_path or ""]

        def visit(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, Mapping):
                for key, child in value.items():
                    values.append(str(key))
                    visit(child)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for child in value:
                    visit(child)
            elif isinstance(value, (str, int, float, bool)):
                values.append(str(value))

        visit(item.data)
        visit(item.metadata)
        visit(item.attachments)
        return "\n".join(values)

    @staticmethod
    def _fuzzy_score(needle: str, haystack: str) -> float:
        try:
            from rapidfuzz.fuzz import WRatio

            return float(WRatio(needle, haystack))
        except ImportError:  # pragma: no cover - project dependency guard
            from difflib import SequenceMatcher

            return SequenceMatcher(None, needle, haystack).ratio() * 100


class _SnapshotDiffSource:
    """Turn a complete current snapshot into a correct parent delta.

    The source is fully exhausted before deletion tombstones are emitted. Therefore a
    partial/failed scan cannot be mistaken for mailbox deletion. The wrapper always
    asks the underlying provider for every item in scope, even during resume, because
    completed keys are still required to distinguish unchanged items from deletions.
    """

    def __init__(
        self,
        service: BackupService,
        source: BackupSource | Iterable[BackupItem | Mapping[str, Any] | Any],
        parent: OfflineArchive,
    ) -> None:
        self._service = service
        self._source = source
        self._parent = parent

    @property
    def backup_source_identity(self) -> str | None:
        return getattr(self._source, "backup_source_identity", None)

    def iter_backup_items(
        self,
        request: BackupRequest,
        completed_item_keys: frozenset[str],
    ) -> Iterator[BackupItem]:
        parent_items: dict[str, tuple[OfflineItem, str]] = {}
        for parent_item in self._parent.iter_items():
            if _incremental_item_in_scope(parent_item, request):
                parent_items[parent_item.item_key] = (
                    parent_item,
                    _offline_item_fingerprint(parent_item),
                )

        seen: set[str] = set()
        raw_source = self._service._iter_source(self._source, request, frozenset())
        for raw in raw_source:
            item = self._service._coerce_item(raw)
            if item.deleted:
                raise BackupIntegrityError(
                    "A complete-snapshot incremental source returned a deletion marker."
                )
            if not _incremental_item_in_scope(item, request):
                continue
            item_key = self._service.item_key(item.kind, item.reference)
            if item_key in seen:
                raise ConflictError(
                    f"Incremental snapshot returned duplicate item reference {item.reference!r}."
                )
            seen.add(item_key)
            parent_entry = parent_items.pop(item_key, None)
            unchanged = parent_entry is not None and parent_entry[1] == _backup_item_fingerprint(
                item, self._service
            )
            if not unchanged and item_key not in completed_item_keys:
                yield item

        # Reaching this point proves the provider completed its snapshot iterator.
        # Only now is absence safe to encode as a tombstone.
        for item_key in sorted(parent_items):
            if item_key in completed_item_keys:
                continue
            parent_item, _fingerprint = parent_items[item_key]
            yield BackupItem(
                reference=parent_item.reference,
                kind=parent_item.kind,
                data={"deleted_from_parent": self._parent.manifest.backup_id},
                folder_path=parent_item.folder_path,
                metadata={"tombstone": True},
                deleted=True,
            )


def _incremental_item_in_scope(item: BackupItem | OfflineItem, request: BackupRequest) -> bool:
    if BackupService._excluded(
        BackupItem(
            reference=item.reference,
            kind=item.kind,
            data={},
            folder_path=item.folder_path,
        ),
        request.exclusions,
    ):
        return False
    selection = request.selection
    if selection.messages and item.reference not in selection.messages:
        return False
    # Folder selectors may be localized paths, aliases or opaque references. The
    # provider resolves them; comparing them to display paths would discard valid
    # records. Parent and child scope equality is checked before reading the source.
    if selection.item_kinds:
        kinds = {value.casefold() for value in selection.item_kinds}
        if item.kind.casefold() not in kinds:
            return False
    return True


def _selection_scope(selection: BackupSelection) -> tuple[Any, ...]:
    return (
        frozenset(selection.folders),
        frozenset(selection.messages),
        selection.search,
        selection.cluster,
        frozenset(value.casefold() for value in selection.item_kinds),
    )


def _backup_item_fingerprint(item: BackupItem, service: BackupService) -> str:
    attachments: list[dict[str, Any]] = []
    for attachment in item.attachments:
        content = service._read_blob(attachment.data)
        attachments.append(
            {
                "original_filename": attachment.original_filename,
                "normalized_filename": attachment.normalized_filename
                or safe_filename(attachment.original_filename),
                "content_type": attachment.content_type or "application/octet-stream",
                "size": len(content),
                "sha256": sha256_bytes(content),
                "metadata": _fingerprint_value(attachment.metadata),
            }
        )
    payload = {
        "reference": item.reference,
        "kind": item.kind,
        "folder_path": item.folder_path,
        "data": _fingerprint_value(item.data),
        "metadata": _fingerprint_value(item.metadata),
        "attachments": attachments,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _offline_item_fingerprint(item: OfflineItem) -> str:
    attachments = [
        {
            "original_filename": str(value.get("original_filename", "")),
            "normalized_filename": str(value.get("normalized_filename", "")),
            "content_type": str(value.get("content_type", "application/octet-stream")),
            "size": int(value.get("size", 0)),
            "sha256": str(value.get("sha256", "")),
            "metadata": _fingerprint_value(value.get("metadata", {})),
        }
        for value in item.attachments
    ]
    payload = {
        "reference": item.reference,
        "kind": item.kind,
        "folder_path": item.folder_path,
        "data": _fingerprint_value(item.data),
        "metadata": _fingerprint_value(item.metadata),
        "attachments": attachments,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        content = bytes(value)
        return {"$binary_sha256": sha256_bytes(content), "size": len(content)}
    if isinstance(value, Path):
        if value.is_symlink():
            raise BackupIntegrityError(
                "Cannot fingerprint a backup object through a symbolic link."
            )
        content = value.read_bytes()
        return {"$binary_sha256": sha256_bytes(content), "size": len(content)}
    if is_dataclass(value) and not isinstance(value, type):
        return _fingerprint_value(to_primitive(value))
    if isinstance(value, Enum):
        return _fingerprint_value(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        object_digest = value.get("$gucmail_object")
        if isinstance(object_digest, str) and value.get("size") is not None:
            return {
                "$binary_sha256": object_digest,
                "size": int(value["size"]),
            }
        return {str(key): _fingerprint_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_fingerprint_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _fingerprint_value(to_primitive(value))


__all__ = [
    "BackupAttachment",
    "BackupCancelledError",
    "BackupEstimate",
    "BackupEstimateSource",
    "BackupItem",
    "BackupKind",
    "BackupOptions",
    "BackupProgress",
    "BackupReadCancelledError",
    "BackupRequest",
    "BackupResult",
    "BackupSelection",
    "BackupService",
    "BackupSource",
    "BackupSummary",
    "OfflineArchive",
    "OfflineItem",
    "OfflineSearchResult",
    "PrunePlan",
    "PruneResult",
    "RestoreItemResult",
    "RestorePlan",
    "RestorePlanEntry",
    "RestoreResult",
    "RestoreTarget",
    "VerificationReport",
]
