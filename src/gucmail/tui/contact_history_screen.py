"""Local-only, bounded contact-history inspection and confirmed clearing."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import cast

from textual import on
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, Vertical
from textual.widgets import Button, Label, Static

from gucmail.services.contact_history import ContactHistoryIndex
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import CorrespondenceEvent
from gucmail.tui.area_common import OperationalScreen, ProgressReporter, ViewState, clean_cell
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class ContactHistoryScreen(OperationalScreen):
    DEFAULT_CSS = """
    ContactHistoryScreen { layout: vertical; }
    #contact-history-root { height: 100%; padding: 1 2; }
    #contact-history-summary { height: auto; }
    #contact-history-table { height: 1fr; }
    #contact-history-actions { height: 3; }
    #contact-history-actions Button { margin-right: 1; }
    #area-state, #area-progress { height: auto; }
    """

    def __init__(
        self,
        index: ContactHistoryIndex,
        preferences: Callable[[], ContactOperationPreferences],
    ) -> None:
        super().__init__(area_name="contacts")
        self.index = index
        self.preferences = preferences

    def compose(self) -> ComposeResult:
        with Vertical(id="contact-history-root"):
            yield Label("Contacts · Local history")
            yield Static("", id="contact-history-summary", markup=False)
            yield Static("Loading local observations…", id="area-state", markup=False)
            yield DataTable(id="contact-history-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="area-progress", markup=False)
            with HorizontalScroll(id="contact-history-actions"):
                yield Button("Refresh", id="contact-history-refresh")
                yield Button("Clear local history", id="contact-history-clear", variant="warning")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        table = self.query_one("#contact-history-table", DataTable)
        table.add_columns("Address", "Date", "Direction", "Local message reference")
        self.action_refresh()

    def on_screen_resume(self) -> None:
        if self.is_mounted and not self._operation_active:
            self.action_refresh()

    def action_refresh(self) -> None:
        try:
            preferences = self.preferences()
        except Exception as exc:
            self._show_error("Local contact history", exc)
            return
        self.query_one("#contact-history-table", DataTable).clear()
        self.query_one("#contact-history-summary", Static).update(
            f"Indexing {'on' if preferences.history_indexing else 'off'} · "
            f"Retention {preferences.history_retention_days} days · "
            "Incomplete observations from explicit reports; no bodies or names. "
            "Clear removes only this account's local observations."
        )

        def load(
            cancel: Event, progress: ProgressReporter
        ) -> tuple[dict[str, object], tuple[CorrespondenceEvent, ...]]:
            return (
                self.index.status(retention_days=preferences.history_retention_days),
                self.index.query(limit=100, retention_days=preferences.history_retention_days)
                if preferences.history_indexing
                else (),
            )

        self._begin_operation("Loading local contact history", load, self._loaded, retry_safe=True)

    def _loaded(self, result: object) -> None:
        status, events = cast(tuple[dict[str, object], tuple[CorrespondenceEvent, ...]], result)
        current = self.preferences()
        table = self.query_one("#contact-history-table", DataTable)
        table.clear()
        if current.history_indexing:
            for event in events:
                if not isinstance(event, CorrespondenceEvent):
                    raise ValueError("Invalid contact history observation.")
                table.add_row(
                    clean_cell(event.address.email, max_length=160),
                    event.occurred_at.isoformat(),
                    event.direction.value,
                    clean_cell(event.message_reference or "(none)", max_length=100),
                )
        self._set_view_state(
            ViewState.READY,
            f"{status['observations']} retained observation(s); showing up to 100."
            if current.history_indexing
            else f"Indexing off; {status['observations']} retained observation(s) "
            "hidden until enabled or cleared.",
        )

    @on(Button.Pressed, "#contact-history-refresh")
    def _refresh_pressed(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#contact-history-clear")
    def _clear_pressed(self) -> None:
        self._confirm(
            "Clear local contact history?",
            "Remove this account's observed address metadata. Mailbox items and composer recent "
            "recipients are unaffected. Indexing stays at its current preference.",
            self._clear_confirmed,
            confirm_label="Clear",
        )

    def _clear_confirmed(self) -> None:
        self._begin_operation(
            "Clearing local contact history",
            lambda cancel, progress: self.index.clear(),
            lambda result: self.action_refresh(),
        )
