"""Production adapters for the operational Textual product-area screens.

The screen modules expose deliberately small protocols.  This module is the
composition boundary that maps those protocols onto the shared application
services and durable state.  Every online dependency is lazy: constructing an
adapter, opening Settings, browsing a local backup, or viewing local Activity
does not resolve credentials or initialize Exchange.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, cast
from uuid import uuid4

from gucmail.backup_format import BackupManifest
from gucmail.errors import (
    ConfigurationError,
    GUCmailError,
    ItemNotFoundError,
    MutationExecutionError,
    UnsupportedFeatureError,
)
from gucmail.headless import (
    WatcherRunState,
    WatcherStatus,
    WatcherStatusStore,
    process_is_alive,
    start_watcher_process,
)
from gucmail.models import MailboxOperationStatus, NotificationEvent, QuotaValue
from gucmail.security import redact_text, sanitize_terminal
from gucmail.services.backup import (
    BackupKind,
    BackupProgress,
    BackupReadCancelledError,
    BackupRequest,
    BackupResult,
    BackupSelection,
    BackupService,
    BackupSource,
    OfflineItem,
    PrunePlan,
    PruneResult,
    RestorePlan,
    RestoreResult,
    RestoreTarget,
    VerificationReport,
)
from gucmail.services.cleanup import (
    CleanupCandidate,
    CleanupPlan,
    create_cleanup_plan,
)
from gucmail.services.cleanup import (
    execute_cleanup_plan as run_cleanup_plan,
)
from gucmail.services.drafts import DraftService, build_attachment_manifest
from gucmail.services.index import SearchIndex
from gucmail.services.insights import (
    InsightAnalyzer,
    InsightMessage,
    StorageReport,
    duplicate_attachment_insights,
    duplicate_insights,
)
from gucmail.services.messages import MessageService
from gucmail.services.monitoring_preferences import configured_watcher
from gucmail.services.notifications import (
    ConsoleNotificationProvider,
    NotificationDispatcher,
    NotificationProvider,
)
from gucmail.services.quota import (
    QuotaReport,
    QuotaService,
    quota_service_from_settings,
)
from gucmail.services.startup_runtime import context_watcher_process
from gucmail.settings import (
    MANAGED_SETTINGS,
    UNAVAILABLE_SETTINGS,
    EffectiveSetting,
    Sensitivity,
    SettingsContext,
    SettingsDiff,
    SettingsImportPlan,
    SettingsRevision,
    SettingsService,
)
from gucmail.settings import (
    SettingLevel as ServiceSettingLevel,
)
from gucmail.state import StateStore
from gucmail.tui.activity_screen import (
    ActivityClearPlanView,
    ActivityClearResultView,
    ActivityEventView,
    ActivitySnapshotView,
    ActivityUndoEntryView,
    ActivityUndoPlanView,
    ActivityUndoResultView,
    WatcherStatusView,
)
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
)
from gucmail.tui.backup_screen import (
    BackupCreateRequest,
    BackupEstimateView,
    BackupItemView,
    BackupPrunePlanView,
    BackupPruneResultView,
    BackupRecordView,
    BackupVerificationView,
    RestorePlanEntryView,
    RestorePlanView,
    RestoreResultView,
)
from gucmail.tui.settings_screen import (
    SettingLevel,
    SettingsApplyView,
    SettingsCatalogView,
    SettingsExportView,
    SettingsImportChangeView,
    SettingsImportPreviewView,
    SettingsPreviewView,
    SettingsRevisionView,
    SettingValidationView,
    SettingValue,
    SettingView,
    display_setting_value,
)
from gucmail.tui.storage_screen import (
    CleanupExecutionView,
    CleanupPlanEntryView,
    CleanupPlanView,
    QuotaThresholdView,
    StorageAnalysisView,
    StorageSnapshotView,
    StorageUsageRow,
)

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext


BackupFolderOpener: TypeAlias = Callable[[Path], None]


BackupSourceFactory: TypeAlias = Callable[[], BackupSource]
BackupPassphraseProvider: TypeAlias = Callable[[str, str], str | bytes | None]
PreviewReverter: TypeAlias = Callable[[], None]
PreviewHandler: TypeAlias = Callable[[SettingValue], PreviewReverter | None]
StorageMessageLoader: TypeAlias = Callable[[str, int], Sequence[object]]
StorageAttachmentLoader: TypeAlias = Callable[[int], Sequence[object]]
CleanupExecutor: TypeAlias = Callable[[CleanupCandidate], object]
CleanupRestorer: TypeAlias = Callable[[str, str], object]
WatcherLauncher: TypeAlias = Callable[[], WatcherStatus | None]
WatcherSynchronizer: TypeAlias = Callable[[], WatcherStatus | None]
NotificationTest: TypeAlias = Callable[[str], ActivityEventView]
DeliveryRetry: TypeAlias = Callable[[str], ActivityEventView]

_UNWRITTEN_REVISION = "unwritten"


def _cancelled(cancel: Event, action: str) -> None:
    if cancel.is_set():
        raise PublicOperationError(f"{action} was cancelled safely.", retryable=False)


def _safe_error(action: str, error: BaseException) -> PublicOperationError:
    if isinstance(error, PublicOperationError):
        return error
    if isinstance(error, (GUCmailError, ConfigurationError, OSError, ValueError, KeyError)):
        message = redact_text(str(error), redact_emails=True).strip()
        return PublicOperationError(f"{action} failed: {message[:500] or type(error).__name__}.")
    return PublicOperationError(
        f"{action} failed ({sanitize_terminal(type(error).__name__, max_length=80)}). "
        "Use sanitized diagnostics for details."
    )


def _atomic_text(path: Path, content: str) -> Path:
    """Write an export atomically without following a target symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise OSError("Refusing to replace a symbolic-link export target.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _open_folder_in_file_manager(path: Path) -> None:
    """Open one validated directory without invoking a command shell."""

    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if not callable(startfile):
            raise OSError("The Windows file-manager launcher is unavailable.")
        startfile(str(path))
        return
    command = ["open", "--", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(  # noqa: S603 - fixed executable plus one validated absolute path
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _directory_size(path: Path, cancel: Event, *, maximum_files: int = 100_000) -> tuple[int, int]:
    """Return a bounded, symlink-safe directory size and number of skipped files."""

    if not path.exists() or path.is_symlink():
        return 0, 0
    total = 0
    visited = 0
    skipped = 0
    pending = [path]
    while pending:
        _cancelled(cancel, "Storage analysis")
        current = pending.pop()
        try:
            entries = tuple(os.scandir(current))
        except OSError:
            skipped += 1
            continue
        for entry in entries:
            if visited >= maximum_files:
                return total, skipped + 1
            visited += 1
            try:
                if entry.is_symlink():
                    skipped += 1
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                skipped += 1
    return total, skipped


class _LazyRestoreTarget:
    """Resolve one provider only after the service validates and claims a create."""

    def __init__(self, loader: Callable[[], BackupSource]):
        self.loader = loader
        self.target: RestoreTarget | None = None

    def restore_item(self, item: OfflineItem, destination_folder: str) -> str:
        if self.target is None:
            candidate = self.loader()
            if not isinstance(candidate, RestoreTarget):
                raise UnsupportedFeatureError("The configured provider cannot restore items.")
            self.target = candidate
        return self.target.restore_item(item, destination_folder)


class BackupAreaBackend:
    """Map :class:`BackupService` to the Backup Center protocol.

    ``source_factory`` is invoked for estimate/create/resume and restore dispatch. Local list,
    verification, browsing and restore planning never resolve the Exchange
    gateway.  Passphrases are requested per operation and are never retained.
    """

    def __init__(
        self,
        repository_root: Path | str,
        *,
        state: object | None = None,
        source_factory: BackupSourceFactory | None = None,
        passphrase_provider: BackupPassphraseProvider | None = None,
        folder_opener: BackupFolderOpener | None = None,
        browse_limit: int = 250,
    ) -> None:
        if not 1 <= browse_limit <= 5_000:
            raise ValueError("Backup browse limit must be between 1 and 5000.")
        self.repository_root = Path(repository_root).expanduser().resolve(strict=False)
        self.state = state
        self.source_factory = source_factory
        self.passphrase_provider = passphrase_provider
        self.folder_opener = folder_opener or _open_folder_in_file_manager
        self.browse_limit = browse_limit
        self._active_root = self.repository_root
        self._verified: dict[tuple[Path, str], str] = {}
        self._lock = RLock()

    @classmethod
    def from_context(
        cls,
        context: AppContext,
        *,
        online: bool,
        repository_root: Path | str | None = None,
        passphrase_provider: BackupPassphraseProvider | None = None,
        browse_limit: int = 250,
    ) -> BackupAreaBackend:
        source_factory: BackupSourceFactory | None = None
        if online:
            source_factory = cast(
                BackupSourceFactory,
                lambda: context.gateway(interactive=True),
            )
        return cls(
            repository_root or context.paths.backup_dir,
            state=context.state,
            source_factory=source_factory,
            passphrase_provider=passphrase_provider,
            browse_limit=browse_limit,
        )

    def _service(self, root: Path | None = None) -> BackupService:
        return BackupService(root or self._active_root, state=self.state)

    def _manifest_and_passphrase(
        self,
        service: BackupService,
        backup_id: str,
        purpose: str,
        *,
        supplied: str | bytes | None = None,
        allow_incomplete: bool = False,
    ) -> tuple[BackupManifest, str | bytes | None]:
        summary = next(
            (
                item
                for item in service.list(include_incomplete=allow_incomplete, limit=10_000)
                if item.backup_id == backup_id
            ),
            None,
        )
        if summary is None:
            raise PublicOperationError(
                f"Backup not found: {sanitize_terminal(backup_id, max_length=120)}",
                retryable=False,
            )
        passphrase = self._passphrase(
            backup_id,
            purpose,
            required=summary.encrypted,
            supplied=supplied,
        )
        return (
            service.inspect(
                backup_id,
                passphrase=passphrase,
                allow_incomplete=allow_incomplete,
            ),
            passphrase,
        )

    def _root_for_request(self, request: BackupCreateRequest) -> Path:
        raw = Path(request.destination).expanduser()
        return raw.resolve(strict=False)

    def _source(self) -> BackupSource:
        if self.source_factory is None:
            raise OfflineOperationError(
                "A mailbox source is required for backup estimate, creation, or resume. "
                "Local backup browsing remains available."
            )
        source = self.source_factory()
        if not isinstance(source, BackupSource):
            raise PublicOperationError(
                "The configured backup source does not expose the required streaming contract."
            )
        return source

    def _passphrase(
        self,
        identifier: str,
        purpose: str,
        *,
        required: bool,
        supplied: str | bytes | None = None,
    ) -> str | bytes | None:
        if not required:
            return None
        if supplied:
            return supplied
        if self.passphrase_provider is None:
            raise PublicOperationError(
                "This encrypted backup requires a secure passphrase prompt. No prompt callback "
                "was configured, so the operation was not started.",
                retryable=False,
            )
        value = self.passphrase_provider(identifier, purpose)
        if not value:
            raise PublicOperationError(
                "No backup passphrase was supplied; the operation was cancelled safely.",
                retryable=False,
            )
        return value

    @staticmethod
    def _service_request(request: BackupCreateRequest) -> BackupRequest:
        content_types = tuple(
            {
                "mail": "message",
                "messages": "message",
                "calendar": "calendar",
                "events": "calendar",
                "contacts": "contact",
                "tasks": "task",
            }.get(value.casefold(), value.casefold())
            for value in request.content_types
        )
        return BackupRequest(
            kind=BackupKind(request.kind),
            selection=BackupSelection(
                folders=request.folders,
                messages=request.messages,
                item_kinds=content_types,
            ),
            source_label="mailbox",
        )

    @staticmethod
    def _progress(progress: ProgressReporter) -> Callable[[BackupProgress], None]:
        def report(value: BackupProgress) -> None:
            progress(
                OperationProgress(
                    value.phase,
                    value.processed_items,
                    None,
                    f"Stored {value.stored_objects} objects / {value.stored_bytes} bytes",
                )
            )

        return report

    def _record(self, summary: object, *, root: Path | None = None) -> BackupRecordView:
        active_root = root or self._active_root
        path = Path(cast(Any, summary).path)
        backup_id = str(cast(Any, summary).backup_id)
        state = str(cast(Any, summary).state)
        verification = self._verified.get((active_root, backup_id), "not verified")
        if state == "corrupt":
            verification = "invalid"
        return BackupRecordView(
            backup_id=backup_id,
            kind=str(cast(Any, summary).kind),
            state=state,
            created_at=cast(str | None, cast(Any, summary).created_at),
            completed_at=cast(str | None, cast(Any, summary).completed_at),
            item_count=int(cast(Any, summary).item_count),
            stored_bytes=int(cast(Any, summary).stored_bytes),
            destination=str(path.parent),
            encrypted=bool(cast(Any, summary).encrypted),
            verification_state=verification,
            warning=cast(str | None, getattr(summary, "error", None)),
        )

    def _result_record(self, result: BackupResult, *, root: Path) -> BackupRecordView:
        return BackupRecordView(
            backup_id=result.backup_id,
            kind=result.kind,
            state=result.state,
            created_at=result.manifest.created_at,
            completed_at=result.manifest.completed_at,
            item_count=result.item_count,
            stored_bytes=result.stored_bytes,
            destination=str(root),
            encrypted=result.encrypted,
            verification_state=self._verified.get((root, result.backup_id), "not verified"),
        )

    def list_backups(
        self, *, cancel: Event, progress: ProgressReporter
    ) -> Sequence[BackupRecordView]:
        try:
            _cancelled(cancel, "Loading backups")
            progress(OperationProgress("Scanning local manifests"))
            service = self._service()
            values = service.list(include_incomplete=True, limit=500)
            _cancelled(cancel, "Loading backups")
            return tuple(self._record(value) for value in values)
        except BaseException as exc:
            raise _safe_error("Loading backups", exc) from exc

    def estimate_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupEstimateView:
        try:
            _cancelled(cancel, "Estimating backup")
            service = self._service(self._root_for_request(request))
            effective = self._create_request(service, request, passphrase)
            source = self._source()
            progress(OperationProgress("Reading bounded mailbox metadata"))
            estimate = service.estimate(
                source,
                effective,
                encrypted=request.encrypted,
            )
            _cancelled(cancel, "Estimating backup")
            return BackupEstimateView(
                estimate.item_count,
                estimate.attachment_count,
                estimate.estimated_bytes,
                estimate.unknown_size_count,
                estimate.note,
            )
        except BaseException as exc:
            raise _safe_error("Estimating backup", exc) from exc

    def _create_request(
        self,
        service: BackupService,
        request: BackupCreateRequest,
        passphrase: str | bytes | None,
    ) -> BackupRequest:
        if request.kind != BackupKind.INCREMENTAL.value:
            return self._service_request(request)
        if not request.parent_backup_id:
            raise PublicOperationError(
                "Choose a complete parent archive for the incremental backup.",
                retryable=False,
            )
        return service.incremental_request(request.parent_backup_id, passphrase=passphrase)

    def create_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupRecordView:
        try:
            _cancelled(cancel, "Creating backup")
            root = self._root_for_request(request)
            passphrase = self._passphrase(
                str(root),
                "create",
                required=request.encrypted,
                supplied=passphrase,
            )
            service = self._service(root)
            effective = self._create_request(service, request, passphrase)
            source = self._source()
            if request.kind == BackupKind.INCREMENTAL.value:
                assert request.parent_backup_id is not None
                result = service.incremental(
                    source,
                    request.parent_backup_id,
                    sync_state={"requested_by": "tui"},
                    passphrase=passphrase,
                    progress=self._progress(progress),
                    cancelled=cancel.is_set,
                )
            else:
                result = service.create(
                    source,
                    effective,
                    passphrase=passphrase,
                    progress=self._progress(progress),
                    cancelled=cancel.is_set,
                )
            with self._lock:
                self._active_root = root
            if request.verify_after_create:
                report = service.verify(
                    result.backup_id,
                    passphrase=passphrase,
                    full=True,
                )
                self._verified[(root, result.backup_id)] = (
                    "verified" if report.valid and report.complete else "invalid"
                )
                report.raise_for_error()
            return self._result_record(result, root=root)
        except BaseException as exc:
            raise _safe_error("Creating backup", exc) from exc

    def resume_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupRecordView:
        try:
            _cancelled(cancel, "Resuming backup")
            service = self._service()
            _manifest, passphrase = self._manifest_and_passphrase(
                service,
                backup_id,
                "resume",
                supplied=passphrase,
                allow_incomplete=True,
            )
            result = service.resume(
                backup_id,
                self._source(),
                passphrase=passphrase,
                progress=self._progress(progress),
                cancelled=cancel.is_set,
            )
            return self._result_record(result, root=self._active_root)
        except BaseException as exc:
            raise _safe_error("Resuming backup", exc) from exc

    def verify_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupVerificationView:
        try:
            _cancelled(cancel, "Verifying backup")
            service = self._service()
            manifest, passphrase = self._manifest_and_passphrase(
                service,
                backup_id,
                "verify",
                supplied=passphrase,
                allow_incomplete=True,
            )
            progress(OperationProgress("Hashing stored backup objects"))
            report = service.verify(
                backup_id,
                passphrase=passphrase,
                full=True,
                allow_incomplete=manifest.state != "complete",
            )
            self._verified[(self._active_root, backup_id)] = (
                "verified" if report.valid and report.complete else "invalid"
            )
            return self._verification(report)
        except BaseException as exc:
            raise _safe_error("Verifying backup", exc) from exc

    @staticmethod
    def _verification(report: VerificationReport) -> BackupVerificationView:
        return BackupVerificationView(
            report.backup_id,
            report.valid,
            report.complete,
            report.checked_objects,
            report.checked_bytes,
            report.warnings,
            report.errors,
        )

    def _open_archive(self, backup_id: str, purpose: str):  # type: ignore[no-untyped-def]
        service = self._service()
        _manifest, passphrase = self._manifest_and_passphrase(
            service,
            backup_id,
            purpose,
        )
        return (
            service,
            service.open_offline(
                backup_id,
                passphrase=passphrase,
                verify=True,
            ),
            passphrase,
        )

    @staticmethod
    def _offline_item(item: OfflineItem) -> BackupItemView:
        metadata = item.metadata
        data = item.data if isinstance(item.data, Mapping) else {}

        def value(*names: str) -> object | None:
            for name in names:
                if name in metadata and metadata[name] is not None:
                    return cast(object, metadata[name])
                if name in data and data[name] is not None:
                    return cast(object, data[name])
            return None

        title = str(value("subject", "title", "name") or "(untitled)")
        timestamp = value("received_at", "sent_at", "start", "due_date", "updated_at")
        size = value("size_bytes", "size")
        numeric_size = int(size) if isinstance(size, (int, float)) and size >= 0 else None
        return BackupItemView(
            reference=item.reference,
            kind=item.kind,
            folder=item.folder_path or "Unfiled",
            title=title,
            timestamp=str(timestamp) if timestamp is not None else None,
            size_bytes=numeric_size,
            attachment_count=len(item.attachments),
            details=(
                f"Offline read-only item from backup {item.backup_id}. "
                "Bodies and attachment payloads are not rendered in this list."
            ),
        )

    def browse_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[BackupItemView]:
        try:
            _cancelled(cancel, "Opening backup")
            service = self._service()
            _manifest, effective_passphrase = self._manifest_and_passphrase(
                service,
                backup_id,
                "browse",
                supplied=passphrase,
            )
            archive = service.open_offline(
                backup_id,
                passphrase=effective_passphrase,
                verify=True,
            )
            progress(OperationProgress("Reading offline item index"))
            items: list[BackupItemView] = []
            for item in archive.iter_items(limit=self.browse_limit):
                _cancelled(cancel, "Opening backup")
                items.append(self._offline_item(item))
            return tuple(items)
        except BaseException as exc:
            raise _safe_error("Opening backup", exc) from exc

    def create_restore_plan(
        self,
        backup_id: str,
        item_references: tuple[str, ...],
        *,
        passphrase: str | bytes | None = None,
        allow_completed_duplicates: bool = False,
        cancel: Event,
        progress: ProgressReporter,
    ) -> RestorePlanView:
        try:
            _cancelled(cancel, "Generating restore plan")
            service = self._service()
            manifest, passphrase = self._manifest_and_passphrase(
                service,
                backup_id,
                "restore-plan",
                supplied=passphrase,
            )
            progress(OperationProgress("Comparing offline item metadata"))
            plan = service.plan_restore(
                backup_id,
                passphrase=passphrase,
                selected_references=item_references or None,
                allow_completed_duplicates=allow_completed_duplicates,
                cancelled=cancel,
            )
            _cancelled(cancel, "Generating restore plan")
            return self._restore_plan(plan, encrypted=manifest.encrypted, cancel=cancel)
        except BackupReadCancelledError as exc:
            raise PublicOperationError(
                "Generating restore plan was cancelled safely. No items were restored.",
                retryable=False,
            ) from exc
        except BaseException as exc:
            raise _safe_error("Generating restore plan", exc) from exc

    @staticmethod
    def _restore_plan(
        plan: RestorePlan, *, encrypted: bool, cancel: Event | None = None,
    ) -> RestorePlanView:
        entries = []
        for entry in plan.entries:
            if cancel is not None:
                _cancelled(cancel, "Generating restore plan")
            entries.append(
                RestorePlanEntryView(
                    entry.source_reference,
                    entry.kind,
                    entry.destination_folder,
                    entry.action,
                    entry.duplicate_reason,
                    entry.warnings,
                    recovery_status=entry.recovery_status,
                    recovery_operation_id=entry.recovery_operation_id,
                    item_key=entry.item_key,
                    title=entry.title,
                ),
            )
        view = RestorePlanView(
            plan.backup_id,
            tuple(entries),
            plan.create_count,
            plan.duplicate_count,
            plan.unsupported_count,
            plan.warnings,
            True,
            encrypted,
            plan.confirmation_token,
            blocked_count=plan.blocked_count,
            allow_completed_duplicates=plan.allow_completed_duplicates,
        )
        if cancel is not None:
            _cancelled(cancel, "Generating restore plan")
        return view

    def execute_restore(
        self,
        plan: RestorePlanView,
        *,
        confirmed: bool,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> RestoreResultView:
        """Execute exactly the previewed plan through the shared Exchange gateway."""

        try:
            if not confirmed:
                raise PublicOperationError(
                    "Restore execution requires explicit confirmation.", retryable=False
                )
            if not plan.confirmation_token:
                raise PublicOperationError(
                    "The restore preview is stale or incomplete; generate a new plan.",
                    retryable=False,
                )
            _cancelled(cancel, "Restoring backup")
            service = self._service()
            _manifest, effective_passphrase = self._manifest_and_passphrase(
                service,
                plan.backup_id,
                "restore",
                supplied=passphrase,
            )
            progress(
                OperationProgress("Restoring previewed items", completed=0, total=plan.create_count)
            )
            result = service.restore(
                plan.backup_id,
                passphrase=effective_passphrase,
                confirmed=True,
                target=_LazyRestoreTarget(self._source),
                confirmation_token=plan.confirmation_token,
                selected_references=tuple(entry.reference for entry in plan.entries),
                continue_on_error=False,
                allow_completed_duplicates=plan.allow_completed_duplicates,
                cancelled=cancel.is_set,
            )
            if not isinstance(result, RestoreResult):
                raise PublicOperationError(
                    "The restore provider returned another preview instead of executing.",
                    retryable=False,
                )
            progress(
                OperationProgress(
                    "Restore stopped"
                    if result.cancelled or result.interrupted
                    else "Restore request completed",
                    completed=result.attempted_count,
                    total=plan.create_count,
                )
            )
            return RestoreResultView(
                backup_id=plan.backup_id,
                attempted_count=result.attempted_count,
                restored_count=result.restored_count,
                failed_count=result.failed_count,
                duplicate_count=result.duplicate_count,
                unsupported_count=result.unsupported_count,
                stopped_early=result.stopped_early,
                server_mutations_performed=result.server_mutations_performed,
                failures=tuple(
                    item.source_reference for item in result.results if not item.succeeded
                ),
                mutation_uncertain=result.mutation_uncertain,
                cancelled=result.cancelled,
                interrupted=result.interrupted,
                operation_id=result.operation_id,
                journal_available=result.journal_available,
                recovery_write_failed=result.recovery_write_failed,
                blocked_count=result.blocked_count,
                uncertain_count=sum(item.mutation_uncertain for item in result.results),
            )
        except BaseException as exc:
            raise _safe_error("Restoring backup", exc) from exc

    def restore_status(
        self, operation_id: str, *, limit: int = 100, offset: int = 0,
        cancel: Event, progress: ProgressReporter,
    ) -> dict[str, Any]:
        from gucmail.services.restore_journal import RestoreJournal

        _cancelled(cancel, "Reading local restore status")
        if self.state is None:
            raise PublicOperationError("No local restore history is configured.", retryable=False)
        progress(OperationProgress("Reading local restore recovery"))
        result = RestoreJournal(cast(Any, self.state)).operation(
            operation_id, limit=limit, offset=offset,
        )
        if result is None:
            raise PublicOperationError(
                "The local restore operation was not found.", retryable=False,
            )
        return result

    def export_backup(
        self,
        backup_id: str,
        destination: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str:
        try:
            _cancelled(cancel, "Exporting backup")
            target = Path(destination).expanduser().resolve(strict=False)
            if target.suffix.casefold() != ".zip":
                raise PublicOperationError(
                    "Backup exports require a .zip destination.", retryable=False
                )
            service = self._service()
            _manifest, effective_passphrase = self._manifest_and_passphrase(
                service,
                backup_id,
                "export",
                supplied=passphrase,
            )
            progress(OperationProgress("Verifying and exporting backup archive"))
            exported = service.export(
                backup_id,
                target,
                passphrase=effective_passphrase,
                overwrite=False,
            )
            _cancelled(cancel, "Exporting backup")
            return str(exported)
        except BaseException as exc:
            raise _safe_error("Exporting backup", exc) from exc

    def plan_prune(
        self,
        keep_last: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupPrunePlanView:
        try:
            _cancelled(cancel, "Planning backup retention")
            progress(OperationProgress("Comparing complete backup manifests"))
            plan = self._service().prune(keep_last=keep_last, apply=False)
            if not isinstance(plan, PrunePlan):
                raise PublicOperationError("The backup service returned an invalid prune plan.")
            return BackupPrunePlanView(
                candidates=plan.candidates,
                retained=plan.retained,
                protected_ancestors=plan.protected_ancestors,
                potential_bytes=plan.potential_bytes,
                keep_last=keep_last,
                confirmation_token=plan.confirmation_token,
            )
        except BaseException as exc:
            raise _safe_error("Planning backup retention", exc) from exc

    def execute_prune(
        self,
        plan: BackupPrunePlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupPruneResultView:
        try:
            if not confirmed:
                raise PublicOperationError(
                    "Backup pruning requires explicit confirmation.", retryable=False
                )
            if not plan.confirmation_token:
                raise PublicOperationError(
                    "The prune preview is stale or incomplete; generate a new plan.",
                    retryable=False,
                )
            _cancelled(cancel, "Applying backup retention")
            progress(OperationProgress("Moving previewed backups to recoverable trash"))
            result = self._service().prune(
                keep_last=plan.keep_last,
                apply=True,
                confirmation_token=plan.confirmation_token,
            )
            if not isinstance(result, PruneResult):
                raise PublicOperationError("The backup service returned another prune preview.")
            return BackupPruneResultView(
                pruned_ids=result.plan.candidates,
                bytes_moved=result.bytes_moved,
                note=result.note,
            )
        except BaseException as exc:
            raise _safe_error("Applying backup retention", exc) from exc

    def open_backup_folder(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str:
        try:
            _cancelled(cancel, "Opening backup folder")
            root = self._active_root.resolve(strict=False)
            root.mkdir(parents=True, exist_ok=True)
            if not root.is_dir():
                raise PublicOperationError(
                    "The configured backup destination is not a directory.", retryable=False
                )
            progress(OperationProgress("Opening the configured backup directory"))
            self.folder_opener(root)
            return str(root)
        except BaseException as exc:
            raise _safe_error("Opening backup folder", exc) from exc


class StorageAreaBackend:
    """Quota, bounded metadata analytics, cleanup preview, and local-size adapter."""

    def __init__(
        self,
        *,
        quota_factory: Callable[[], QuotaService] | None,
        message_loader: StorageMessageLoader | None,
        attachment_loader: StorageAttachmentLoader | None = None,
        state: StateStore | None = None,
        cleanup_executor: CleanupExecutor | None = None,
        local_paths: Mapping[str, Path | str],
        export_directory: Path | str,
        online: bool = True,
        maximum_scan_items: int = 500,
    ) -> None:
        if not 1 <= maximum_scan_items <= 10_000:
            raise ValueError("Storage scan limit must be between 1 and 10000.")
        self.quota_factory = quota_factory
        self.message_loader = message_loader
        self.attachment_loader = attachment_loader
        self.state = state
        self.cleanup_executor = cleanup_executor
        self.local_paths = {
            str(label): Path(path).expanduser().resolve(strict=False)
            for label, path in local_paths.items()
        }
        self.export_directory = Path(export_directory).expanduser().resolve(strict=False)
        self.online = online
        self.maximum_scan_items = maximum_scan_items
        self._snapshot: StorageSnapshotView | None = None
        self._analysis_rows: dict[str, InsightMessage] = {}
        self._analysis: StorageAnalysisView | None = None
        self._cleanup_plans: dict[str, CleanupPlan] = {}

    @classmethod
    def from_context(
        cls,
        context: AppContext,
        *,
        online: bool,
        maximum_scan_items: int = 500,
    ) -> StorageAreaBackend:
        class _ConfiguredFallbackGateway:
            def get_quota(self) -> None:
                return None

        def quota_factory() -> QuotaService:
            settings = SettingsService(context.config_store)

            def setting(setting_id: str, fallback: object) -> object:
                try:
                    return settings.value(setting_id).effective_value
                except ConfigurationError:
                    return fallback

            return quota_service_from_settings(
                (context.gateway(interactive=True) if online else _ConfiguredFallbackGateway()),
                setting,
            )

        def load_messages(analysis_id: str, limit: int) -> Sequence[object]:
            gateway = context.gateway(interactive=True)
            bounded_limit = min(limit, maximum_scan_items)
            folder_limit = min(25, max(1, bounded_limit))
            folders = [context.config.default_folder]
            seen = {context.config.default_folder.casefold()}
            pending: list[str | None] = [None]
            while pending and len(folders) < folder_limit:
                parent_reference = pending.pop(0)
                children = gateway.list_folders(
                    parent_reference=parent_reference,
                    limit=folder_limit - len(folders),
                    offset=0,
                )
                for child in children:
                    reference = str(child.reference)
                    if reference.casefold() not in seen:
                        folder_class = str(child.folder_class or "").casefold()
                        if not folder_class or folder_class.startswith("ipf.note"):
                            seen.add(reference.casefold())
                            folders.append(reference)
                    if child.child_count and child.child_count > 0:
                        pending.append(reference)
                    if len(folders) >= folder_limit:
                        break

            per_folder = max(1, (bounded_limit + len(folders) - 1) // len(folders))
            messages: list[object] = []
            for folder in folders:
                remaining = bounded_limit - len(messages)
                if remaining <= 0:
                    break
                try:
                    messages.extend(
                        gateway.list_messages(
                            folder=folder,
                            limit=min(per_folder, remaining),
                        )
                    )
                except ConfigurationError:
                    # Non-mail folders can be exposed with an unknown class by old
                    # servers. Skip only that bounded folder and preserve real
                    # connectivity/authentication failures.
                    continue
            messages = messages[:bounded_limit]
            if analysis_id not in {"largest-attachments", "attachment-types"}:
                return messages

            # Attachment analytics need the attachment collection, but never the
            # message body or attachment payload.  Keep the per-item EWS projection
            # deliberately bounded even when the general metadata scan is larger.
            attachment_owner_limit = min(50, bounded_limit)
            enriched: list[object] = []
            attachment_owners = 0
            for message in messages:
                if (
                    bool(getattr(message, "has_attachments", False))
                    and attachment_owners < attachment_owner_limit
                ):
                    try:
                        attachments = gateway.list_attachments(
                            str(cast(Any, message).reference),
                            limit=100,
                            offset=0,
                        )
                    except ItemNotFoundError:
                        # A message can disappear between the bounded folder page and
                        # the metadata projection.  Retain its summary without
                        # inventing attachment sizes.
                        attachments = ()
                    enriched.append({"summary": message, "attachments": attachments})
                    attachment_owners += 1
                else:
                    enriched.append(message)
            return enriched

        def load_known_attachment_hashes(limit: int) -> Sequence[object]:
            bounded_limit = min(maximum_scan_items, max(1, limit))
            records: list[object] = []
            drafts = DraftService(
                context.state,
                context.paths.recovery_dir,
                maximum_attachment_bytes=context.config.max_attachment_bytes,
            )
            for draft in drafts.list():
                for path in draft.attachment_paths:
                    if len(records) >= bounded_limit:
                        return records
                    try:
                        entries = build_attachment_manifest(
                            (path,),
                            maximum_bytes=context.config.max_attachment_bytes,
                        )
                    except GUCmailError:
                        continue
                    records.extend(
                        {
                            **entry,
                            "message_reference": draft.reference,
                            "source": "local_draft",
                        }
                        for entry in entries
                    )

            try:
                indexed = SearchIndex(context.paths.index_database).records(limit=bounded_limit)
            except GUCmailError:
                indexed = ()
            for record in indexed:
                raw_attachments = (record.payload or {}).get("attachments", ())
                if not isinstance(raw_attachments, Sequence) or isinstance(
                    raw_attachments, (str, bytes)
                ):
                    continue
                for raw in raw_attachments:
                    if len(records) >= bounded_limit:
                        return records
                    if isinstance(raw, Mapping):
                        records.append(
                            {
                                **raw,
                                "message_reference": record.reference,
                                "source": f"index:{record.source}",
                            }
                        )
            return records[:bounded_limit]

        return cls(
            quota_factory=quota_factory,
            message_loader=load_messages if online else None,
            attachment_loader=load_known_attachment_hashes,
            state=context.state,
            cleanup_executor=(
                lambda candidate: context.gateway(interactive=True).delete_message(
                    candidate.reference,
                    permanent=False,
                )
            )
            if online
            else None,
            local_paths={
                "Cache": context.paths.cache_dir,
                "Index": context.paths.index_database,
                "Backups": context.paths.backup_dir,
                "State": context.paths.state_database,
            },
            export_directory=context.paths.data_dir / "exports",
            online=online,
            maximum_scan_items=maximum_scan_items,
        )

    @staticmethod
    def _threshold(identifier: str, label: str, value: QuotaValue) -> QuotaThresholdView:
        return QuotaThresholdView(
            identifier,
            label,
            value.value_bytes,
            value.source.value.replace("_", " ").capitalize(),
            value.confidence.value.replace("_", " ").capitalize(),
            value.observed_at.astimezone(UTC).isoformat() if value.observed_at else None,
            None,
            value.detail or "",
        )

    def _local_rows(self, cancel: Event) -> tuple[StorageUsageRow, ...]:
        rows: list[StorageUsageRow] = []
        for label, path in self.local_paths.items():
            _cancelled(cancel, "Loading local storage")
            if path.is_file() and not path.is_symlink():
                try:
                    size = path.stat().st_size
                    skipped = 0
                except OSError:
                    size, skipped = 0, 1
            else:
                size, skipped = _directory_size(path, cancel)
            detail = f"Local path: {path}"
            if skipped:
                detail += f" · {skipped} inaccessible, linked, or unscanned entries omitted"
            rows.append(
                StorageUsageRow(
                    f"local:{label.casefold()}",
                    label,
                    "local",
                    size,
                    source="filesystem",
                    details=detail,
                )
            )
        return tuple(rows)

    def load_storage(self, *, cancel: Event, progress: ProgressReporter) -> StorageSnapshotView:
        try:
            progress(OperationProgress("Measuring local cache, index, and backups"))
            local_rows = self._local_rows(cancel)
            if self.quota_factory is None:
                unknown = QuotaThresholdView("unknown", "Unknown", None, "unknown")
                snapshot = StorageSnapshotView(
                    None,
                    "unavailable",
                    unknown,
                    QuotaThresholdView("prohibit-send", "Prohibit send", None, "unknown"),
                    QuotaThresholdView(
                        "prohibit-send-receive",
                        "Prohibit send and receive",
                        None,
                        "unknown",
                    ),
                    None,
                    None,
                    server_available=False,
                    unavailable_reason=(
                        "Offline mode: server quota was not requested. Local storage remains "
                        "available."
                    ),
                    overview_rows=local_rows,
                )
                self._snapshot = snapshot
                return snapshot

            if not self.online:
                progress(OperationProgress("Loading configured fallback capacity"))
                report = self.quota_factory().report(local_paths=self.local_paths)
                snapshot = self._snapshot_from_report(report, local_rows)
                snapshot = replace(
                    snapshot,
                    server_available=False,
                    unavailable_reason=(
                        "Offline mode: Exchange usage and thresholds were not requested. "
                        "Configured fallback capacity and local storage remain available."
                    ),
                )
                self._snapshot = snapshot
                return snapshot

            _cancelled(cancel, "Loading mailbox quota")
            progress(OperationProgress("Requesting mailbox quota evidence"))
            report = self.quota_factory().report(local_paths=self.local_paths)
            snapshot = self._snapshot_from_report(report, local_rows)
            if snapshot.usage_bytes is None and self.message_loader is not None:
                try:
                    progress(
                        OperationProgress("Estimating usage from bounded visible message metadata")
                    )
                    estimated_bytes, sample_count = self._bounded_usage_estimate(cancel)
                    snapshot = replace(
                        snapshot,
                        usage_bytes=estimated_bytes,
                        usage_source="Bounded visible-item lower-bound estimate",
                        usage_confidence="Low (bounded estimate)",
                        usage_sample_count=sample_count,
                        usage_sample_limit=self.maximum_scan_items,
                        unavailable_reason=(
                            "Exchange did not expose authoritative usage/total values. The "
                            "visible-item estimate excludes hidden, recoverable, and "
                            "unscanned data."
                        ),
                    )
                except (GUCmailError, OSError, ValueError):
                    # The fallback capacity remains useful when a bounded metadata estimate is
                    # not available. Connectivity/authentication failures have already been
                    # translated by the surrounding operation boundary.
                    pass
            self._snapshot = snapshot
            return snapshot
        except BaseException as exc:
            raise _safe_error("Loading storage", exc) from exc

    def _snapshot_from_report(
        self, report: QuotaReport, local_rows: tuple[StorageUsageRow, ...]
    ) -> StorageSnapshotView:
        mailbox = report.mailbox
        capacity = report.effective_capacity
        capacity_source = capacity.source.value
        if report.capacity_is_fallback:
            capacity_source = "Configured fallback (editable; not server-reported)"
        else:
            capacity_source = capacity_source.replace("_", " ").capitalize()
        if mailbox is None:
            unknown = QuotaThresholdView("issue-warning", "Issue warning", None, "unknown")
            return StorageSnapshotView(
                None,
                "unavailable",
                unknown,
                QuotaThresholdView("prohibit-send", "Prohibit send", None, "unknown"),
                QuotaThresholdView(
                    "prohibit-send-receive",
                    "Prohibit send and receive",
                    None,
                    "unknown",
                ),
                None,
                None,
                total_capacity_bytes=capacity.value_bytes,
                total_capacity_source=capacity_source,
                total_capacity_is_fallback=report.capacity_is_fallback,
                server_available=False,
                unavailable_reason=report.unavailable_reason,
                overview_rows=local_rows,
            )
        overview = list(local_rows)
        if mailbox.deleted_bytes is not None:
            overview.append(
                StorageUsageRow(
                    "server:deleted",
                    "Deleted Items",
                    "server",
                    mailbox.deleted_bytes,
                    source="server_reported",
                    details="Server-reported Deleted Items size.",
                )
            )
        if mailbox.recoverable_bytes is not None:
            overview.append(
                StorageUsageRow(
                    "server:recoverable",
                    "Recoverable Items",
                    "server",
                    mailbox.recoverable_bytes,
                    source="server_reported",
                    details="Server-reported recoverable-item size.",
                )
            )
        observed = (
            mailbox.observed_at.astimezone(UTC).isoformat()
            if mailbox.observed_at is not None
            else None
        )
        return StorageSnapshotView(
            mailbox.used_bytes,
            mailbox.current_usage.source.value,
            self._threshold("issue-warning", "Issue warning", mailbox.issue_warning),
            self._threshold("prohibit-send", "Prohibit send", mailbox.prohibit_send),
            self._threshold(
                "prohibit-send-receive",
                "Prohibit send and receive",
                mailbox.prohibit_send_receive,
            ),
            report.send_status is MailboxOperationStatus.BLOCKED,
            report.receive_status is MailboxOperationStatus.BLOCKED,
            total_capacity_bytes=capacity.value_bytes,
            total_capacity_source=capacity_source,
            total_capacity_is_fallback=report.capacity_is_fallback,
            usage_confidence=mailbox.current_usage.confidence.value,
            observed_at=observed,
            server_available=True,
            unavailable_reason=report.unavailable_reason,
            overview_rows=tuple(overview),
        )

    def _bounded_usage_estimate(self, cancel: Event) -> tuple[int, int]:
        if self.message_loader is None:
            raise ValueError("A message loader is required for a bounded usage estimate.")
        raw = self.message_loader("quota-usage", self.maximum_scan_items)
        total = 0
        count = 0
        for value in raw[: self.maximum_scan_items]:
            _cancelled(cancel, "Estimating mailbox usage")
            record = InsightMessage.from_value(value)
            total += record.size_bytes
            count += 1
        return total, count

    def _load_insights(
        self, analysis_id: str, limit: int, cancel: Event, progress: ProgressReporter
    ) -> tuple[tuple[InsightMessage, ...], StorageReport]:
        requested = min(self.maximum_scan_items, max(limit, 100))
        if self.online and self.message_loader is not None:
            progress(OperationProgress("Loading bounded message metadata"))
            raw = self.message_loader(analysis_id, requested)
        elif analysis_id in {"largest-attachments", "attachment-types"} and (
            self.attachment_loader is not None
        ):
            progress(OperationProgress("Reading locally known attachment metadata"))
            known = tuple(self.attachment_loader(requested))
            grouped: dict[str, dict[str, object]] = {}
            for index, value in enumerate(known):
                _cancelled(cancel, "Storage analysis")
                if isinstance(value, Mapping):
                    attachment = dict(value)
                else:
                    attachment = {
                        "name": getattr(value, "name", None),
                        "size": getattr(value, "size", None),
                        "content_type": getattr(value, "content_type", None),
                    }
                owner = str(
                    attachment.get("message_reference")
                    or attachment.get("draft_reference")
                    or f"local-attachment-{index + 1}"
                )
                record = grouped.setdefault(
                    owner,
                    {
                        "reference": owner,
                        "subject": str(
                            attachment.get("message_subject")
                            or attachment.get("draft_subject")
                            or "Locally known attachment"
                        ),
                        "folder_path": str(attachment.get("source") or "local"),
                        "attachments": [],
                    },
                )
                cast(list[object], record["attachments"]).append(attachment)
            raw = tuple(grouped.values())
        else:
            raise OfflineOperationError(
                "Mailbox metadata analyses are unavailable offline. Local storage analysis "
                "remains available."
            )
        records: list[InsightMessage] = []
        for value in raw[: self.maximum_scan_items]:
            _cancelled(cancel, "Storage analysis")
            records.append(InsightMessage.from_value(value))
        analyzer = InsightAnalyzer(max_items=self.maximum_scan_items)
        return tuple(records), analyzer.storage(records, largest_limit=min(limit, 100))

    @staticmethod
    def _bucket_rows(
        prefix: str, category: str, buckets: Sequence[object]
    ) -> tuple[StorageUsageRow, ...]:
        return tuple(
            StorageUsageRow(
                f"{prefix}:{index}",
                str(cast(Any, bucket).label),
                category,
                int(cast(Any, bucket).total_bytes),
                int(cast(Any, bucket).item_count),
                "bounded_metadata",
                (
                    f"Unread: {int(getattr(bucket, 'unread_count', 0))} · "
                    f"Attachment bytes: {int(getattr(bucket, 'attachment_bytes', 0))}"
                ),
            )
            for index, bucket in enumerate(buckets)
        )

    def analyze_storage(
        self,
        analysis_id: str,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> StorageAnalysisView:
        try:
            if analysis_id == "duplicate-attachments":
                progress(OperationProgress("Reading trusted local attachment hashes"))
                raw = (
                    tuple(self.attachment_loader(min(limit, self.maximum_scan_items)))
                    if self.attachment_loader is not None
                    else ()
                )
                groups = duplicate_attachment_insights(
                    raw,
                    max_items=self.maximum_scan_items,
                )
                rows = tuple(
                    StorageUsageRow(
                        f"attachment-duplicate:{group.digest}",
                        ", ".join(dict.fromkeys(group.names)) or "attachment",
                        "exact duplicate attachment",
                        group.total_bytes,
                        group.copies,
                        "trusted SHA-256 metadata",
                        (
                            f"Reclaimable estimate: {group.reclaimable_bytes} bytes · "
                            f"Messages/drafts: {', '.join(group.message_references[:12])} · "
                            f"SHA-256: {group.digest}"
                        ),
                        cleanup_candidate=False,
                    )
                    for group in groups[:limit]
                )
                result = StorageAnalysisView(
                    analysis_id,
                    "Duplicate attachments",
                    rows,
                    True,
                    limit,
                    (
                        f"Inspected {len(raw)} locally known attachment hashes. "
                        "Unhashed server attachments are not downloaded automatically; "
                        "filename and size alone are never treated as duplicate proof."
                    ),
                )
                self._analysis = result
                self._analysis_rows = {}
                return result
            if analysis_id == "local":
                rows = self._local_rows(cancel)
                result = StorageAnalysisView(
                    analysis_id,
                    "Local cache, index, and backup storage",
                    rows[:limit],
                    True,
                    limit,
                    "Symlinks and inaccessible entries are omitted.",
                )
                self._analysis = result
                self._analysis_rows = {}
                return result
            records, report = self._load_insights(analysis_id, limit, cancel, progress)
            self._analysis_rows = {record.reference: record for record in records}
            if analysis_id == "mailbox-overview":
                rows_list = [
                    StorageUsageRow(
                        "overview:mailbox-sample",
                        "Bounded mailbox sample",
                        "overview",
                        report.total_bytes,
                        report.scanned_count,
                        "bounded_metadata",
                        (
                            "Total visible message bytes in this bounded scan. "
                            "This is not an authoritative Exchange mailbox total."
                        ),
                    ),
                    StorageUsageRow(
                        "overview:attachments",
                        "Attachment storage in sample",
                        "overview",
                        report.attachment_bytes,
                        sum(bucket.item_count for bucket in report.by_attachment_type),
                        "bounded_attachment_metadata",
                        "Known attachment bytes in the same bounded message sample.",
                    ),
                ]
                for row_id, label, category, bucket in (
                    (
                        "overview:largest-folder",
                        "Largest folder in sample",
                        "folder",
                        report.by_folder[0] if report.by_folder else None,
                    ),
                    (
                        "overview:largest-sender",
                        "Largest sender in sample",
                        "sender",
                        report.by_sender[0] if report.by_sender else None,
                    ),
                ):
                    if bucket is not None:
                        rows_list.append(
                            StorageUsageRow(
                                row_id,
                                f"{label}: {bucket.label}",
                                category,
                                bucket.total_bytes,
                                bucket.item_count,
                                "bounded_metadata",
                                (
                                    f"Unread: {bucket.unread_count} · "
                                    f"Attachment bytes: {bucket.attachment_bytes}"
                                ),
                            )
                        )
                if report.largest_messages:
                    item = report.largest_messages[0]
                    rows_list.append(
                        StorageUsageRow(
                            "overview:largest-message",
                            f"Largest message: {item.subject or '(no subject)'}",
                            "message",
                            item.size_bytes,
                            1,
                            "bounded_metadata",
                            f"Folder: {item.folder_path} · Sender: {item.sender}",
                        )
                    )
                rows = tuple(rows_list[:limit])
                title = "Mailbox cleanup overview"
            elif analysis_id == "duplicates":
                duplicates = duplicate_insights(
                    records,
                    include_near=False,
                    max_items=self.maximum_scan_items,
                )
                duplicate_rows: list[StorageUsageRow] = []
                for group in duplicates.exact_groups:
                    kept = group.references[0]
                    for reference in group.references[1:]:
                        record = self._analysis_rows[reference]
                        duplicate_rows.append(
                            StorageUsageRow(
                                reference,
                                record.subject or "(no subject)",
                                "exact duplicate",
                                record.size_bytes,
                                1,
                                "complete-content hash",
                                (
                                    f"Folder: {record.folder_path} · kept reference: {kept} · "
                                    f"digest: {group.digest[:16]}… · {group.explanation}"
                                ),
                                cleanup_candidate=True,
                            )
                        )
                for group in duplicates.possible_groups:
                    duplicate_rows.append(
                        StorageUsageRow(
                            f"possible:{group.digest}",
                            group.representative_subject or "(no subject)",
                            "possible duplicate group",
                            group.total_bytes,
                            len(group.references),
                            "bounded metadata similarity",
                            (
                                f"References: {', '.join(group.references[:8])} · "
                                f"{group.explanation} Manual review is required; this row "
                                "cannot enter an executable cleanup plan."
                            ),
                            cleanup_candidate=False,
                        )
                    )
                rows = tuple(duplicate_rows[:limit])
                title = "Duplicate messages"
            elif analysis_id in {"folders", "largest-folders"}:
                buckets = report.by_folder
                if analysis_id == "largest-folders":
                    buckets = tuple(
                        sorted(buckets, key=lambda item: item.total_bytes, reverse=True)
                    )
                rows = self._bucket_rows("folder", "folder", buckets)[:limit]
                title = (
                    "Largest folders" if analysis_id == "largest-folders" else "Storage by folder"
                )
            elif analysis_id == "largest-messages":
                rows = tuple(
                    StorageUsageRow(
                        item.reference,
                        item.subject or "(no subject)",
                        "message",
                        item.size_bytes,
                        1,
                        "bounded_metadata",
                        f"Folder: {item.folder_path} · Sender: {item.sender}",
                        cleanup_candidate=True,
                    )
                    for item in report.largest_messages[:limit]
                )
                title = "Largest messages"
            elif analysis_id == "largest-attachments":
                attached = sorted(
                    (item for item in records if item.attachment_bytes > 0),
                    key=lambda item: item.attachment_bytes,
                    reverse=True,
                )
                rows = tuple(
                    StorageUsageRow(
                        item.reference,
                        item.subject or "(no subject)",
                        "attachments",
                        item.attachment_bytes,
                        len(item.attachment_names) or None,
                        "bounded_attachment_metadata",
                        f"Folder: {item.folder_path}",
                        cleanup_candidate=True,
                    )
                    for item in attached[:limit]
                )
                title = "Largest attachments"
            elif analysis_id == "senders":
                rows = self._bucket_rows("sender", "sender", report.by_sender)[:limit]
                title = "Storage by sender"
            elif analysis_id == "dates":
                rows = self._bucket_rows("date", "date", report.by_period)[:limit]
                title = "Storage by date"
            elif analysis_id == "attachment-types":
                rows = self._bucket_rows(
                    "attachment-type", "attachment type", report.by_attachment_type
                )[:limit]
                title = "Storage by attachment type"
            elif analysis_id in {"deleted-items", "recoverable-items"}:
                key = "server:deleted" if analysis_id == "deleted-items" else "server:recoverable"
                overview = self._snapshot.overview_rows if self._snapshot else ()
                rows = tuple(row for row in overview if row.row_id == key)
                title = "Deleted Items" if analysis_id == "deleted-items" else "Recoverable Items"
            else:
                raise PublicOperationError(
                    f"Unknown storage analysis: {analysis_id}", retryable=False
                )
            if analysis_id in {"largest-attachments", "attachment-types"}:
                note = (
                    f"Bounded to {len(records)} attachment-owner records. Online analysis "
                    "projects metadata for at most 50 attachment-bearing messages; offline "
                    "analysis uses locally known draft/index metadata. Message bodies and "
                    "attachment payloads were not downloaded."
                )
            else:
                note = (
                    f"Bounded to {len(records)} message summaries from the configured metadata "
                    "loader; bodies and attachment payloads were not downloaded."
                )
            result = StorageAnalysisView(
                analysis_id,
                title,
                tuple(rows),
                True,
                limit,
                note,
            )
            self._analysis = result
            return result
        except BaseException as exc:
            raise _safe_error("Analyzing storage", exc) from exc

    def create_cleanup_plan(
        self,
        analysis_id: str,
        candidate_references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CleanupPlanView:
        try:
            if self._analysis is None or self._analysis.analysis_id != analysis_id:
                raise PublicOperationError(
                    "The storage analysis changed; run it again before planning cleanup.",
                    retryable=False,
                )
            progress(OperationProgress("Building recoverable cleanup preview"))
            candidates: list[CleanupCandidate] = []
            for reference in candidate_references:
                _cancelled(cancel, "Creating cleanup plan")
                row = next((item for item in self._analysis.rows if item.row_id == reference), None)
                record = self._analysis_rows.get(reference)
                if row is None or record is None or not row.cleanup_candidate:
                    continue
                candidates.append(
                    CleanupCandidate(
                        reference=reference,
                        reason=f"selected_from_{analysis_id}",
                        size_bytes=max(0, row.size_bytes or 0),
                        folder_path=record.folder_path,
                    )
                )
            plan = create_cleanup_plan(candidates, action="move_to_deleted")
            self._cleanup_plans[plan.plan_id] = plan
            return CleanupPlanView(
                plan.plan_id,
                tuple(
                    CleanupPlanEntryView(
                        item.reference,
                        self._analysis_rows[item.reference].subject or "(no subject)",
                        item.folder_path,
                        item.size_bytes,
                        item.action,
                        item.recoverable,
                        item.reason,
                    )
                    for item in plan.candidates
                ),
                plan.estimated_reclaimable_bytes,
                warnings=(plan.recovery_guidance,),
                executable=self.cleanup_executor is not None,
                confirmation_token=plan.confirmation_token,
            )
        except BaseException as exc:
            raise _safe_error("Creating cleanup plan", exc) from exc

    def execute_cleanup_plan(
        self,
        plan: CleanupPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CleanupExecutionView:
        try:
            if not confirmed:
                raise PublicOperationError(
                    "Cleanup execution requires explicit confirmation.", retryable=False
                )
            service_plan = self._cleanup_plans.get(plan.plan_id)
            if service_plan is None:
                raise PublicOperationError(
                    "The cleanup preview is stale; create and review a new plan.",
                    retryable=False,
                )
            expected_references = tuple(item.reference for item in service_plan.candidates)
            preview_references = tuple(item.reference for item in plan.entries)
            if (
                plan.confirmation_token != service_plan.confirmation_token
                or preview_references != expected_references
            ):
                raise PublicOperationError(
                    "The cleanup preview no longer matches its immutable service plan.",
                    retryable=False,
                )
            executor = self.cleanup_executor
            if executor is None:
                raise OfflineOperationError("No live recoverable cleanup executor is available.")
            # One reviewed execution is consumed even if a dispatched write loses
            # its acknowledgment. Reusing the preview must never replay it.
            self._cleanup_plans.pop(plan.plan_id)
            completed = 0
            undoable = 0
            failed: list[str] = []
            uncertain: list[str] = []
            recovery_failed: list[str] = []
            operation_ids: list[str] = []

            def execute(candidate: CleanupCandidate) -> object:
                nonlocal completed, undoable
                _cancelled(cancel, "Executing cleanup plan")
                progress(
                    OperationProgress(
                        "Moving exact cleanup candidate to Deleted Items",
                        completed=completed,
                        total=len(service_plan.candidates),
                    )
                )

                def delete_exact(reference: str, permanent: bool = False) -> object:
                    if reference != candidate.reference or permanent:
                        raise ConfigurationError("The cleanup target changed after review.")
                    return executor(candidate)

                # Reuse the CLI/mail mutation intent/dispatch/acknowledgment
                # boundary. The outer cleanup review already confirms this
                # immutable target and its recoverable action.
                service = MessageService(
                    cast(Any, SimpleNamespace(delete_message=delete_exact)), state=self.state
                )
                mutation = service.plan_delete((candidate.reference,), permanent=False)
                try:
                    execution = service.execute_mutation(
                        mutation, confirmation_scope=mutation.confirmation_scope
                    )
                except MutationExecutionError as error:
                    operation_ids.append(error.operation_id)
                    status = error.outcomes[0]["status"]
                    if status == "completed":
                        completed += 1
                        recovery_failed.append(candidate.reference)
                    elif status == "uncertain":
                        uncertain.append(candidate.reference)
                    else:
                        failed.append(candidate.reference)
                    raise
                except Exception:
                    # Intent persistence failed before provider dispatch.
                    failed.append(candidate.reference)
                    raise
                if execution.operation_id is not None:
                    operation_ids.append(execution.operation_id)
                outcome = execution.results[0]
                completed += 1
                if self.state is not None:
                    moved_at = datetime.now(UTC)
                    key_digest = hashlib.sha256(candidate.reference.encode("utf-8")).hexdigest()[
                        :16
                    ]
                    replacement = execution.outcomes[0].result_reference or candidate.reference
                    try:
                        self.state.put_ui_preference(
                            f"{service_plan.plan_id[:16]}-{key_digest}",
                            {
                                "plan_id": service_plan.plan_id,
                                "reference": candidate.reference,
                                "current_reference": replacement,
                                "original_folder": candidate.folder_path,
                                "action": candidate.action,
                                "moved_at": moved_at.isoformat(),
                                "expires_at": (moved_at + timedelta(minutes=30)).isoformat(),
                            },
                            scope="cleanup-undo",
                        )
                    except Exception:
                        # A local recovery failure cannot undo an acknowledged
                        # provider effect or relabel it as a rejected move.
                        recovery_failed.append(candidate.reference)
                        raise
                    undoable += 1
                return outcome

            result = run_cleanup_plan(
                service_plan,
                execute,
                confirmed=True,
                continue_on_error=False,
            )
            progress(
                OperationProgress(
                    "Cleanup execution completed",
                    completed=result.attempted_count,
                    total=len(service_plan.candidates),
                )
            )
            return CleanupExecutionView(
                plan_id=result.plan_id,
                attempted_count=result.attempted_count,
                succeeded_count=completed,
                failed_count=len(failed),
                stopped_early=result.stopped_early,
                undoable_count=undoable,
                failed_references=tuple(failed),
                uncertain_count=len(uncertain),
                uncertain_references=tuple(uncertain),
                recovery_failed_count=len(recovery_failed),
                recovery_failed_references=tuple(recovery_failed),
                not_started_count=len(service_plan.candidates) - result.attempted_count,
                operation_ids=tuple(operation_ids),
                journal_available=self.state is not None,
            )
        except BaseException as exc:
            raise _safe_error("Executing cleanup plan", exc) from exc

    def export_statistics(
        self,
        snapshot: StorageSnapshotView,
        analysis: StorageAnalysisView | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str:
        try:
            _cancelled(cancel, "Exporting storage statistics")
            progress(OperationProgress("Writing sanitized JSON export"))
            payload = {
                "schema_version": 1,
                "exported_at": datetime.now(UTC).isoformat(),
                "snapshot": asdict(snapshot),
                "analysis": asdict(analysis) if analysis is not None else None,
                "limitations": [
                    "Storage analytics are bounded metadata estimates.",
                    "Attachment payloads and message bodies are not downloaded for this report.",
                ],
            }
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = self.export_directory / f"gucmail-storage-{stamp}.json"
            return str(
                _atomic_text(
                    path,
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
            )
        except BaseException as exc:
            raise _safe_error("Exporting storage statistics", exc) from exc


class SettingsAreaBackend:
    """Atomic, revision-aware adapter for the shared Settings service."""

    def __init__(
        self,
        service: SettingsService,
        *,
        preview_handlers: Mapping[str, PreviewHandler] | None = None,
        secret_configured: Callable[[str], bool] | None = None,
        on_applied: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.service = service
        self.preview_handlers = dict(preview_handlers or {})
        self.secret_configured = secret_configured
        self.on_applied = on_applied
        self._previews: dict[str, PreviewReverter] = {}
        self._import_plans: dict[str, SettingsImportPlan] = {}
        self._lock = RLock()

    @classmethod
    def from_context(
        cls,
        context: AppContext,
        *,
        preview_handlers: Mapping[str, PreviewHandler] | None = None,
        secret_configured: Callable[[str], bool] | None = None,
        on_applied: Callable[[tuple[str, ...]], None] | None = None,
    ) -> SettingsAreaBackend:
        return cls(
            SettingsService(
                context.config_store,
                context=SettingsContext(
                    account_id=context.config.email,
                    machine_id=f"{sys.platform}:{platform.node()}",
                ),
            ),
            preview_handlers=preview_handlers,
            secret_configured=secret_configured,
            on_applied=on_applied,
        )

    def _notify_applied(self, setting_ids: Sequence[str]) -> None:
        """Notify the mounted runtime after durable commit without undoing that commit."""

        try:
            # Restore temporary runtime state before loading the committed values,
            # including commits made through reset/import/history while previewing.
            self.abandon_previews()
            if self.on_applied is not None:
                self.on_applied(tuple(setting_ids))
        except Exception:
            # The configuration revision is already durable. A presentation
            # refresh failure must not be reported as an atomic-write failure.
            return

    @staticmethod
    def _revision(value: str | None) -> str:
        return value or _UNWRITTEN_REVISION

    def _assert_revision(self, expected: str) -> None:
        current = self._revision(self.service.config_store.load_document().revision)
        if current != expected:
            raise PublicOperationError(
                "Settings changed in another process. Refresh before applying this edit.",
                retryable=False,
            )

    @staticmethod
    def _level(value: ServiceSettingLevel) -> SettingLevel:
        return SettingLevel(value.value)

    def _setting(self, value: EffectiveSetting) -> SettingView:
        spec = value.spec
        source = value.source.value
        if value.environment_variable:
            source += f" ({value.environment_variable})"
        is_secret = spec.sensitivity is Sensitivity.SECRET
        configured = (
            bool(self.secret_configured(spec.setting_id))
            if is_secret and self.secret_configured is not None
            else False
        )
        return SettingView(
            setting_id=spec.setting_id,
            label=spec.label,
            description=(
                MANAGED_SETTINGS[spec.setting_id][1]
                if spec.setting_id in MANAGED_SETTINGS
                else UNAVAILABLE_SETTINGS.get(spec.setting_id, spec.description)
            ),
            category=spec.category,
            value_type=spec.value_type.value,
            default_value=None if is_secret else cast(SettingValue, spec.default),
            current_value=None if is_secret else cast(SettingValue, value.configured_value),
            effective_value=None if is_secret else cast(SettingValue, value.effective_value),
            source=source,
            keywords=tuple(dict.fromkeys((*spec.keywords, *spec.aliases))),
            aliases=spec.aliases,
            options=spec.choices,
            minimum=spec.minimum,
            maximum=spec.maximum,
            level=self._level(spec.level),
            scope=spec.scope.value,
            help_topic=spec.help_id,
            restart_required=spec.restart_required,
            secret=is_secret,
            secret_configured=configured,
            exportable=spec.exportable and not is_secret,
            editable=not value.overridden
            and not is_secret
            and spec.setting_id not in MANAGED_SETTINGS
            and spec.setting_id not in UNAVAILABLE_SETTINGS,
            previewable=spec.setting_id in self.preview_handlers,
            external_operation=spec.external_effect,
        )

    def load_catalog(self, *, cancel: Event, progress: ProgressReporter) -> SettingsCatalogView:
        try:
            _cancelled(cancel, "Loading settings")
            progress(OperationProgress("Reading configuration and setting metadata"))
            values = self.service.values()
            document = self.service.config_store.load_document()
            diagnostics = self.service.config_store.diagnostics
            try:
                active_value = self.service.value("advanced.mode").effective_value
                active_level = SettingLevel(str(active_value))
            except (ConfigurationError, ValueError):
                active_level = SettingLevel.BASIC
            warning = " ".join(diagnostics.messages) or None
            settings = tuple(self._setting(value) for value in values)
            if diagnostics.future_schema:
                settings = tuple(
                    SettingView(
                        **{
                            **asdict(setting),
                            "editable": False,
                            "previewable": False,
                        }
                    )
                    for setting in settings
                )
                warning = (
                    warning + " " if warning else ""
                ) + "This configuration uses a newer schema and is read-only here."
            return SettingsCatalogView(
                settings,
                self._revision(document.revision),
                active_level,
                warning,
                diagnostics.recovered,
            )
        except BaseException as exc:
            raise _safe_error("Loading settings", exc) from exc

    def validate_change(
        self,
        setting_id: str,
        raw_value: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingValidationView:
        del progress
        try:
            _cancelled(cancel, "Validating setting")
            spec = self.service.registry.get(setting_id)
            if spec.sensitivity is Sensitivity.SECRET:
                raise PublicOperationError(
                    "Secret values must be changed through secure credential storage.",
                    retryable=False,
                )
            parsed = spec.parse(raw_value)
            warnings: list[str] = []
            if spec.restart_required:
                warnings.append("This setting takes effect after restart.")
            if spec.external_effect:
                warnings.append("Applying this setting requires external-effect confirmation.")
            return SettingValidationView(
                setting_id, True, cast(SettingValue, parsed), (), tuple(warnings)
            )
        except PublicOperationError:
            raise
        except BaseException as exc:
            return SettingValidationView(
                setting_id,
                False,
                None,
                (redact_text(str(exc), redact_emails=True)[:300],),
            )

    def preview_change(
        self,
        setting_id: str,
        value: SettingValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsPreviewView:
        del progress
        try:
            _cancelled(cancel, "Previewing setting")
            value = self.service.registry.get(setting_id).parse(value)
            handler = self.preview_handlers.get(setting_id)
            if handler is None:
                raise PublicOperationError(
                    "This setting has no reversible live-preview adapter.", retryable=False
                )
            self.abandon_previews()
            reverter = handler(value)
            if reverter is None:
                raise PublicOperationError(
                    "The live-preview adapter did not provide an exact revert operation.",
                    retryable=False,
                )
            token = uuid4().hex
            with self._lock:
                cancelled = cancel.is_set()
                if not cancelled:
                    self._previews[token] = reverter
            if cancelled:
                reverter()
                _cancelled(cancel, "Previewing setting")
            return SettingsPreviewView(token, setting_id, "Reversible preview applied")
        except BaseException as exc:
            raise _safe_error("Previewing setting", exc) from exc

    def revert_preview(self, token: str, *, cancel: Event, progress: ProgressReporter) -> None:
        del progress
        try:
            _cancelled(cancel, "Reverting preview")
            with self._lock:
                reverter = self._previews.pop(token, None)
            if reverter is None:
                raise PublicOperationError(
                    "The preview token is no longer active.", retryable=False
                )
            reverter()
        except BaseException as exc:
            raise _safe_error("Reverting preview", exc) from exc

    def abandon_previews(self) -> None:
        """Restore temporary state on replacement, commit, or screen teardown."""
        with self._lock:
            reverters = tuple(reversed(tuple(self._previews.values())))
            self._previews.clear()
        for reverter in reverters:
            reverter()

    def _apply_view(
        self,
        revision: SettingsRevision | None,
        changed: Sequence[str],
        *,
        message: str,
    ) -> SettingsApplyView:
        document = self.service.config_store.load_document()
        restart = tuple(
            setting_id
            for setting_id in changed
            if self.service.registry.get(setting_id).restart_required
        )
        return SettingsApplyView(
            self._revision(document.revision),
            tuple(changed),
            restart,
            message if revision is not None else "No settings changed.",
        )

    def apply_settings(
        self,
        changes: Mapping[str, SettingValue],
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView:
        try:
            _cancelled(cancel, "Applying settings")
            self._assert_revision(expected_revision)
            progress(OperationProgress("Committing one atomic settings revision"))
            session = self.service.begin()
            for setting_id, value in changes.items():
                _cancelled(cancel, "Applying settings")
                session.set(setting_id, value)
            revision = session.apply(reason="Settings updated from TUI", confirm_external=True)
            self._notify_applied(tuple(changes))
            return self._apply_view(
                revision,
                tuple(changes),
                message="Settings applied atomically.",
            )
        except BaseException as exc:
            raise _safe_error("Applying settings", exc) from exc

    def reset_settings(
        self,
        scope: str,
        target: str | None,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView:
        try:
            _cancelled(cancel, "Resetting settings")
            self._assert_revision(expected_revision)
            session = self.service.begin()
            if scope == "setting" and target:
                changed: tuple[str, ...] = (self.service.registry.get(target).setting_id,)
                session.reset(target)
            elif scope == "category" and target:
                session.reset_category(target)
            elif scope == "all":
                session.reset_all()
            else:
                raise PublicOperationError("Unknown settings reset scope.", retryable=False)
            changed = tuple(difference.setting_id for difference in session.preview())
            progress(OperationProgress("Committing reset revision"))
            revision = session.apply(reason=f"TUI reset: {scope}", confirm_external=True)
            self._notify_applied(changed)
            return self._apply_view(revision, changed, message="Preferences reset safely.")
        except BaseException as exc:
            raise _safe_error("Resetting settings", exc) from exc

    def export_settings(
        self,
        scope: str,
        target: str | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsExportView:
        del progress
        try:
            _cancelled(cancel, "Exporting settings")
            payload = self.service.export(
                categories=(target,) if scope == "category" and target else None
            )
            settings = cast(Mapping[str, object], payload.get("settings", {}))
            excluded = cast(Sequence[object], payload.get("excluded_secrets", ()))
            return SettingsExportView(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                "GUC Mail settings JSON",
                len(settings),
                len(excluded),
            )
        except BaseException as exc:
            raise _safe_error("Exporting settings", exc) from exc

    def preview_import(
        self, payload: str, *, cancel: Event, progress: ProgressReporter
    ) -> SettingsImportPreviewView:
        del progress
        try:
            _cancelled(cancel, "Previewing settings import")
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise ConfigurationError("Settings import must be a JSON object.")
            plan = self.service.plan_import(decoded)
            token = uuid4().hex
            with self._lock:
                self._import_plans[token] = plan
            changes = tuple(self._import_change(change) for change in plan.changes)
            return SettingsImportPreviewView(
                token,
                changes,
                plan.unsupported,
                plan.excluded_secrets,
                (
                    "External-effect settings require confirmation before import."
                    if any(change.external_effect for change in plan.changes)
                    else "",
                )
                if any(change.external_effect for change in plan.changes)
                else (),
            )
        except BaseException as exc:
            raise _safe_error("Previewing settings import", exc) from exc

    def _import_change(self, change: SettingsDiff) -> SettingsImportChangeView:
        spec = self.service.registry.get(change.setting_id)
        view = self._setting(self.service.value(change.setting_id))
        return SettingsImportChangeView(
            change.setting_id,
            spec.label,
            display_setting_value(view, cast(SettingValue, change.before)),
            display_setting_value(view, cast(SettingValue, change.after)),
            True,
            external_operation=change.external_effect,
        )

    def apply_import(
        self,
        token: str,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView:
        try:
            _cancelled(cancel, "Applying settings import")
            self._assert_revision(expected_revision)
            with self._lock:
                plan = self._import_plans.pop(token, None)
            if plan is None:
                raise PublicOperationError(
                    "The settings import preview expired; preview it again.", retryable=False
                )
            progress(OperationProgress("Committing validated import"))
            revision = self.service.apply_import(plan, confirm_external=True)
            changed = tuple(change.setting_id for change in plan.changes)
            self._notify_applied(changed)
            return self._apply_view(revision, changed, message="Settings import applied.")
        except BaseException as exc:
            raise _safe_error("Applying settings import", exc) from exc

    def list_history(
        self,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[SettingsRevisionView]:
        del progress
        try:
            _cancelled(cancel, "Loading settings history")
            return tuple(
                SettingsRevisionView(
                    item.revision_id,
                    item.created_at,
                    item.reason,
                    tuple(change.setting_id for change in item.changes),
                    True,
                )
                for item in self.service.revisions.list()[:limit]
            )
        except BaseException as exc:
            raise _safe_error("Loading settings history", exc) from exc

    def restore_revision(
        self,
        revision: str,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView:
        try:
            _cancelled(cancel, "Restoring settings revision")
            self._assert_revision(expected_revision)
            original = self.service.revisions.get(revision)
            if original is None:
                raise PublicOperationError("Settings revision not found.", retryable=False)
            progress(OperationProgress("Restoring prior values as a new revision"))
            restored = self.service.undo(revision)
            changed = tuple(change.setting_id for change in original.changes)
            self._notify_applied(changed)
            return self._apply_view(restored, changed, message="Settings revision restored.")
        except BaseException as exc:
            raise _safe_error("Restoring settings revision", exc) from exc


class ActivityStateReader(Protocol):
    """Minimum durable state surface used by :class:`ActivityAreaBackend`."""

    account_key: str

    def initialize(self) -> None: ...

    def connection(self): ...  # type: ignore[no-untyped-def]


class ActivityAreaBackend:
    """Read sanitized durable activity and control an injected headless watcher."""

    _PRESERVED_ACTIVITY_STATE: tuple[str, ...] = (
        "watcher checkpoints and leases",
        "notification deduplication and pending deliveries",
        "scheduled jobs and recurrence state",
        "backup manifests and restore records",
        "stable message references",
        "message operation recovery records",
    )

    def __init__(
        self,
        state: StateStore,
        *,
        export_directory: Path | str,
        watcher_store: WatcherStatusStore | None = None,
        watcher_launcher: WatcherLauncher | None = None,
        watcher_synchronizer: WatcherSynchronizer | None = None,
        retry_delivery_callback: DeliveryRetry | None = None,
        notification_test_callback: NotificationTest | None = None,
        cleanup_restorer: CleanupRestorer | None = None,
        configured_method: str = "bounded polling",
        configured_folders: Sequence[str] = (),
        active_profile: str | None = None,
        offline: bool = False,
        redact_addresses: bool = True,
        log_path: Path | str | None = None,
    ) -> None:
        self.state = state
        self.export_directory = Path(export_directory).expanduser().resolve(strict=False)
        self.watcher_store = watcher_store
        self.watcher_launcher = watcher_launcher
        self.watcher_synchronizer = watcher_synchronizer
        self.retry_delivery_callback = retry_delivery_callback
        self.notification_test_callback = notification_test_callback
        self.cleanup_restorer = cleanup_restorer
        self.configured_method = configured_method
        self.configured_folders = tuple(configured_folders)
        self.active_profile = active_profile
        self.offline = offline
        self.redact_addresses = redact_addresses
        self._launch_requested = False
        self.log_path = (
            Path(log_path).expanduser().resolve(strict=False) if log_path is not None else None
        )

    @classmethod
    def from_context(
        cls,
        context: AppContext,
        *,
        watcher_store: WatcherStatusStore | None = None,
        watcher_launcher: WatcherLauncher | None = None,
        watcher_synchronizer: WatcherSynchronizer | None = None,
        retry_delivery_callback: DeliveryRetry | None = None,
        notification_test_callback: NotificationTest | None = None,
        offline: bool = False,
    ) -> ActivityAreaBackend:
        configured_folders: tuple[str, ...] = (context.config.default_folder,)
        active_profile: str | None = None
        configured_method = "bounded polling"
        try:
            watcher = configured_watcher(context)
        except GUCmailError:
            watcher = None
        if watcher is not None:
            configured_folders = watcher.folders
            active_profile = watcher.profile_id
            if watcher.strategy_preference:
                configured_method = watcher.strategy_preference.replace("_", " ")
        if watcher_store is None and context.config.email:
            selected_profile = active_profile or "default"
            process_spec = context_watcher_process(context, profile=selected_profile)
            watcher_store = WatcherStatusStore(
                (context.paths.data_dir / "watcher").resolve(), process_spec.scope_id
            )
            if watcher_launcher is None and not offline:

                def launch() -> WatcherStatus | None:
                    current = watcher_store.read()
                    if (
                        current is not None
                        and current.state not in {WatcherRunState.STOPPED, WatcherRunState.FAILED}
                        and process_is_alive(current.pid)
                    ):
                        return current
                    start_watcher_process(process_spec)
                    return None

                watcher_launcher = launch
            if watcher_synchronizer is None and not offline:

                def synchronize() -> WatcherStatus | None:
                    current = watcher_store.read()
                    if (
                        current is not None
                        and current.state not in {WatcherRunState.STOPPED, WatcherRunState.FAILED}
                        and process_is_alive(current.pid)
                    ):
                        if current.state is WatcherRunState.PAUSED:
                            raise PublicOperationError(
                                "The watcher is paused. Resume it before forcing "
                                "a synchronization.",
                                retryable=False,
                            )
                        watcher_store.request("sync")
                        return current
                    one_shot = replace(
                        process_spec,
                        arguments=tuple(
                            argument
                            for argument in process_spec.arguments
                            if argument != "--forever"
                        ),
                    )
                    start_watcher_process(one_shot)
                    return None

                watcher_synchronizer = synchronize
        if retry_delivery_callback is None:

            def schedule_delivery_retry(event_id: str) -> ActivityEventView:
                current = datetime.now(UTC)
                attempts = context.state.list_provider_attempts(event_id=event_id, limit=1)
                provider = attempts[0].provider_id if attempts else None
                context.state.update_notification_outbox(
                    event_id,
                    "retry_scheduled",
                    at=current,
                    next_attempt_at=current,
                    increment_cycle=False,
                )
                if provider is not None:
                    context.state.resolve_dead_letter(
                        event_id,
                        provider,
                        resolution="Manual retry scheduled from Activity.",
                        resolved_at=current,
                    )
                return ActivityEventView(
                    event_id,
                    current.isoformat(),
                    "notification",
                    "info",
                    "Delivery retry scheduled",
                    "The durable event is due immediately; the watcher will reuse its stable "
                    "event ID and provider idempotency key.",
                    correlation_id=event_id,
                    provider=provider,
                )

            retry_delivery_callback = schedule_delivery_retry
        if notification_test_callback is None:

            def test_local_notification(provider_kind: str) -> ActivityEventView:
                now = datetime.now(UTC)
                event = NotificationEvent(
                    event_id=f"synthetic-tui-notification-{uuid4().hex}",
                    event_type="test",
                    occurred_at=now,
                    account_id=context.config.email or "configured-account",
                    subject="Synthetic notification test",
                    preview="No mailbox data was used.",
                )
                if provider_kind == "desktop":
                    from gucmail.services.windows_desktop import desktop_notification_provider

                    provider: NotificationProvider = desktop_notification_provider(context)
                    summary = "Synthetic desktop notification test"
                elif provider_kind == "console":
                    provider = ConsoleNotificationProvider(stream=io.StringIO())
                    summary = "Synthetic local notification test"
                else:
                    raise ValueError("unsupported notification test provider")
                results = NotificationDispatcher(
                    [provider],
                    state=context.state,
                    deduplicate=False,
                ).dispatch(event)
                result = results[0]
                return ActivityEventView(
                    event.event_id,
                    now.isoformat(),
                    "notification",
                    "info" if result.state.value == "delivered" else "error",
                    summary,
                    result.detail or f"The {provider_kind} provider accepted the synthetic event.",
                    provider=result.provider,
                    attempt_count=result.attempts,
                )

            notification_test_callback = test_local_notification
        cleanup_restorer: CleanupRestorer | None = None
        if not offline:

            def restore_cleanup_item(reference: str, folder: str) -> object:
                return context.gateway(interactive=True).move_message(reference, folder)

            cleanup_restorer = restore_cleanup_item
        return cls(
            context.state,
            export_directory=context.paths.data_dir / "exports",
            watcher_store=watcher_store,
            watcher_launcher=watcher_launcher,
            watcher_synchronizer=watcher_synchronizer,
            retry_delivery_callback=retry_delivery_callback,
            notification_test_callback=notification_test_callback,
            cleanup_restorer=cleanup_restorer,
            configured_method=configured_method,
            configured_folders=configured_folders,
            active_profile=active_profile,
            offline=offline,
            redact_addresses=context.config.redact_email_addresses,
            log_path=context.paths.log_dir / "gucmail.log",
        )

    def _watcher(self) -> WatcherStatusView:
        status: WatcherStatus | None = None
        if self.watcher_store is not None:
            try:
                status = self.watcher_store.read()
            except GUCmailError:
                status = None
        if status is not None:
            return self._watcher_status_view(status)
        rows = self._watcher_rows()
        if rows:
            row = rows[0]
            state = str(row.get("state") or "stopped")
            folders = self._json_strings(row.get("folders_json")) or self.configured_folders
            return WatcherStatusView(
                state not in {"stopped", "failed", "disabled"},
                state,
                str(row.get("strategy") or "unknown"),
                self.configured_method,
                str(row.get("profile_id") or self.active_profile or "default"),
                folders,
                self._optional(row.get("last_success_at")),
                self._safe(row.get("error_summary")),
                self._optional(row.get("next_poll_at")),
                int(row["process_id"]) if row.get("process_id") else None,
                self._outbox_queue_count(),
            )
        return WatcherStatusView(
            False,
            "stopped",
            "not running",
            self.configured_method,
            self.active_profile,
            self.configured_folders,
            None,
            None,
            None,
            None,
            self._outbox_queue_count(),
        )

    def _watcher_status_view(self, status: WatcherStatus) -> WatcherStatusView:
        enabled = status.state not in {WatcherRunState.STOPPED, WatcherRunState.FAILED}
        return WatcherStatusView(
            enabled,
            status.state.value,
            status.strategy,
            self.configured_method,
            status.profile,
            status.folders or self.configured_folders,
            status.last_successful_check.astimezone(UTC).isoformat()
            if status.last_successful_check
            else None,
            self._safe(status.current_error),
            status.next_poll_at.astimezone(UTC).isoformat() if status.next_poll_at else None,
            status.pid,
            max(0, status.messages_detected - status.notifications_delivered),
        )

    def _watcher_rows(self) -> list[dict[str, Any]]:
        method = getattr(self.state, "list_watcher_status", None)
        if callable(method):
            return [asdict(row) for row in method()]
        return self._query(
            "SELECT * FROM watcher_runtime WHERE account_key=? ORDER BY updated_at DESC LIMIT 10",
            (self.state.account_key,),
        )

    def _outbox_queue_count(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS count FROM notification_outbox "
            "WHERE account_key=? AND state IN ('pending','retry','in_progress')",
            (self.state.account_key,),
        )
        return int(rows[0]["count"]) if rows else 0

    def _query(self, sql: str, parameters: Sequence[object] = ()) -> list[dict[str, Any]]:
        self.state.initialize()
        with self.state.connection() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]

    @staticmethod
    def _optional(value: object) -> str | None:
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _json_strings(value: object) -> tuple[str, ...]:
        if not isinstance(value, str):
            return ()
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(str(item) for item in decoded if isinstance(item, str))

    def _safe(self, value: object, *, length: int = 700) -> str:
        if value is None:
            return ""
        return sanitize_terminal(
            redact_text(str(value), redact_emails=self.redact_addresses),
            max_length=length,
        )

    def _activity_cleared_before(self) -> str | None:
        record = self.state.get_ui_preference("cleared_before", scope="activity")
        return str(record.value) if record is not None and isinstance(record.value, str) else None

    @staticmethod
    def _occurred_after(value: str, cutoff: str) -> bool:
        try:
            occurred = datetime.fromisoformat(value.replace("Z", "+00:00"))
            boundary = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=UTC)
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=UTC)
            return occurred > boundary
        except ValueError:
            return value > cutoff

    def _visible_activity_events(self) -> list[ActivityEventView]:
        events = [*self._events(), *self._watcher_log_events()]
        cutoff = self._activity_cleared_before()
        if cutoff is None:
            return events
        return [event for event in events if self._occurred_after(event.occurred_at, cutoff)]

    def _watcher_log_events(self) -> list[ActivityEventView]:
        """Return a bounded, re-sanitized tail of the real rotating watcher log."""

        path = self.log_path
        if path is None:
            return []
        occurred_at = "1970-01-01T00:00:00+00:00"
        if not path.is_file():
            return [
                ActivityEventView(
                    "watcher-log:unavailable",
                    occurred_at,
                    "watcher",
                    "info",
                    "Watcher log · no log file yet",
                    f"The configured redacted log will appear at {self._safe(path, length=500)}.",
                )
            ]
        try:
            occurred_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - (256 * 1024)))
                raw = handle.read(256 * 1024)
            lines = raw.decode("utf-8", errors="replace").splitlines()[-250:]
        except OSError as exc:
            return [
                ActivityEventView(
                    "watcher-log:read-error",
                    occurred_at,
                    "watcher",
                    "warning",
                    "Watcher log · unavailable",
                    self._safe(f"The redacted log could not be read ({type(exc).__name__})."),
                )
            ]
        events: list[ActivityEventView] = []
        for index, line in enumerate(lines):
            if not any(
                token in line.casefold()
                for token in ("watch", "monitor", "synchron", "checkpoint", "headless")
            ):
                continue
            safe_line = self._safe(line, length=1_000)
            digest = hashlib.sha256(f"{index}\0{safe_line}".encode()).hexdigest()[:16]
            parsed_time = safe_line[:23]
            try:
                datetime.fromisoformat(parsed_time)
            except ValueError:
                parsed_time = occurred_at
            events.append(
                ActivityEventView(
                    f"watcher-log:{digest}",
                    parsed_time,
                    "watcher",
                    "error" if " error " in f" {safe_line.casefold()} " else "info",
                    f"Watcher log · {safe_line[:180]}",
                    safe_line,
                )
            )
        if events:
            return events
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        return [
            ActivityEventView(
                "watcher-log:no-matching-lines",
                modified,
                "watcher",
                "info",
                "Watcher log · no watcher entries in bounded tail",
                f"Inspected the last 256 KiB of {self._safe(path, length=500)}.",
            )
        ]

    @staticmethod
    def _activity_plan_token(events: Sequence[ActivityEventView]) -> str:
        payload = json.dumps(
            [(event.event_id, event.occurred_at) for event in events],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _cleanup_undo_groups(self) -> dict[str, list[tuple[str, Mapping[str, Any]]]]:
        groups: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        now = datetime.now(UTC)
        for record in self.state.list_ui_preferences(scope="cleanup-undo"):
            value = record.value
            if not isinstance(value, Mapping):
                continue
            plan_id = value.get("plan_id")
            reference = value.get("reference")
            current_reference = value.get("current_reference")
            original_folder = value.get("original_folder")
            moved_at = value.get("moved_at")
            expires_at = value.get("expires_at")
            if not all(
                isinstance(item, str) and item
                for item in (
                    plan_id,
                    reference,
                    current_reference,
                    original_folder,
                    moved_at,
                    expires_at,
                )
            ):
                continue
            try:
                expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=UTC)
            except ValueError:
                continue
            if expiry.astimezone(UTC) <= now:
                continue
            groups.setdefault(str(plan_id), []).append((record.key, value))
        return groups

    @staticmethod
    def _cleanup_undo_token(records: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
        payload = json.dumps(
            [
                (
                    key,
                    value.get("current_reference"),
                    value.get("original_folder"),
                    value.get("expires_at"),
                )
                for key, value in sorted(records, key=lambda item: item[0])
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _cleanup_undo_plan(
        self,
        plan_id: str,
        records: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> ActivityUndoPlanView:
        ordered = sorted(records, key=lambda item: str(item[1].get("reference")))
        return ActivityUndoPlanView(
            event_id=f"cleanup-undo:{plan_id}",
            entries=tuple(
                ActivityUndoEntryView(
                    str(value["reference"]),
                    str(value["current_reference"]),
                    str(value["original_folder"]),
                )
                for _key, value in ordered
            ),
            moved_at=min(str(value["moved_at"]) for _key, value in ordered),
            expires_at=min(str(value["expires_at"]) for _key, value in ordered),
            confirmation_token=self._cleanup_undo_token(records),
        )

    def _events(self) -> list[ActivityEventView]:
        events: list[ActivityEventView] = []
        account = self.state.account_key
        operations = self._query(
            "SELECT o.operation_id,o.operation,o.updated_at,"
            "SUM(t.status='completed') AS completed,SUM(t.status='failed') AS failed,"
            "SUM(t.status IN ('uncertain','dispatching')) AS uncertain,"
            "SUM(t.status='not_started') AS not_started,SUM(t.status='cancelled') AS cancelled "
            "FROM (SELECT * FROM message_operations WHERE account_key=? "
            "ORDER BY updated_at DESC,operation_id LIMIT 200) o "
            "JOIN message_operation_targets t ON t.account_key=o.account_key "
            "AND t.operation_id=o.operation_id GROUP BY o.operation_id "
            "ORDER BY o.updated_at DESC,o.operation_id",
            (account,),
        )
        for row in operations:
            counts = {
                key: int(row[key] or 0)
                for key in ("completed", "failed", "uncertain", "not_started", "cancelled")
            }
            total = sum(counts.values())
            operation_id = str(row["operation_id"])
            summary = f"{self._safe(row['operation']).replace('_', ' ').capitalize()}: "
            summary += f"{counts['completed']} of {total} completed"
            if counts["uncertain"]:
                verb = "needs" if counts["uncertain"] == 1 else "need"
                summary += f" · {counts['uncertain']} {verb} checking"
            elif counts["failed"]:
                summary += f" · {counts['failed']} rejected"
            events.append(
                ActivityEventView(
                    f"message-operation:{operation_id}",
                    str(row["updated_at"]),
                    "mail",
                    "error" if counts["uncertain"] or counts["failed"] else "info",
                    summary,
                    " · ".join(
                        f"{key.replace('_', ' ').capitalize()}: {value}"
                        for key, value in counts.items()
                    )
                    + ". Open Operation details before retrying. "
                    "Recovery records survive Clear history.",
                    correlation_id=operation_id,
                )
            )
        for plan_id, records in self._cleanup_undo_groups().items():
            plan = self._cleanup_undo_plan(plan_id, records)
            events.append(
                ActivityEventView(
                    plan.event_id,
                    plan.moved_at,
                    "mail",
                    "info",
                    f"Recoverable cleanup move · {len(plan.entries)} item(s)",
                    f"Undo available until {plan.expires_at}. Original folders are retained "
                    "locally without message bodies.",
                    correlation_id=plan_id,
                    undoable=self.cleanup_restorer is not None,
                )
            )
        ledger = self._query(
            "SELECT event_id,event_type,detected_at,folder,payload_json FROM event_ledger "
            "WHERE account_key=? ORDER BY detected_at DESC LIMIT 500",
            (account,),
        )
        ledger_ids = {str(row["event_id"]) for row in ledger}
        webhook_names = {
            str(row["name"]).casefold() for row in self._query("SELECT name FROM webhooks")
        }

        def provider_category(provider: str) -> str:
            normalized = provider.casefold()
            bare = normalized.removeprefix("webhook:")
            return "webhook" if "webhook" in normalized or bare in webhook_names else "notification"

        for row in ledger:
            folder = self._safe(row.get("folder"), length=100)
            event_type = self._safe(row.get("event_type"), length=50) or "mail event"
            events.append(
                ActivityEventView(
                    str(row["event_id"]),
                    str(row["detected_at"]),
                    "mail",
                    "info",
                    f"{event_type.replace('_', ' ').title()}" + (f" · {folder}" if folder else ""),
                    "Durable event ledger entry; private payload fields are omitted.",
                )
            )
        attempts = self._query(
            "SELECT id,event_id,provider_id,attempt_no,state,status_code,error_summary,retry_at,"
            "attempted_at FROM notification_provider_attempts WHERE account_key=? "
            "ORDER BY attempted_at DESC,id DESC LIMIT 500",
            (account,),
        )
        for row in attempts:
            state = str(row["state"])
            provider = self._safe(row["provider_id"], length=100)
            source_event_id = str(row["event_id"])
            retryable = (
                self.retry_delivery_callback is not None
                and source_event_id in ledger_ids
                and state in {"failed", "dead_letter", "retry"}
            )
            events.append(
                ActivityEventView(
                    f"attempt:{row['id']}",
                    str(row["attempted_at"]),
                    provider_category(provider),
                    "error" if retryable else "info",
                    f"{provider or 'Provider'} delivery: {self._safe(state, length=30)}",
                    self._safe(row.get("error_summary")) or "No error detail recorded.",
                    correlation_id=source_event_id,
                    provider=provider or None,
                    retryable=retryable,
                    attempt_count=int(row.get("attempt_no") or 0),
                    next_retry_at=self._optional(row.get("retry_at")),
                )
            )
        legacy_deliveries = self._query(
            "SELECT id,event_id,provider,state,detail,attempts,created_at "
            "FROM notification_history ORDER BY created_at DESC,id DESC LIMIT 500"
        )
        for row in legacy_deliveries:
            state = str(row["state"])
            provider = self._safe(row["provider"], length=100)
            source_event_id = str(row["event_id"])
            retryable = (
                self.retry_delivery_callback is not None
                and source_event_id in ledger_ids
                and state in {"failed", "dead_letter", "retry"}
            )
            events.append(
                ActivityEventView(
                    f"delivery:{row['id']}",
                    str(row["created_at"]),
                    provider_category(provider),
                    "error" if retryable else "info",
                    f"{provider or 'Provider'} delivery: {self._safe(state, length=30)}",
                    self._safe(row.get("detail")) or "No additional delivery detail recorded.",
                    correlation_id=self._safe(source_event_id, length=180),
                    provider=provider or None,
                    retryable=retryable,
                    attempt_count=int(row.get("attempts") or 0),
                )
            )
        audits = self._query(
            "SELECT id,action,target_reference,detail_json,occurred_at FROM local_audit_events "
            "ORDER BY occurred_at DESC,id DESC LIMIT 500"
        )
        for row in audits:
            action = self._safe(row["action"], length=100)
            events.append(
                ActivityEventView(
                    f"audit:{row['id']}",
                    str(row["occurred_at"]),
                    "mail",
                    "info",
                    action or "Application operation",
                    "Sanitized local audit event; raw structured fields are omitted.",
                )
            )
        checkpoints = self._query(
            "SELECT scope,last_success_at,last_error,updated_at FROM sync_checkpoints "
            "ORDER BY updated_at DESC LIMIT 200"
        )
        for index, row in enumerate(checkpoints):
            error = self._safe(row.get("last_error"))
            events.append(
                ActivityEventView(
                    f"sync:{index}:{self._safe(row['scope'], length=80)}",
                    str(row["updated_at"]),
                    "watcher",
                    "error" if error else "info",
                    f"Synchronization checkpoint · {self._safe(row['scope'], length=100)}",
                    error
                    or f"Last success: {self._optional(row.get('last_success_at')) or 'unknown'}",
                    retryable=False,
                )
            )
        for index, row in enumerate(self._watcher_rows()):
            connection_state = self._safe(row.get("connection_state"), length=100)
            error = self._safe(row.get("error_summary"))
            occurred_at = self._optional(row.get("updated_at")) or self._optional(
                row.get("heartbeat_at")
            )
            if occurred_at is None:
                continue
            state = connection_state or "unknown"
            # A stopped one-shot watcher closes its session normally. A persisted
            # error or explicit failed/offline state still represents a failure.
            failed = bool(error) or state.casefold() in {"failed", "offline"}
            events.append(
                ActivityEventView(
                    f"connection:{index}:{self._safe(row.get('scope'), length=80)}",
                    occurred_at,
                    "connection",
                    "error" if failed else "info",
                    f"Mailbox connection: {state}",
                    error
                    or "Watcher connection state; credentials and endpoint secrets are omitted.",
                    retryable=False,
                )
            )
        schedule = self._query(
            "SELECT h.id,h.job_reference,h.event,h.detail,h.occurred_at "
            "FROM schedule_history AS h ORDER BY h.occurred_at DESC,h.id DESC LIMIT 300"
        )
        for row in schedule:
            detail = self._safe(row.get("detail"))
            events.append(
                ActivityEventView(
                    f"schedule:{row['id']}",
                    str(row["occurred_at"]),
                    "scheduler",
                    "error" if str(row["event"]) in {"failed", "stale_lease"} else "info",
                    f"Scheduled send: {self._safe(row['event'], length=80)}",
                    detail or "No additional detail recorded.",
                    retryable=False,
                )
            )
        backups = self._query(
            "SELECT backup_id,kind,state,item_count,size_bytes,verified_at,updated_at "
            "FROM backup_records ORDER BY updated_at DESC LIMIT 200"
        )
        for row in backups:
            state = str(row["state"])
            events.append(
                ActivityEventView(
                    f"backup:{row['backup_id']}:{row['updated_at']}",
                    str(row["updated_at"]),
                    "backup",
                    "error" if state in {"failed", "corrupt"} else "info",
                    f"Backup {self._safe(row['kind'], length=40)}: {self._safe(state, length=40)}",
                    f"Items: {int(row['item_count'])} · Stored bytes: {int(row['size_bytes'])}",
                    retryable=False,
                )
            )
        return events

    def load_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivitySnapshotView:
        try:
            _cancelled(cancel, "Loading activity")
            progress(OperationProgress("Reading durable local history"))
            events = self._visible_activity_events()
            if category == "error":
                events = [event for event in events if event.severity == "error"]
            elif category:
                events = [event for event in events if event.category == category]
            if text:
                needle = text.casefold()
                events = [
                    event
                    for event in events
                    if needle
                    in "\n".join(
                        (
                            event.event_id,
                            event.summary,
                            event.detail,
                            event.correlation_id or "",
                        )
                    ).casefold()
                ]
            events.sort(key=lambda item: (item.occurred_at, item.event_id), reverse=True)
            cutoff = self._activity_cleared_before()
            retention = "bounded durable local history"
            if cutoff is not None:
                retention += f" after {cutoff}"
            return ActivitySnapshotView(
                self._watcher(),
                tuple(events[:limit]),
                retention,
                limit,
                self.offline,
                self.redact_addresses,
            )
        except BaseException as exc:
            raise _safe_error("Loading activity", exc) from exc

    def set_watcher_enabled(
        self,
        enabled: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> WatcherStatusView:
        return self.control_watcher(
            "start" if enabled else "stop",
            cancel=cancel,
            progress=progress,
        )

    def control_watcher(
        self,
        action: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> WatcherStatusView:
        try:
            _cancelled(cancel, "Changing watcher state")
            normalized = action.casefold().strip()
            if normalized not in {"start", "stop", "pause", "resume", "sync"}:
                raise PublicOperationError("Unknown watcher control action.", retryable=False)
            if self.offline and normalized in {"start", "resume", "sync"}:
                raise OfflineOperationError()
            if normalized == "start":
                if self.watcher_launcher is None:
                    raise PublicOperationError(
                        "No safe watcher launcher is configured for this TUI session. Use the "
                        "headless watcher command or configure startup integration.",
                        retryable=False,
                    )
                progress(OperationProgress("Starting single-instance watcher"))
                launched = self.watcher_launcher()
                if launched is not None:
                    return self._watcher_status_view(launched)
                self._launch_requested = True
                current = self._watcher()
                if current.enabled:
                    return current
                return WatcherStatusView(
                    True,
                    "starting",
                    "detecting",
                    self.configured_method,
                    self.active_profile or "default",
                    self.configured_folders,
                    None,
                    None,
                    None,
                    None,
                    current.notification_queue_count,
                )
            if normalized == "sync":
                if self.watcher_synchronizer is None:
                    raise PublicOperationError(
                        "No safe one-shot synchronization launcher is configured.",
                        retryable=False,
                    )
                progress(OperationProgress("Requesting immediate watcher synchronization"))
                status = self.watcher_synchronizer()
                if status is not None:
                    current = self._watcher_status_view(status)
                    return replace(
                        current,
                        state="synchronization requested",
                        next_check_at=datetime.now(UTC).isoformat(),
                    )
                return WatcherStatusView(
                    True,
                    "one-shot synchronization starting",
                    "detecting",
                    self.configured_method,
                    self.active_profile or "default",
                    self.configured_folders,
                    None,
                    None,
                    datetime.now(UTC).isoformat(),
                    None,
                    self._outbox_queue_count(),
                )
            if self.watcher_store is None:
                raise PublicOperationError(
                    "No watcher control channel is configured.", retryable=False
                )
            current_status = self.watcher_store.read()
            active = (
                current_status is not None
                and current_status.state not in {WatcherRunState.STOPPED, WatcherRunState.FAILED}
                and process_is_alive(current_status.pid)
            )
            active = active or self._launch_requested
            if not active:
                if normalized == "stop":
                    return self._watcher()
                raise PublicOperationError(
                    f"The watcher is not running, so it cannot be {normalized}d.",
                    retryable=False,
                )
            progress(
                OperationProgress(
                    {
                        "stop": "Requesting clean watcher shutdown",
                        "pause": "Requesting watcher pause",
                        "resume": "Requesting watcher resume",
                    }[normalized]
                )
            )
            self.watcher_store.request(normalized)
            current = (
                self._watcher_status_view(current_status)
                if current_status is not None
                else WatcherStatusView(
                    True,
                    "starting",
                    "detecting",
                    self.configured_method,
                    self.active_profile or "default",
                    self.configured_folders,
                    None,
                    None,
                    None,
                    None,
                    self._outbox_queue_count(),
                )
            )
            if normalized == "stop":
                self._launch_requested = False
                return replace(current, enabled=False, state="stopping", next_check_at=None)
            if normalized == "pause":
                return replace(current, state="pause requested", next_check_at=None)
            return replace(current, enabled=True, state="resume requested")
        except BaseException as exc:
            raise _safe_error("Changing watcher state", exc) from exc

    def retry_delivery(
        self,
        event_id: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityEventView:
        try:
            _cancelled(cancel, "Retrying delivery")
            if self.retry_delivery_callback is None:
                raise PublicOperationError(
                    "No provider delivery dispatcher is attached to this TUI session.",
                    retryable=False,
                )
            progress(OperationProgress("Dispatching one idempotent retry"))
            return self.retry_delivery_callback(event_id)
        except BaseException as exc:
            raise _safe_error("Retrying delivery", exc) from exc

    def test_notifications(
        self,
        *,
        provider: str = "console",
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityEventView:
        try:
            _cancelled(cancel, "Testing notifications")
            if self.notification_test_callback is None:
                raise PublicOperationError(
                    "No notification dispatcher is attached to this TUI session.",
                    retryable=False,
                )
            progress(OperationProgress("Sending synthetic privacy-safe event"))
            return self.notification_test_callback(provider)
        except BaseException as exc:
            raise _safe_error("Testing notifications", exc) from exc

    def export_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str:
        try:
            snapshot = self.load_activity(
                category=category,
                text=text,
                limit=5_000,
                cancel=cancel,
                progress=progress,
            )
            _cancelled(cancel, "Exporting activity")
            lines = [
                json.dumps(
                    {
                        "schema_version": 1,
                        "exported_at": datetime.now(UTC).isoformat(),
                        "redaction_enabled": snapshot.redaction_enabled,
                        "bounded_limit": snapshot.bounded_limit,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ]
            lines.extend(
                json.dumps(asdict(event), ensure_ascii=False, sort_keys=True)
                for event in snapshot.events
            )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            target = self.export_directory / f"gucmail-activity-{stamp}.ndjson"
            return str(_atomic_text(target, "\n".join(lines) + "\n"))
        except BaseException as exc:
            raise _safe_error("Exporting activity", exc) from exc

    def plan_clear_activity(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityClearPlanView:
        try:
            _cancelled(cancel, "Planning activity-history clear")
            progress(OperationProgress("Fingerprinting visible local Activity events"))
            events = self._visible_activity_events()
            events.sort(key=lambda item: (item.occurred_at, item.event_id))
            return ActivityClearPlanView(
                event_count=len(events),
                categories=tuple(sorted({event.category for event in events})),
                oldest_at=events[0].occurred_at if events else None,
                newest_at=events[-1].occurred_at if events else None,
                preserved_operational_records=self._PRESERVED_ACTIVITY_STATE,
                confirmation_token=self._activity_plan_token(events),
            )
        except BaseException as exc:
            raise _safe_error("Planning activity-history clear", exc) from exc

    def execute_clear_activity(
        self,
        plan: ActivityClearPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityClearResultView:
        try:
            if not confirmed:
                raise PublicOperationError(
                    "Clearing Activity history requires explicit confirmation.",
                    retryable=False,
                )
            _cancelled(cancel, "Clearing local Activity history")
            current = self.plan_clear_activity(cancel=cancel, progress=progress)
            if current.confirmation_token != plan.confirmation_token:
                raise PublicOperationError(
                    "Local Activity changed after the preview; review the new exact scope.",
                    retryable=False,
                )
            cutoff = plan.newest_at or datetime.now(UTC).isoformat()
            progress(OperationProgress("Persisting the local Activity clear boundary"))
            self.state.put_ui_preference("cleared_before", cutoff, scope="activity")
            return ActivityClearResultView(
                cleared_count=plan.event_count,
                cleared_before=cutoff,
                preserved_operational_records=self._PRESERVED_ACTIVITY_STATE,
            )
        except BaseException as exc:
            raise _safe_error("Clearing local Activity history", exc) from exc

    def plan_cleanup_undo(
        self,
        event_id: str | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityUndoPlanView:
        try:
            _cancelled(cancel, "Planning cleanup undo")
            progress(OperationProgress("Reading unexpired recoverable cleanup journal"))
            groups = self._cleanup_undo_groups()
            if not groups:
                raise PublicOperationError(
                    "No unexpired recoverable cleanup move is available.", retryable=False
                )
            requested = (
                event_id.removeprefix("cleanup-undo:")
                if event_id and event_id.startswith("cleanup-undo:")
                else None
            )
            if requested is not None:
                records = groups.get(requested)
                if records is None:
                    raise PublicOperationError(
                        "The selected cleanup undo entry expired or was already used.",
                        retryable=False,
                    )
                plan_id = requested
            else:
                plan_id, records = max(
                    groups.items(),
                    key=lambda item: max(
                        str(value.get("moved_at") or "") for _key, value in item[1]
                    ),
                )
            return self._cleanup_undo_plan(plan_id, records)
        except BaseException as exc:
            raise _safe_error("Planning cleanup undo", exc) from exc

    def execute_cleanup_undo(
        self,
        plan: ActivityUndoPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityUndoResultView:
        try:
            if not confirmed:
                raise PublicOperationError(
                    "Cleanup undo requires explicit confirmation.", retryable=False
                )
            restorer = self.cleanup_restorer
            if restorer is None:
                raise OfflineOperationError("A live cleanup-undo provider is unavailable.")
            current = self.plan_cleanup_undo(
                plan.event_id,
                cancel=cancel,
                progress=progress,
            )
            if current.confirmation_token != plan.confirmation_token:
                raise PublicOperationError(
                    "The cleanup undo journal changed after preview; review it again.",
                    retryable=False,
                )
            plan_id = plan.event_id.removeprefix("cleanup-undo:")
            records = self._cleanup_undo_groups().get(plan_id, [])
            by_current = {str(value["current_reference"]): (key, value) for key, value in records}
            restored = 0
            failures: list[str] = []
            for entry in plan.entries:
                _cancelled(cancel, "Undoing cleanup move")
                progress(
                    OperationProgress(
                        "Restoring cleanup item to its original folder",
                        completed=restored,
                        total=len(plan.entries),
                    )
                )
                record = by_current.get(entry.current_reference)
                if record is None:
                    failures.append(entry.reference)
                    break
                key, _value = record
                try:
                    restorer(entry.current_reference, entry.original_folder)
                except Exception:
                    failures.append(entry.reference)
                    break
                self.state.remove_ui_preference(key, scope="cleanup-undo")
                restored += 1
            return ActivityUndoResultView(
                restored_count=restored,
                failed_count=len(failures),
                failed_references=tuple(failures),
            )
        except BaseException as exc:
            raise _safe_error("Undoing cleanup move", exc) from exc


__all__ = [
    "ActivityAreaBackend",
    "BackupAreaBackend",
    "BackupPassphraseProvider",
    "BackupSourceFactory",
    "DeliveryRetry",
    "NotificationTest",
    "PreviewHandler",
    "SettingsAreaBackend",
    "StorageAreaBackend",
    "StorageAttachmentLoader",
    "StorageMessageLoader",
    "WatcherLauncher",
]
