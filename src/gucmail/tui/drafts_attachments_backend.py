"""Typed service adapter for the drafts and attachments TUI workspace.

The adapter intentionally contains no Textual objects.  It translates the
existing revisioned draft, draft synchronization, and hardened attachment
services into small immutable view records that can safely cross a worker
thread boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import BinaryIO, Protocol

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError, ConflictError, ItemNotFoundError
from gucmail.models import AttachmentInfo, DraftData, DraftLocation
from gucmail.security import is_dangerous_attachment, safe_filename, unique_download_path
from gucmail.services.attachments import (
    AttachmentSavePlan,
    AttachmentService,
    CollisionPolicy,
    DownloadProgress,
    _commit_download,
)
from gucmail.services.drafts import (
    DraftService,
    DraftSyncCoordinator,
    DraftSyncMetadata,
    DraftSyncState,
    draft_content_hash,
    draft_to_dict,
)
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
)

MAX_WORKSPACE_ROWS = 1_000
LOCAL_COPY_CHUNK_SIZE = 256 * 1024


@dataclass(frozen=True, slots=True)
class WorkspaceCapability:
    """A truthful status row for one optional workspace boundary."""

    name: str
    available: bool
    online_required: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DraftRowView:
    reference: str
    subject: str
    recipients: tuple[str, ...]
    body_preview: str
    location: str
    sync_state: str
    revision: int
    updated_at: str | None
    attachment_count: int
    server_id_present: bool
    conflicted: bool
    conflict_reason: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class DraftWorkspaceSnapshot:
    drafts: tuple[DraftRowView, ...]
    matched_count: int
    truncated: bool
    capabilities: tuple[WorkspaceCapability, ...]
    offset: int = 0
    next_offset: int | None = None


@dataclass(frozen=True, slots=True)
class DraftConflictView:
    reference: str
    reason: str
    detected_at: str | None
    local_subject: str
    server_subject: str
    local_recipients: tuple[str, ...]
    server_recipients: tuple[str, ...]
    local_body_preview: str
    server_body_preview: str
    local_attachment_count: int
    server_attachment_count: int


@dataclass(frozen=True, slots=True)
class DraftOperationView:
    reference: str
    detail: str
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class AttachmentRowView:
    owner_reference: str
    reference: str
    name: str
    size: int | None
    content_type: str | None
    source: str
    dangerous: bool
    is_inline: bool
    is_item_attachment: bool
    content_id: str | None = None
    local_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AttachmentWorkspaceSnapshot:
    owner_reference: str
    attachments: tuple[AttachmentRowView, ...]
    matched_count: int
    truncated: bool
    capabilities: tuple[WorkspaceCapability, ...]


@dataclass(frozen=True, slots=True)
class PreparedAttachmentSave:
    """Immutable, exact-path attachment preview used for confirmation."""

    owner_reference: str
    attachment: AttachmentRowView
    destination: Path
    collision_policy: CollisionPolicy
    overwrite: bool
    requires_confirmation: bool
    confirmation_scope: str
    expected_sha256: str | None
    warnings: tuple[str, ...]
    service_plan: AttachmentSavePlan | None = None


@dataclass(frozen=True, slots=True)
class SavedAttachmentView:
    reference: str
    name: str
    path: Path
    bytes_written: int
    sha256: str | None
    warnings: tuple[str, ...]


class DraftAttachmentWorkspaceBackend(Protocol):
    """Presentation-neutral contract consumed by :class:`DraftAttachmentScreen`."""

    @property
    def online(self) -> bool: ...

    @property
    def server_drafts_available(self) -> bool: ...

    @property
    def server_attachments_available(self) -> bool: ...

    def list_drafts(
        self,
        *,
        query: str,
        locations: tuple[str, ...],
        older_than_days: int | None,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
        offset: int = 0,
    ) -> DraftWorkspaceSnapshot: ...

    def create_draft(self, *, cancel: Event, progress: ProgressReporter) -> DraftOperationView: ...

    def duplicate_draft(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftOperationView: ...

    def delete_local_draft(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> DraftOperationView: ...

    def synchronize_draft(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftOperationView: ...

    def conflict(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftConflictView: ...

    def resolve_conflict(
        self,
        reference: str,
        *,
        use: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView: ...

    def export_draft(
        self,
        reference: str,
        destination: Path,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView: ...

    def list_attachments(
        self,
        owner_reference: str,
        *,
        query: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> AttachmentWorkspaceSnapshot: ...

    def prepare_attachment_save(
        self,
        owner_reference: str,
        attachment_reference: str,
        destination: Path,
        *,
        collision: str,
        expected_sha256: str | None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> PreparedAttachmentSave: ...

    def prepare_all_attachment_saves(
        self,
        owner_reference: str,
        destination: Path,
        *,
        collision: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> tuple[PreparedAttachmentSave, ...]: ...

    def execute_attachment_saves(
        self,
        prepared: Sequence[PreparedAttachmentSave],
        *,
        confirmed: bool,
        calculate_sha256: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> tuple[SavedAttachmentView, ...]: ...

    def export_attachment_metadata(
        self,
        owner_reference: str,
        destination: Path,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView: ...


def _cancelled(cancel: Event, action: str) -> None:
    if cancel.is_set():
        raise PublicOperationError(f"{action} was cancelled.", retryable=True)


def _preview(value: str, *, limit: int = 280) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}..."


def _recipients(draft: DraftData) -> tuple[str, ...]:
    return tuple(address.email for address in (*draft.to, *draft.cc, *draft.bcc))


def _local_attachment_reference(path: Path, index: int) -> str:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:16]
    return f"local-{index + 1}-{digest}"


def _validate_limit(limit: int) -> int:
    if limit < 1:
        raise ConfigurationError("The workspace result limit must be positive.")
    return min(limit, MAX_WORKSPACE_ROWS)


def _validate_sha256(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ConfigurationError("Expected SHA-256 must contain 64 hexadecimal digits.")
    return normalized


def _write_json_safely(directory: Path, filename: str, payload: Mapping[str, object]) -> Path:
    resolved = directory.expanduser().resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise ConfigurationError("Export destination is not a directory.")
    resolved.mkdir(parents=True, exist_ok=True)
    normalized = safe_filename(filename, fallback="export.json")
    if not normalized.casefold().endswith(".json"):
        normalized += ".json"
    target = unique_download_path(resolved, normalized, overwrite=False)
    if target.resolve(strict=False).parent != resolved:
        raise ConfigurationError("Export target would escape its destination.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=resolved
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except OSError:
            with target.open("xb"):
                pass
            try:
                os.replace(temporary, target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        else:
            temporary.unlink()
        return target
    finally:
        temporary.unlink(missing_ok=True)


class ServiceDraftAttachmentBackend:
    """Operational adapter backed by the project's existing service layer."""

    def __init__(
        self,
        drafts: DraftService,
        *,
        synchronizer: DraftSyncCoordinator | None = None,
        attachments: AttachmentService | None = None,
        online: bool = True,
        maximum_local_copy_bytes: int | None = None,
    ) -> None:
        self.drafts = drafts
        self.synchronizer = synchronizer
        self.attachments = attachments
        self._online = online
        configured_limit = maximum_local_copy_bytes or drafts.maximum_attachment_bytes
        self.maximum_local_copy_bytes = configured_limit or 100 * 1024 * 1024

    @property
    def online(self) -> bool:
        return self._online

    @online.setter
    def online(self, value: bool) -> None:
        self._online = bool(value)

    @property
    def server_drafts_available(self) -> bool:
        return self.synchronizer is not None

    @property
    def server_attachments_available(self) -> bool:
        return self.attachments is not None

    def _capabilities(self) -> tuple[WorkspaceCapability, ...]:
        sync_available = self.synchronizer is not None and self.online
        attachment_available = self.attachments is not None and self.online
        return (
            WorkspaceCapability(
                "local_drafts",
                True,
                False,
                "Revisioned local drafts and recovery copies are available.",
            ),
            WorkspaceCapability(
                "server_draft_sync",
                sync_available,
                True,
                (
                    "Exchange draft synchronization is available."
                    if sync_available
                    else "Exchange draft synchronization is offline."
                    if self.synchronizer is not None
                    else "No server-draft capability was supplied."
                ),
            ),
            WorkspaceCapability(
                "server_attachments",
                attachment_available,
                True,
                (
                    "Server attachment metadata and downloads are available."
                    if attachment_available
                    else "Server attachment access is offline."
                    if self.attachments is not None
                    else "No server-attachment capability was supplied."
                ),
            ),
        )

    def _sync_metadata(self, draft: DraftData) -> DraftSyncMetadata | None:
        if self.synchronizer is None:
            return None
        try:
            return self.synchronizer.metadata(draft.reference)
        except (ItemNotFoundError, ConflictError):
            return None

    def _draft_view(self, draft: DraftData, *, content_hash: str | None = None) -> DraftRowView:
        metadata = self._sync_metadata(draft)
        sync_state = metadata.state.value if metadata is not None else draft.location.value
        conflict_reason = metadata.conflict_reason if metadata is not None else None
        return DraftRowView(
            reference=draft.reference,
            content_hash=content_hash or draft_content_hash(draft),
            subject=draft.subject,
            recipients=_recipients(draft),
            body_preview=_preview(draft.body_text),
            location=draft.location.value,
            sync_state=sync_state,
            revision=draft.revision,
            updated_at=(
                draft.updated_at.astimezone(UTC).isoformat()
                if draft.updated_at is not None and draft.updated_at.tzinfo is not None
                else draft.updated_at.replace(tzinfo=UTC).isoformat()
                if draft.updated_at is not None
                else None
            ),
            attachment_count=len(draft.attachment_paths),
            server_id_present=bool(draft.server_id),
            conflicted=(
                metadata.state is DraftSyncState.CONFLICTED
                if metadata is not None
                else draft.location is DraftLocation.CONFLICTED
            ),
            conflict_reason=conflict_reason,
        )

    def list_drafts(
        self,
        *,
        query: str,
        locations: tuple[str, ...],
        older_than_days: int | None,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
        offset: int = 0,
    ) -> DraftWorkspaceSnapshot:
        bounded = _validate_limit(limit)
        _cancelled(cancel, "Loading drafts")
        progress(OperationProgress("Loading local draft index", completed=0, total=None))
        normalized_locations = tuple(
            DraftLocation(item.strip().casefold()) for item in locations if item.strip()
        )
        cutoff = None
        if older_than_days is not None:
            if not 1 <= older_than_days <= 36_500:
                raise ConfigurationError("Draft age must be between 1 and 36500 days.")
            cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

        page = self.drafts.list_page(
            query=query,
            locations=normalized_locations,
            updated_before=cutoff,
            limit=bounded,
            offset=offset,
            cancel=cancel,
        )
        selected = page.items
        views: list[DraftRowView] = []
        for index, draft in enumerate(selected, start=1):
            _cancelled(cancel, "Loading drafts")
            views.append(
                self._draft_view(
                    draft,
                    content_hash=page.content_hashes[index - 1] if page.content_hashes else None,
                )
            )
            progress(
                OperationProgress("Loading local drafts", completed=index, total=len(selected))
            )
        return DraftWorkspaceSnapshot(
            drafts=tuple(views),
            matched_count=page.total,
            truncated=page.next_offset is not None,
            capabilities=self._capabilities(),
            offset=page.offset,
            next_offset=page.next_offset,
        )

    def create_draft(self, *, cancel: Event, progress: ProgressReporter) -> DraftOperationView:
        _cancelled(cancel, "Creating draft")
        progress(OperationProgress("Creating revisioned local draft", completed=0, total=1))
        draft = self.drafts.create()
        progress(OperationProgress("Created local draft", completed=1, total=1))
        return DraftOperationView(draft.reference, "Created a revisioned local draft.")

    def duplicate_draft(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftOperationView:
        _cancelled(cancel, "Duplicating draft")
        progress(OperationProgress("Copying draft and attachment manifest", completed=0, total=1))
        draft = self.drafts.duplicate(reference)
        progress(OperationProgress("Duplicated local draft", completed=1, total=1))
        return DraftOperationView(draft.reference, f"Duplicated local draft {reference}.")

    def delete_local_draft(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> DraftOperationView:
        _cancelled(cancel, "Deleting local draft")
        progress(OperationProgress("Removing local draft and recovery copy", completed=0, total=1))
        removed = self.drafts.delete(
            reference,
            remove_recovery=True,
            expected_revision=expected_revision,
            expected_hash=expected_hash,
        )
        if not removed:
            raise ItemNotFoundError(f"Draft not found: {reference}")
        progress(OperationProgress("Removed local draft", completed=1, total=1))
        return DraftOperationView(
            reference,
            "Removed only the local draft and recovery copy; no Exchange item was deleted.",
        )

    def synchronize_draft(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftOperationView:
        if self.synchronizer is None:
            raise PublicOperationError(
                "Server draft synchronization is unavailable for this session.", retryable=False
            )
        if not self.online:
            raise OfflineOperationError("Server draft synchronization requires an online session.")
        _cancelled(cancel, "Synchronizing draft")
        progress(OperationProgress("Saving draft to Exchange", completed=0, total=1))
        draft = self.synchronizer.sync_to_server(reference)
        progress(OperationProgress("Draft synchronized", completed=1, total=1))
        return DraftOperationView(
            draft.reference,
            "Synchronized the selected draft in place with Exchange.",
        )

    def conflict(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> DraftConflictView:
        if self.synchronizer is None:
            raise PublicOperationError("Draft conflict metadata is unavailable.", retryable=False)
        _cancelled(cancel, "Loading draft conflict")
        local = self.drafts.get(reference)
        metadata = self.synchronizer.metadata(reference)
        if not metadata.conflicted or metadata.remote_draft is None:
            raise ConflictError("The selected draft has no unresolved server conflict.")
        from gucmail.services.drafts import draft_from_dict

        remote = draft_from_dict(metadata.remote_draft)
        progress(OperationProgress("Loaded local and server conflict copies", completed=1, total=1))
        return DraftConflictView(
            reference=reference,
            reason=metadata.conflict_reason or "Local and server copies differ.",
            detected_at=(
                metadata.conflict_detected_at.isoformat()
                if metadata.conflict_detected_at is not None
                else None
            ),
            local_subject=local.subject,
            server_subject=remote.subject,
            local_recipients=_recipients(local),
            server_recipients=_recipients(remote),
            local_body_preview=_preview(local.body_text, limit=800),
            server_body_preview=_preview(remote.body_text, limit=800),
            local_attachment_count=len(local.attachment_paths),
            server_attachment_count=len(remote.attachment_paths),
        )

    def resolve_conflict(
        self,
        reference: str,
        *,
        use: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView:
        if self.synchronizer is None:
            raise PublicOperationError("Draft conflict resolution is unavailable.", retryable=False)
        _cancelled(cancel, "Resolving draft conflict")
        normalized = use.strip().casefold()
        if normalized not in {"local", "server"}:
            raise ConfigurationError("Conflict resolution must choose local or server.")
        progress(OperationProgress(f"Selecting {normalized} conflict copy", completed=0, total=1))
        draft = self.synchronizer.resolve_conflict(reference, use=normalized)
        progress(OperationProgress("Resolved draft conflict", completed=1, total=1))
        return DraftOperationView(
            draft.reference,
            f"Resolved the conflict using the {normalized} copy. No message was sent.",
        )

    def export_draft(
        self,
        reference: str,
        destination: Path,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView:
        _cancelled(cancel, "Exporting draft")
        draft = self.drafts.get(reference)
        metadata = self._sync_metadata(draft)
        payload: dict[str, object] = {
            "format": "gucmail-draft-export-v1",
            "draft": draft_to_dict(draft),
            "synchronization": metadata.as_dict() if metadata is not None else None,
        }
        progress(OperationProgress("Writing draft JSON atomically", completed=0, total=1))
        path = _write_json_safely(destination, f"{reference}.draft.json", payload)
        progress(OperationProgress("Exported draft JSON", completed=1, total=1))
        return DraftOperationView(reference, f"Exported draft metadata to {path}.", path)

    def _local_attachments(self, draft: DraftData) -> tuple[AttachmentRowView, ...]:
        rows: list[AttachmentRowView] = []
        for index, path in enumerate(draft.attachment_paths):
            expanded = path.expanduser().resolve(strict=False)
            try:
                size = expanded.stat().st_size if expanded.is_file() else None
            except OSError:
                size = None
            rows.append(
                AttachmentRowView(
                    owner_reference=draft.reference,
                    reference=_local_attachment_reference(expanded, index),
                    name=expanded.name,
                    size=size,
                    content_type=None,
                    source="local",
                    dangerous=is_dangerous_attachment(expanded.name),
                    is_inline=False,
                    is_item_attachment=False,
                    local_path=expanded,
                )
            )
        return tuple(rows)

    @staticmethod
    def _server_attachment(owner: str, attachment: AttachmentInfo) -> AttachmentRowView:
        return AttachmentRowView(
            owner_reference=owner,
            reference=attachment.reference,
            name=attachment.name,
            size=attachment.size,
            content_type=attachment.content_type,
            source="server",
            dangerous=attachment.dangerous or is_dangerous_attachment(attachment.name),
            is_inline=attachment.is_inline,
            is_item_attachment=attachment.is_item_attachment,
            content_id=attachment.content_id,
        )

    def list_attachments(
        self,
        owner_reference: str,
        *,
        query: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> AttachmentWorkspaceSnapshot:
        reference = owner_reference.strip()
        if not reference:
            raise ConfigurationError("A message or draft reference is required.")
        bounded = _validate_limit(limit)
        _cancelled(cancel, "Loading attachment metadata")
        local: tuple[AttachmentRowView, ...] = ()
        if self.drafts.exists(reference):
            local = self._local_attachments(self.drafts.get(reference))
        server: tuple[AttachmentRowView, ...] = ()
        server_truncated = False
        if self.attachments is not None and self.online:
            remaining = max(0, bounded - min(len(local), bounded))
            offset = 0
            rows: list[AttachmentRowView] = []
            while remaining:
                page_limit = min(remaining, MAX_PAGE_SIZE)
                page = self.attachments.list_attachments(
                    reference,
                    limit=page_limit,
                    offset=offset,
                )
                rows.extend(self._server_attachment(reference, item) for item in page.items)
                offset += len(page.items)
                remaining -= len(page.items)
                if not page.has_more or not page.items:
                    break
                if remaining == 0:
                    server_truncated = True
            server = tuple(rows)
        elif not local and self.attachments is None:
            raise PublicOperationError(
                "No local draft attachments exist and server attachment access is unavailable.",
                retryable=False,
            )
        elif not local and not self.online:
            raise OfflineOperationError(
                "No local draft attachments exist; server attachment metadata "
                "requires online access."
            )
        combined_rows = (*local, *server)
        needle = query.strip().casefold()
        if needle:
            combined_rows = tuple(
                row
                for row in combined_rows
                if needle
                in f"{row.name} {row.content_type or ''} {row.source} {row.reference}".casefold()
            )
        matched_count = len(combined_rows) + (1 if server_truncated else 0)
        selected = tuple(combined_rows[:bounded])
        progress(
            OperationProgress(
                "Loaded attachment metadata", completed=len(selected), total=matched_count
            )
        )
        return AttachmentWorkspaceSnapshot(
            owner_reference=reference,
            attachments=selected,
            matched_count=matched_count,
            truncated=matched_count > len(selected),
            capabilities=self._capabilities(),
        )

    def _find_attachment_row(self, owner: str, reference: str) -> AttachmentRowView:
        snapshot = self.list_attachments(
            owner,
            query="",
            limit=MAX_WORKSPACE_ROWS,
            cancel=Event(),
            progress=lambda _progress: None,
        )
        matches = [row for row in snapshot.attachments if row.reference == reference]
        if len(matches) != 1:
            raise ItemNotFoundError(f"Attachment not found: {reference}")
        return matches[0]

    def prepare_attachment_save(
        self,
        owner_reference: str,
        attachment_reference: str,
        destination: Path,
        *,
        collision: str,
        expected_sha256: str | None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> PreparedAttachmentSave:
        return self._prepare_attachment_save(
            owner_reference,
            attachment_reference,
            destination,
            collision=collision,
            expected_sha256=expected_sha256,
            cancel=cancel,
            progress=progress,
        )

    def _prepare_attachment_save(
        self,
        owner_reference: str,
        attachment_reference: str,
        destination: Path,
        *,
        collision: str,
        expected_sha256: str | None,
        cancel: Event,
        progress: ProgressReporter,
        reserved_targets: Sequence[Path] = (),
        attachment_row: AttachmentRowView | None = None,
    ) -> PreparedAttachmentSave:
        _cancelled(cancel, "Preparing attachment download")
        row = attachment_row or self._find_attachment_row(owner_reference, attachment_reference)
        policy = CollisionPolicy(collision)
        digest = _validate_sha256(expected_sha256)
        if row.source == "server":
            if self.attachments is None:
                raise PublicOperationError(
                    "Server attachment access is unavailable.", retryable=False
                )
            if not self.online:
                raise OfflineOperationError("Server attachment downloads require online access.")
            metadata = AttachmentInfo(
                reference=row.reference,
                name=row.name,
                size=row.size,
                content_type=row.content_type,
                is_inline=row.is_inline,
                is_item_attachment=row.is_item_attachment,
                content_id=row.content_id,
                dangerous=row.dangerous,
            )
            service_plan = self.attachments.plan_save(
                owner_reference,
                row.reference,
                destination,
                collision=policy,
                expected_sha256=digest,
                attachment=metadata,
                reserved_targets=reserved_targets,
            )
            prepared = PreparedAttachmentSave(
                owner_reference=owner_reference,
                attachment=row,
                destination=service_plan.target,
                collision_policy=policy,
                overwrite=service_plan.overwrite,
                requires_confirmation=service_plan.requires_confirmation,
                confirmation_scope=service_plan.confirmation_scope,
                expected_sha256=digest,
                warnings=service_plan.warnings,
                service_plan=service_plan,
            )
        else:
            prepared = self._prepare_local(row, destination, policy, digest, reserved_targets)
        progress(OperationProgress("Prepared exact attachment target", completed=1, total=1))
        return prepared

    def _prepare_local(
        self,
        row: AttachmentRowView,
        destination: Path,
        policy: CollisionPolicy,
        digest: str | None,
        reserved_targets: Sequence[Path] = (),
    ) -> PreparedAttachmentSave:
        source = row.local_path
        if source is None or not source.is_file():
            raise ItemNotFoundError(f"Local attachment is unavailable: {row.name}")
        size = source.stat().st_size
        if size > self.maximum_local_copy_bytes:
            raise ConfigurationError("Local attachment exceeds the configured copy limit.")
        directory = destination.expanduser().resolve(strict=False)
        if directory.exists() and not directory.is_dir():
            raise ConfigurationError("Attachment destination is not a directory.")
        normalized_name = safe_filename(row.name)
        candidate = directory / normalized_name
        if candidate.parent != directory:
            raise ConfigurationError("Attachment target would escape its destination.")
        if candidate.is_symlink():
            raise ConfigurationError("Refusing to replace an attachment destination symlink.")
        if policy is CollisionPolicy.ERROR and candidate.exists():
            raise ConflictError(f"Destination file already exists: {candidate.name}")
        if policy is not CollisionPolicy.RENAME and os.path.normcase(str(candidate)) in {
            os.path.normcase(str(path)) for path in reserved_targets
        }:
            raise ConflictError(
                f"Multiple attachments would use the same destination: {candidate.name}. "
                "Choose rename collisions or save them individually with distinct names."
            )
        target = (
            unique_download_path(directory, normalized_name, reserved=reserved_targets)
            if policy is CollisionPolicy.RENAME
            else candidate
        )
        overwrite = policy is CollisionPolicy.OVERWRITE
        requires_confirmation = overwrite and target.exists()
        warnings: list[str] = []
        if normalized_name != row.name:
            warnings.append(f"Unsafe filename was normalized to {normalized_name!r}.")
        if row.dangerous:
            warnings.append("This file type can execute code. It will be copied but never opened.")
        scope_payload = f"{row.owner_reference}\0{row.reference}\0{target}\0{overwrite}"
        scope = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest()
        return PreparedAttachmentSave(
            owner_reference=row.owner_reference,
            attachment=row,
            destination=target,
            collision_policy=policy,
            overwrite=overwrite,
            requires_confirmation=requires_confirmation,
            confirmation_scope=scope,
            expected_sha256=digest,
            warnings=tuple(warnings),
        )

    def prepare_all_attachment_saves(
        self,
        owner_reference: str,
        destination: Path,
        *,
        collision: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> tuple[PreparedAttachmentSave, ...]:
        snapshot = self.list_attachments(
            owner_reference,
            query="",
            limit=limit,
            cancel=cancel,
            progress=progress,
        )
        prepared: list[PreparedAttachmentSave] = []
        for index, row in enumerate(snapshot.attachments, start=1):
            _cancelled(cancel, "Preparing attachment downloads")
            prepared.append(
                self._prepare_attachment_save(
                    owner_reference,
                    row.reference,
                    destination,
                    collision=collision,
                    expected_sha256=None,
                    cancel=cancel,
                    progress=lambda _value: None,
                    reserved_targets=tuple(plan.destination for plan in prepared),
                    attachment_row=row,
                )
            )
            progress(
                OperationProgress(
                    "Preparing attachment downloads",
                    completed=index,
                    total=len(snapshot.attachments),
                )
            )
        return tuple(prepared)

    def execute_attachment_saves(
        self,
        prepared: Sequence[PreparedAttachmentSave],
        *,
        confirmed: bool,
        calculate_sha256: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> tuple[SavedAttachmentView, ...]:
        if any(plan.requires_confirmation for plan in prepared) and not confirmed:
            raise PublicOperationError(
                "Overwriting existing attachment targets requires explicit confirmation.",
                retryable=False,
            )
        saved: list[SavedAttachmentView] = []
        for index, plan in enumerate(prepared, start=1):
            _cancelled(cancel, "Saving attachments")

            attachment_name = plan.attachment.name

            def report(
                download: DownloadProgress,
                name: str = attachment_name,
            ) -> None:
                progress(
                    OperationProgress(
                        f"Downloading {name}",
                        completed=download.bytes_downloaded,
                        total=download.total_bytes,
                    )
                )

            if plan.service_plan is not None:
                if self.attachments is None or not self.online:
                    raise OfflineOperationError(
                        "Server attachment downloads require online access."
                    )
                result = self.attachments.execute_save(
                    plan.service_plan,
                    confirmation_scope=(
                        plan.confirmation_scope
                        if plan.requires_confirmation and confirmed
                        else None
                    ),
                    calculate_sha256=calculate_sha256,
                    progress=report,
                )
                if result.path is None:
                    raise PublicOperationError("Attachment download did not produce a file.")
                view = SavedAttachmentView(
                    reference=plan.attachment.reference,
                    name=plan.attachment.name,
                    path=result.path,
                    bytes_written=result.bytes_written,
                    sha256=result.sha256,
                    warnings=result.warnings,
                )
            else:
                view = self._copy_local(plan, calculate_sha256=calculate_sha256)
            saved.append(view)
            progress(OperationProgress("Saving attachments", completed=index, total=len(prepared)))
        return tuple(saved)

    def _copy_local(
        self, plan: PreparedAttachmentSave, *, calculate_sha256: bool
    ) -> SavedAttachmentView:
        source = plan.attachment.local_path
        if source is None or not source.is_file():
            raise ItemNotFoundError(f"Local attachment is unavailable: {plan.attachment.name}")
        directory = plan.destination.parent.resolve(strict=False)
        target = plan.destination.resolve(strict=False)
        if (
            directory != plan.destination.parent
            or target != plan.destination
            or plan.destination.is_symlink()
        ):
            raise ConflictError("The attachment target changed after its preview was created.")
        if target.parent != directory:
            raise ConfigurationError("Attachment target no longer stays in its destination.")
        if target.exists() and (not plan.overwrite or not plan.requires_confirmation):
            raise ConflictError(f"Destination appeared after preview: {target.name}")
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".part", dir=directory
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
                self._copy_stream(input_stream, output_stream, digest, plan)
                total = output_stream.tell()
                output_stream.flush()
                os.fsync(output_stream.fileno())
            checksum = digest.hexdigest()
            if plan.expected_sha256 and not hmac.compare_digest(checksum, plan.expected_sha256):
                raise ConflictError("Copied attachment did not match the expected SHA-256.")
            _commit_download(
                temporary,
                target,
                overwrite=plan.overwrite and plan.requires_confirmation,
            )
            return SavedAttachmentView(
                reference=plan.attachment.reference,
                name=plan.attachment.name,
                path=target,
                bytes_written=total,
                sha256=checksum if calculate_sha256 or plan.expected_sha256 else None,
                warnings=plan.warnings,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _copy_stream(
        self,
        source: BinaryIO,
        destination: BinaryIO,
        digest: hashlib._Hash,
        plan: PreparedAttachmentSave,
    ) -> None:
        total = 0
        while True:
            chunk = source.read(LOCAL_COPY_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > self.maximum_local_copy_bytes:
                raise ConfigurationError("Local attachment exceeded the configured copy limit.")
            destination.write(chunk)
            digest.update(chunk)

    def export_attachment_metadata(
        self,
        owner_reference: str,
        destination: Path,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DraftOperationView:
        snapshot = self.list_attachments(
            owner_reference,
            query="",
            limit=limit,
            cancel=cancel,
            progress=progress,
        )
        payload: dict[str, object] = {
            "format": "gucmail-attachment-metadata-v1",
            "owner_reference": owner_reference,
            "truncated": snapshot.truncated,
            "attachments": [
                {
                    "reference": row.reference,
                    "name": row.name,
                    "size": row.size,
                    "content_type": row.content_type,
                    "source": row.source,
                    "dangerous": row.dangerous,
                    "is_inline": row.is_inline,
                    "is_item_attachment": row.is_item_attachment,
                    "content_id": row.content_id,
                    "local_path": str(row.local_path) if row.local_path is not None else None,
                }
                for row in snapshot.attachments
            ],
        }
        path = _write_json_safely(
            destination,
            f"{owner_reference}.attachments.json",
            payload,
        )
        return DraftOperationView(
            owner_reference,
            f"Exported attachment metadata to {path}.",
            path,
        )


__all__ = [
    "AttachmentRowView",
    "AttachmentWorkspaceSnapshot",
    "DraftAttachmentWorkspaceBackend",
    "DraftConflictView",
    "DraftOperationView",
    "DraftRowView",
    "DraftWorkspaceSnapshot",
    "PreparedAttachmentSave",
    "SavedAttachmentView",
    "ServiceDraftAttachmentBackend",
    "WorkspaceCapability",
]
