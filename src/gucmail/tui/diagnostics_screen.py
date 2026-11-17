"""Operational, offline-first diagnostics, repair, index, and bundle surface."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, ClassVar, Protocol

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, Input, Label, Static

from gucmail.bootstrap import AppContext
from gucmail.doctor import run_doctor, write_diagnostic_bundle
from gucmail.errors import ConflictError
from gucmail.ews.capabilities import DEFAULT_REGISTRY
from gucmail.security import sanitize_terminal
from gucmail.services.attachment_indexing import (
    AttachmentIndexPreferences,
    MailboxIndexProducer,
    attachment_index_preferences,
)
from gucmail.services.index import IndexRevision, SearchIndex
from gucmail.settings import SettingsContext, SettingsService
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
    ViewState,
)


@dataclass(frozen=True, slots=True)
class DiagnosticsSnapshot:
    checks: tuple[str, ...]
    state_health: Mapping[str, Any]
    index_health: Mapping[str, Any]
    view: str = "overview"
    title: str = "Local overview"
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CacheRepairPlan:
    counts: tuple[tuple[str, int], ...]
    confirmation_scope: str

    @property
    def total_rows(self) -> int:
        return sum(count for _table, count in self.counts)


@dataclass(frozen=True, slots=True)
class CacheRepairResult:
    removed: tuple[tuple[str, int], ...]
    integrity: str


@dataclass(frozen=True, slots=True)
class IndexRebuildPlan:
    folder: str
    limit: int
    existing_documents: int
    confirmation_scope: str
    updated_at: str | None = None
    revision_id: str | None = None
    attachment_preferences: AttachmentIndexPreferences = field(
        default_factory=AttachmentIndexPreferences
    )


@dataclass(frozen=True, slots=True)
class IndexRebuildResult:
    removed: int
    indexed: int
    integrity: str
    attachment_files: int = 0
    attachment_outcomes: tuple[tuple[str, int], ...] = ()
    attachments_enabled: bool = False


class DiagnosticsBackend(Protocol):
    def snapshot(
        self,
        *,
        view: str = "overview",
        cancel: Event,
        progress: ProgressReporter,
    ) -> DiagnosticsSnapshot: ...

    def export_bundle(
        self, destination: str, *, cancel: Event, progress: ProgressReporter
    ) -> Path: ...

    def plan_cache_repair(
        self, *, cancel: Event, progress: ProgressReporter
    ) -> CacheRepairPlan: ...

    def repair_cache(
        self,
        plan: CacheRepairPlan,
        *,
        confirmation_scope: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CacheRepairResult: ...

    def plan_index_rebuild(
        self,
        folder: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> IndexRebuildPlan: ...

    def rebuild_index(
        self,
        plan: IndexRebuildPlan,
        *,
        confirmation_scope: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> IndexRebuildResult: ...


class LocalDiagnosticsBackend:
    """Compose existing doctor, StateStore, and SearchIndex contracts."""

    def __init__(
        self,
        context: AppContext,
        *,
        gateway_factory: Callable[[], Any] | None = None,
        close_runtime: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self.gateway_factory = gateway_factory
        self.close_runtime = close_runtime or context.close

    def _index_preferences(self) -> AttachmentIndexPreferences:
        source = SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )
        return attachment_index_preferences(source)

    @staticmethod
    def _cancelled(cancel: Event, action: str) -> None:
        if cancel.is_set():
            raise PublicOperationError(f"{action} was cancelled safely.", retryable=False)

    @staticmethod
    def _scope(name: str, payload: object) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return f"{name}:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"

    def snapshot(
        self,
        *,
        view: str = "overview",
        cancel: Event,
        progress: ProgressReporter,
    ) -> DiagnosticsSnapshot:
        if view not in {
            "overview",
            "connection_status",
            "ews_capability_matrix",
            "cache_health",
            "database_integrity_check",
        }:
            raise PublicOperationError("Unknown diagnostics view.", retryable=False)
        self._cancelled(cancel, "Diagnostics refresh")
        progress(OperationProgress("Running local doctor checks"))
        checks = run_doctor(self.context, network=False, authenticate=False)
        self._cancelled(cancel, "Diagnostics refresh")
        progress(OperationProgress("Verifying local databases"))
        state_health = self.context.state.health()
        index = SearchIndex(self.context.paths.index_database)
        index_health = asdict(index.verify())
        title = "Local overview"
        details: tuple[str, ...] = ()
        if view == "connection_status":
            title = "Read-only connection status"
            if self.gateway_factory is None:
                details = (
                    "Connection check not run: the application is offline.",
                    "Leave offline mode before requesting a authenticated status read.",
                )
            else:
                self._cancelled(cancel, "Connection check")
                progress(OperationProgress("Reading one mailbox status response"))
                status = self.gateway_factory().status()
                details = (
                    "Connection: available",
                    "Exchange version: "
                    + sanitize_terminal(str(status.get("exchange_version") or "unknown")),
                    "Inbox count: " + sanitize_terminal(str(status.get("inbox_count", "unknown"))),
                    "This read does not modify mailbox items.",
                )
        elif view == "ews_capability_matrix":
            title = "EWS operation and capability matrix"
            capabilities = DEFAULT_REGISTRY.all()
            implemented = sum(
                item.implementation_status.value == "implemented" for item in capabilities
            )
            details = (
                f"Represented operations: {len(capabilities)} · "
                f"implemented adapters: {implemented}",
                "Server and account axes remain not_probed until live evidence exists.",
                "",
                *(
                    f"{item.family} / {item.operation} · {item.safety.value} · "
                    f"{item.implementation_path.value}:{item.implementation_status.value} · "
                    f"server={item.server_support.value} · account={item.account_access.value} · "
                    f"verification={item.verification.value}"
                    for item in capabilities
                ),
            )
        elif view == "cache_health":
            title = "Disposable cache health"
            counts = self.context.state.disposable_cache_counts()
            details = (
                "Disposable cache rows (safe to rebuild; server remains authoritative):",
                *(f"{name}: {count}" for name, count in sorted(counts.items())),
            )
        elif view == "database_integrity_check":
            title = "Local database integrity"
            details = (
                "State and FTS databases were opened read-only for integrity reporting.",
                f"State integrity: {state_health.get('integrity', 'unknown')}",
                f"State foreign-key errors: {state_health.get('foreign_key_errors', 'unknown')}",
                f"Index integrity: {index_health.get('integrity', 'unknown')}",
                f"Index valid: {bool(index_health.get('valid'))}",
            )
        return DiagnosticsSnapshot(
            checks=tuple(f"[{row.status}] {row.name}: {row.detail}" for row in checks),
            state_health=state_health,
            index_health=index_health,
            view=view,
            title=title,
            details=details,
        )

    def export_bundle(self, destination: str, *, cancel: Event, progress: ProgressReporter) -> Path:
        self._cancelled(cancel, "Diagnostic export")
        progress(OperationProgress("Building redacted local checks"))
        checks = run_doctor(self.context, network=False, authenticate=False)
        self._cancelled(cancel, "Diagnostic export")
        progress(OperationProgress("Writing JSON bundle"))
        return write_diagnostic_bundle(self.context, Path(destination), results=checks)

    def plan_cache_repair(self, *, cancel: Event, progress: ProgressReporter) -> CacheRepairPlan:
        self._cancelled(cancel, "Cache repair preview")
        progress(OperationProgress("Counting disposable cache rows"))
        counts = tuple(sorted(self.context.state.disposable_cache_counts().items()))
        return CacheRepairPlan(counts, self._scope("cache-repair", counts))

    def repair_cache(
        self,
        plan: CacheRepairPlan,
        *,
        confirmation_scope: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CacheRepairResult:
        if confirmation_scope != plan.confirmation_scope:
            raise PublicOperationError("Cache repair confirmation does not match its preview.")
        self._cancelled(cancel, "Cache repair")
        current = tuple(sorted(self.context.state.disposable_cache_counts().items()))
        if current != plan.counts:
            raise ConflictError("Disposable cache changed after preview; review the scope again.")
        progress(OperationProgress("Closing reusable runtime handles"))
        self.close_runtime()
        self._cancelled(cancel, "Cache repair")
        progress(OperationProgress("Clearing only rebuildable cache tables"))
        removed = tuple(sorted(self.context.state.clear_disposable_cache().items()))
        integrity = str(self.context.state.health().get("integrity", "unknown"))
        return CacheRepairResult(removed, integrity)

    def plan_index_rebuild(
        self,
        folder: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> IndexRebuildPlan:
        clean_folder = folder.strip()
        if not clean_folder:
            raise PublicOperationError("Choose one folder for the index rebuild.")
        if not 1 <= limit <= 500:
            raise PublicOperationError("Index rebuild limit must be between 1 and 500.")
        self._cancelled(cancel, "Index rebuild preview")
        progress(OperationProgress("Inspecting disposable local index"))
        revision = SearchIndex(self.context.paths.index_database).revision()
        existing = revision.documents
        preferences = self._index_preferences()
        payload = {
            "folder": clean_folder,
            "limit": limit,
            "existing": existing,
            "updated_at": revision.updated_at,
            "revision_id": revision.revision_id,
            "attachment_policy": preferences.fingerprint,
        }
        return IndexRebuildPlan(
            clean_folder,
            limit,
            existing,
            self._scope("index-rebuild", payload),
            revision.updated_at,
            revision.revision_id,
            preferences,
        )

    def rebuild_index(
        self,
        plan: IndexRebuildPlan,
        *,
        confirmation_scope: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> IndexRebuildResult:
        if confirmation_scope != plan.confirmation_scope:
            raise PublicOperationError("Index rebuild confirmation does not match its preview.")
        if self.gateway_factory is None:
            raise OfflineOperationError(
                "Index rebuild needs online message metadata; local verification "
                "remains available."
            )
        index = SearchIndex(self.context.paths.index_database)
        expected = IndexRevision(plan.existing_documents, plan.updated_at, plan.revision_id)
        if index.revision() != expected:
            raise ConflictError("The local index changed after preview; review the rebuild again.")
        if self._index_preferences() != plan.attachment_preferences:
            raise ConflictError(
                "Attachment indexing settings changed after preview; review the rebuild again."
            )
        self._cancelled(cancel, "Index rebuild")
        progress(OperationProgress("Loading index sources", total=plan.limit))
        producer = MailboxIndexProducer(
            self.gateway_factory(), plan.attachment_preferences, cancel_event=cancel
        )
        documents = producer.documents(
            folder=plan.folder,
            limit=plan.limit,
            progress=lambda completed: progress(
                OperationProgress(
                    "Preparing index replacement", completed=completed, total=plan.limit
                )
            ),
        )
        replaced = index.replace_many(
            documents,
            limit=plan.limit,
            expected_revision=expected,
            cancel_event=cancel,
        )
        verification = index.verify()
        if not verification.valid:
            raise PublicOperationError("The rebuilt local index failed its integrity check.")
        return IndexRebuildResult(
            replaced.removed,
            replaced.indexed,
            verification.integrity,
            producer.report.files_downloaded,
            tuple(sorted(producer.report.outcomes.items())),
            plan.attachment_preferences.enabled,
        )


class DiagnosticsScreen(OperationalScreen):
    """One coherent local diagnostics screen with previewed repair actions."""

    DEFAULT_CSS = """
    DiagnosticsScreen { layout: vertical; }
    #diagnostics-root { width: 100%; height: 100%; padding: 1 2; }
    #diagnostics-title { height: 2; text-style: bold; }
    #diagnostics-summary-pane { height: 1fr; min-height: 8; border: solid $panel; padding: 1; }
    #diagnostics-summary { height: auto; }
    #diagnostics-export-row, #diagnostics-index-row { height: 3; }
    #diagnostics-export-path, #diagnostics-index-folder { width: 1fr; margin-right: 1; }
    #diagnostics-index-limit { width: 12; margin-right: 1; }
    #diagnostics-actions { height: auto; min-height: 3; align-horizontal: left; }
    #diagnostics-actions Button { margin-right: 1; }
    #diagnostics-footer { height: 3; align-horizontal: right; }
    #diagnostics-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    DiagnosticsScreen.compact #diagnostics-root { padding: 1; }
    DiagnosticsScreen.compact #diagnostics-export-row,
    DiagnosticsScreen.compact #diagnostics-index-row,
    DiagnosticsScreen.compact #diagnostics-actions { layout: vertical; height: auto; }
    DiagnosticsScreen.compact #diagnostics-export-row Input,
    DiagnosticsScreen.compact #diagnostics-index-row Input,
    DiagnosticsScreen.compact #diagnostics-actions Button { width: 100%; margin-right: 0; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("e", "export_bundle", "Export", show=False),
        Binding("i", "rebuild_index", "Rebuild index", show=False),
        Binding("c", "repair_cache", "Repair cache", show=False),
    ]

    def __init__(
        self,
        backend: DiagnosticsBackend,
        *,
        default_export_path: str,
        default_folder: str = "inbox",
        offline: bool = False,
        initial_action: str | None = None,
    ) -> None:
        super().__init__(area_name="diagnostics", offline=offline)
        self.backend = backend
        self.default_export_path = default_export_path
        self.default_folder = default_folder
        self._view = "overview"
        self._snapshot: DiagnosticsSnapshot | None = None
        if initial_action not in {None, "rebuild_index", "repair_cache", "export_bundle"}:
            raise ValueError("Unknown initial diagnostics action.")
        self._initial_action = initial_action

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="diagnostics-root"):
            yield Label("Diagnostics & Local Repair", id="diagnostics-title")
            yield Static("Loading local checks…", id="area-state", markup=False)
            with VerticalScroll(id="diagnostics-summary-pane", can_focus=True):
                yield Static("", id="diagnostics-summary", markup=False)
            with Horizontal(id="diagnostics-export-row"):
                yield Input(value=self.default_export_path, id="diagnostics-export-path")
                yield Button("Export bundle", id="diagnostics-export")
            with Horizontal(id="diagnostics-index-row"):
                yield Input(value=self.default_folder, id="diagnostics-index-folder")
                yield Input(value="500", id="diagnostics-index-limit", type="integer")
                yield Button("Rebuild index…", id="diagnostics-rebuild-index")
            with Horizontal(id="diagnostics-actions"):
                yield Button("Repair disposable cache…", id="diagnostics-repair-cache")
                yield Button("Verify / Refresh", id="diagnostics-refresh", variant="primary")
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="diagnostics-footer"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 90 or self.size.height < 28, "compact")
        action = (
            getattr(self, f"action_{self._initial_action}")
            if self._initial_action is not None
            else self.action_refresh
        )
        self.call_after_refresh(action)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 90 or event.size.height < 28, "compact")

    def action_refresh(self) -> None:
        self._begin_operation(
            "Verifying local diagnostics",
            lambda cancel, progress: self.backend.snapshot(
                view=self._view,
                cancel=cancel,
                progress=progress,
            ),
            self._snapshot_loaded,
            retry_safe=True,
        )

    def configure_view(self, view: str) -> None:
        """Select and execute one exact read-only palette diagnostics view."""

        if view not in {
            "overview",
            "connection_status",
            "ews_capability_matrix",
            "cache_health",
            "database_integrity_check",
        }:
            raise ValueError("unknown diagnostics view")
        self._view = view
        if self.is_mounted:
            self.action_refresh()

    def _snapshot_loaded(self, value: object) -> None:
        if not isinstance(value, DiagnosticsSnapshot):
            raise PublicOperationError("Diagnostics backend returned an invalid snapshot.")
        self._snapshot = value
        state = value.state_health
        index = value.index_health
        lines = [
            *value.details,
            "",
            f"State database: {state.get('integrity', 'unknown')} · "
            f"schema {state.get('schema_version', 'unknown')} · "
            f"foreign-key errors {state.get('foreign_key_errors', 'unknown')}",
            f"Search index: {'valid' if index.get('valid') else 'invalid'} · "
            f"documents {index.get('documents', 'unknown')} · "
            f"FTS rows {index.get('full_text_rows', 'unknown')}",
            "",
            *value.checks,
        ]
        self.query_one("#diagnostics-title", Label).update(f"Diagnostics · {value.title}")
        self.query_one("#diagnostics-summary", Static).update("\n".join(lines))
        valid = state.get("integrity") == "ok" and bool(index.get("valid"))
        self._set_view_state(
            ViewState.READY if valid else ViewState.ERROR,
            "Local databases verified." if valid else "One or more local checks need attention.",
        )

    def action_export_bundle(self) -> None:
        destination = self.query_one("#diagnostics-export-path", Input).value.strip()
        if not destination:
            self._show_error(
                "Export diagnostics",
                PublicOperationError("Choose a new .json destination first.", retryable=False),
            )
            return
        detail = (
            f"Destination: {sanitize_terminal(destination, max_length=500)}\n\n"
            "The bundle contains redacted configuration, local paths, package/platform facts, "
            "and local health checks. It contains no credentials, message bodies, attachments, "
            "or authenticated/network probes. Existing files are never overwritten."
        )
        self._confirm(
            "Export diagnostics?",
            detail,
            lambda: self._begin_operation(
                "Exporting diagnostics",
                lambda cancel, progress: self.backend.export_bundle(
                    destination, cancel=cancel, progress=progress
                ),
                self._bundle_exported,
            ),
            confirm_label="Export",
        )

    def _bundle_exported(self, value: object) -> None:
        if not isinstance(value, Path):
            raise PublicOperationError("Diagnostics backend returned an invalid export path.")
        self._set_view_state(ViewState.READY, f"Diagnostic report written to {value}")

    def action_repair_cache(self) -> None:
        self._begin_operation(
            "Checking the cache",
            lambda cancel, progress: self.backend.plan_cache_repair(
                cancel=cancel, progress=progress
            ),
            self._cache_plan_loaded,
            retry_safe=True,
        )

    def _cache_plan_loaded(self, value: object) -> None:
        if not isinstance(value, CacheRepairPlan):
            raise PublicOperationError("Diagnostics backend returned an invalid cache plan.")
        detail = (
            f"Rebuildable rows to clear: {value.total_rows}\n"
            + "\n".join(f"{table}: {count}" for table, count in value.counts)
            + "\n\nPreserved: settings, saved searches, watcher checkpoints/leases, schedules, "
            "notification delivery state, backups, drafts, and activity history. Runtime handles "
            "are closed before the exact previewed scope is cleared."
        )
        self._confirm(
            "Repair disposable cache?",
            detail,
            lambda: self._begin_operation(
                "Repairing the cache",
                lambda cancel, progress: self.backend.repair_cache(
                    value,
                    confirmation_scope=value.confirmation_scope,
                    cancel=cancel,
                    progress=progress,
                ),
                self._cache_repaired,
            ),
            confirm_label="Repair cache",
        )

    def _cache_repaired(self, value: object) -> None:
        if not isinstance(value, CacheRepairResult):
            raise PublicOperationError("Diagnostics backend returned an invalid repair result.")
        self._set_view_state(
            ViewState.READY,
            f"Cleared {sum(count for _table, count in value.removed)} rebuildable row(s); "
            f"database integrity: {value.integrity}.",
        )
        self.action_refresh()

    def action_rebuild_index(self) -> None:
        folder = self.query_one("#diagnostics-index-folder", Input).value.strip()
        try:
            limit = int(self.query_one("#diagnostics-index-limit", Input).value)
        except ValueError:
            limit = 0
        self._begin_operation(
            "Previewing index rebuild",
            lambda cancel, progress: self.backend.plan_index_rebuild(
                folder,
                limit,
                cancel=cancel,
                progress=progress,
            ),
            self._index_plan_loaded,
            retry_safe=True,
        )

    def _index_plan_loaded(self, value: object) -> None:
        if not isinstance(value, IndexRebuildPlan):
            raise PublicOperationError("Diagnostics backend returned an invalid index plan.")
        detail = (
            f"Folder: {value.folder}\nBounded metadata limit: {value.limit}\n"
            f"Current disposable index rows to replace: {value.existing_documents}\n\n"
            + (
                "Message bodies and attachments are not downloaded. "
                if not value.attachment_preferences.enabled
                else "Message bodies are not downloaded. "
                + value.attachment_preferences.review()
                + "\n\n"
            )
            + "Up to 32 MiB of prepared "
            "field/payload data is collected before one atomic replacement. A read, validation "
            "or SQL failure, observed cancellation before commit, or a changed index revision "
            "retains the previous usable index. The mailbox is never mutated."
        )
        self.query_one("#diagnostics-title", Label).update("Diagnostics · Index rebuild preview")
        self.query_one("#diagnostics-summary", Static).update(detail)
        self._set_view_state(
            ViewState.READY, "Rebuild preview prepared; no index or mailbox change."
        )
        self._confirm(
            "Rebuild local search index?",
            detail,
            lambda: self._begin_operation(
                "Rebuilding local search index",
                lambda cancel, progress: self.backend.rebuild_index(
                    value,
                    confirmation_scope=value.confirmation_scope,
                    cancel=cancel,
                    progress=progress,
                ),
                self._index_rebuilt,
                timeout_seconds=120.0,
            ),
            confirm_label="Rebuild index",
        )

    def _index_rebuilt(self, value: object) -> None:
        if not isinstance(value, IndexRebuildResult):
            raise PublicOperationError("Diagnostics backend returned an invalid rebuild result.")
        self.query_one("#diagnostics-title", Label).update("Diagnostics · Index rebuild result")
        self.query_one("#diagnostics-summary", Static).update(
            f"Previous index rows replaced: {value.removed}\n"
            f"New indexed metadata records: {value.indexed}\n"
            f"Database integrity: {value.integrity}\n\n"
            + (
                "Message bodies were not downloaded. "
                f"Attachment files downloaded: {value.attachment_files}. "
                f"Outcomes: {dict(value.attachment_outcomes)}.\n"
                if value.attachments_enabled
                else "Message bodies and attachments were not downloaded. "
            )
            + "The mailbox was not changed.\n"
            "Use Verify / Refresh for current local health checks, or open Local Search."
        )
        self._set_view_state(
            ViewState.READY,
            f"Index rebuilt: {value.removed} removed, {value.indexed} indexed; "
            f"integrity {value.integrity}.",
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "diagnostics-export": self.action_export_bundle,
            "diagnostics-rebuild-index": self.action_rebuild_index,
            "diagnostics-repair-cache": self.action_repair_cache,
            "diagnostics-refresh": self.action_refresh,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


__all__: Sequence[str] = (
    "CacheRepairPlan",
    "CacheRepairResult",
    "DiagnosticsBackend",
    "DiagnosticsScreen",
    "DiagnosticsSnapshot",
    "IndexRebuildPlan",
    "IndexRebuildResult",
    "LocalDiagnosticsBackend",
)
