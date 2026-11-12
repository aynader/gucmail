"""First-class saved-search and recent-search browser for the Textual app."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.errors import LocalStateError
from gucmail.security import sanitize_terminal
from gucmail.services.search import build_search_plan, parse_query
from gucmail.state import StateStore
from gucmail.tui.area_common import ConfirmationScreen, clean_cell, current_row_highlight
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(frozen=True, slots=True)
class SavedSearchEntry:
    """Presentation-safe persisted query with execution metadata."""

    name: str
    query_text: str
    payload: Mapping[str, Any]
    scope: str
    execution_summary: str
    fallback_reasons: tuple[str, ...]
    created_at: str
    updated_at: str
    last_run_at: str | None
    last_result_count: int | None
    run_count: int


@dataclass(frozen=True, slots=True)
class RecentSearchEntry:
    """One bounded local search-history row."""

    history_id: int
    name: str | None
    query_text: str
    payload: Mapping[str, Any]
    source: str
    executed_at: str
    result_count: int
    scope: str
    execution_summary: str


@dataclass(frozen=True, slots=True)
class SavedSearchPreview:
    """Validated query preview shared by editor and list details."""

    query_text: str
    payload: Mapping[str, Any]
    scope: str
    execution_summary: str
    fallback_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SavedSearchEditorValue:
    name: str
    query_text: str


class SavedSearchRunRequested(Message):
    """Ask the app to run one exact advanced expression in its mail result view."""

    def __init__(
        self,
        *,
        name: str | None,
        query_text: str,
        payload: Mapping[str, Any],
        source: str,
    ) -> None:
        super().__init__()
        self.name = sanitize_terminal(name, max_length=120) if name else None
        self.query_text = query_text.strip()
        self.payload = dict(payload)
        self.source = source


class SavedSearchHelpRequested(Message):
    """Ask the app to open context Help for the saved-search product area."""


class SavedSearchBackend(Protocol):
    timezone: str

    def list_saved(self) -> tuple[SavedSearchEntry, ...]: ...

    def list_recent(self, *, limit: int = 50) -> tuple[RecentSearchEntry, ...]: ...

    def preview(self, query_text: str) -> SavedSearchPreview: ...

    def create(self, name: str, query_text: str) -> SavedSearchEntry: ...

    def update(self, name: str, query_text: str) -> SavedSearchEntry: ...

    def rename(self, old_name: str, new_name: str) -> SavedSearchEntry: ...

    def duplicate(self, source_name: str, new_name: str) -> SavedSearchEntry: ...

    def delete(self, name: str) -> bool: ...


def _quoted(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _legacy_query_text(payload: Mapping[str, Any]) -> str:
    """Render pre-v7 flat saved-query payloads as stable expert syntax."""

    aliases = {
        "sender": "from",
        "recipient": "recipient",
        "to": "to",
        "cc": "cc",
        "bcc": "bcc",
        "subject": "subject",
        "body": "body",
        "after": "after",
        "before": "before",
        "attachment_name": "attachment",
        "importance": "importance",
        "folder": "folder",
        "minimum_size": "min-size",
        "maximum_size": "max-size",
        "internet_message_id": "message-id",
    }
    parts: list[str] = []
    text = payload.get("text")
    if text:
        parts.append(_quoted(text))
    for key, alias in aliases.items():
        value = payload.get(key)
        if value is not None and str(value).strip():
            parts.append(f"{alias}:{_quoted(value)}")
    if payload.get("has_attachments") is True:
        parts.append("has:attachment")
    elif payload.get("has_attachments") is False:
        parts.append("NOT has:attachment")
    if payload.get("is_read") is not None:
        parts.append(f"read:{str(bool(payload['is_read'])).lower()}")
    if payload.get("flagged") is not None:
        parts.append(f"flag:{str(bool(payload['flagged'])).lower()}")
    categories = payload.get("categories")
    if isinstance(categories, Sequence) and not isinstance(categories, (str, bytes, bytearray)):
        parts.extend(f"category:{_quoted(value)}" for value in categories if str(value).strip())
    return " AND ".join(parts)


def _query_text(payload: Mapping[str, Any]) -> str:
    query = payload.get("query")
    if isinstance(query, str):
        return query.strip()
    if isinstance(query, Mapping):
        nested = query.get("query")
        if isinstance(nested, str):
            return nested.strip()
        return _legacy_query_text(query)
    return _legacy_query_text(payload)


class StateSavedSearchBackend:
    """Validated account-scoped adapter over :class:`StateStore`."""

    def __init__(
        self,
        state: StateStore,
        *,
        timezone: str,
        history_enabled: bool = True,
    ) -> None:
        self.state = state
        self.timezone = timezone
        self.history_enabled = history_enabled

    def preview(self, query_text: str) -> SavedSearchPreview:
        normalized = query_text.strip()
        if not normalized:
            raise LocalStateError("A saved search needs a non-empty query.")
        expression = parse_query(normalized, timezone=self.timezone)
        plan = build_search_plan(expression)
        payload: Mapping[str, Any] = {
            "query": normalized,
            "ast": expression.to_dict(),
            "scope": "mailbox" if plan.mailbox_scope else plan.folder_scope or "default",
        }
        return SavedSearchPreview(
            query_text=normalized,
            payload=payload,
            scope="All mail folders (bounded)"
            if plan.mailbox_scope
            else plan.folder_scope or "Default folder",
            execution_summary=plan.execution_summary,
            fallback_reasons=plan.fallback_reasons,
        )

    def _entry(self, row: Mapping[str, Any]) -> SavedSearchEntry:
        raw_payload = row.get("query")
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        preview = self.preview(_query_text(payload))
        count = row.get("last_result_count")
        return SavedSearchEntry(
            name=str(row.get("name") or ""),
            query_text=preview.query_text,
            payload=preview.payload,
            scope=preview.scope,
            execution_summary=preview.execution_summary,
            fallback_reasons=preview.fallback_reasons,
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            last_run_at=str(row["last_run_at"]) if row.get("last_run_at") else None,
            last_result_count=int(count) if isinstance(count, int | float) else None,
            run_count=int(row.get("run_count") or 0),
        )

    def list_saved(self) -> tuple[SavedSearchEntry, ...]:
        entries: list[SavedSearchEntry] = []
        for row in self.state.list_searches():
            try:
                entries.append(self._entry(row))
            except (LocalStateError, TypeError, ValueError):
                # Preserve invalid legacy rows for CLI repair without crashing the TUI.
                continue
        return tuple(entries)

    def list_recent(self, *, limit: int = 50) -> tuple[RecentSearchEntry, ...]:
        if not self.history_enabled:
            return ()
        entries: list[RecentSearchEntry] = []
        for row in self.state.list_search_history(limit=limit):
            raw_payload = row.get("query")
            payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
            try:
                preview = self.preview(_query_text(payload))
            except (LocalStateError, TypeError, ValueError):
                continue
            entries.append(
                RecentSearchEntry(
                    history_id=int(row["id"]),
                    name=str(row["name"]) if row.get("name") else None,
                    query_text=preview.query_text,
                    payload=preview.payload,
                    source=str(row.get("source") or "interactive"),
                    executed_at=str(row.get("executed_at") or ""),
                    result_count=int(row.get("result_count") or 0),
                    scope=preview.scope,
                    execution_summary=preview.execution_summary,
                )
            )
        return tuple(entries)

    def _required(self, name: str) -> SavedSearchEntry:
        row = self.state.get_search_record(name)
        if row is None:
            raise LocalStateError(f"Saved search not found: {name}")
        return self._entry(row)

    def create(self, name: str, query_text: str) -> SavedSearchEntry:
        if self.state.get_search_record(name) is not None:
            raise LocalStateError(f"A saved search named {name.strip()!r} already exists.")
        preview = self.preview(query_text)
        self.state.save_search(name, dict(preview.payload))
        return self._required(name)

    def update(self, name: str, query_text: str) -> SavedSearchEntry:
        self._required(name)
        preview = self.preview(query_text)
        self.state.save_search(name, dict(preview.payload))
        return self._required(name)

    def rename(self, old_name: str, new_name: str) -> SavedSearchEntry:
        if not self.state.rename_search(old_name, new_name):
            raise LocalStateError(f"Saved search not found: {old_name}")
        return self._required(new_name)

    def duplicate(self, source_name: str, new_name: str) -> SavedSearchEntry:
        if not self.state.duplicate_search(source_name, new_name):
            raise LocalStateError(f"Saved search not found: {source_name}")
        return self._required(new_name)

    def delete(self, name: str) -> bool:
        return self.state.remove_search(name)


class SavedSearchEditorScreen(ModalScreen[SavedSearchEditorValue | None]):
    """Validated name/query editor with an honest execution preview."""

    DEFAULT_CSS = """
    SavedSearchEditorScreen { align: center middle; background: $background 75%; }
    #saved-search-editor-dialog {
        width: 92%; max-width: 92; height: 82%; max-height: 34;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #saved-search-editor-title { height: 2; text-style: bold; }
    #saved-search-editor-name { height: 3; }
    #saved-search-editor-query { height: 1fr; min-height: 7; }
    #saved-search-editor-preview { height: 7; border-top: solid $panel; padding: 1; }
    #saved-search-editor-state { height: auto; min-height: 2; }
    #saved-search-editor-actions { height: 3; align-horizontal: right; }
    #saved-search-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        backend: SavedSearchBackend,
        *,
        title: str,
        name: str = "",
        query_text: str = "",
        name_editable: bool = True,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.title_text = clean_cell(title, max_length=120)
        self.initial_name = name
        self.initial_query = query_text
        self.name_editable = name_editable
        self._valid = False

    def compose(self) -> ComposeResult:
        with Vertical(id="saved-search-editor-dialog"):
            yield Label(self.title_text, id="saved-search-editor-title", markup=False)
            yield Input(
                value=self.initial_name,
                placeholder="Human-readable saved-search name",
                id="saved-search-editor-name",
                disabled=not self.name_editable,
                max_length=120,
            )
            yield TextArea(self.initial_query, id="saved-search-editor-query")
            yield Static("", id="saved-search-editor-preview", markup=False)
            yield Static("Enter a name and advanced query.", id="saved-search-editor-state")
            with Horizontal(id="saved-search-editor-actions"):
                yield Button("Cancel", id="saved-search-editor-cancel")
                yield Button("Save", id="saved-search-editor-save", variant="primary")

    def on_mount(self) -> None:
        # A modal may be pushed from the callback that dismisses the browser. Let
        # Textual finish registering this screen's composed children before querying
        # them; otherwise rapid cross-screen transitions can observe a half-mounted
        # editor and raise NoMatches.
        self.call_after_refresh(self._finish_mount)

    def _finish_mount(self) -> None:
        self._validate()
        target = (
            "#saved-search-editor-query" if not self.name_editable else "#saved-search-editor-name"
        )
        self.query_one(target).focus()

    def _validate(self) -> None:
        name = self.query_one("#saved-search-editor-name", Input).value.strip()
        query_text = self.query_one("#saved-search-editor-query", TextArea).text.strip()
        error: str | None = None
        preview: SavedSearchPreview | None = None
        if not name:
            error = "Name is required."
        else:
            try:
                preview = self.backend.preview(query_text)
            except Exception as exc:
                error = sanitize_terminal(str(exc), max_length=500)
        self._valid = error is None and preview is not None
        self.query_one("#saved-search-editor-save", Button).disabled = not self._valid
        self.query_one("#saved-search-editor-state", Static).update(
            "Ready to save." if self._valid else f"Invalid: {error or 'query'}"
        )
        if preview is None:
            self.query_one("#saved-search-editor-preview", Static).update(
                "Query preview appears here after validation."
            )
            return
        self.query_one("#saved-search-editor-preview", Static).update(
            "\n".join(
                (
                    f"Scope: {preview.scope}",
                    preview.execution_summary,
                )
            )
        )

    @on(Input.Changed, "#saved-search-editor-name")
    @on(TextArea.Changed, "#saved-search-editor-query")
    def _changed(self) -> None:
        self._validate()

    def action_save(self) -> None:
        self._validate()
        if not self._valid:
            return
        self.dismiss(
            SavedSearchEditorValue(
                self.query_one("#saved-search-editor-name", Input).value.strip(),
                self.query_one("#saved-search-editor-query", TextArea).text.strip(),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "saved-search-editor-save":
            self.action_save()
        elif event.button.id == "saved-search-editor-cancel":
            self.action_cancel()


class SavedSearchNameScreen(ModalScreen[str | None]):
    """Small collision-safe rename/duplicate prompt."""

    DEFAULT_CSS = """
    SavedSearchNameScreen { align: center middle; background: $background 75%; }
    #saved-search-name-dialog {
        width: 72; max-width: 92%; height: auto;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #saved-search-name-title { height: 2; text-style: bold; }
    #saved-search-name-actions { height: 3; align-horizontal: right; }
    #saved-search-name-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, initial: str) -> None:
        super().__init__()
        self.title_text = clean_cell(title, max_length=120)
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="saved-search-name-dialog"):
            yield Label(self.title_text, id="saved-search-name-title", markup=False)
            yield Input(value=self.initial, max_length=120, id="saved-search-name-input")
            with Horizontal(id="saved-search-name-actions"):
                yield Button("Cancel", id="saved-search-name-cancel")
                yield Button("Save", id="saved-search-name-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#saved-search-name-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "saved-search-name-cancel":
            self.action_cancel()
        elif event.button.id == "saved-search-name-save":
            value = self.query_one("#saved-search-name-input", Input).value.strip()
            if value:
                self.dismiss(value)

    @on(Input.Submitted, "#saved-search-name-input")
    def _submitted(self) -> None:
        value = self.query_one("#saved-search-name-input", Input).value.strip()
        if value:
            self.dismiss(value)


class SavedSearchScreen(Screen[None]):
    """Searchable CRUD browser for saved and recent advanced queries."""

    DEFAULT_CSS = """
    SavedSearchScreen { layout: vertical; background: $background; }
    #saved-search-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #saved-search-title { height: 2; text-style: bold; }
    #saved-search-subtitle { height: auto; min-height: 2; color: $text-muted; }
    #saved-search-filter { height: 3; }
    #saved-search-content { height: 1fr; min-height: 8; }
    #saved-search-list-pane { width: 3fr; min-width: 38; height: 100%; }
    #saved-search-table { height: 1fr; }
    #saved-search-empty { display: none; height: 1fr; content-align: center middle; }
    #saved-search-detail {
        width: 2fr; min-width: 30; height: 100%;
        border-left: solid $panel; padding: 1 2;
    }
    #saved-search-actions { height: auto; min-height: 3; }
    #saved-search-actions Button { margin-right: 1; margin-bottom: 1; }
    #saved-search-state { height: auto; min-height: 1; }
    #saved-search-hints { height: auto; min-height: 1; color: $text-muted; }
    SavedSearchScreen.narrow #saved-search-content { layout: vertical; }
    SavedSearchScreen.narrow #saved-search-list-pane {
        width: 100%; height: 2fr; min-width: 0; min-height: 8;
    }
    SavedSearchScreen.narrow #saved-search-detail {
        width: 100%; height: 1fr; min-width: 0; min-height: 5;
        border-left: none; border-top: solid $panel; padding: 1;
    }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+f", "focus_filter", "Filter", show=False),
        Binding("ctrl+n", "create", "Create", show=False),
        Binding("enter", "run", "Run", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("r", "rename", "Rename", show=False),
        Binding("d", "duplicate", "Duplicate", show=False),
        Binding("delete", "delete", "Delete", show=False),
        Binding("h", "toggle_recent", "Recent", show=False),
        Binding("f1", "help", "Help", show=False),
    ]

    def __init__(
        self,
        backend: SavedSearchBackend,
        *,
        initial_query: str = "",
        mode: str = "saved",
        create_on_mount: bool = False,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.initial_query = initial_query.strip()
        self.mode = "recent" if mode == "recent" else "saved"
        self.create_on_mount = create_on_mount
        self._saved: dict[str, SavedSearchEntry] = {}
        self._recent: dict[str, RecentSearchEntry] = {}
        self._selected_key: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="saved-search-root"):
            yield Label("Search · Saved searches", id="saved-search-title")
            yield Static(
                "Named advanced queries are local, account-scoped, and run through the "
                "same bounded Exchange/residual planner as live search.",
                id="saved-search-subtitle",
                markup=False,
            )
            yield Input(
                placeholder="Filter names, query text, scope, or execution details",
                id="saved-search-filter",
            )
            with Horizontal(id="saved-search-content"):
                with Vertical(id="saved-search-list-pane"):
                    yield DataTable(id="saved-search-table", cursor_type="row", zebra_stripes=True)
                    yield Static("", id="saved-search-empty", markup=False)
                with VerticalScroll(id="saved-search-detail"):
                    yield Static(
                        "Select a saved search for details.", id="saved-search-detail-text"
                    )
            with HorizontalScroll(id="saved-search-actions"):
                yield Button("Create", id="saved-search-create", variant="primary")
                yield Button("Run", id="saved-search-run")
                yield Button("Edit", id="saved-search-edit")
                yield Button("Rename", id="saved-search-rename")
                yield Button("Duplicate", id="saved-search-duplicate")
                yield Button("Delete", id="saved-search-delete", variant="warning")
                yield Button("Recent", id="saved-search-recent")
                yield Button("Help", id="saved-search-help")
                yield Button("Close", id="saved-search-close")
            yield Static("", id="saved-search-state", markup=False)
            yield Static(
                "↑↓ select · Enter run · Tab actions · Ctrl+N create · E edit · R rename · "
                "D duplicate · "
                "Delete remove · H recent · F1 help",
                id="saved-search-hints",
                markup=False,
            )

    def on_mount(self) -> None:
        table = self.query_one("#saved-search-table", DataTable)
        table.add_columns("Name", "Scope", "Query", "Last run", "Results")
        self._reload()
        self._apply_width(self.app.size.width)
        if self.create_on_mount:
            self.call_after_refresh(self.action_create)

    @on(Resize)
    def _resized(self, event: Resize) -> None:
        self._apply_width(event.size.width)

    def _apply_width(self, width: int) -> None:
        self.set_class(width < 96, "narrow")

    def _matches(self, values: Sequence[str]) -> bool:
        needle = self.query_one("#saved-search-filter", Input).value.strip().casefold()
        return not needle or any(needle in value.casefold() for value in values)

    def _reload(self, *, select_name: str | None = None) -> None:
        table = self.query_one("#saved-search-table", DataTable)
        table.clear(columns=False)
        self._saved.clear()
        self._recent.clear()
        if self.mode == "recent":
            for recent_entry in self.backend.list_recent(limit=50):
                if not self._matches(
                    (
                        recent_entry.name or "",
                        recent_entry.query_text,
                        recent_entry.scope,
                        recent_entry.execution_summary,
                        recent_entry.source,
                    )
                ):
                    continue
                key = f"recent:{recent_entry.history_id}"
                self._recent[key] = recent_entry
                table.add_row(
                    clean_cell(recent_entry.name or "Ad hoc", max_length=60),
                    clean_cell(recent_entry.scope, max_length=50),
                    clean_cell(recent_entry.query_text, max_length=100),
                    clean_cell(recent_entry.executed_at, max_length=32),
                    str(recent_entry.result_count),
                    key=key,
                )
        else:
            for saved_entry in self.backend.list_saved():
                if not self._matches(
                    (
                        saved_entry.name,
                        saved_entry.query_text,
                        saved_entry.scope,
                        saved_entry.execution_summary,
                    )
                ):
                    continue
                key = saved_entry.name
                self._saved[key] = saved_entry
                table.add_row(
                    clean_cell(saved_entry.name, max_length=60),
                    clean_cell(saved_entry.scope, max_length=50),
                    clean_cell(saved_entry.query_text, max_length=100),
                    clean_cell(saved_entry.last_run_at or "Never", max_length=32),
                    str(saved_entry.last_result_count)
                    if saved_entry.last_result_count is not None
                    else "—",
                    key=key,
                )
        count = len(self._recent) if self.mode == "recent" else len(self._saved)
        empty = self.query_one("#saved-search-empty", Static)
        empty.display = count == 0
        if count == 0:
            history_enabled = bool(getattr(self.backend, "history_enabled", True))
            empty.update(
                (
                    "No recent searches yet. Run a saved or interactive query to build history."
                    if history_enabled
                    else "Search history is disabled. Enable it in Search & Indexing settings."
                )
                if self.mode == "recent"
                else "No saved searches yet. Press Ctrl+N to save a reusable advanced query."
            )
            self.query_one("#saved-search-detail-text", Static).update(
                "Saved searches remain available offline; running mailbox searches requires "
                "an Exchange connection."
            )
        else:
            row = 0
            if select_name and self.mode == "saved":
                keys = list(self._saved)
                row = next(
                    (
                        index
                        for index, key in enumerate(keys)
                        if key.casefold() == select_name.casefold()
                    ),
                    0,
                )
            table.move_cursor(row=row)
            self._show_selected()
        self._update_actions()
        title = "Search · Recent searches" if self.mode == "recent" else "Search · Saved searches"
        self.query_one("#saved-search-title", Label).update(title)
        self.query_one("#saved-search-recent", Button).label = (
            "Saved" if self.mode == "recent" else "Recent"
        )
        self.query_one("#saved-search-state", Static).update(
            f"{count} {'recent run' if self.mode == 'recent' else 'saved search'}"
            + ("es" if count != 1 else "")
            + " shown."
        )

    def _selected_saved(self) -> SavedSearchEntry | None:
        return self._saved.get(self._selected_key or "")

    def _selected_recent(self) -> RecentSearchEntry | None:
        return self._recent.get(self._selected_key or "")

    def _show_selected(self) -> None:
        entry = self._selected_saved()
        recent = self._selected_recent()
        if entry is not None:
            detail = "\n".join(
                (
                    entry.name,
                    f"Query: {entry.query_text}",
                    f"Scope: {entry.scope}",
                    f"Execution: {entry.execution_summary}",
                    f"Created: {entry.created_at}",
                    f"Updated: {entry.updated_at}",
                    f"Last run: {entry.last_run_at or 'Never'}",
                    "Last result count: "
                    + (
                        str(entry.last_result_count)
                        if entry.last_result_count is not None
                        else "Unknown"
                    ),
                    f"Run count: {entry.run_count}",
                )
            )
        elif recent is not None:
            detail = "\n".join(
                (
                    recent.name or "Ad hoc search",
                    f"Query: {recent.query_text}",
                    f"Scope: {recent.scope}",
                    f"Execution: {recent.execution_summary}",
                    f"Source: {recent.source}",
                    f"Ran: {recent.executed_at}",
                    f"Results: {recent.result_count}",
                )
            )
        else:
            detail = "Select a row to inspect or run it."
        self.query_one("#saved-search-detail-text", Static).update(
            sanitize_terminal(detail, max_length=4_000)
        )
        self._update_actions()

    def _update_actions(self) -> None:
        saved_selected = self._selected_saved() is not None
        any_selected = saved_selected or self._selected_recent() is not None
        for button_id in ("run",):
            self.query_one(f"#saved-search-{button_id}", Button).disabled = not any_selected
        for button_id in ("edit", "rename", "duplicate", "delete"):
            self.query_one(f"#saved-search-{button_id}", Button).disabled = not saved_selected

    @on(DataTable.RowHighlighted, "#saved-search-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_key = str(event.row_key.value)
        self._show_selected()

    @on(Input.Changed, "#saved-search-filter")
    def _filter_changed(self) -> None:
        self._reload()

    def action_focus_filter(self) -> None:
        self.query_one("#saved-search-filter", Input).focus()

    def action_create(self) -> None:
        def saved(value: SavedSearchEditorValue | None) -> None:
            if value is None:
                return
            try:
                entry = self.backend.create(value.name, value.query_text)
            except Exception as exc:
                self.query_one("#saved-search-state", Static).update(
                    f"Create failed: {sanitize_terminal(str(exc), max_length=500)}"
                )
                return
            self.mode = "saved"
            self._reload(select_name=entry.name)
            self.query_one("#saved-search-state", Static).update(f"Created {entry.name}.")

        self.app.push_screen(
            SavedSearchEditorScreen(
                self.backend,
                title="Create saved search",
                query_text=self.initial_query,
            ),
            saved,
        )

    def action_edit(self) -> None:
        entry = self._selected_saved()
        if entry is None:
            return

        def saved(value: SavedSearchEditorValue | None) -> None:
            if value is None:
                return
            try:
                updated = self.backend.update(entry.name, value.query_text)
            except Exception as exc:
                self.query_one("#saved-search-state", Static).update(
                    f"Edit failed: {sanitize_terminal(str(exc), max_length=500)}"
                )
                return
            self._reload(select_name=updated.name)
            self.query_one("#saved-search-state", Static).update(f"Updated {updated.name}.")

        self.app.push_screen(
            SavedSearchEditorScreen(
                self.backend,
                title=f"Edit · {entry.name}",
                name=entry.name,
                query_text=entry.query_text,
                name_editable=False,
            ),
            saved,
        )

    def action_rename(self) -> None:
        entry = self._selected_saved()
        if entry is None:
            return

        def renamed(value: str | None) -> None:
            if value is None:
                return
            try:
                updated = self.backend.rename(entry.name, value)
            except Exception as exc:
                self.query_one("#saved-search-state", Static).update(
                    f"Rename failed: {sanitize_terminal(str(exc), max_length=500)}"
                )
                return
            self._reload(select_name=updated.name)
            self.query_one("#saved-search-state", Static).update(f"Renamed to {updated.name}.")

        self.app.push_screen(SavedSearchNameScreen("Rename saved search", entry.name), renamed)

    def action_duplicate(self) -> None:
        entry = self._selected_saved()
        if entry is None:
            return

        def duplicated(value: str | None) -> None:
            if value is None:
                return
            try:
                created = self.backend.duplicate(entry.name, value)
            except Exception as exc:
                self.query_one("#saved-search-state", Static).update(
                    f"Duplicate failed: {sanitize_terminal(str(exc), max_length=500)}"
                )
                return
            self._reload(select_name=created.name)
            self.query_one("#saved-search-state", Static).update(f"Duplicated as {created.name}.")

        self.app.push_screen(
            SavedSearchNameScreen("Duplicate saved search", f"{entry.name} copy"),
            duplicated,
        )

    def action_delete(self) -> None:
        entry = self._selected_saved()
        if entry is None:
            return

        def confirmed(value: bool | None) -> None:
            if not value:
                return
            try:
                removed = self.backend.delete(entry.name)
            except Exception as exc:
                self.query_one("#saved-search-state", Static).update(
                    f"Delete failed: {sanitize_terminal(str(exc), max_length=500)}"
                )
                return
            self._reload()
            self.query_one("#saved-search-state", Static).update(
                f"Deleted {entry.name}." if removed else f"{entry.name} was already absent."
            )

        self.app.push_screen(
            ConfirmationScreen(
                "Delete saved search?",
                f"Exactly one local saved query will be removed: {entry.name}\n"
                "No mailbox item or search result will be changed.",
                confirm_label="Delete saved search",
            ),
            confirmed,
        )

    def action_run(self) -> None:
        entry = self._selected_saved()
        recent = self._selected_recent()
        if entry is not None:
            self.post_message(
                SavedSearchRunRequested(
                    name=entry.name,
                    query_text=entry.query_text,
                    payload=entry.payload,
                    source="saved",
                )
            )
        elif recent is not None:
            self.post_message(
                SavedSearchRunRequested(
                    name=recent.name,
                    query_text=recent.query_text,
                    payload=recent.payload,
                    source="interactive",
                )
            )

    def action_toggle_recent(self) -> None:
        self.mode = "saved" if self.mode == "recent" else "recent"
        self._selected_key = None
        self._reload()

    def action_help(self) -> None:
        self.post_message(SavedSearchHelpRequested())

    def action_close(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "saved-search-create": self.action_create,
            "saved-search-run": self.action_run,
            "saved-search-edit": self.action_edit,
            "saved-search-rename": self.action_rename,
            "saved-search-duplicate": self.action_duplicate,
            "saved-search-delete": self.action_delete,
            "saved-search-recent": self.action_toggle_recent,
            "saved-search-help": self.action_help,
            "saved-search-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


__all__ = [
    "RecentSearchEntry",
    "SavedSearchBackend",
    "SavedSearchEditorScreen",
    "SavedSearchEntry",
    "SavedSearchHelpRequested",
    "SavedSearchPreview",
    "SavedSearchRunRequested",
    "SavedSearchScreen",
    "StateSavedSearchBackend",
]
