"""Operational drafts and attachments workspace for the Textual application."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from gucmail.errors import ConflictError, ItemNotFoundError
from gucmail.services.attachments import CollisionPolicy
from gucmail.tui.area_common import (
    OperationalScreen,
    ViewState,
    clean_cell,
    current_row_highlight,
    human_bytes,
)
from gucmail.tui.drafts_attachments_backend import (
    AttachmentRowView,
    AttachmentWorkspaceSnapshot,
    DraftAttachmentWorkspaceBackend,
    DraftConflictView,
    DraftOperationView,
    DraftRowView,
    DraftWorkspaceSnapshot,
    PreparedAttachmentSave,
    SavedAttachmentView,
    WorkspaceCapability,
)
from gucmail.tui.events import (
    DraftHandoff,
    DraftRecoveryRequested,
)
from gucmail.tui.labels import when
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class DraftHandoffKind(StrEnum):
    OPEN = "open"
    EDIT = "edit"
    SEND = "send"
    SCHEDULE = "schedule"


@dataclass(frozen=True, slots=True)
class DraftHandoffRequest:
    kind: DraftHandoffKind
    reference: str
    schedule_expression: str | None = None


DestinationChooser = Callable[[str], Path | None]
HandoffCallback = Callable[[DraftHandoffRequest], None]


class DraftConflictScreen(ModalScreen[str | None]):
    """Side-by-side, explicit local/server conflict decision."""

    DEFAULT_CSS = """
    DraftConflictScreen { align: center middle; background: $background 75%; }
    #draft-conflict-dialog {
        width: 94%; max-width: 112; height: 90%; max-height: 35; border: thick $warning;
        background: $surface; padding: 1 2;
    }
    #draft-conflict-title { height: 2; text-style: bold; }
    #draft-conflict-reason { height: auto; min-height: 3; color: $warning; }
    #draft-conflict-columns { height: 1fr; }
    .draft-conflict-copy { width: 1fr; height: 100%; border: solid $panel; padding: 1; }
    #draft-conflict-actions { height: 3; align-horizontal: right; }
    #draft-conflict-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("l", "local", "Keep local", show=False),
        Binding("s", "server", "Use server", show=False),
    ]

    def __init__(self, conflict: DraftConflictView) -> None:
        super().__init__()
        self.conflict = conflict

    @staticmethod
    def _copy_text(
        label: str,
        subject: str,
        recipients: tuple[str, ...],
        body: str,
        attachment_count: int,
    ) -> str:
        return (
            f"{label}\n\n"
            f"Subject: {clean_cell(subject, max_length=240) or '(no subject)'}\n"
            f"Recipients: {clean_cell(', '.join(recipients), max_length=300) or '(none)'}\n"
            f"Attachments: {attachment_count}\n\n"
            f"{clean_cell(body, max_length=1_600) or '(empty body)'}"
        )

    def compose(self) -> ComposeResult:
        value = self.conflict
        with Vertical(id="draft-conflict-dialog"):
            yield Label(
                f"Resolve draft conflict - {clean_cell(value.reference)}",
                id="draft-conflict-title",
                markup=False,
            )
            yield Static(
                f"{clean_cell(value.reason, max_length=600)}\n"
                "Choose which complete copy becomes authoritative locally. Nothing is sent.",
                id="draft-conflict-reason",
                markup=False,
            )
            with Horizontal(id="draft-conflict-columns"):
                with VerticalScroll(classes="draft-conflict-copy"):
                    yield Static(
                        self._copy_text(
                            "LOCAL COPY",
                            value.local_subject,
                            value.local_recipients,
                            value.local_body_preview,
                            value.local_attachment_count,
                        ),
                        markup=False,
                    )
                with VerticalScroll(classes="draft-conflict-copy"):
                    yield Static(
                        self._copy_text(
                            "SERVER COPY",
                            value.server_subject,
                            value.server_recipients,
                            value.server_body_preview,
                            value.server_attachment_count,
                        ),
                        markup=False,
                    )
            with Horizontal(id="draft-conflict-actions"):
                yield Button("Keep local", id="draft-conflict-local", variant="warning")
                yield Button("Use server", id="draft-conflict-server", variant="warning")
                yield Button("Cancel", id="draft-conflict-cancel", variant="primary")

    def action_local(self) -> None:
        self.dismiss("local")

    def action_server(self) -> None:
        self.dismiss("server")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "draft-conflict-local":
            self.action_local()
        elif event.button.id == "draft-conflict-server":
            self.action_server()
        elif event.button.id == "draft-conflict-cancel":
            self.action_cancel()


class DraftScheduleScreen(ModalScreen[str | None]):
    """Small reviewed entry point into the durable scheduler service."""

    DEFAULT_CSS = """
    DraftScheduleScreen { align: center middle; background: $background 75%; }
    #draft-schedule-dialog {
        width: 88%; max-width: 78; height: auto; max-height: 18;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #draft-schedule-title { height: 2; text-style: bold; }
    #draft-schedule-help { height: auto; min-height: 3; color: $text-muted; }
    #draft-schedule-expression { width: 100%; }
    #draft-schedule-actions { height: 3; align-horizontal: right; }
    #draft-schedule-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        reference: str,
        *,
        expression: str = "tomorrow 09:00",
        title: str = "Schedule selected draft",
        help_text: str | None = None,
    ) -> None:
        super().__init__()
        self.reference = reference
        self.expression = expression
        self.title_text = title
        self.help_text = help_text

    def compose(self) -> ComposeResult:
        with Vertical(id="draft-schedule-dialog"):
            yield Label(self.title_text, id="draft-schedule-title")
            yield Static(
                self.help_text
                or (
                    f"Draft: {clean_cell(self.reference, max_length=240)}\n"
                    "Enter a future local time such as 'tomorrow 09:00'. The durable job "
                    "will require final confirmation before sending."
                ),
                id="draft-schedule-help",
                markup=False,
            )
            yield Input(value=self.expression, id="draft-schedule-expression")
            with Horizontal(id="draft-schedule-actions"):
                yield Button("Cancel", id="draft-schedule-cancel")
                yield Button("Schedule", id="draft-schedule-confirm", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#draft-schedule-expression", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _finish(self) -> None:
        expression = self.query_one("#draft-schedule-expression", Input).value.strip()
        if not expression:
            self.query_one("#draft-schedule-expression", Input).focus()
            return
        self.dismiss(expression)

    @on(Input.Submitted, "#draft-schedule-expression")
    def _submitted(self) -> None:
        self._finish()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "draft-schedule-confirm":
            self._finish()
        else:
            self.action_cancel()


class DraftAttachmentScreen(OperationalScreen):
    """Bounded drafts manager plus an explicit attachment download center."""

    DEFAULT_CSS = """
    DraftAttachmentScreen { layout: vertical; }
    #draft-workspace-root { width: 100%; height: 100%; padding: 1 2; }
    #draft-workspace-title { height: 2; text-style: bold; }
    #draft-workspace-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #draft-workspace-tabs { height: 1fr; }
    #draft-workspace-tabs ContentSwitcher { height: 1fr; }
    #draft-workspace-tabs TabPane { height: 1fr; overflow-y: auto; }
    #draft-toolbar, #attachment-toolbar, #attachment-destination-row { height: 3; }
    #draft-filter, #attachment-owner, #attachment-destination { width: 2fr; margin-right: 1; }
    #attachment-filter { width: 2fr; margin-right: 1; }
    #attachment-load { width: 18; }
    #draft-location, #draft-age, #attachment-collision { width: 1fr; margin-right: 1; }
    #draft-table, #attachment-table { height: 1fr; min-height: 7; }
    #attachment-detail { height: 8; border-top: solid $panel; padding: 1; }
    #draft-details { height: auto; padding: 0; margin: 0; border: none; }
    #draft-detail-scroll { height: 6; }
    #draft-detail { height: auto; padding: 0 1; }
    #draft-actions, #attachment-actions { height: auto; min-height: 3; }
    #draft-actions Button, #attachment-actions Button { margin-right: 1; }
    #draft-actions { layout: grid; grid-size: 5 2; grid-rows: 3; height: 6; }
    #draft-actions Button { width: 1fr; min-width: 0; }
    #draft-pagination { height: 1; align-horizontal: right; }
    #draft-pagination Button { min-width: 8; margin-left: 1; }
    DraftAttachmentScreen.compact-drafts #draft-actions {
        grid-size: 3 4; height: 12;
    }
    DraftAttachmentScreen.compact-drafts #draft-toolbar { layout: grid; grid-size: 2 2; height: 6; }
    DraftAttachmentScreen.frontend.compact-drafts #draft-toolbar { height: 2; grid-rows: 1; }
    DraftAttachmentScreen.frontend.compact-drafts #draft-filter {
        height: 1; min-height: 1; border: none; padding: 0 1;
    }
    DraftAttachmentScreen.frontend.compact-drafts #draft-table { min-height: 4; }
    DraftAttachmentScreen.frontend.compact-drafts #draft-workspace-capabilities { min-height: 1; }
    DraftAttachmentScreen.compact-drafts #draft-toolbar > * { width: 1fr; }
    DraftAttachmentScreen.compact-drafts #attachment-toolbar {
        layout: grid; grid-size: 2; grid-rows: 3; height: 6;
    }
    DraftAttachmentScreen.compact-drafts #attachment-toolbar > * { width: 1fr; }
    #attachment-hash-row { height: 3; }
    #attachment-expected-hash { width: 2fr; margin-right: 1; }
    #attachment-calculate-hash { width: 1fr; }
    DraftAttachmentScreen.compact-drafts #attachment-destination-row {
        layout: grid; grid-size: 2; grid-rows: 3; height: 6;
    }
    DraftAttachmentScreen.compact-drafts #attachment-destination { column-span: 2; width: 1fr; }
    DraftAttachmentScreen.compact-drafts #attachment-collision { width: 1fr; }
    DraftAttachmentScreen.compact-drafts #attachment-hash-row { layout: vertical; height: 6; }
    DraftAttachmentScreen.compact-drafts #attachment-expected-hash,
    DraftAttachmentScreen.compact-drafts #attachment-calculate-hash { width: 1fr; }
    #draft-workspace-footer { height: 3; align-horizontal: right; }
    #draft-workspace-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create_draft", "New draft", show=False),
        Binding("enter", "open_draft", "Open draft", show=False),
        Binding("e", "edit_draft", "Edit draft", show=False),
        Binding("ctrl+d", "duplicate_draft", "Duplicate", show=False),
        Binding("ctrl+s", "sync_draft", "Save to server", show=False),
        Binding("a", "show_attachments", "Attachments", show=False),
    ]

    def __init__(
        self,
        backend: DraftAttachmentWorkspaceBackend,
        *,
        page_size: int = 200,
        download_directory: Path | None = None,
        collision_policy: CollisionPolicy = CollisionPolicy.RENAME,
        initial_owner_reference: str | None = None,
        offline: bool = False,
        destination_chooser: DestinationChooser | None = None,
        handoff: HandoffCallback | None = None,
    ) -> None:
        super().__init__(area_name="drafts", offline=offline)
        self.backend = backend
        self.page_size = max(1, min(page_size, 1_000))
        self.download_directory = (download_directory or Path.cwd()).expanduser()
        self._collision_default = collision_policy.value
        self._collision_overridden = False
        self.initial_owner_reference = initial_owner_reference
        self.destination_chooser = destination_chooser
        self.handoff = handoff
        self._drafts: dict[str, DraftRowView] = {}
        self._attachments: dict[str, AttachmentRowView] = {}
        self._selected_draft: str | None = None
        self._selected_attachment: str | None = None
        self._pending_attachment_action: str | None = None
        self._draft_selection_prompt: str | None = None
        self._draft_offset = 0
        self._draft_next_offset: int | None = None
        self._draft_filter_key: tuple[str, tuple[str, ...], int | None] | None = None

    @property
    def selected_draft_reference(self) -> str | None:
        """Return the highlighted draft for context-aware commands."""

        return self._selected_draft

    def compose(self) -> ComposeResult:
        with Vertical(id="draft-workspace-root"):
            yield Label("Drafts & Attachments", id="draft-workspace-title")
            yield Static("", id="draft-workspace-capabilities", markup=False)
            yield Static("", id="area-state", markup=False)
            with TabbedContent(id="draft-workspace-tabs"):
                with TabPane("Drafts", id="draft-workspace-drafts-tab"):
                    with Horizontal(id="draft-toolbar"):
                        yield Input(
                            placeholder="Search subject, body, or recipients",
                            id="draft-filter",
                        )
                        yield Select(
                            (
                                ("Saved anywhere", ""),
                                ("This computer", "local"),
                                ("Server", "server"),
                                ("Both", "synchronized"),
                                ("Conflict", "conflicted"),
                            ),
                            value="",
                            id="draft-location",
                        )
                        yield Select(
                            (
                                ("Any age", ""),
                                ("Older than 7 days", "7"),
                                ("Older than 30 days", "30"),
                                ("Older than 90 days", "90"),
                            ),
                            value="",
                            id="draft-age",
                        )
                        yield Button("Refresh", id="draft-refresh")
                    yield Button("Recover unsaved draft", id="draft-recover")
                    yield DataTable(id="draft-table", cursor_type="row", zebra_stripes=True)
                    with Horizontal(id="draft-pagination"):
                        yield Button("Previous", id="draft-previous", disabled=True)
                        yield Button("Next", id="draft-next", disabled=True)
                    with (
                        Collapsible(
                            title="Draft details",
                            id="draft-details",
                            collapsed=self.app.size.width < 100 or self.app.size.height < 30,
                        ),
                        VerticalScroll(id="draft-detail-scroll"),
                    ):
                        yield Static("Select a draft.", id="draft-detail", markup=False)
                    with Horizontal(id="draft-actions"):
                        yield Button("New", id="draft-create", variant="success")
                        yield Button("Open", id="draft-open")
                        yield Button("Duplicate", id="draft-duplicate")
                        yield Button("Save to server", id="draft-sync")
                        yield Button("Fix conflict", id="draft-resolve", variant="warning")
                        yield Button("Attachments", id="draft-attachments")
                        yield Button("Export", id="draft-export")
                        yield Button("Schedule", id="draft-schedule")
                        yield Button("Send", id="draft-send", variant="warning")
                        yield Button("Delete", id="draft-delete", variant="error")
                with TabPane("Attachments", id="draft-workspace-attachments-tab"):
                    with Horizontal(id="attachment-toolbar"):
                        yield Input(
                            value=self.initial_owner_reference or "",
                            placeholder="Message or local draft reference",
                            id="attachment-owner",
                        )
                        yield Input(
                            placeholder="Filter attachment name or type",
                            id="attachment-filter",
                        )
                        yield Button("Load metadata", id="attachment-load")
                    with Horizontal(id="attachment-destination-row"):
                        yield Input(
                            value=str(self.download_directory),
                            placeholder="Safe destination directory",
                            id="attachment-destination",
                        )
                        yield Button("Browse...", id="attachment-browse")
                        yield Select(
                            (
                                ("Rename collisions", "rename"),
                                ("Error on collision", "error"),
                                ("Overwrite with confirmation", "overwrite"),
                            ),
                            value=self._collision_default,
                            id="attachment-collision",
                        )
                    with Horizontal(id="attachment-hash-row"):
                        yield Input(
                            placeholder="Optional expected SHA-256 for Save selected",
                            id="attachment-expected-hash",
                        )
                        yield Checkbox(
                            "Calculate SHA-256 after saving",
                            value=True,
                            id="attachment-calculate-hash",
                        )
                    yield DataTable(id="attachment-table", cursor_type="row", zebra_stripes=True)
                    yield Static(
                        "Enter a message or local draft reference, then load metadata.",
                        id="attachment-detail",
                        markup=False,
                    )
                    with Horizontal(id="attachment-actions"):
                        yield Button("Save selected", id="attachment-save")
                        yield Button("Save all", id="attachment-save-all")
                        yield Button("Export metadata", id="attachment-export")
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="draft-workspace-footer"):
                yield Button("Close", classes="area-close", variant="primary")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 100 or self.size.height < 30, "compact-drafts")
        self._layout_compact_search()
        draft_table = self.query_one("#draft-table", DataTable)
        draft_table.add_columns("Saved", "Subject", "To", "Changed", "Files")
        attachment_table = self.query_one("#attachment-table", DataTable)
        attachment_table.add_columns("Source", "Name", "Size", "Type", "Risk", "Reference")
        self.call_after_refresh(self.action_refresh)
        if self.initial_owner_reference:
            self.query_one(
                "#draft-workspace-tabs", TabbedContent
            ).active = "draft-workspace-attachments-tab"
            self.call_after_refresh(self.action_load_attachments)

    def on_resize(self, event: Resize) -> None:
        compact = event.size.width < 100 or event.size.height < 30
        changed = compact != self.has_class("compact-drafts")
        self.set_class(compact, "compact-drafts")
        self._layout_compact_search()
        details = self.query("#draft-details")
        if changed and details:
            details.first(Collapsible).collapsed = compact

    def _layout_compact_search(self) -> None:
        searches = self.query("#draft-filter")
        if searches:
            search = searches.first(Input)
            compact = self.has_class("compact-drafts")
            # One terminal row cannot contain a border and a content row.
            # Own geometry inline so shared focus CSS cannot reintroduce it.
            search.styles.border = "none" if compact else None
            search.styles.height = 1 if compact else None
            search.styles.min_height = 1 if compact else None
            search.styles.padding = (0, 1) if compact else None

    @staticmethod
    def _capability_text(capabilities: Sequence[WorkspaceCapability]) -> str:
        # Only mention what is missing. Working features need no label.
        names = {
            "local_drafts": "Drafts on this computer",
            "server_draft_sync": "Saving drafts to the server",
            "server_attachments": "Mail attachments",
        }
        missing = [
            names.get(capability.name, capability.name.replace("_", " "))
            for capability in capabilities
            if not capability.available
        ]
        return ("Not available now: " + ", ".join(missing)) if missing else ""

    def action_refresh(self) -> None:
        query = self.query_one("#draft-filter", Input).value.strip()
        raw_location = self.query_one("#draft-location", Select).value
        locations = (str(raw_location),) if raw_location else ()
        raw_age = self.query_one("#draft-age", Select).value
        older_than_days = int(str(raw_age)) if raw_age else None
        filter_key = (query, locations, older_than_days)
        if filter_key != self._draft_filter_key:
            self._draft_offset = 0
            self._draft_filter_key = filter_key
        offset = self._draft_offset
        self.query_one("#draft-previous", Button).disabled = True
        self.query_one("#draft-next", Button).disabled = True
        self._begin_operation(
            "Loading drafts",
            lambda cancel, progress: self.backend.list_drafts(
                query=query,
                locations=locations,
                older_than_days=older_than_days,
                limit=self.page_size,
                cancel=cancel,
                progress=progress,
                offset=offset,
            ),
            self._drafts_loaded,
            retry_safe=True,
        )

    def action_next_draft_page(self) -> None:
        if not self._operation_active and self._draft_next_offset is not None:
            self._draft_offset = self._draft_next_offset
            self.action_refresh()

    def action_previous_draft_page(self) -> None:
        if not self._operation_active and self._draft_offset > 0:
            self._draft_offset = max(0, self._draft_offset - self.page_size)
            self.action_refresh()

    def _show_error(self, action: str, error: BaseException) -> None:
        super()._show_error(action, error)
        if action == "Loading drafts" and self.is_mounted:
            # A failed query is not an empty collection. Do not leave prior
            # no-match wording or actions on stale rows under a new query.
            self._invalidate_draft_rows(
                "Drafts could not be loaded. Check the error above, then choose Refresh. "
                "Your saved drafts have not been removed."
            )
        elif (
            action == "Deleting local draft"
            and self.is_mounted
            and isinstance(error, (ConflictError, ItemNotFoundError))
        ):
            self._invalidate_draft_rows(
                "This draft view is out of date. Choose Refresh to review the latest saved copies."
            )

    def _invalidate_draft_rows(self, detail: str) -> None:
        self._drafts.clear()
        self._selected_draft = None
        self._draft_next_offset = None
        self.query_one("#draft-table", DataTable).clear()
        self.query_one("#draft-previous", Button).disabled = True
        self.query_one("#draft-next", Button).disabled = True
        self.query_one("#draft-detail", Static).update(detail)
        self._update_draft_actions()

    def configure_draft_location(self, location: str) -> None:
        """Select a persisted/synchronization view from a palette route."""

        self._draft_selection_prompt = None
        value = location.strip().casefold()
        allowed = {"", "local", "server", "synchronized", "conflicted"}
        if value not in allowed:
            raise ValueError(f"Unknown draft location filter: {location}")
        self.query_one("#draft-location", Select).value = value
        self.query_one("#draft-workspace-tabs", TabbedContent).active = "draft-workspace-drafts-tab"
        self.action_refresh()

    def configure_draft_search(
        self,
        query: str = "",
        *,
        older_than_days: int | None = None,
        focus_filter: bool = False,
    ) -> None:
        """Apply a palette-provided local draft query/age view and refresh it."""

        if older_than_days not in {None, 7, 30, 90}:
            raise ValueError("Unsupported draft age filter.")
        self._draft_selection_prompt = None
        self.query_one("#draft-filter", Input).value = query.strip()
        self.query_one("#draft-age", Select).value = (
            str(older_than_days) if older_than_days is not None else ""
        )
        self.query_one("#draft-workspace-tabs", TabbedContent).active = "draft-workspace-drafts-tab"
        self.action_refresh()
        if focus_filter:
            self.query_one("#draft-filter", Input).focus()

    def prompt_draft_selection(self, action: str) -> None:
        """Keep a context-free edit/duplicate command inside a usable workflow."""

        label = action.strip().casefold()
        if label not in {"duplicate", "edit"}:
            raise ValueError("Unsupported draft selection action.")
        self._draft_selection_prompt = label
        self.query_one("#draft-workspace-tabs", TabbedContent).active = "draft-workspace-drafts-tab"
        self.query_one("#draft-table", DataTable).focus()
        self._set_view_state(
            ViewState.READY,
            f"Select a draft, then choose {label.title()}.",
        )

    def _drafts_loaded(self, result: object) -> None:
        snapshot = cast(DraftWorkspaceSnapshot, result)
        # A delete/filter update may shrink the last page. Return to the final
        # available page rather than falsely presenting an empty collection.
        if not snapshot.drafts and snapshot.offset > 0:
            self._draft_offset = (
                ((snapshot.matched_count - 1) // self.page_size) * self.page_size
                if snapshot.matched_count
                else 0
            )
            self.action_refresh()
            return
        self._draft_offset = snapshot.offset
        self._draft_next_offset = snapshot.next_offset
        self.query_one("#draft-previous", Button).disabled = snapshot.offset == 0
        self.query_one("#draft-next", Button).disabled = snapshot.next_offset is None
        previous = self._selected_draft
        self._drafts = {draft.reference: draft for draft in snapshot.drafts}
        table = self.query_one("#draft-table", DataTable)
        table.clear()
        for draft in snapshot.drafts:
            table.add_row(
                _draft_place(draft),
                clean_cell(draft.subject, max_length=100) or "(no subject)",
                clean_cell(", ".join(draft.recipients), max_length=90),
                clean_cell(self._when(draft.updated_at), max_length=36),
                str(draft.attachment_count) if draft.attachment_count else "",
                key=draft.reference,
            )
        self.query_one("#draft-workspace-capabilities", Static).update(
            self._capability_text(snapshot.capabilities)
        )
        if snapshot.drafts:
            count = snapshot.matched_count
            self._set_view_state(ViewState.READY, f"{count} draft{'' if count == 1 else 's'}")
            target = previous if previous in self._drafts else snapshot.drafts[0].reference
            row = next(
                index for index, item in enumerate(snapshot.drafts) if item.reference == target
            )
            table.move_cursor(row=row)
            self._select_draft(target)
        else:
            self._selected_draft = None
            self.query_one("#draft-detail", Static).update(
                "No drafts here. If the app closed while you were writing, "
                "press Recover unsaved draft."
            )
            self._set_view_state(ViewState.EMPTY, "No drafts")
        if self._draft_selection_prompt is not None and snapshot.drafts:
            self._set_view_state(
                ViewState.READY,
                f"Select a draft, then choose {self._draft_selection_prompt.title()}.",
            )
        self._update_draft_actions()

    @on(Input.Submitted, "#draft-filter")
    @on(Select.Changed, "#draft-location")
    @on(Select.Changed, "#draft-age")
    def _draft_filter_changed(self) -> None:
        self.action_refresh()

    @on(DataTable.RowHighlighted, "#draft-table")
    def _draft_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_draft(str(event.row_key.value))

    def _when(self, value: str | None) -> str:
        context = getattr(self.app, "context", None)
        zone = str(getattr(getattr(context, "config", None), "timezone", None) or "UTC")
        return when(value, zone)

    def _select_draft(self, reference: str) -> None:
        draft = self._drafts.get(reference)
        if draft is None:
            return
        self._selected_draft = reference
        detail = [clean_cell(draft.subject, max_length=240) or "(no subject)"]
        if draft.recipients:
            detail.append(f"To: {clean_cell(', '.join(draft.recipients), max_length=400)}")
        detail.append(f"Saved: {_draft_place(draft)}")
        if draft.updated_at:
            detail.append(f"Changed: {clean_cell(self._when(draft.updated_at))}")
        if draft.attachment_count:
            detail.append(f"Attachments: {draft.attachment_count}")
        detail.extend(("", clean_cell(draft.body_preview, max_length=700) or "(empty)"))
        if draft.conflict_reason:
            detail.append(
                "The server copy changed too. Open Fix conflict to choose which to keep. "
                f"({clean_cell(draft.conflict_reason, max_length=300)})"
            )
        self.query_one("#draft-detail", Static).update("\n".join(detail))
        self._update_draft_actions()

    def _selected_draft_view(self) -> DraftRowView | None:
        return self._drafts.get(self._selected_draft or "")

    def _update_draft_actions(self) -> None:
        draft = self._selected_draft_view()
        selected = draft is not None
        for selector in (
            "#draft-open",
            "#draft-duplicate",
            "#draft-attachments",
            "#draft-export",
            "#draft-schedule",
            "#draft-delete",
        ):
            self.query_one(selector, Button).disabled = not selected
        self.query_one("#draft-sync", Button).disabled = not (
            selected
            and not bool(draft and draft.conflicted)
            and self.backend.server_drafts_available
            and self.backend.online
            and not self.offline
        )
        self.query_one("#draft-resolve", Button).disabled = not bool(draft and draft.conflicted)
        self.query_one("#draft-send", Button).disabled = not (
            selected and self.backend.online and not self.offline
        )

    def _handoff(self, kind: DraftHandoffKind, reference: str) -> None:
        request = DraftHandoffRequest(kind, reference)
        if self.handoff is not None:
            self.handoff(request)
        else:
            self.post_message(DraftHandoff(request))

    def action_create_draft(self) -> None:
        self._begin_operation(
            "Creating draft",
            lambda cancel, progress: self.backend.create_draft(cancel=cancel, progress=progress),
            self._created_draft,
        )

    def _created_draft(self, result: object) -> None:
        operation = cast(DraftOperationView, result)
        self._selected_draft = operation.reference
        self._handoff(DraftHandoffKind.EDIT, operation.reference)
        self.set_timer(0.05, self.action_refresh)

    def action_open_draft(self) -> None:
        if self._selected_draft:
            self._handoff(DraftHandoffKind.OPEN, self._selected_draft)

    def action_edit_draft(self) -> None:
        if self._selected_draft:
            self._handoff(DraftHandoffKind.EDIT, self._selected_draft)

    def action_duplicate_draft(self) -> None:
        reference = self._selected_draft
        if reference is None:
            return
        self._begin_operation(
            "Duplicating draft",
            lambda cancel, progress: self.backend.duplicate_draft(
                reference, cancel=cancel, progress=progress
            ),
            self._duplicated_draft,
        )

    def _duplicated_draft(self, result: object) -> None:
        operation = cast(DraftOperationView, result)
        self._selected_draft = operation.reference
        self._handoff(DraftHandoffKind.EDIT, operation.reference)
        self.set_timer(0.05, self.action_refresh)

    def action_sync_draft(self) -> None:
        reference = self._selected_draft
        if reference is None or self.offline:
            return
        self._begin_operation(
            "Synchronizing draft",
            lambda cancel, progress: self.backend.synchronize_draft(
                reference, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
            timeout_seconds=60.0,
        )

    def action_resolve_conflict(self) -> None:
        reference = self._selected_draft
        if reference is None:
            return
        self._begin_operation(
            "Loading draft conflict",
            lambda cancel, progress: self.backend.conflict(
                reference, cancel=cancel, progress=progress
            ),
            self._open_conflict,
            retry_safe=True,
        )

    def _open_conflict(self, result: object) -> None:
        conflict = cast(DraftConflictView, result)

        def resolved(choice: str | None) -> None:
            if choice is None:
                return
            self._begin_operation(
                "Resolving draft conflict",
                lambda cancel, progress: self.backend.resolve_conflict(
                    conflict.reference,
                    use=choice,
                    cancel=cancel,
                    progress=progress,
                ),
                lambda _result: self.action_refresh(),
            )

        self.app.push_screen(DraftConflictScreen(conflict), resolved)

    def action_export_draft(self) -> None:
        reference = self._selected_draft
        if reference is None:
            return
        destination = self._destination()
        self._begin_operation(
            "Exporting draft",
            lambda cancel, progress: self.backend.export_draft(
                reference, destination, cancel=cancel, progress=progress
            ),
            self._operation_done,
        )

    def action_delete_local_draft(self) -> None:
        draft = self._selected_draft_view()
        if draft is None:
            return

        def delete() -> None:
            self._begin_operation(
                "Deleting local draft",
                lambda cancel, progress: self.backend.delete_local_draft(
                    draft.reference,
                    cancel=cancel,
                    progress=progress,
                    expected_revision=draft.revision,
                    expected_hash=draft.content_hash,
                ),
                lambda _result: self.action_refresh(),
            )

        server_note = (
            " The Exchange copy is deliberately not deleted." if draft.server_id_present else ""
        )
        self._confirm(
            "Delete this local draft?",
            f"Subject: {clean_cell(draft.subject, max_length=200) or '(no subject)'}\n"
            f"Recipients: {clean_cell(', '.join(draft.recipients), max_length=180) or '(none)'}\n"
            f"Revision: {draft.revision}\n\n"
            f"Remove local draft {clean_cell(draft.reference)} and its recovery copy."
            f"{server_note}\n\nThis cannot be undone from this screen.",
            delete,
            confirm_label="Delete local copy",
        )

    def action_send_draft(self) -> None:
        if self._selected_draft and self.backend.online and not self.offline:
            self._handoff(DraftHandoffKind.SEND, self._selected_draft)

    def action_schedule_draft(self, *, expression: str | None = None) -> None:
        if self._selected_draft:
            request = DraftHandoffRequest(
                DraftHandoffKind.SCHEDULE,
                self._selected_draft,
                schedule_expression=expression,
            )
            if self.handoff is not None:
                self.handoff(request)
            else:
                self.post_message(DraftHandoff(request))

    def action_show_attachments(self) -> None:
        reference = self._selected_draft
        if reference is None:
            return
        self.query_one("#attachment-owner", Input).value = reference
        self.query_one(
            "#draft-workspace-tabs", TabbedContent
        ).active = "draft-workspace-attachments-tab"
        self.action_load_attachments()

    def show_attachments_for(self, owner_reference: str) -> None:
        """Small integration hook for a message reader's selected reference."""

        self.query_one("#attachment-owner", Input).value = owner_reference
        self.query_one(
            "#draft-workspace-tabs", TabbedContent
        ).active = "draft-workspace-attachments-tab"
        self.action_load_attachments()

    def queue_attachment_action(self, action: str) -> None:
        """Run a concrete attachment action after the next metadata load."""

        allowed = {"save_all"}
        if action not in allowed:
            raise ValueError(f"Unknown queued attachment action: {action}")
        self._pending_attachment_action = action

    def action_load_attachments(self) -> None:
        owner = self.query_one("#attachment-owner", Input).value.strip()
        query = self.query_one("#attachment-filter", Input).value.strip()
        self._begin_operation(
            "Loading attachment metadata",
            lambda cancel, progress: self.backend.list_attachments(
                owner,
                query=query,
                limit=self.page_size,
                cancel=cancel,
                progress=progress,
            ),
            self._attachments_loaded,
            retry_safe=True,
        )

    def _attachments_loaded(self, result: object) -> None:
        snapshot = cast(AttachmentWorkspaceSnapshot, result)
        previous = self._selected_attachment
        self._attachments = {item.reference: item for item in snapshot.attachments}
        table = self.query_one("#attachment-table", DataTable)
        table.clear()
        for item in snapshot.attachments:
            risk = "DANGEROUS" if item.dangerous else ""
            if item.is_item_attachment:
                risk = "ITEM" if not risk else f"{risk}, ITEM"
            table.add_row(
                item.source,
                clean_cell(item.name, max_length=100),
                human_bytes(item.size),
                clean_cell(item.content_type, max_length=60),
                risk,
                clean_cell(item.reference, max_length=72),
                key=item.reference,
            )
        self.query_one("#draft-workspace-capabilities", Static).update(
            self._capability_text(snapshot.capabilities)
        )
        if snapshot.attachments:
            suffix = " (bounded; more matched)" if snapshot.truncated else ""
            self._set_view_state(
                ViewState.READY,
                f"Loaded {len(snapshot.attachments)} of {snapshot.matched_count} "
                f"attachments{suffix}.",
            )
            target = (
                previous if previous in self._attachments else snapshot.attachments[0].reference
            )
            row = next(
                index for index, item in enumerate(snapshot.attachments) if item.reference == target
            )
            table.move_cursor(row=row)
            self._select_attachment(target)
        else:
            self._selected_attachment = None
            self.query_one("#attachment-detail", Static).update(
                "No attachments match this view."
            )
            self._set_view_state(ViewState.EMPTY, "No attachments match this view.")
        self._update_attachment_actions()
        pending = self._pending_attachment_action
        self._pending_attachment_action = None
        if snapshot.attachments and pending == "save_all":
            self.set_timer(0.01, self.action_save_all_attachments)

    @on(Input.Submitted, "#attachment-owner")
    @on(Input.Submitted, "#attachment-filter")
    def _attachment_filter_changed(self) -> None:
        self.action_load_attachments()

    @on(DataTable.RowHighlighted, "#attachment-table")
    def _attachment_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_attachment(str(event.row_key.value))

    def _select_attachment(self, reference: str) -> None:
        item = self._attachments.get(reference)
        if item is None:
            return
        self._selected_attachment = reference
        detail = [
            clean_cell(item.name, max_length=240),
            f"Source: {item.source} | Size: {human_bytes(item.size)} | "
            f"Type: {clean_cell(item.content_type) or '(unknown)'}",
            f"Reference: {clean_cell(item.reference, max_length=240)}",
        ]
        if item.local_path is not None:
            detail.append(f"Local path: {clean_cell(item.local_path, max_length=500)}")
        if item.dangerous:
            detail.append(
                "Warning: this file type can execute code; it is never opened automatically."
            )
        if item.is_item_attachment:
            detail.append(
                "Attached Exchange item: its server export format is capability-dependent."
            )
        if item.is_inline:
            detail.append("Inline attachment (remote content is not rendered or fetched here).")
        self.query_one("#attachment-detail", Static).update("\n".join(detail))
        self._update_attachment_actions()

    def _update_attachment_actions(self) -> None:
        selected = self._selected_attachment in self._attachments
        owner_present = bool(self.query_one("#attachment-owner", Input).value.strip())
        self.query_one("#attachment-save", Button).disabled = not selected
        self.query_one("#attachment-save-all", Button).disabled = not self._attachments
        self.query_one("#attachment-export", Button).disabled = not owner_present

    def _destination(self) -> Path:
        return Path(self.query_one("#attachment-destination", Input).value.strip()).expanduser()

    def action_choose_destination(self) -> None:
        if self.destination_chooser is None:
            self._set_view_state(
                ViewState.READY,
                "Enter a destination path directly; no platform folder picker is configured.",
            )
            return
        selected = self.destination_chooser(self.query_one("#attachment-destination", Input).value)
        if selected is not None:
            self.query_one("#attachment-destination", Input).value = str(selected)

    @on(Select.Changed, "#attachment-collision")
    def _collision_changed(self, event: Select.Changed) -> None:
        if event.value != self._collision_default:
            self._collision_overridden = True

    def update_collision_default(self, policy: CollisionPolicy) -> None:
        """Apply a saved default without replacing a per-download choice."""

        self._collision_default = policy.value
        if self.is_mounted and not self._collision_overridden:
            self.query_one("#attachment-collision", Select).value = policy.value

    def action_save_attachment(self) -> None:
        owner = self.query_one("#attachment-owner", Input).value.strip()
        reference = self._selected_attachment
        if not owner or reference is None:
            return
        collision = str(self.query_one("#attachment-collision", Select).value)
        digest = self.query_one("#attachment-expected-hash", Input).value.strip() or None
        self._begin_operation(
            "Preparing attachment save",
            lambda cancel, progress: self.backend.prepare_attachment_save(
                owner,
                reference,
                self._destination(),
                collision=collision,
                expected_sha256=digest,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._prepared_saves((cast(PreparedAttachmentSave, result),)),
            retry_safe=True,
        )

    def action_save_attachment_as(self) -> None:
        """Expose the normalized destination before executing a selected save."""

        self.query_one(
            "#draft-workspace-tabs", TabbedContent
        ).active = "draft-workspace-attachments-tab"
        self.query_one("#attachment-destination", Input).focus()
        self._set_view_state(
            ViewState.READY,
            "Review the normalized destination and collision policy, then choose Save selected.",
        )

    def action_hash_attachment(self) -> None:
        """Save the selected attachment safely and calculate its SHA-256 digest."""

        self.query_one("#attachment-calculate-hash", Checkbox).value = True
        self.action_save_attachment()

    def action_save_all_attachments(self) -> None:
        owner = self.query_one("#attachment-owner", Input).value.strip()
        if not owner or not self._attachments:
            return
        collision = str(self.query_one("#attachment-collision", Select).value)
        self._begin_operation(
            "Preparing attachment saves",
            lambda cancel, progress: self.backend.prepare_all_attachment_saves(
                owner,
                self._destination(),
                collision=collision,
                limit=self.page_size,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._prepared_saves(cast(tuple[PreparedAttachmentSave, ...], result)),
            retry_safe=True,
        )

    def _prepared_saves(self, prepared: tuple[PreparedAttachmentSave, ...]) -> None:
        if not prepared:
            self._set_view_state(ViewState.EMPTY, "There are no attachments to save.")
            return
        overwrites = [plan for plan in prepared if plan.requires_confirmation]
        if overwrites:
            targets = "\n".join(
                f"- {clean_cell(plan.destination, max_length=300)}" for plan in overwrites[:10]
            )
            if len(overwrites) > 10:
                targets += f"\n- and {len(overwrites) - 10} more exact targets"
            self._confirm(
                "Overwrite existing attachment files?",
                f"The following exact paths already exist:\n{targets}\n\n"
                "Only these prepared paths will be replaced; files are never opened.",
                lambda: self._execute_saves(prepared, confirmed=True),
                confirm_label="Overwrite files",
            )
            return
        self._execute_saves(prepared, confirmed=False)

    def _execute_saves(
        self, prepared: tuple[PreparedAttachmentSave, ...], *, confirmed: bool
    ) -> None:
        calculate_hash = self.query_one("#attachment-calculate-hash", Checkbox).value
        self._begin_operation(
            "Saving attachments",
            lambda cancel, progress: self.backend.execute_attachment_saves(
                prepared,
                confirmed=confirmed,
                calculate_sha256=calculate_hash,
                cancel=cancel,
                progress=progress,
            ),
            self._attachments_saved,
            timeout_seconds=120.0,
        )

    def _attachments_saved(self, result: object) -> None:
        saved = cast(tuple[SavedAttachmentView, ...], result)
        lines = [f"Saved {len(saved)} attachment(s)."]
        for item in saved[:8]:
            digest = f" | SHA-256 {item.sha256}" if item.sha256 else ""
            lines.append(
                f"{clean_cell(item.path, max_length=350)} "
                f"({human_bytes(item.bytes_written)}){digest}"
            )
            lines.extend(clean_cell(warning, max_length=400) for warning in item.warnings)
        self.query_one("#attachment-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, f"Saved {len(saved)} attachment(s) safely.")

    def action_export_attachment_metadata(self) -> None:
        owner = self.query_one("#attachment-owner", Input).value.strip()
        if not owner:
            return
        self._begin_operation(
            "Exporting attachment metadata",
            lambda cancel, progress: self.backend.export_attachment_metadata(
                owner,
                self._destination(),
                limit=self.page_size,
                cancel=cancel,
                progress=progress,
            ),
            self._operation_done,
        )

    def _operation_done(self, result: object) -> None:
        operation = cast(DraftOperationView, result)
        self._set_view_state(ViewState.READY, operation.detail)

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "draft-refresh":
            self.action_refresh()
        elif button_id == "draft-next":
            self.action_next_draft_page()
        elif button_id == "draft-previous":
            self.action_previous_draft_page()
        elif button_id == "draft-recover":
            self.post_message(DraftRecoveryRequested())
        elif button_id == "draft-create":
            self.action_create_draft()
        elif button_id == "draft-open":
            self.action_open_draft()
        elif button_id == "draft-duplicate":
            self.action_duplicate_draft()
        elif button_id == "draft-sync":
            self.action_sync_draft()
        elif button_id == "draft-resolve":
            self.action_resolve_conflict()
        elif button_id == "draft-attachments":
            self.action_show_attachments()
        elif button_id == "draft-export":
            self.action_export_draft()
        elif button_id == "draft-schedule":
            self.action_schedule_draft()
        elif button_id == "draft-send":
            self.action_send_draft()
        elif button_id == "draft-delete":
            self.action_delete_local_draft()
        elif button_id == "attachment-load":
            self.action_load_attachments()
        elif button_id == "attachment-browse":
            self.action_choose_destination()
        elif button_id == "attachment-save":
            self.action_save_attachment()
        elif button_id == "attachment-save-all":
            self.action_save_all_attachments()
        elif button_id == "attachment-export":
            self.action_export_attachment_metadata()


__all__ = [
    "DestinationChooser",
    "DraftAttachmentScreen",
    "DraftConflictScreen",
    "DraftHandoff",
    "DraftHandoffKind",
    "DraftHandoffRequest",
    "DraftScheduleScreen",
    "HandoffCallback",
]


_DRAFT_PLACES = {
    "local_only": "This computer",
    "server_only": "Server",
    "synchronized": "Both",
    "local_dirty": "This computer (newer)",
    "server_dirty": "Server (newer)",
    "conflicted": "Conflict",
    "server_missing": "This computer (server copy gone)",
}


def _draft_place(draft: DraftRowView) -> str:
    if draft.conflicted:
        return "Conflict"
    return _DRAFT_PLACES.get(draft.sync_state, clean_cell(draft.sync_state, max_length=24))
