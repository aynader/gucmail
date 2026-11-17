"""First-class local index, backup, and similarity search workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import ClassVar, Protocol, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, Checkbox, Input, Label, Static

from gucmail.services.local_search import (
    LocalSearchMode,
    LocalSearchReport,
    LocalSearchResult,
    LocalSearchService,
)
from gucmail.tui.area_common import (
    OperationalScreen,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.events import (
    LocalSearchHelpRequested,
    LocalSearchIndexRequested,
    LocalSearchOpenRequested,
    LocalSearchScopeChanged,
    LocalSearchSemanticSetupRequested,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(frozen=True, slots=True)
class LocalSearchRequest:
    query: str
    mode: LocalSearchMode
    fuzzy: bool = False
    exact: bool = False
    minimum_score: int = 75
    limit: int = 100
    reference: str | None = None
    sender: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    minimum_size: int | None = None
    maximum_size: int | None = None
    attachment_extension: str | None = None
    backup_id: str | None = None


class LocalSearchBackend(Protocol):
    def search(
        self,
        request: LocalSearchRequest,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> LocalSearchReport: ...


class ServiceLocalSearchBackend:
    """Thread-bound adapter over the Exchange-independent local search service."""

    def __init__(self, service: LocalSearchService) -> None:
        self.service = service

    def search(
        self,
        request: LocalSearchRequest,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> LocalSearchReport:
        if cancel.is_set():
            raise PublicOperationError("Search was cancelled safely.", retryable=False)
        phase = {
            LocalSearchMode.INDEX: "Searching local full-text index",
            LocalSearchMode.ATTACHMENTS: "Searching indexed attachment names and text",
            LocalSearchMode.BACKUPS: "Searching immutable backup archives",
            LocalSearchMode.ALL: "Searching all local sources",
            LocalSearchMode.SEMANTIC: "Searching private local topics",
            LocalSearchMode.SIMILAR: "Comparing indexed content",
            LocalSearchMode.DUPLICATES: "Analyzing exact and possible duplicates",
            LocalSearchMode.NEAR_DUPLICATES: "Analyzing near-duplicates",
        }[request.mode]
        progress(OperationProgress(phase))
        return self.service.search(
            request.query,
            mode=request.mode,
            fuzzy=request.fuzzy,
            exact=request.exact,
            minimum_score=request.minimum_score,
            limit=request.limit,
            reference=request.reference,
            sender=request.sender,
            after=request.after,
            before=request.before,
            minimum_size=request.minimum_size,
            maximum_size=request.maximum_size,
            attachment_extension=request.attachment_extension,
            backup_id=request.backup_id,
        )


class LocalSearchScreen(OperationalScreen):
    """Responsive result explorer whose source labels never imply live Exchange data."""

    DEFAULT_CSS = """
    LocalSearchScreen { layout: vertical; background: $background; color: $foreground; }
    #local-search-root { width: 100%; height: 100%; padding: 1 2; }
    #local-search-title { height: 2; text-style: bold; }
    #local-search-controls { height: auto; min-height: 3; }
    #local-search-query { width: 2fr; min-width: 20; }
    #local-search-mode { width: 28; }
    #local-search-fuzzy { width: auto; min-width: 13; padding: 0 1; }
    #local-search-exact { width: auto; min-width: 20; padding: 0 1; }
    #local-search-run { width: 12; }
    #area-state { height: auto; min-height: 1; color: $text-muted; }
    #local-search-source { height: auto; min-height: 2; padding: 0 1; }
    #local-search-content { height: 1fr; min-height: 8; }
    #local-search-table { width: 3fr; height: 100%; border: solid $panel; }
    #local-search-detail-pane {
        width: 2fr; height: 100%; border: solid $panel; padding: 1; overflow-y: auto;
    }
    #area-progress { height: auto; min-height: 1; }
    #local-search-actions { height: auto; min-height: 3; align-horizontal: right; }
    #local-search-actions Button { margin-left: 1; }
    LocalSearchScreen.narrow #local-search-controls { layout: vertical; height: auto; }
    LocalSearchScreen.narrow #local-search-query,
    LocalSearchScreen.narrow #local-search-mode,
    LocalSearchScreen.narrow #local-search-fuzzy,
    LocalSearchScreen.narrow #local-search-exact { width: 100%; }
    LocalSearchScreen.narrow #local-search-content { layout: vertical; }
    LocalSearchScreen.narrow #local-search-table { width: 100%; height: 2fr; }
    LocalSearchScreen.narrow #local-search-detail-pane { width: 100%; height: 1fr; min-height: 8; }
    LocalSearchScreen.narrow #local-search-content { min-height: 20; }
    LocalSearchScreen.narrow #local-search-actions {
        layout: grid; grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 3; height: 12;
    }
    LocalSearchScreen.narrow #local-search-actions Button { width: 1fr; min-width: 0; margin: 0; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("ctrl+f", "focus_query", "Search", show=True),
        Binding("enter", "open_result", "Open", show=True),
        Binding("s", "similar", "Similar", show=True),
        Binding("d", "duplicates", "Duplicates", show=False),
        Binding("n", "near_duplicates", "Near duplicates", show=False),
        Binding("f1", "help", "Help", show=True),
    ]

    def __init__(
        self,
        backend: LocalSearchBackend,
        *,
        initial_mode: LocalSearchMode | str = LocalSearchMode.ALL,
        initial_query: str = "",
        initial_reference: str | None = None,
        initial_fuzzy: bool = False,
        minimum_score: int = 75,
        initial_exact: bool = False,
        initial_sender: str | None = None,
        initial_after: datetime | None = None,
        initial_before: datetime | None = None,
        initial_minimum_size: int | None = None,
        initial_maximum_size: int | None = None,
        initial_attachment_extension: str | None = None,
        initial_backup_id: str | None = None,
        auto_run: bool = False,
    ) -> None:
        super().__init__(area_name="search", offline=True, operation_timeout_seconds=60)
        self.backend = backend
        self.initial_mode = LocalSearchMode(initial_mode)
        self.initial_query = initial_query
        self.selected_reference = initial_reference
        self.initial_fuzzy = initial_fuzzy
        self.minimum_score = minimum_score
        self.initial_exact = initial_exact
        self.initial_sender = initial_sender
        self.initial_after = initial_after
        self.initial_before = initial_before
        self.initial_minimum_size = initial_minimum_size
        self.initial_maximum_size = initial_maximum_size
        self.initial_attachment_extension = initial_attachment_extension
        self.selected_backup_id = initial_backup_id
        self.auto_run = auto_run
        self._results: dict[str, LocalSearchResult] = {}
        self._last_report: LocalSearchReport | None = None

    def compose(self) -> ComposeResult:
        modes = (
            ("All local sources", LocalSearchMode.ALL.value),
            ("Local full-text index", LocalSearchMode.INDEX.value),
            ("Indexed attachment content", LocalSearchMode.ATTACHMENTS.value),
            ("Backup archives", LocalSearchMode.BACKUPS.value),
            ("Similar to selected", LocalSearchMode.SIMILAR.value),
            ("Duplicate groups", LocalSearchMode.DUPLICATES.value),
            ("Near-duplicate groups", LocalSearchMode.NEAR_DUPLICATES.value),
            ("Local topic search", LocalSearchMode.SEMANTIC.value),
        )
        with VerticalScroll(id="local-search-root"):
            yield Label("Search · local index and offline archives", id="local-search-title")
            with Horizontal(id="local-search-controls"):
                yield Input(
                    value=self.initial_query,
                    placeholder="Search saved mail, attachments and backups…",
                    id="local-search-query",
                )
                yield Select(
                    modes,
                    value=self.initial_mode.value,
                    allow_blank=False,
                    id="local-search-mode",
                )
                yield Checkbox("Fuzzy", value=self.initial_fuzzy, id="local-search-fuzzy")
                yield Checkbox("Exact phrase", value=self.initial_exact, id="local-search-exact")
                yield Button("Search", id="local-search-run", variant="primary")
            yield Static(
                "Searches copies on this computer. Nothing is sent anywhere.",
                id="area-state",
                markup=False,
            )
            yield Static(
                "Type what you are looking for and press Enter.",
                id="local-search-source",
                markup=False,
            )
            with Horizontal(id="local-search-content"):
                yield DataTable(id="local-search-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="local-search-detail-pane"):
                    yield Static(
                        "Select a result to see where it matched.",
                        id="local-search-detail",
                        markup=False,
                    )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="local-search-actions"):
                yield Button("Build index…", id="local-search-build-index")
                yield Button("Semantic setup…", id="local-search-semantic-setup")
                yield Button("Similar", id="local-search-similar", disabled=True)
                yield Button("Duplicates", id="local-search-duplicates")
                yield Button("Near duplicates", id="local-search-near")
                yield Button("Open", id="local-search-open", disabled=True)
                yield Button("Retry", id="area-retry")
                yield Button("Cancel", id="area-cancel-operation")
                yield Button("Help", id="local-search-help")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        table = self.query_one("#local-search-table", DataTable)
        table.add_columns("Source", "Type", "Score", "Title", "Folder", "Date")
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        self._apply_responsive(self.size.width)
        if self.auto_run:
            self.call_after_refresh(self.action_search)
        else:
            self.query_one("#local-search-query", Input).focus()

    def on_resize(self, event: Resize) -> None:
        self._apply_responsive(event.size.width)

    def _apply_responsive(self, width: int) -> None:
        self.set_class(width < 92, "narrow")

    def configure(
        self,
        *,
        mode: LocalSearchMode | str,
        query: str | None = None,
        reference: str | None = None,
        fuzzy: bool | None = None,
        exact: bool | None = None,
        backup_id: str | None = None,
        run: bool = True,
    ) -> None:
        selected_mode = LocalSearchMode(mode)
        self.query_one("#local-search-mode", Select).value = selected_mode.value
        if query is not None:
            self.query_one("#local-search-query", Input).value = query
        if reference:
            self.selected_reference = reference
        if fuzzy is not None:
            self.query_one("#local-search-fuzzy", Checkbox).value = fuzzy
        if exact is not None:
            self.query_one("#local-search-exact", Checkbox).value = exact
        if backup_id is not None:
            self.selected_backup_id = backup_id
            self.query_one("#local-search-source", Static).update(
                f"Scoped to selected backup: {clean_cell(backup_id, max_length=300)}"
            )
        if run:
            self.action_search()

    @on(Select.Changed, "#local-search-mode")
    def _scope_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self.post_message(LocalSearchScopeChanged(LocalSearchMode(str(event.value))))

    def action_focus_query(self) -> None:
        self.query_one("#local-search-query", Input).focus()

    @on(Button.Pressed, "#local-search-semantic-setup")
    def semantic_setup(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(LocalSearchSemanticSetupRequested())

    def action_refresh(self) -> None:
        self.action_search()

    def action_search(self) -> None:
        mode_value = self.query_one("#local-search-mode", Select).value
        if mode_value is Select.BLANK:
            mode = LocalSearchMode.ALL
        else:
            mode = LocalSearchMode(str(mode_value))
        query = self.query_one("#local-search-query", Input).value.strip()
        request = LocalSearchRequest(
            query=query,
            mode=mode,
            minimum_score=self.minimum_score,
            fuzzy=self.query_one("#local-search-fuzzy", Checkbox).value,
            exact=self.query_one("#local-search-exact", Checkbox).value,
            reference=self.selected_reference,
            sender=self.initial_sender,
            after=self.initial_after,
            before=self.initial_before,
            minimum_size=self.initial_minimum_size,
            maximum_size=self.initial_maximum_size,
            attachment_extension=self.initial_attachment_extension,
            backup_id=self.selected_backup_id,
        )
        if mode is LocalSearchMode.SEMANTIC:
            # A rejected or stale query must not leave previous matches appearing current.
            self._last_report = None
            self._results.clear()
            self.query_one("#local-search-table", DataTable).clear()
            self._enable_result_actions(False)
            self.query_one("#local-search-source", Static).update(
                "Local topic results await a successful query of unchanged indexed text."
            )
            self.query_one("#local-search-detail", Static).update(
                "No current result is selected. Update the model if indexed text has changed."
            )
        self._begin_operation(
            "Local search",
            lambda cancel, progress: self.backend.search(request, cancel=cancel, progress=progress),
            self._search_complete,
            timeout_seconds=60,
            retry_safe=True,
        )

    def _search_complete(self, value: object) -> None:
        report = cast(LocalSearchReport, value)
        self._last_report = report
        self._results = {result.result_id: result for result in report.results}
        table = self.query_one("#local-search-table", DataTable)
        table.clear()
        for result in report.results:
            table.add_row(
                clean_cell(result.source, max_length=36),
                clean_cell(result.kind, max_length=28),
                f"{result.score:.1f}",
                clean_cell(result.title, max_length=120),
                clean_cell(result.folder, max_length=70),
                clean_cell(result.timestamp or "", max_length=24),
                key=result.result_id,
            )
        source_label = ", ".join(report.searched_sources) or "none (status only)"
        notice = " ".join(report.notices)
        summary = (
            f"Sources: {source_label} · Scanned: {report.scanned_count} · "
            f"Results: {len(report.results)}"
        )
        if report.truncated:
            summary += " · result limit reached"
        if notice:
            summary += f"\n{clean_cell(notice, max_length=1_500)}"
        self.query_one("#local-search-source", Static).update(summary)
        if report.results:
            self._set_view_state(ViewState.READY, "Search results are ready.")
            table.move_cursor(row=0)
            self._show_result(report.results[0])
        else:
            message = report.notices[0] if report.notices else "No matching local records."
            self._set_view_state(ViewState.EMPTY, message)
            self.query_one("#local-search-detail", Static).update(
                "No result is selected. Refine the query, change sources, or build the local index."
            )
            self._enable_result_actions(False)

    @on(Input.Submitted, "#local-search-query")
    def _query_submitted(self) -> None:
        self.action_search()

    @on(Checkbox.Changed, "#local-search-fuzzy")
    def _fuzzy_changed(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.query_one("#local-search-exact", Checkbox).value = False

    @on(Checkbox.Changed, "#local-search-exact")
    def _exact_changed(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.query_one("#local-search-fuzzy", Checkbox).value = False

    @on(Button.Pressed, "#local-search-run")
    def _run_pressed(self) -> None:
        self.action_search()

    @on(DataTable.RowHighlighted, "#local-search-table")
    def _result_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        result = self._results.get(str(event.row_key.value))
        if result is not None:
            self.selected_reference = result.reference
            self._show_result(result)

    @on(DataTable.RowSelected, "#local-search-table")
    def _result_selected(self, event: DataTable.RowSelected) -> None:
        result = self._results.get(str(event.row_key.value))
        if result is not None:
            self.post_message(LocalSearchOpenRequested(result))

    def _current_result(self) -> LocalSearchResult | None:
        table = self.query_one("#local-search-table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return self._results.get(str(key.value))

    def current_member_references(self) -> tuple[str, ...]:
        """Return the exact bounded members of the highlighted local cluster/group."""

        result = self._current_result()
        if result is None:
            return ()
        return tuple(
            dict.fromkeys(
                clean_cell(str(reference), max_length=180)
                for reference in result.member_references
                if str(reference).strip()
            )
        )

    def _show_result(self, result: LocalSearchResult) -> None:
        members = (
            "\nMembers:\n"
            + "\n".join(
                f"  {clean_cell(item, max_length=160)}" for item in result.member_references
            )
            if result.member_references
            else ""
        )
        self.query_one("#local-search-detail", Static).update(
            "\n".join(
                (
                    clean_cell(result.title, max_length=240) or "(untitled)",
                    f"Source: {clean_cell(result.source, max_length=160)}",
                    f"Type: {clean_cell(result.kind, max_length=80)}",
                    f"Reference: {clean_cell(result.reference, max_length=180)}",
                    f"Folder: {clean_cell(result.folder, max_length=160) or '(none)'}",
                    f"Score: {result.score:.1f}",
                    f"Why: {clean_cell(result.explanation, max_length=500)}",
                    "",
                    clean_cell(result.snippet, max_length=1_000),
                    members,
                )
            )
        )
        self._enable_result_actions(True)

    def _enable_result_actions(self, enabled: bool) -> None:
        self.query_one("#local-search-open", Button).disabled = not enabled
        self.query_one("#local-search-similar", Button).disabled = not enabled

    def action_open_result(self) -> None:
        if result := self._current_result():
            self.post_message(LocalSearchOpenRequested(result))

    def action_similar(self) -> None:
        result = self._current_result()
        reference = result.reference if result is not None else self.selected_reference
        if not reference:
            self._set_view_state(ViewState.EMPTY, "Select an indexed message before comparing it.")
            return
        self.configure(mode=LocalSearchMode.SIMILAR, reference=reference, run=True)

    def action_duplicates(self) -> None:
        self.configure(mode=LocalSearchMode.DUPLICATES, run=True)

    def action_near_duplicates(self) -> None:
        self.configure(mode=LocalSearchMode.NEAR_DUPLICATES, run=True)

    @on(Button.Pressed, "#local-search-build-index")
    def _build_index_pressed(self) -> None:
        self.post_message(LocalSearchIndexRequested())

    def action_help(self) -> None:
        self.post_message(LocalSearchHelpRequested())

    @on(Button.Pressed, "#local-search-open")
    def _open_pressed(self) -> None:
        self.action_open_result()

    @on(Button.Pressed, "#local-search-similar")
    def _similar_pressed(self) -> None:
        self.action_similar()

    @on(Button.Pressed, "#local-search-duplicates")
    def _duplicates_pressed(self) -> None:
        self.action_duplicates()

    @on(Button.Pressed, "#local-search-near")
    def _near_pressed(self) -> None:
        self.action_near_duplicates()

    @on(Button.Pressed, "#local-search-help")
    def _help_pressed(self) -> None:
        self.action_help()


__all__ = [
    "LocalSearchBackend",
    "LocalSearchHelpRequested",
    "LocalSearchOpenRequested",
    "LocalSearchRequest",
    "LocalSearchScreen",
    "ServiceLocalSearchBackend",
]
