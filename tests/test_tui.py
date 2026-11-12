from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea

from gucmail.bootstrap import AppContext
from gucmail.command_catalog import build_core_command_registry
from gucmail.commands import CommandContext, CommandInvocation
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.headless import WatcherProcessSpec
from gucmail.models import (
    Address,
    AttachmentInfo,
    CalendarEvent,
    ContactInfo,
    DraftData,
    DraftLocation,
    FolderInfo,
    Importance,
    MessageDetail,
    MessageSummary,
    NotificationEvent,
    SearchQuery,
    TaskInfo,
)
from gucmail.nlp import DateRange, SizeConstraint
from gucmail.notification_events import PrivacyMode
from gucmail.notification_policies import MonitoringProfile
from gucmail.palette import universal_palette_registry
from gucmail.services.backup import BackupItem, BackupRequest, BackupService
from gucmail.services.compose import ComposeService
from gucmail.services.contacts import CorrespondenceDirection, CorrespondenceEvent
from gucmail.services.drafts import DraftService, draft_to_dict
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.local_search import LocalSearchMode, LocalSearchReport, LocalSearchResult
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.quota import QuotaService
from gucmail.services.recurring import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurringService,
    RecurringState,
)
from gucmail.services.search import QueryExpression, SearchPlan
from gucmail.settings import SettingsService
from gucmail.state import StateStore
from gucmail.tui.activity_screen import ActivityScreen
from gucmail.tui.app import (
    AreaScreen,
    ComposeScreen,
    ConfirmationScreen,
    FloatingReader,
    FolderMoveScreen,
    GUCmailApp,
    HelpScreen,
    MessageExportPathScreen,
    PaletteParameterReviewScreen,
    PaletteScreen,
    PaletteSequencePreviewScreen,
    PaneDivider,
    ReaderModeScreen,
    ReaderScreen,
)
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from gucmail.tui.area_backends import (
    ActivityAreaBackend,
    SettingsAreaBackend,
    StorageAreaBackend,
)
from gucmail.tui.area_common import (
    ConfirmationScreen as OperationalConfirmationScreen,
)
from gucmail.tui.area_common import (
    PublicOperationError,
    ViewState,
)
from gucmail.tui.backup_screen import (
    BackupBrowseScreen,
    BackupFolderBrowseScreen,
    BackupScreen,
)
from gucmail.tui.branding_screen import BrandingBrowserScreen
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen, DraftScheduleScreen
from gucmail.tui.local_search_screen import LocalSearchScreen
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from gucmail.tui.palette_routes import CATALOG_ROUTES, catalog_route
from gucmail.tui.pim_screens import (
    CalendarAvailabilityScreen,
    CalendarEditorScreen,
    CalendarScreen,
    ContactEditorScreen,
    ContactsScreen,
    CorrespondentsScreen,
    FilePathScreen,
    MeetingResponseScreen,
    PimActionRequested,
    TaskEditorScreen,
    TasksScreen,
)
from gucmail.tui.saved_search_screen import (
    SavedSearchEditorScreen,
    SavedSearchScreen,
    StateSavedSearchBackend,
)
from gucmail.tui.scheduled_screen import ScheduledScreen
from gucmail.tui.settings_form import SettingsForm
from gucmail.tui.settings_screen import SettingsScreen, SettingsTransferScreen
from gucmail.tui.shortcuts_screen import ShortcutsScreen
from gucmail.tui.storage_screen import CleanupPlanScreen, StorageScreen
from gucmail.tui.write_coordinator import app_write_coordinator


class FakeGateway:
    def status(self):  # type: ignore[no-untyped-def]
        return {"exchange_version": "Synthetic Exchange", "inbox_count": 1}

    def list_messages(
        self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
    ):  # type: ignore[no-untyped-def]
        assert folder == "inbox"
        assert limit > 0
        return [
            MessageSummary(
                reference="m-demo",
                subject="Synthetic welcome",
                sender=Address("teacher@example.test", "Course Team"),
                received_at=datetime(2026, 8, 5, 9, 15, tzinfo=UTC),
                is_read=False,
            )
        ]

    def get_quota(self):  # type: ignore[no-untyped-def]
        return None

    def show_message(self, reference: str, *, mark_read: bool = False) -> MessageDetail:
        assert reference == "m-demo"
        assert mark_read is False
        return MessageDetail(
            summary=self.list_messages(folder="inbox", limit=1)[0], body_text="Hello"
        )

    def raw_message(self, reference: str) -> bytes:
        assert reference == "m-demo"
        return b"From: teacher@example.test\r\nSubject: Synthetic welcome\r\n\r\nHello\r\n"

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> list[CalendarEvent]:
        assert start.tzinfo is not None
        assert end > start
        assert calendar_reference is None
        assert 0 < limit <= 200
        event = self.get_event("event-demo")
        return [event] if event.start < end and event.end > start else []

    def get_event(self, reference: str) -> CalendarEvent:
        assert reference == "event-demo"
        if not hasattr(self, "_calendar_fixture"):
            fixture_start = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
            self._calendar_fixture = CalendarEvent(
                reference="event-demo",
                subject="Synthetic lecture",
                start=fixture_start,
                end=fixture_start + timedelta(hours=1),
                timezone="UTC",
                location="Synthetic room",
                organizer=Address("organizer@example.test", "Organizer"),
                required_attendees=(Address("student@example.test", "Student"),),
                is_meeting=True,
            )
        return self._calendar_fixture

    def get_availability(
        self,
        attendees: tuple[Address, ...],
        *,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> tuple[dict[str, object], ...]:
        assert attendees
        assert end > start
        assert interval_minutes in {15, 30, 60}
        slots = int((end - start).total_seconds() // (interval_minutes * 60))
        return tuple(
            {
                "attendee": attendee,
                "available": True,
                "merged": "0" * slots,
                "events": (),
            }
            for attendee in attendees
        )

    def search_messages(
        self,
        query: SearchQuery,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MessageSummary]:
        assert query.subject == "Synthetic lecture" or query.sender or query.recipient
        assert 0 < limit <= 100
        assert offset == 0
        return self.list_messages(folder="inbox", limit=1)

    def list_contacts(self, *, folder_reference: str | None, limit: int) -> list[ContactInfo]:
        assert folder_reference is None
        assert 0 < limit <= 100
        return [
            ContactInfo(
                reference="contact-demo",
                display_name="Synthetic Contact",
                emails=(Address("contact@example.test"),),
            )
        ]

    def list_correspondence(
        self, *, since: datetime | None, limit: int
    ) -> list[CorrespondenceEvent]:
        assert since is None
        assert 0 < limit <= 2_500
        return [
            CorrespondenceEvent(
                Address("contact@example.test", "Synthetic Contact"),
                datetime(2026, 8, 5, 8, 30, tzinfo=UTC),
                CorrespondenceDirection.OUTBOUND,
                "m-demo",
            )
        ]

    def search_directory(self, query: str, *, limit: int = 25) -> list[Address]:
        assert query == "directory@example.test"
        assert 0 < limit <= 100
        return [Address("directory@example.test", "Directory Result")]

    def list_tasks(self, *, folder_reference: str | None, limit: int) -> list[TaskInfo]:
        assert folder_reference is None
        assert 0 < limit <= 100
        return [
            TaskInfo(
                reference="task-demo",
                subject="Synthetic follow-up",
                status="not_started",
            )
        ]

    def search_tasks(
        self,
        _query: object,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> list[TaskInfo]:
        return self.list_tasks(folder_reference=folder_reference, limit=limit)

    def get_task(self, reference: str) -> TaskInfo:
        assert reference == "task-demo"
        return self.list_tasks(folder_reference=None, limit=1)[0]

    def list_attachments(
        self,
        message_reference: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AttachmentInfo, ...]:
        assert message_reference == "m-demo"
        values = (
            AttachmentInfo(
                reference="att-demo",
                name="notes.txt",
                size=len(b"synthetic attachment"),
                content_type="text/plain",
            ),
        )
        return values[offset : offset + limit]

    def open_attachment(self, message_reference: str, attachment_reference: str) -> BytesIO:
        assert message_reference == "m-demo"
        assert attachment_reference == "att-demo"
        return BytesIO(b"synthetic attachment")


class ChainGateway(FakeGateway):
    """Synthetic result set with observable bulk mutations and a real PDF payload."""

    def __init__(self) -> None:
        self.search_queries: list[SearchQuery] = []
        self.marked: list[tuple[str, bool]] = []

    def search_messages(
        self,
        query: SearchQuery,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MessageSummary]:
        assert 0 < limit <= 100
        assert offset == 0
        self.search_queries.append(query)
        return [
            MessageSummary(
                reference="m-demo",
                subject="Synthetic bounded chain result",
                sender=Address("student-affairs@example.test", "Student Affairs"),
                received_at=datetime(2026, 8, 5, 9, 15, tzinfo=UTC),
                is_read=False,
                has_attachments=True,
                size=6_000_000,
            )
        ]

    def mark_read(self, reference: str, read: bool = True) -> str:
        assert reference == "m-demo"
        self.marked.append((reference, read))
        return reference

    def iter_backup_items(
        self,
        request: BackupRequest,
        _completed_item_keys: frozenset[str],
    ) -> list[BackupItem]:
        return [
            BackupItem(
                reference,
                "message",
                {"subject": "Synthetic bounded chain result", "body_text": "Synthetic body"},
            )
            for reference in request.selection.messages
        ]

    def list_attachments(
        self,
        message_reference: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AttachmentInfo, ...]:
        assert message_reference == "m-demo"
        values = (
            AttachmentInfo(
                reference="att-pdf",
                name="synthetic-report.pdf",
                size=len(b"synthetic pdf"),
                content_type="application/pdf",
            ),
            AttachmentInfo(
                reference="att-text",
                name="notes.txt",
                size=len(b"ignored text"),
                content_type="text/plain",
            ),
        )
        return values[offset : offset + limit]

    def open_attachment(self, message_reference: str, attachment_reference: str) -> BytesIO:
        assert message_reference == "m-demo"
        assert attachment_reference == "att-pdf"
        return BytesIO(b"synthetic pdf")


class SavedSearchGateway(FakeGateway):
    def search_messages_expression_planned(
        self,
        query: QueryExpression,
        plan: SearchPlan,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MessageSummary]:
        assert query == plan.ast
        assert 0 < limit <= 100
        assert offset == 0
        return self.list_messages(folder="inbox", limit=1)


def context_for(tmp_path: Path) -> AppContext:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.db",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.db",
        backup_dir=tmp_path / "data" / "backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="demo@example.test")
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def reloaded_context(context: AppContext) -> AppContext:
    return AppContext(
        paths=context.paths,
        config_store=context.config_store,
        config=context.config_store.load(),
        state=StateStore(context.paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def test_layout_and_folder_settings_survive_a_real_app_reconstruction(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    first = GUCmailApp(context, offline=True)
    first._message_ratio = 0.55
    first._split_orientation = "horizontal"
    first._preview_visible = False
    first._sidebar_visible = False
    first._sidebar_ratio = 0.31
    first._status_bar_visible = False
    first._layout_last_preset = "custom"
    first.ui.folder = "Projects/Research"
    first._expanded_folders = {"folder-projects", "folder-research"}

    first._persist_message_layout(reason="Synthetic restart test")
    first._persist_setting_values(
        {"mail.last_folder": first.ui.folder}, reason="Synthetic restart test"
    )
    first._persist_folder_expansion()

    second = GUCmailApp(reloaded_context(context), offline=True)
    assert second._message_ratio == pytest.approx(0.55)
    assert second._split_orientation == "horizontal"
    assert not second._preview_visible
    assert not second._sidebar_visible
    assert second._sidebar_ratio == pytest.approx(0.31)
    assert not second._status_bar_visible
    assert second._layout_last_preset == "custom"
    assert second.ui.folder == "Projects/Research"
    assert second._expanded_folders == {"folder-projects", "folder-research"}


@pytest.mark.asyncio
async def test_settings_commit_applies_mounted_layout_consumers_immediately(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(140, 40)) as pilot:
        backend = SettingsAreaBackend.from_context(
            context,
            on_applied=app._settings_applied_callback,
        )
        catalog = backend.load_catalog(cancel=Event(), progress=lambda _value: None)
        backend.apply_settings(
            {
                "layout.message_list_ratio": 0.60,
                "layout.preview_ratio": 0.40,
                "layout.split_orientation": "horizontal",
                "layout.preview_visible": False,
                "layout.sidebar_visible": False,
                "layout.sidebar_ratio": 0.30,
                "layout.status_bar_visible": False,
                "layout.reader_mode": "floating",
                "appearance.density": "spacious",
                "appearance.border_style": "double",
                "help.favorites": ["start.overview"],
                "help.recent_pages": ["start.overview"],
            },
            expected_revision=catalog.revision,
            cancel=Event(),
            progress=lambda _value: None,
        )
        await pilot.pause()

        assert app._message_ratio == pytest.approx(0.60)
        assert app._split_orientation == "horizontal"
        assert not app._preview_visible
        assert not app._sidebar_visible
        assert app._sidebar_ratio == pytest.approx(0.30)
        assert not app._status_bar_visible
        assert app._reader_mode == "floating"
        assert app._message_row_density == "spacious"
        assert app.query_one("#message-table", DataTable).cell_padding == 2
        assert app.query_one("#messages").styles.border.top[0] == "double"
        assert app.has_class("split-horizontal")
        assert app.has_class("preview-hidden")
        assert app.has_class("sidebar-hidden")
        assert not app.query_one("#status-line").display
        app._render_message(FakeGateway().show_message("m-demo"), mode="auto")
        await pilot.pause()
        assert app.query_one("#floating-reader").is_mounted
        help_state = app.context.state.load_help_state()
        assert help_state.favorite_page_ids == frozenset({"start.overview"})
        assert help_state.recent_page_ids == ("start.overview",)


@pytest.mark.asyncio
async def test_reader_preferences_execute_ask_external_and_narrow_fallback(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("layout.reader_mode", "ask")
    app = GUCmailApp(reloaded_context(context), gateway=FakeGateway())
    detail = FakeGateway().show_message("m-demo")

    async with app.run_test(size=(79, 26)) as pilot:
        for _ in range(30):
            if app.ui.selected_reference == "m-demo":
                break
            await pilot.pause(0.05)
        assert app.ui.selected_reference == "m-demo"

        app._render_message(detail, mode="auto")
        await pilot.pause()
        assert isinstance(app.screen, ReaderModeScreen)
        await pilot.click("#reader-mode-floating")
        await pilot.pause()
        assert not isinstance(app.screen, ReaderModeScreen)
        assert isinstance(app.query_one("#floating-reader"), FloatingReader)
        assert app.query_one("#message-table").can_focus

        app._reader_mode = "external"
        app._render_message(detail, mode="auto")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "registered handler" in app.screen.detail
        app.screen.dismiss(False)
        await pilot.pause()

        app._render_message(detail, mode="pane")
        await pilot.pause()
        assert isinstance(app.screen, ReaderScreen)


@pytest.mark.asyncio
async def test_layout_divider_keyboard_presets_reset_and_persistence(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(140, 40)) as pilot:
        original = app._message_ratio
        divider = app.query_one("#pane-divider", PaneDivider)
        preview = app.query_one("#preview-scroll")
        assert await pilot.mouse_down(divider, offset=(0, max(0, divider.size.height // 2)))
        assert await pilot.hover(preview, offset=(14, max(0, preview.size.height // 2)))
        assert await pilot.mouse_up(preview, offset=(14, max(0, preview.size.height // 2)))
        await pilot.pause()
        assert app._message_ratio > original

        ratio_after_drag = app._message_ratio
        await pilot.press("alt+right")
        assert app._message_ratio < ratio_after_drag
        await pilot.press("ctrl+backslash")
        assert not app._preview_visible
        await pilot.press("ctrl+backslash")
        assert app._preview_visible

        reading = catalog_route("layout.switch_to_reading_layout")
        app._catalog_direct(reading, CommandInvocation(reading.command_id))
        assert app._split_orientation == "horizontal"
        assert app._preview_visible
        assert app._layout_last_preset == "reading"

        list_only = catalog_route("layout.switch_to_list_only_layout")
        app._catalog_direct(list_only, CommandInvocation(list_only.command_id))
        assert not app._preview_visible
        assert app._layout_last_preset == "list_only"

        await pilot.press("alt+0")
        assert app._split_orientation == "vertical"
        assert app._preview_visible
        assert app._layout_last_preset == "default"

    restarted = GUCmailApp(reloaded_context(context), offline=True)
    assert restarted._split_orientation == "vertical"
    assert restarted._preview_visible
    assert restarted._layout_last_preset == "default"
    assert 0.20 <= restarted._message_ratio <= 0.80


@pytest.mark.asyncio
async def test_layout_palette_executes_every_required_local_command(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(30):
            if app.ui.selected_reference == "m-demo":
                break
            await pilot.pause(0.05)
        assert app.ui.selected_reference == "m-demo"

        original_ratio = app._message_ratio
        for command_id in (
            "layout.make_message_list_wider",
            "layout.make_message_list_narrower",
        ):
            app._palette_catalog_handler(CommandInvocation(command_id), app._palette_context())
        assert app._message_ratio == pytest.approx(original_ratio)

        handlers = app._palette_handlers()
        handlers["layout.preview_wider"](
            CommandInvocation("layout.preview_wider"), app._palette_context()
        )
        assert app._message_ratio < original_ratio
        handlers["layout.preview_narrower"](
            CommandInvocation("layout.preview_narrower"), app._palette_context()
        )
        assert app._message_ratio == pytest.approx(original_ratio)

        app._palette_catalog_handler(
            CommandInvocation("layout.collapse_preview"), app._palette_context()
        )
        assert not app._preview_visible
        collapsed_ratio = app._message_ratio
        app._palette_catalog_handler(
            CommandInvocation("layout.collapse_preview"), app._palette_context()
        )
        assert app._message_ratio == collapsed_ratio
        app._palette_catalog_handler(
            CommandInvocation("layout.expand_preview"), app._palette_context()
        )
        assert app._preview_visible
        handlers["layout.toggle_preview"](
            CommandInvocation("layout.toggle_preview"), app._palette_context()
        )
        assert not app._preview_visible
        handlers["layout.toggle_preview"](
            CommandInvocation("layout.toggle_preview"), app._palette_context()
        )
        assert app._preview_visible

        app._palette_catalog_handler(
            CommandInvocation("layout.open_floating_reader"), app._palette_context()
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query("#floating-reader"):
                break
        assert isinstance(app.query_one("#floating-reader"), FloatingReader)
        app._palette_catalog_handler(
            CommandInvocation("layout.close_floating_reader"), app._palette_context()
        )
        await pilot.pause()
        assert not app.query("#floating-reader")

        sidebar_ratio = app._sidebar_ratio
        app._palette_catalog_handler(
            CommandInvocation("layout.make_sidebar_wider"), app._palette_context()
        )
        assert app._sidebar_ratio > sidebar_ratio
        app._palette_catalog_handler(
            CommandInvocation("layout.make_sidebar_narrower"), app._palette_context()
        )
        assert app._sidebar_ratio == pytest.approx(sidebar_ratio, abs=0.02)
        app._palette_catalog_handler(
            CommandInvocation("layout.toggle_sidebar"), app._palette_context()
        )
        assert not app._sidebar_visible
        app._palette_catalog_handler(
            CommandInvocation("layout.toggle_sidebar"), app._palette_context()
        )
        assert app._sidebar_visible

        expected = {
            "layout.switch_to_compact_layout": ("compact", "vertical", True, 0.55),
            "layout.switch_to_reading_layout": ("reading", "horizontal", True, 0.30),
            "layout.switch_to_list_only_layout": ("list_only", "vertical", False, 0.40),
        }
        for command_id, state in expected.items():
            app._palette_catalog_handler(CommandInvocation(command_id), app._palette_context())
            preset, orientation, visible, ratio = state
            assert app._layout_last_preset == preset
            assert app._split_orientation == orientation
            assert app._preview_visible is visible
            assert app._message_ratio == pytest.approx(ratio)

        handlers["layout.reset"](CommandInvocation("layout.reset"), app._palette_context())
        assert app._layout_last_preset == "default"
        assert app._split_orientation == "vertical"
        assert app._preview_visible
        assert app._sidebar_visible

    restarted = GUCmailApp(reloaded_context(context), offline=True)
    assert restarted._layout_last_preset == "default"
    assert restarted._split_orientation == "vertical"
    assert restarted._preview_visible
    assert restarted._sidebar_visible


@pytest.mark.asyncio
async def test_help_favorite_mirrors_to_settings_and_survives_restart(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    first = GUCmailApp(context, offline=True)

    async with first.run_test(size=(120, 35)) as pilot:
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(first.screen, HelpScreen)
        page_id = first.screen.navigator.current.page_id
        await first.screen.action_favorite()
        favorites = first._settings_service().value("help.favorites").effective_value
        assert page_id in favorites

    second = GUCmailApp(reloaded_context(context), offline=True)
    async with second.run_test(size=(120, 35)) as pilot:
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(second.screen, HelpScreen)
        assert page_id in second.screen.navigator.state.favorite_page_ids


@pytest.mark.asyncio
async def test_every_help_palette_route_opens_its_real_interactive_destination(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("palette.primary_shortcut", "ctrl+space")
    SettingsService(context.config_store).set("storage.prohibit_send_mb", 128.0)
    context.state.record_quota_observation(
        "used",
        12 * 1024 * 1024,
        source="estimated",
        confidence="low",
        observed_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
    )
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(79, 26)) as pilot:
        handlers = app._palette_handlers()

        handlers["help.open"](CommandInvocation("help.open"), app._palette_context())
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.navigator.current.page_id == "start.overview"
        assert app.screen.has_class("narrow-help")
        index = app.screen.query_one("#help-index")
        article = app.screen.query_one("#help-article-pane")
        assert index.region.y < article.region.y
        app.screen.dismiss(None)
        await pilot.pause()

        handlers["help.search"](
            CommandInvocation.from_mapping("help.search", {"query": "webhhok discord"}),
            app._palette_context(),
        )
        for _ in range(30):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, HelpScreen) and app.screen.is_mounted
            ) and app.screen._result_page_ids:
                break
        assert isinstance(app.screen, HelpScreen)
        assert app.screen.query_one("#help-search", Input).value == "webhhok discord"
        assert app.screen._result_page_ids[0] == "integrations.discord"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.navigator.current.page_id == "integrations.discord"
        app.screen.dismiss(None)
        await pilot.pause()

        handlers["help.shortcuts"](CommandInvocation("help.shortcuts"), app._palette_context())
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)
        assert any(
            record.key == "ctrl+space" and record.customizable for record in app.screen.records
        )
        await pilot.press("escape")
        await pilot.pause()

        destinations = {
            "help.explain_command_palette": "palette.overview",
            "help.explain_natural_language_commands": "tutorial.palette",
            "help.explain_backups": "backup.overview",
            "help.explain_notifications": "notifications.rules",
            "help.explain_webhooks": "integrations.webhooks",
            "help.explain_scheduled_messages": "mail.scheduled",
            "help.explain_mailbox_quota": "storage.quota",
            "help.explain_a_selected_setting": "storage.quota",
            "help.show_command_examples": "palette.overview",
            "help.show_command_reference": "reference.commands",
        }
        catalog_help = handlers["catalog.help"]
        for command_id, expected_page in destinations.items():
            parameters = (
                {"reference": "storage.prohibit_send_mb"}
                if command_id == "help.explain_a_selected_setting"
                else {}
            )
            catalog_help(
                CommandInvocation.from_mapping(command_id, parameters),
                app._palette_context(),
            )
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            assert app.screen.navigator.current.page_id == expected_page
            if command_id == "help.explain_a_selected_setting":
                assert (
                    app.screen.query_one("#help-search", Input).value == "storage.prohibit_send_mb"
                )
            if command_id == "help.explain_mailbox_quota":
                quota_article = app.screen.repository.render(
                    expected_page, context=app.screen.help_context
                ).markdown
                assert "Current local quota snapshot" in quota_article
                assert "12.0 MiB" in quota_article
                assert "Effective capacity: **Unknown**" in quota_article
                assert "never contacts Exchange" in quota_article
            if command_id == "help.show_command_examples":
                examples = app.screen.repository.render(
                    expected_page, context=app.screen.help_context
                ).markdown
                assert "Tested natural-language example library" in examples
            app.screen.dismiss(None)
            await pilot.pause()

        app._open_help(initial_page_id="reference.commands")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)

        class LinkEvent:
            def __init__(self, href: str) -> None:
                self.href = href

            def stop(self) -> None:
                return None

        await app.screen.help_link_clicked(LinkEvent("command:help.open"))  # type: ignore[arg-type]
        for _ in range(40):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, PaletteScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#palette-input", Input).value:
                break
        assert isinstance(app.screen, PaletteScreen)
        assert app.screen.query_one("#palette-input", Input).value == "help.open"
        app.screen.dismiss(None)
        await pilot.pause()

        app._open_help(initial_page_id="settings.overview")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await app.screen.help_link_clicked(  # type: ignore[arg-type]
            LinkEvent("setting:storage.prohibit_send_mb")
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, SettingsScreen) and app.screen.is_mounted
            ) and app.screen._selected_setting_id == "storage.prohibit_send_mb":
                break
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._selected_setting_id == "storage.prohibit_send_mb"


def test_activity_backend_composes_durable_headless_start_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[tuple[str, ...]] = []

    def fake_start(spec: WatcherProcessSpec) -> int:
        launched.append(spec.command)
        return 4321

    monkeypatch.setattr("gucmail.tui.area_backends.start_watcher_process", fake_start)
    backend = ActivityAreaBackend.from_context(context_for(tmp_path), offline=False)
    cancel = Event()

    def progress(_value: object) -> None:
        return None

    starting = backend.set_watcher_enabled(True, cancel=cancel, progress=progress)

    assert starting.enabled
    assert starting.state == "starting"
    assert launched
    assert launched[0][1:3] == ("-m", "gucmail.watcher_entry")
    assert launched[0][-3:] == ("--profile", "default", "--forever")
    assert "--config" in launched[0] and "--data-dir" in launched[0]
    paused = backend.control_watcher("pause", cancel=cancel, progress=progress)
    assert paused.state == "pause requested"
    assert backend.watcher_store is not None
    assert backend.watcher_store.read_request() == "pause"
    resumed = backend.control_watcher("resume", cancel=cancel, progress=progress)
    assert resumed.state == "resume requested"
    assert backend.watcher_store.read_request() == "resume"
    stopped = backend.set_watcher_enabled(False, cancel=cancel, progress=progress)
    assert not stopped.enabled
    assert backend.watcher_store.read_request() == "stop"

    synchronized = backend.control_watcher("sync", cancel=cancel, progress=progress)
    assert synchronized.state == "one-shot synchronization starting"
    assert len(launched) == 2
    assert "--forever" not in launched[1]


def test_activity_backend_exposes_bounded_redacted_watcher_log_tail(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    log_path = context.paths.log_dir / "gucmail.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "2026-08-22 10:25:07 INFO gucmail.watcher synchronized user@example.test\n"
        "2026-08-22 10:26:07 INFO gucmail.ui unrelated line\n",
        encoding="utf-8",
    )
    backend = ActivityAreaBackend.from_context(context, offline=True)

    result = backend.load_activity(
        category="watcher",
        text="Watcher log",
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert len(result.events) == 1
    assert result.events[0].event_id.startswith("watcher-log:")
    assert "user@example.test" not in result.events[0].detail
    assert "<redacted>" in result.events[0].detail


def test_activity_backend_exposes_connection_failures_and_error_filter(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    observed_at = datetime(2026, 8, 31, 10, 25, tzinfo=UTC)
    claimed = context.state.claim_watcher_lease(
        scope="Inbox",
        lease_id="synthetic-lease",
        owner_id="synthetic-owner",
        process_id=4321,
        profile_id="default",
        ttl_seconds=120,
        now=observed_at,
        strategy="poll",
    )
    assert claimed is not None
    assert context.state.release_watcher_lease(
        "Inbox",
        "synthetic-lease",
        state="failed",
        at=observed_at + timedelta(seconds=10),
        error_summary="Synthetic TLS failure for user@example.test",
    )
    backend = ActivityAreaBackend.from_context(context, offline=True)

    connection = backend.load_activity(
        category="connection",
        text=None,
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )
    errors = backend.load_activity(
        category="error",
        text=None,
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )

    event = next(item for item in connection.events if item.category == "connection")
    assert event.severity == "error"
    assert "disconnected" in event.summary.casefold()
    assert "user@example.test" not in event.detail
    assert "<redacted>" in event.detail
    assert event in errors.events
    assert all(item.severity == "error" for item in errors.events)


def test_activity_backend_composes_privacy_safe_local_notification_test(tmp_path: Path) -> None:
    backend = ActivityAreaBackend.from_context(context_for(tmp_path), offline=False)
    cancel = Event()

    result = backend.test_notifications(
        cancel=cancel,
        progress=lambda _value: None,
    )

    assert result.category == "notification"
    assert result.severity == "info"
    assert result.provider == "console"
    assert "synthetic" in result.summary.casefold()
    history = backend.load_activity(
        category="notification",
        text=result.event_id,
        limit=10,
        cancel=cancel,
        progress=lambda _value: None,
    )
    assert any(item.correlation_id == result.event_id for item in history.events)

    plan = backend.plan_clear_activity(cancel=cancel, progress=lambda _value: None)
    assert plan.event_count >= 1
    assert "notification" in plan.categories
    assert plan.confirmation_token not in repr(plan)
    with pytest.raises(PublicOperationError, match="explicit confirmation"):
        backend.execute_clear_activity(
            plan,
            confirmed=False,
            cancel=cancel,
            progress=lambda _value: None,
        )
    cleared = backend.execute_clear_activity(
        plan,
        confirmed=True,
        cancel=cancel,
        progress=lambda _value: None,
    )
    assert cleared.cleared_count == plan.event_count
    hidden = backend.load_activity(
        category="notification",
        text=result.event_id,
        limit=10,
        cancel=cancel,
        progress=lambda _value: None,
    )
    assert hidden.events == ()

    later = backend.test_notifications(cancel=cancel, progress=lambda _value: None)
    current = backend.load_activity(
        category="notification",
        text=later.event_id,
        limit=10,
        cancel=cancel,
        progress=lambda _value: None,
    )
    assert any(item.correlation_id == later.event_id for item in current.events)


def test_activity_backend_reschedules_failed_webhook_from_durable_event(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    occurred_at = datetime(2026, 8, 22, 10, 20, tzinfo=UTC)
    event = NotificationEvent(
        "evt-webhook-retry",
        "new_message",
        occurred_at,
        "synthetic-account",
        folder="Inbox",
        subject="Synthetic retry fixture",
    )
    assert context.state.enqueue_notification_event(
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        detected_at=occurred_at,
        payload={
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "account_id": event.account_id,
            "folder": event.folder,
            "subject": event.subject,
        },
        folder=event.folder,
    )
    context.state.record_provider_attempt(
        event.event_id,
        "webhook:example",
        "dead_letter",
        attempted_at=occurred_at,
        error_summary="Synthetic 503",
        dead_letter=True,
    )
    context.state.update_notification_outbox(
        event.event_id,
        "dead_letter",
        at=occurred_at,
        increment_cycle=False,
    )
    backend = ActivityAreaBackend.from_context(context, offline=True)
    history = backend.load_activity(
        category="webhook",
        text=event.event_id,
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )
    retryable = next(item for item in history.events if item.retryable)
    assert retryable.correlation_id == event.event_id

    scheduled = backend.retry_delivery(
        event.event_id,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert scheduled.correlation_id == event.event_id
    with context.state.connection() as connection:
        row = connection.execute(
            "SELECT state,next_attempt_at,delivery_cycles FROM notification_outbox "
            "WHERE account_key=? AND event_id=?",
            (context.state.account_key, event.event_id),
        ).fetchone()
    assert row is not None
    assert row["state"] == "retry_scheduled"
    assert row["next_attempt_at"] is not None
    assert row["delivery_cycles"] == 0
    assert context.state.list_dead_letters() == ()


@pytest.mark.asyncio
async def test_activity_palette_executes_every_history_route_and_sanitized_export(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    observed_at = datetime(2026, 8, 31, 11, 5, tzinfo=UTC)
    claimed = context.state.claim_watcher_lease(
        scope="Inbox",
        lease_id="palette-activity-lease",
        owner_id="palette-activity-owner",
        process_id=5432,
        now=observed_at,
        strategy="poll",
    )
    assert claimed is not None
    assert context.state.release_watcher_lease(
        "Inbox",
        "palette-activity-lease",
        state="failed",
        at=observed_at + timedelta(seconds=5),
        error_summary="Synthetic error for private.user@example.test",
    )
    app = GUCmailApp(context, offline=True)
    handlers = app._palette_handlers()

    async with app.run_test(size=(140, 40)) as pilot:
        for command_id, expected_category in (
            ("activity.open", "all"),
            ("activity.show_errors", "error"),
            ("activity.show_recent_operations", "all"),
            ("activity.show_webhook_activity", "webhook"),
            ("activity.show_scheduled_send_activity", "scheduler"),
            ("activity.show_backup_activity", "backup"),
            ("activity.show_connection_activity", "connection"),
        ):
            invocation = CommandInvocation(command_id)
            if command_id == "activity.open":
                handlers[command_id](invocation, app._palette_context())
            else:
                app._palette_catalog_handler(invocation, app._palette_context())
            for _ in range(60):
                await pilot.pause(0.025)
                if isinstance(app.screen, ActivityScreen):
                    selected = app.screen.query_one("#activity-category", Select).value
                    if selected == expected_category and app.screen._snapshot is not None:
                        break
            assert isinstance(app.screen, ActivityScreen)
            assert app.screen.query_one("#activity-category", Select).value == expected_category

        activity = app.screen
        assert isinstance(activity, ActivityScreen)
        assert any(item.category == "connection" for item in activity._events)
        export_route = catalog_route("activity.export_sanitized_activity_log")
        app._catalog_screen_action(export_route, CommandInvocation(export_route.command_id))
        export_dir = context.paths.data_dir / "exports"
        for _ in range(80):
            await pilot.pause(0.025)
            exports = list(export_dir.glob("gucmail-activity-*.ndjson"))
            if exports:
                break
        assert len(exports) == 1
        document = exports[0].read_text(encoding="utf-8")
        records = [json.loads(line) for line in document.splitlines()]
        assert records[0]["schema_version"] == 1
        assert records[0]["redaction_enabled"] is True
        assert any(record.get("category") == "connection" for record in records[1:])
        assert "private.user@example.test" not in document
        assert "<redacted>@example.test" in document


@pytest.mark.asyncio
async def test_notification_palette_opens_and_mutates_runtime_configuration(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    service = NotificationConfigurationService(context.state)
    service.profiles.save_profile(
        MonitoringProfile("default", "Default monitoring", ("Inbox",), enabled=True)
    )
    service.profiles.activate("default")
    app = GUCmailApp(context, offline=True)
    handlers = app._palette_handlers()

    async with app.run_test(size=(140, 42)) as pilot:

        async def settings_idle() -> None:
            for _ in range(500):
                await pilot.pause(0.02)
                screen = app.screen
                if (
                    isinstance(screen, NotificationSettingsScreen)
                    and not screen._operation_active
                    and screen._pending_configuration_action is None
                ):
                    await pilot.pause()
                    if app.screen is screen and not screen._operation_active:
                        return
            raise AssertionError("Notification configuration did not finish before the next action")

        handlers["notifications.settings"](
            CommandInvocation("notifications.settings"), app._palette_context()
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, NotificationSettingsScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, NotificationSettingsScreen)

        enable = catalog_route("notifications.enable_desktop_notifications")
        app._catalog_screen_action(enable, CommandInvocation(enable.command_id))
        for _ in range(40):
            await pilot.pause(0.025)
            metadata = service.providers.get_provider("desktop")
            if metadata is not None and metadata.enabled:
                break
        metadata = service.providers.get_provider("desktop")
        assert metadata is not None and metadata.enabled
        await settings_idle()

        full = catalog_route("notifications.show_full_preview")
        app._catalog_screen_action(full, CommandInvocation(full.command_id))
        for _ in range(40):
            await pilot.pause(0.025)
            active = service.profiles.active_profile()
            if (
                active is not None
                and active.providers
                and active.providers[0].privacy.mode is PrivacyMode.FULL
            ):
                break
        active = service.profiles.active_profile()
        assert active is not None
        assert {item.privacy.mode for item in active.providers} == {PrivacyMode.FULL}

        app.screen.dismiss(None)
        await pilot.pause()
        handlers["notifications.history"](
            CommandInvocation("notifications.history"), app._palette_context()
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, ActivityScreen):
                value = app.screen.query_one("#activity-category", Select).value
                if value == "notification":
                    break
        assert isinstance(app.screen, ActivityScreen)
        assert app.screen.query_one("#activity-category", Select).value == "notification"


@pytest.mark.asyncio
async def test_notification_test_palette_executes_the_desktop_provider_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    observed: list[str] = []
    monkeypatch.setattr(app, "_open_activity_with_notification_test", observed.append)

    async with app.run_test(size=(120, 35)):
        app._palette_handlers()["notifications.test"](
            CommandInvocation("notifications.test"), app._palette_context()
        )

    assert observed == ["desktop"]


@pytest.mark.asyncio
async def test_integration_palette_targets_webhook_surface_and_copies_real_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=False)
    opened: list[tuple[str, str | None, str | None]] = []
    copied: list[str] = []

    def capture(
        action: str,
        *,
        name: str | None = None,
        provider_kind: str | None = None,
    ) -> None:
        opened.append((action, name, provider_kind))

    monkeypatch.setattr(app, "_open_notification_webhook_action", capture)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async with app.run_test(size=(120, 35)):
        handlers = app._palette_handlers()
        handlers["integrations.open"](
            CommandInvocation("integrations.open"), app._palette_context()
        )
        handlers["integrations.webhook_test"](
            CommandInvocation("integrations.webhook_test", (("name", "example"),)),
            app._palette_context(),
        )
        handlers["integrations.discord_test"](
            CommandInvocation("integrations.discord_test"), app._palette_context()
        )
        schema_route = catalog_route("integrations.copy_webhook_event_schema")
        app._catalog_application_action(schema_route)

    assert opened == [
        ("show", None, None),
        ("test", "example", None),
        ("test", None, "discord"),
    ]
    assert len(copied) == 1
    schema = json.loads(copied[0])
    assert schema["additionalProperties"] is False
    assert "event_id" in schema["properties"]


@pytest.mark.asyncio
async def test_backup_settings_drive_the_reopened_backup_center(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    destination = tmp_path / "configured-backups"
    settings = SettingsService(context.config_store)
    settings.set("backup.default_destination", str(destination))
    settings.set("backup.default_kind", "incremental")
    settings.set("backup.encryption_enabled", True)
    settings.set("backup.verify_after_create", False)
    settings.set("backup.retention_count", 7)
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("backups")
        await pilot.pause(0.5)
        assert isinstance(app.screen, BackupScreen)
        screen = app.screen
        assert screen.query_one("#backup-destination", Input).value == str(destination)
        assert screen.query_one("#backup-kind", Select).value == "incremental"
        assert screen.query_one("#backup-encrypt", Checkbox).value
        assert not screen.query_one("#backup-verify-after", Checkbox).value
        assert (
            "keep 7"
            in str(screen.query_one("#backup-retention-summary", Static).render()).casefold()
        )


@pytest.mark.asyncio
async def test_backup_palette_routes_preserve_kind_folder_search_and_cluster_scope(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())

    async def invoke_create(command_id: str) -> BackupScreen:
        route = catalog_route(command_id)
        app._catalog_screen_action(route, CommandInvocation(route.command_id))
        for _ in range(80):
            if isinstance(app.screen, OperationalConfirmationScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.03)
        assert isinstance(app.screen, OperationalConfirmationScreen)
        backup = next(
            screen for screen in reversed(app.screen_stack) if isinstance(screen, BackupScreen)
        )
        return backup

    async def close_create_workflow() -> None:
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert isinstance(app.screen, BackupScreen)
        app.screen.dismiss(None)
        await pilot.pause()

    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(40):
            if app.query_one("#message-table", DataTable).row_count:
                break
            await pilot.pause(0.03)

        full = await invoke_create("backup.create_full_backup")
        assert full._parse_request().kind == "full"
        await close_create_workflow()

        route = catalog_route("backup.create_incremental_backup")
        app._catalog_screen_action(route, CommandInvocation(route.command_id))
        for _ in range(80):
            if isinstance(app.screen, BackupScreen) and app.screen._repository_loaded:
                break
            await pilot.pause(0.03)
        assert isinstance(app.screen, BackupScreen)
        assert "Select a complete archive" in str(
            app.screen.query_one("#area-state", Static).render()
        )
        app.screen.dismiss(None)
        await pilot.pause()
        parent = BackupService(app.context.paths.backup_dir).create(
            [BackupItem("m-demo", "message", {"subject": "Synthetic parent"})],
            backup_id="palette-parent",
        )
        incremental = await invoke_create("backup.create_incremental_backup")
        assert incremental._parse_request().kind == "incremental"
        assert incremental._parse_request().parent_backup_id == parent.backup_id
        await close_create_workflow()

        expected_folder = app._selected_folder_info()
        current_folder = await invoke_create("backup.back_up_current_folder")
        assert current_folder._parse_request().folders == (
            expected_folder.path if expected_folder is not None else app.ui.folder,
        )
        await close_create_workflow()

        app._current_search_text = "unread:true"
        search_results = await invoke_create("backup.back_up_search_results")
        assert search_results._parse_request().messages == ("m-demo",)
        await close_create_workflow()

        app._open_local_search()
        await pilot.pause()
        assert isinstance(app.screen, LocalSearchScreen)
        app.screen._search_complete(
            LocalSearchReport(
                mode=LocalSearchMode.DUPLICATES,
                query="",
                results=(
                    LocalSearchResult(
                        result_id="cluster-1",
                        reference="cluster-1",
                        title="Synthetic duplicate group",
                        source="local-index",
                        kind="duplicate-group",
                        score=100.0,
                        member_references=("m-demo", "m-related"),
                    ),
                ),
                searched_sources=("local-index",),
                scanned_count=2,
            )
        )
        cluster = await invoke_create("backup.back_up_cluster")
        assert cluster._parse_request().messages == ("m-demo", "m-related")
        await close_create_workflow()


def test_storage_backend_uses_a_bounded_mailbox_wide_sample(tmp_path: Path) -> None:
    class StorageGateway:
        def __init__(self) -> None:
            self.message_folders: list[str] = []

        def list_folders(
            self,
            parent_reference: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[FolderInfo, ...]:
            assert offset == 0
            if parent_reference is not None:
                return ()
            return (
                FolderInfo("projects", "Projects", "Projects", None, folder_class="IPF.Note"),
                FolderInfo(
                    "calendar", "Calendar", "Calendar", None, folder_class="IPF.Appointment"
                ),
            )[:limit]

        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ) -> tuple[MessageSummary, ...]:
            self.message_folders.append(folder)
            return (
                MessageSummary(
                    reference=f"message-{folder}",
                    subject=f"Synthetic {folder}",
                    sender=Address("sender@example.test"),
                    received_at=datetime(2026, 8, 5, tzinfo=UTC),
                    size=1_024,
                    folder_path=folder,
                ),
            )[:limit]

    context = context_for(tmp_path)
    gateway = StorageGateway()
    context._gateway = gateway  # type: ignore[assignment]
    backend = StorageAreaBackend.from_context(context, online=True, maximum_scan_items=10)

    result = backend.analyze_storage(
        "folders",
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert result.bounded
    assert gateway.message_folders == ["inbox", "projects"]
    assert {row.label for row in result.rows} == {"inbox", "projects"}


def test_storage_backend_projects_bounded_attachment_metadata_without_message_detail(
    tmp_path: Path,
) -> None:
    class StorageGateway:
        def __init__(self) -> None:
            self.attachment_calls: list[tuple[str, int, int]] = []

        def list_folders(
            self,
            parent_reference: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[FolderInfo, ...]:
            return ()

        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ) -> tuple[MessageSummary, ...]:
            assert folder == "inbox"
            return (
                MessageSummary(
                    reference="message-with-attachment",
                    subject="Synthetic attachment owner",
                    sender=Address("sender@example.test"),
                    received_at=datetime(2026, 8, 5, tzinfo=UTC),
                    size=8_192,
                    folder_path="inbox",
                    has_attachments=True,
                ),
            )[:limit]

        def list_attachments(
            self,
            message_reference: str,
            limit: int = 100,
            offset: int = 0,
        ) -> tuple[AttachmentInfo, ...]:
            self.attachment_calls.append((message_reference, limit, offset))
            return (
                AttachmentInfo("attachment-one", "report.pdf", 4_096, "application/pdf"),
                AttachmentInfo("attachment-two", "notes.txt", 1_024, "text/plain"),
            )

        def show_message(self, _reference: str) -> MessageDetail:
            raise AssertionError("storage metadata analysis fetched message detail")

    context = context_for(tmp_path)
    gateway = StorageGateway()
    context._gateway = gateway  # type: ignore[assignment]
    backend = StorageAreaBackend.from_context(context, online=True, maximum_scan_items=10)

    largest = backend.analyze_storage(
        "largest-attachments",
        limit=10,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert gateway.attachment_calls == [("message-with-attachment", 100, 0)]
    assert largest.rows[0].size_bytes == 5_120
    assert largest.rows[0].item_count == 2
    assert "bodies and attachment payloads were not downloaded" in largest.note.casefold()


def test_storage_backend_finds_exact_duplicate_local_draft_attachments(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    attachment = tmp_path / "shared.pdf"
    attachment.write_bytes(b"trusted synthetic attachment")
    drafts = DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )
    drafts.create(DraftData(reference="draft-one", attachment_paths=[attachment]))
    drafts.create(DraftData(reference="draft-two", attachment_paths=[attachment]))
    backend = StorageAreaBackend.from_context(context, online=False, maximum_scan_items=20)

    result = backend.analyze_storage(
        "duplicate-attachments",
        limit=20,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert len(result.rows) == 1
    assert result.rows[0].item_count == 2
    assert "draft-one" in result.rows[0].details
    assert "draft-two" in result.rows[0].details
    assert "not downloaded automatically" in result.note


def test_storage_backend_analyzes_local_draft_attachments_offline(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    pdf = tmp_path / "slides.pdf"
    text = tmp_path / "notes.txt"
    pdf.write_bytes(b"p" * 4_096)
    text.write_bytes(b"t" * 1_024)
    DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    ).create(DraftData(reference="draft-local", attachment_paths=[pdf, text]))
    backend = StorageAreaBackend.from_context(context, online=False, maximum_scan_items=20)

    largest = backend.analyze_storage(
        "largest-attachments",
        limit=20,
        cancel=Event(),
        progress=lambda _value: None,
    )
    by_type = backend.analyze_storage(
        "attachment-types",
        limit=20,
        cancel=Event(),
        progress=lambda _value: None,
    )

    assert largest.rows[0].size_bytes == 5_120
    assert largest.rows[0].item_count == 2
    assert {row.label for row in by_type.rows} == {".pdf", ".txt"}
    assert "offline analysis uses locally known" in by_type.note.casefold()


@pytest.mark.asyncio
async def test_tui_startup_navigation_help_and_palette(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(140, 40)) as pilot:
        assert str(app.query_one("#brand-g", Static).content) == "G"
        assert str(app.query_one("#brand-u", Static).content) == "U"
        assert str(app.query_one("#brand-c", Static).content) == "C"
        assert str(app.query_one("#brand-name", Static).content) == " Mail CLI"
        for _ in range(20):
            if app.query_one("#message-table").row_count == 1:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app.query_one("#message-table").row_count == 1  # type: ignore[attr-defined]
        assert "Send limit ?/92 MiB" in str(app.query_one("#account-bar", Static).content)
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        assert app.screen.runtime_context.online is True
        assert "Course Team" in app.screen.person_candidates
        assert "teacher@example.test" in app.screen.person_candidates
        await pilot.press("escape")
        app._open_area("settings")
        for _ in range(20):
            await pilot.pause(0.05)
            if app.screen.query_one("#settings-form", SettingsForm).row_count:
                break
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen.query_one("#settings-form", SettingsForm).row_count > 0
        await pilot.press("escape")
        await pilot.press("c")
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        app.screen.query_one("#compose-subject", Input).value = "Synthetic recovery"
        app.screen._request_autosave()
        recovery_workers = [worker for worker in app.workers if worker.group == "compose-autosave"]
        await asyncio.wait_for(app.workers.wait_for_complete(recovery_workers), timeout=5)
        assert list(app.context.paths.recovery_dir.glob("*.json"))
        recovered = DraftService(app.context.state, app.context.paths.recovery_dir).read_recovery(
            app.screen.draft.reference
        )
        assert recovered.subject == "Synthetic recovery"
        app.screen.dismiss(None)


@pytest.mark.asyncio
async def test_palette_shortcut_and_visible_launcher_follow_settings(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("palette.primary_shortcut", "alt+k")
    settings.set("palette.launcher_style", "text")
    settings.set("palette.launcher_position", "top_right")
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 36)) as pilot:
        assert str(app.query_one("#palette-launcher", Static).content) == "Commands"
        await pilot.press("alt+k")
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        assert await pilot.click("#palette-launcher")
        assert isinstance(app.screen, PaletteScreen)


@pytest.mark.asyncio
async def test_palette_shortcuts_can_be_rebound_or_disabled_without_textual_fallback(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("palette.primary_shortcut", "alt+k")
    settings.set("palette.secondary_shortcut", "")
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("ctrl+k")
        assert not isinstance(app.screen, PaletteScreen)
        await pilot.press("ctrl+p")
        assert not isinstance(app.screen, PaletteScreen)
        await pilot.press("alt+k")
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")

        app._palette_primary_shortcut = "ctrl+space"
        app._palette_secondary_shortcut = ""
        app._bind_palette_shortcuts()
        await pilot.press("alt+k")
        assert not isinstance(app.screen, PaletteScreen)
        await pilot.press("ctrl+space")
        assert isinstance(app.screen, PaletteScreen)


@pytest.mark.asyncio
async def test_palette_custom_launcher_grouping_previews_and_frequency(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("palette.launcher_style", "custom")
    settings.set("palette.launcher_symbol", "★")
    settings.set("palette.launcher_position", "top_right")
    settings.set("palette.command_history", ["help.open"])
    for _ in range(3):
        context.state.record_command_usage("layout.preview_wider", context="mail")
    app = GUCmailApp(context, gateway=FakeGateway(), offline=True)

    async with app.run_test(size=(120, 36)) as pilot:
        assert str(app.query_one("#palette-launcher", Static).content) == "★"
        assert app._recent_palette_commands()[0] == "help.open"
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        screen = app.screen
        display = screen._display_suggestions
        assert display
        assert display[0] is None
        categories = [
            str(item.query_one("Label").content) for item in screen.query(".palette-category")
        ]
        assert len(categories) >= 2

        screen.query_one("#palette-input", Input).value = "make preview wider"
        await pilot.pause()
        interpretation = str(screen.query_one("#palette-explanation", Static).content)
        assert interpretation.startswith("→ Make Preview Wider")
        for _ in range(80):
            items = tuple(screen.query(".palette-item"))
            if items and all(item.query("Label") for item in items):
                break
            await pilot.pause(0.025)
        labels = "\n".join(
            str(item.query_one("Label").content) for item in screen.query(".palette-item")
        )
        for _ in range(40):
            if "Frequent x3" in labels:
                break
            await pilot.pause(0.025)
            labels = "\n".join(
                str(item.query_one("Label").content) for item in screen.query(".palette-item")
            )
        assert "Frequent x3" in labels
        suggestion = next(
            item
            for item in screen._suggestions
            if item.command.command_id == "layout.preview_wider"
        )
        detail = screen._suggestion_detail(suggestion)
        plan = screen.engine.plan(
            suggestion.invocation or CommandInvocation(suggestion.command.command_id),
            context=screen.runtime_context,
        )
        assert not plan.cli.available and plan.cli.argv is None
        assert plan.cli.reason  # The mapping limitation remains in the real plan.
        assert "gucmail " not in detail and "No exact CLI projection" not in detail
        assert "Next:" in detail
        await pilot.press("down")
        # Selection explains the actual command, availability, and follow-ups;
        # it need not reproduce the old generic implementation-description text.
        explanation = str(screen.query_one("#palette-explanation", Static).content)
        assert "Make Preview Wider" in explanation
        assert "Available" in explanation
        assert "Next:" in explanation


@pytest.mark.asyncio
async def test_palette_renders_secondpass_short_families_and_executes_tomorrow_create(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    expected = {
        "atta": {
            "attachments.open",
            "attachments.show_largest_attachments",
            "attachments.search",
            "attachments.download",
        },
        "bkp": {"backup.create", "backup.open", "backup.verify"},
        "notif": {
            "notifications.settings",
            "notifications.enable_desktop_notifications",
            "notifications.test",
            "notifications.history",
        },
        "cal tom": {
            "calendar.tomorrow",
            "calendar.create_tomorrow",
            "calendar.show_tomorrow_events",
        },
    }

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            if app.ui.online:
                break
            await pilot.pause(0.05)
        assert app.ui.online
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        palette = app.screen
        for query, command_ids in expected.items():
            palette.query_one("#palette-input", Input).value = query
            await pilot.pause()
            assert {
                suggestion.command.command_id
                for suggestion in palette._suggestions[: len(command_ids)]
            } == command_ids
            assert all(
                "command-family abbreviation" in suggestion.reason
                for suggestion in palette._suggestions[: len(command_ids)]
            )

        tomorrow_create = next(
            suggestion
            for suggestion in palette._suggestions
            if suggestion.command.command_id == "calendar.create_tomorrow"
        )
        palette._submit_suggestion(tomorrow_create)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Create Event Tomorrow" in app.screen._dialog_title
        app.screen.dismiss(True)
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        expected_day = (datetime.now(ZoneInfo("Africa/Cairo")) + timedelta(days=1)).date()
        start = app.screen.query_one("#calendar-edit-start", Input).value
        assert datetime.fromisoformat(start).date() == expected_day


@pytest.mark.asyncio
async def test_configurable_theme_cycle_shortcuts_preview_both_directions(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("appearance.previous_theme_shortcut", "ctrl+alt+left")
    settings.set("appearance.next_theme_shortcut", "ctrl+alt+right")
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        original = app.active_theme
        expected_next = app.theme_registry.next(original.id)
        await pilot.press("ctrl+alt+right")
        await pilot.pause()
        assert app.active_theme == expected_next
        assert app.theme_session.is_previewing

        await pilot.press("ctrl+alt+left")
        await pilot.pause()
        assert app.active_theme == original


@pytest.mark.asyncio
async def test_appearance_palette_executes_browser_and_durable_accessibility_actions(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(140, 42)) as pilot:
        open_route = catalog_route("appearance.open_theme_settings")
        app._palette_catalog_handler(
            CommandInvocation(open_route.command_id), app._palette_context()
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, ThemeBrowserScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ThemeBrowserScreen)

        search_route = catalog_route("appearance.search_themes")
        app._palette_catalog_handler(
            CommandInvocation.from_mapping(search_route.command_id, {"query": "defalt drk"}),
            app._palette_context(),
        )
        await pilot.pause()
        assert app.screen.query_one("#theme-search", Input).value == "defalt drk"
        for _ in range(40):
            if app.screen._selected_theme_id == "builtin.default-dark":
                break
            await pilot.pause(0.025)
        assert app.screen._selected_theme_id == "builtin.default-dark"

        favorite_route = catalog_route("appearance.favorite_theme")
        app._palette_catalog_handler(
            CommandInvocation(favorite_route.command_id), app._palette_context()
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.theme_session.preferences.favorites == ("builtin.default-dark",)

        custom = app.theme_browser_backend.duplicate(
            "builtin.default-dark",
            name="Route Transfer Theme",
        )
        app.screen.query_one("#theme-search", Input).value = "Route Transfer Theme"
        await pilot.pause()
        assert app.screen._selected_theme_id == custom.id
        transfer_path = tmp_path / "route-transfer-theme.json"
        export_route = catalog_route("appearance.export_theme")
        app._palette_catalog_handler(
            CommandInvocation.from_mapping(
                export_route.command_id,
                {"path": str(transfer_path)},
            ),
            app._palette_context(),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert transfer_path.exists()

        app.theme_browser_backend.delete_custom(custom.id)
        app.screen._refresh_rows()
        import_route = catalog_route("appearance.import_theme")
        app._palette_catalog_handler(
            CommandInvocation.from_mapping(
                import_route.command_id,
                {"path": str(transfer_path)},
            ),
            app._palette_context(),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.theme_registry.get(custom.id).name == "Route Transfer Theme"

        app.screen.dismiss(None)
        await pilot.pause()
        app._palette_handlers()["appearance.apply_theme"](
            CommandInvocation.from_mapping(
                "appearance.apply_theme",
                {"theme": "Default Light"},
            ),
            app._palette_context(),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.active_theme.id == "builtin.default-light"
        assert (
            SettingsService(context.config_store).value("appearance.theme").effective_value
            == "builtin.default-light"
        )

        for command_id in (
            "appearance.hide_ascii_banner",
            "appearance.show_ascii_banner",
            "appearance.change_density",
            "appearance.toggle_high_contrast",
            "appearance.toggle_monochrome",
        ):
            app._palette_catalog_handler(CommandInvocation(command_id), app._palette_context())
            await pilot.pause()

        settings = SettingsService(context.config_store)
        assert settings.value("appearance.banner_visible").effective_value is True
        assert app.branding_preferences.visible
        assert settings.value("appearance.density").effective_value == "compact"
        assert app._message_row_density == "compact"
        assert settings.value("accessibility.high_contrast").effective_value is True
        assert app._runtime_theme.contrast.value == "maximum"
        assert settings.value("accessibility.monochrome").effective_value is True
        assert context.config.no_color
        assert {color for _, color in app._runtime_theme.palette.colors()} == {
            "#000000",
            "#ffffff",
        }

        app._palette_catalog_handler(
            CommandInvocation("appearance.reset_appearance"), app._palette_context()
        )
        await pilot.pause()
        assert settings.value("appearance.banner_visible").effective_value is True
        assert settings.value("appearance.density").effective_value == "theme"
        assert settings.value("accessibility.high_contrast").effective_value is False
        assert settings.value("accessibility.monochrome").effective_value is False
        assert context.config.no_color is False

        banner_route = catalog_route("appearance.change_ascii_banner")
        app._palette_catalog_handler(
            CommandInvocation(banner_route.command_id), app._palette_context()
        )
        await pilot.pause()
        assert isinstance(app.screen, BrandingBrowserScreen)

    restarted = GUCmailApp(reloaded_context(context), offline=True)
    assert restarted.context.config.theme == "builtin.default-dark"
    assert restarted.context.config.no_color is False
    assert restarted.theme_session.preferences.favorites == ()


@pytest.mark.asyncio
async def test_mail_bounded_selection_bulk_export_backup_and_external_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MultiMessageGateway(FakeGateway):
        def __init__(self) -> None:
            self.marked: list[tuple[str, bool]] = []

        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ):  # type: ignore[no-untyped-def]
            assert folder == "inbox"
            return [
                MessageSummary(
                    reference="m-one",
                    subject="First / unsafe",
                    sender=Address("teacher@example.test", "Teacher"),
                    received_at=datetime(2026, 8, 5, 9, 15, tzinfo=UTC),
                    is_read=False,
                ),
                MessageSummary(
                    reference="m-two",
                    subject="Second",
                    sender=Address("other@example.test", "Other"),
                    received_at=datetime(2026, 8, 5, 9, 16, tzinfo=UTC),
                    is_read=True,
                ),
                MessageSummary(
                    reference="m-three",
                    subject="Third",
                    sender=Address("teacher@example.test", "Teacher"),
                    received_at=datetime(2026, 8, 5, 9, 17, tzinfo=UTC),
                    is_read=False,
                ),
            ][:limit]

        def mark_read(self, reference: str, read: bool = True) -> None:
            self.marked.append((reference, read))

        def raw_message(self, reference: str) -> bytes:
            return f"Subject: {reference}\r\n\r\nSynthetic\r\n".encode()

    gateway = MultiMessageGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    opened: list[Path] = []
    monkeypatch.setattr("gucmail.tui.app._open_file_with_registered_handler", opened.append)

    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            if app.query_one("#message-table").row_count == 3:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app.query_one("#message-table").row_count == 3  # type: ignore[attr-defined]

        select_unread = catalog_route("mail.select_unread")
        app._catalog_direct(select_unread, CommandInvocation(select_unread.command_id))
        assert app._selected_message_references == {"m-one", "m-three"}
        select_read = catalog_route("mail.select_read")
        app._catalog_direct(select_read, CommandInvocation(select_read.command_id))
        assert app._selected_message_references == {"m-two"}
        select_all = catalog_route("mail.select_all")
        app._catalog_direct(select_all, CommandInvocation(select_all.command_id))
        assert app._selected_message_references == {"m-one", "m-two", "m-three"}
        app.ui.selected_reference = "m-one"
        select_sender = catalog_route("mail.select_messages_from_sender")
        app._catalog_direct(select_sender, CommandInvocation(select_sender.command_id))
        assert app._selected_message_references == {"m-one", "m-three"}
        app._current_search_text = "unread:true"
        select_search = catalog_route("mail.select_messages_matching_current_search")
        app._catalog_direct(select_search, CommandInvocation(select_search.command_id))
        assert app._selected_message_references == {"m-one", "m-two", "m-three"}
        app._selected_message_references = {"m-one", "m-three"}

        visible_route = catalog_route("mail.mark_all_visible_read")
        app._catalog_direct(visible_route, CommandInvocation(visible_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "m-one" in app.screen.detail and "m-three" in app.screen.detail
        assert "m-two" not in app.screen.detail
        app.screen.dismiss(False)
        await pilot.pause()

        route = catalog_route("mail.mark_all_selected_read")
        app._catalog_direct(route, CommandInvocation(route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "m-one" in app.screen.detail and "m-three" in app.screen.detail
        assert "m-two" not in app.screen.detail
        app.screen.dismiss(True)
        for _ in range(30):
            if len(gateway.marked) == 2:
                break
            await pilot.pause(0.05)
        assert gateway.marked == [("m-one", True), ("m-three", True)]

        app._selected_message_references = {"m-one", "m-three"}
        export_route = catalog_route("mail.export_selected_messages")
        app._catalog_direct(export_route, CommandInvocation(export_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, MessageExportPathScreen)
        destination = tmp_path / "mail-export"
        app.screen.query_one("#message-export-destination", Input).value = str(destination)
        app.screen._submit()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(40):
            if len(tuple(destination.glob("*.eml"))) == 2:
                break
            await pilot.pause(0.05)
        exported = tuple(destination.glob("*.eml"))
        assert len(exported) == 2
        assert all(path.read_bytes().startswith(b"Subject: m-") for path in exported)

        app._selected_message_references = {"m-one", "m-three"}
        backup_route = catalog_route("mail.back_up_selected_messages")
        app._catalog_direct(backup_route, CommandInvocation(backup_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, BackupScreen)
        assert app.screen.selected_messages == ("m-one", "m-three")
        assert app.screen.query_one("#backup-kind", Select).value == "selected-messages"
        assert app.screen._parse_request().messages == ("m-one", "m-three")
        app.screen.dismiss(None)
        await pilot.pause()

        app._selected_message_references = {"m-one"}
        app.ui.selected_reference = "m-one"
        external_route = catalog_route("mail.open_selected_message_externally")
        app._catalog_direct(external_route, CommandInvocation(external_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        app.screen.dismiss(True)
        deadline = asyncio.get_running_loop().time() + 10
        while not opened and asyncio.get_running_loop().time() < deadline:
            await pilot.pause(0.05)
        assert len(opened) == 1
        assert opened[0].suffix == ".eml"
        assert opened[0].is_file()


@pytest.mark.asyncio
async def test_catalog_message_mutations_execute_real_gateway_operations(tmp_path: Path) -> None:
    # This stateless gateway checks dispatch. Persistent restore/restart effects
    # are covered separately with the actual local mailbox and operation journal.
    class MutatingGateway(FakeGateway):
        def __init__(self) -> None:
            self.mutations: list[tuple[object, ...]] = []

        def set_importance(self, reference: str, importance: Importance) -> str:
            self.mutations.append(("importance", reference, importance))
            return reference

        def categorize(self, reference: str, categories: tuple[str, ...], mode: str = "set") -> str:
            self.mutations.append(("categorize", reference, categories, mode))
            return reference

        def copy_message(self, reference: str, destination: str) -> str:
            self.mutations.append(("copy", reference, destination))
            return reference

        def move_message(self, reference: str, destination: str) -> str:
            self.mutations.append(("move", reference, destination))
            return reference

        def delete_message(self, reference: str, permanent: bool = False) -> str:
            self.mutations.append(("delete", reference, permanent))
            return reference

    gateway = MutatingGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 35)) as pilot:
        async with asyncio.timeout(15):
            while app.query_one("#message-table", DataTable).row_count != 1 or app.ui.busy:
                await asyncio.sleep(0.005)

        async def completed(expected_count: int) -> None:
            async with asyncio.timeout(15):
                while (len(gateway.mutations) != expected_count
                       or app_write_coordinator(app).pending):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert len(gateway.mutations) == expected_count
            assert not app_write_coordinator(app).pending

        app.ui.selected_reference = "m-demo"

        routed = (
            ("mail.set_importance", {"importance": "high"}),
            ("mail.add_category", {"category": "Course"}),
            ("mail.remove_category", {"category": "Course"}),
            ("mail.copy_message", {"folder": "Archive"}),
        )
        for expected_count, (command_id, parameters) in enumerate(routed, start=1):
            route = catalog_route(command_id)
            app._catalog_direct(route, CommandInvocation.from_mapping(command_id, parameters))
            await completed(expected_count)

        permanent = catalog_route("mail.permanently_delete")
        app._catalog_direct(permanent, CommandInvocation(permanent.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "m-demo" in app.screen.detail
        assert "cannot be undone" in app.screen.detail
        app.screen.dismiss(True)
        await completed(5)

        app.ui.selected_reference = "m-demo"
        move = CommandInvocation.from_mapping("mail.move", {"folder": "Archive"})
        app._palette_handlers()["mail.move"](move, app._palette_context())
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Archive" in app.screen.detail and "m-demo" in app.screen.detail
        app.screen.dismiss(True)
        await completed(6)

        app.ui.selected_reference = "m-demo"
        restore = CommandInvocation("mail.restore")
        app._palette_handlers()["mail.restore"](restore, app._palette_context())
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "inbox" in app.screen.detail and "m-demo" in app.screen.detail
        app.screen.dismiss(True)
        await completed(7)

    assert gateway.mutations == [
        ("importance", "m-demo", Importance.HIGH),
        ("categorize", "m-demo", ("Course",), "add"),
        ("categorize", "m-demo", ("Course",), "remove"),
        ("copy", "m-demo", "Archive"),
        ("delete", "m-demo", True),
        ("move", "m-demo", "Archive"),
        ("move", "m-demo", "inbox"),
    ]


@pytest.mark.asyncio
async def test_mail_folder_navigation_opens_adjacent_and_returns_to_real_history(
    tmp_path: Path,
) -> None:
    class NavigationGateway(FakeGateway):
        def __init__(self) -> None:
            self.loaded_folders: list[str] = []

        def list_folders(
            self,
            parent_reference: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[FolderInfo, ...]:
            if parent_reference is not None or offset:
                return ()
            return (
                FolderInfo("inbox", "Inbox", "Inbox", None, distinguished_role="inbox"),
                FolderInfo("sent", "Sent", "Sent", None, distinguished_role="sent"),
                FolderInfo("archive", "Archive", "Archive", None),
            )[:limit]

        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ):  # type: ignore[no-untyped-def]
            self.loaded_folders.append(folder)
            return [
                replace(
                    super().list_messages(folder="inbox", limit=limit)[0],
                    reference=f"message-{folder}",
                )
            ]

    gateway = NavigationGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            if gateway.loaded_folders:
                break
            await pilot.pause(0.05)

        open_sent = CommandInvocation("mail.open_sent")
        app._palette_handlers()["mail.open_sent"](open_sent, app._palette_context())
        for _ in range(30):
            if app.ui.folder == "sent":
                break
            await pilot.pause(0.05)
        assert app.ui.folder == "sent"

        open_archive = CommandInvocation("mail.open_archive")
        app._palette_handlers()["mail.open_archive"](open_archive, app._palette_context())
        for _ in range(30):
            if app.ui.folder == "archive" and not app.ui.busy:
                break
            await pilot.pause(0.05)
        assert app.ui.folder == "archive"
        assert not app.ui.busy

        app._catalog_application_action(catalog_route("mail.return_to_last_location"))
        for _ in range(30):
            if app.ui.folder == "sent":
                break
            await pilot.pause(0.05)
        assert app.ui.folder == "sent"
        assert app._folder_location_history == ["inbox", "sent"]

        tree = app.query_one("#folder-tree")
        tree.root.expand()
        for _ in range(30):
            if "sent" in app._folder_nodes and "archive" in app._folder_nodes:
                break
            await pilot.pause(0.05)
        tree.select_node(tree.get_node_by_id(app._folder_nodes["sent"]))
        await pilot.pause()
        app._catalog_direct(
            catalog_route("mail.open_next_folder"),
            CommandInvocation("mail.open_next_folder"),
        )
        for _ in range(30):
            if app.ui.folder != "sent" and gateway.loaded_folders[-1] == app.ui.folder:
                break
            await pilot.pause(0.05)
        assert app.ui.folder in {"inbox", "archive"}
        assert gateway.loaded_folders[-1] == app.ui.folder


@pytest.mark.asyncio
async def test_mail_reader_detail_source_and_copy_routes_honor_their_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            if app.ui.selected_reference == "m-demo":
                break
            await pilot.pause(0.05)

        full = catalog_route("mail.open_selected_message_in_reader")
        app._catalog_direct(full, CommandInvocation(full.command_id))
        for _ in range(30):
            if isinstance(app.screen, ReaderScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ReaderScreen)
        assert "Hello" in str(app.screen.query_one("#reader-screen-content", Static).content)
        app.screen.dismiss(None)
        await pilot.pause()

        details = catalog_route("mail.open_message_details")
        app._catalog_direct(details, CommandInvocation(details.command_id))
        for _ in range(30):
            if isinstance(app.screen, ReaderScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ReaderScreen)
        rendered = str(app.screen.query_one("#reader-screen-content", Static).content)
        assert "Local reference: m-demo" in rendered
        assert "Sensitivity: normal" in rendered
        assert "Hello" not in rendered
        app.screen.dismiss(None)
        await pilot.pause()

        raw_mime = catalog_route("mail.open_raw_mime")
        app._catalog_direct(raw_mime, CommandInvocation(raw_mime.command_id))
        await pilot.pause()
        assert isinstance(app.screen, AreaScreen)
        assert app.screen.area_title == "Raw MIME"
        app.screen.dismiss(None)
        await pilot.pause()

        app._message_detail = gateway.show_message("m-demo")
        app._message_detail_reference = "m-demo"
        for command_id in (
            "mail.copy_sender_address",
            "mail.copy_subject",
            "mail.copy_selected_message_as_text",
        ):
            route = catalog_route(command_id)
            app._catalog_direct(route, CommandInvocation(route.command_id))
        assert copied[0] == "teacher@example.test"
        assert copied[1] == "Synthetic welcome"
        assert "Hello" in copied[2]


@pytest.mark.asyncio
async def test_every_mail_palette_command_is_executable_and_core_handlers_mutate(
    tmp_path: Path,
) -> None:
    class CoreMailGateway(FakeGateway):
        def __init__(self) -> None:
            self.read_changes: list[tuple[str, bool]] = []
            self.flag_changes: list[tuple[str, bool]] = []
            self.searches: list[SearchQuery] = []

        def list_messages(
            self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
        ):  # type: ignore[no-untyped-def]
            return [
                replace(
                    super().list_messages(folder="inbox", limit=limit)[0],
                    reference=f"message-{folder}",
                )
            ]

        def search_messages(
            self,
            query: SearchQuery,
            limit: int = 50,
            offset: int = 0,
        ) -> list[MessageSummary]:
            self.searches.append(query)
            return self.list_messages(folder="inbox", limit=limit)

        def mark_read(self, reference: str, read: bool = True) -> str:
            self.read_changes.append((reference, read))
            return reference

        def flag(self, reference: str, flagged: bool = True) -> str:
            self.flag_changes.append((reference, flagged))
            return reference

    gateway = CoreMailGateway()
    app = GUCmailApp(context_for(tmp_path), gateway=gateway)
    handlers = app._palette_handlers()
    mail_ids = {
        spec.command_id for spec in build_core_command_registry().all() if spec.category == "mail"
    }
    assert mail_ids <= set(handlers) | set(CATALOG_ROUTES)

    async with app.run_test(size=(120, 35)) as pilot:
        for command_id, folder in (
            ("mail.open_inbox", "inbox"),
            ("mail.open_sent", "sent"),
            ("mail.open_drafts", "drafts"),
            ("mail.open_deleted", "trash"),
            ("mail.open_junk", "junk"),
            ("mail.open_archive", "archive"),
        ):
            handlers[command_id](CommandInvocation(command_id), app._palette_context())
            for _ in range(30):
                if app.ui.folder == folder and not app.ui.busy:
                    break
                await pilot.pause(0.05)
            assert app.ui.folder == folder

        handlers["mail.open_unread"](CommandInvocation("mail.open_unread"), app._palette_context())
        for _ in range(30):
            if gateway.searches and gateway.searches[-1].is_read is False:
                break
            await pilot.pause(0.05)
        assert gateway.searches[-1].is_read is False

        handlers["mail.open_flagged"](
            CommandInvocation("mail.open_flagged"), app._palette_context()
        )
        for _ in range(30):
            if gateway.searches and gateway.searches[-1].flagged is True:
                break
            await pilot.pause(0.05)
        assert gateway.searches[-1].flagged is True

        app.ui.selected_reference = "message-inbox"
        for command_id in ("mail.mark_read", "mail.mark_unread", "mail.flag", "mail.unflag"):
            expected = len(gateway.read_changes) + len(gateway.flag_changes) + 1
            handlers[command_id](CommandInvocation(command_id), app._palette_context())
            for _ in range(30):
                if (
                    len(gateway.read_changes) + len(gateway.flag_changes) >= expected
                    and not app_write_coordinator(app).pending
                ):
                    break
                await pilot.pause(0.05)
            app.ui.selected_reference = "message-inbox"

    assert gateway.read_changes == [("message-inbox", True), ("message-inbox", False)]
    assert gateway.flag_changes == [("message-inbox", True), ("message-inbox", False)]


@pytest.mark.asyncio
async def test_compose_prefill_routes_use_contact_sender_and_selected_recipients(
    tmp_path: Path,
) -> None:
    class RecipientGateway(FakeGateway):
        def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail:
            assert not mark_read
            detail = super().show_message(reference)
            return replace(
                detail,
                to=(Address("demo@example.test"), Address("peer@example.test", "Peer")),
                cc=(Address("PEER@example.test"), Address("other@example.test")),
            )

    app = GUCmailApp(context_for(tmp_path), gateway=RecipientGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            if app.ui.selected_reference == "m-demo":
                break
            await pilot.pause(0.05)

        sender = catalog_route("compose.compose_to_selected_sender")
        app._catalog_application_action(sender, CommandInvocation(sender.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-to", Input).value == "teacher@example.test"
        app.screen.dismiss(None)
        await pilot.pause()

        contact = catalog_route("compose.compose_to_contact")
        app._catalog_application_action(
            contact,
            CommandInvocation.from_mapping(contact.command_id, {"person": "contact@example.test"}),
        )
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-to", Input).value == "contact@example.test"
        app.screen.dismiss(None)
        await pilot.pause()

        recipients = catalog_route("compose.compose_to_selected_recipients")
        app._catalog_application_action(recipients, CommandInvocation(recipients.command_id))
        for _ in range(30):
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-to", Input).value == (
            "peer@example.test, other@example.test"
        )


@pytest.mark.asyncio
async def test_compose_current_draft_save_modes_duplicate_discard_and_handler_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    compose_ids = {
        spec.command_id
        for spec in build_core_command_registry().all()
        if spec.category == "compose"
    }
    assert compose_ids <= set(app._palette_handlers()) | set(CATALOG_ROUTES)
    captured: list[dict[str, object]] = []

    def capture_submission(payload, *, composer):
        # Routing-only double: model an acknowledgment after proving the editor
        # is still mounted/pending. Real persistence/rejection has separate SQL
        # campaigns; capturing a payload alone is not a successful draft Save.
        assert isinstance(composer, ComposeScreen) and composer._submission_pending
        captured.append(payload)
        composer.submission_finished("saved", "Synthetic route acknowledgment")

    monkeypatch.setattr(app, "submit_composer", capture_submission)

    async with app.run_test(size=(120, 35)) as pilot:
        original = DraftData(reference="dft-current", subject="Current subject", body_text="Body")
        app._open_compose(original)
        await pilot.pause()
        composer = app.screen
        assert isinstance(composer, ComposeScreen)
        await _ready_composer_for_editing(pilot)

        edit = catalog_route("compose.edit_current_draft")
        app._catalog_open(edit, CommandInvocation(edit.command_id))
        assert app.screen is composer
        assert "Continuing the current draft" in str(
            composer.query_one("#compose-note", Static).content
        )

        duplicate = catalog_route("compose.duplicate_current_draft")
        app._catalog_open(duplicate, CommandInvocation(duplicate.command_id))
        for _ in range(30):
            if (
                isinstance(app.screen, ComposeScreen)
                and app.screen is not composer
                and app.screen.is_mounted
            ):
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        assert app.screen.draft.reference != "dft-current"
        assert app.screen.query_one("#compose-subject", Input).value == "Current subject"
        app.screen.dismiss(None)
        await pilot.pause()

        for command_id, mode in (
            ("compose.save_local_draft", "local_only"),
            ("compose.save_server_draft", "server_sync"),
            ("compose.save_draft", "local_then_server"),
        ):
            app._open_compose(DraftData(reference=f"dft-{mode}", subject=mode))
            await pilot.pause()
            await _ready_composer_for_editing(pilot)
            route = catalog_route(command_id)
            app._catalog_direct(route, CommandInvocation(route.command_id))
            for _ in range(30):
                if captured and captured[-1].get("save_mode") == mode:
                    break
                await pilot.pause(0.05)
            assert captured[-1]["save_mode"] == mode

        service = DraftService(
            app.context.state,
            app.context.paths.recovery_dir,
            maximum_attachment_bytes=app.context.config.max_attachment_bytes,
        )
        doomed = service.create(DraftData(reference="dft-discard", subject="Discard me"))
        service.write_recovery(doomed)
        app._open_compose(doomed)
        await pilot.pause()
        await _ready_composer_for_editing(pilot)
        discard = catalog_route("compose.discard_draft")
        app._catalog_direct(discard, CommandInvocation(discard.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "dft-discard" in app.screen.detail
        app.screen.dismiss(True)
        for _ in range(30):
            if not service.exists("dft-discard"):
                break
            await pilot.pause(0.05)
        assert not service.exists("dft-discard")
        assert not service.recovery_path("dft-discard").exists()

        recovered = DraftData(reference="dft-recover", subject="Recovered through palette")
        service.write_recovery(recovered)
        invocation = CommandInvocation.from_mapping(
            "compose.recover_draft", {"reference": "dft-recover"}
        )
        app._palette_handlers()["compose.recover_draft"](invocation, app._palette_context())
        for _ in range(30):
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        assert app.screen.query_one("#compose-subject", Input).value == "Recovered through palette"


@pytest.mark.asyncio
async def test_palette_executes_bound_command_in_process_and_records_usage(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        original_ratio = app._message_ratio
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        app.screen.query_one("#palette-input", Input).value = "make preview wider"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert not isinstance(app.screen, PaletteScreen)
        assert app._message_ratio < original_ratio
        usage = app.context.state.list_command_usage(context="mail", limit=10)
        assert usage[0].command_id == "layout.preview_wider"


@pytest.mark.asyncio
async def test_folder_move_palette_route_uses_selected_exchange_folder_and_destination_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    prepared: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app,
        "_prepare_folder_move",
        lambda source, destination: prepared.append((source, destination)),
    )
    monkeypatch.setattr(
        app,
        "_selected_exchange_folder",
        lambda: ("folder-source", "/Inbox/Projects"),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        route = catalog_route("folders.move_folder")
        app._catalog_direct(
            route,
            CommandInvocation.from_mapping(
                route.command_id,
                {"folder": "id:folder-destination"},
            ),
        )
        assert prepared == [("folder-source", "id:folder-destination")]

        app._catalog_direct(route, CommandInvocation.from_mapping(route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, FolderMoveScreen)
        app.screen.query_one("#folder-move-destination", Input).value = "Archive"
        await pilot.press("enter")
        await pilot.pause()
        assert prepared[-1] == ("folder-source", "Archive")


@pytest.mark.asyncio
async def test_pim_cross_area_actions_open_composer_and_related_mail(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("contacts")
        for _ in range(20):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-table").row_count:
                break
        assert isinstance(app.screen, ContactsScreen)
        app.screen.action_compose()
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-to", Input).value == "contact@example.test"
        app.screen.dismiss(None)

        app._open_area("contacts")
        for _ in range(20):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-table").row_count:
                break
        assert isinstance(app.screen, ContactsScreen)
        app.screen.post_message(
            PimActionRequested(
                "contacts",
                "correspondence_messages",
                ("contact-demo",),
                payload={"addresses": ("contact@example.test",)},
            )
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if app.screen is app.screen_stack[0] and "Correspondence with" in str(
                app.query_one("#status-bar", Static).content
            ):
                break
        assert app.screen is app.screen_stack[0]
        assert app.query_one("#message-table").row_count == 1

        app._open_area("calendar")
        for _ in range(20):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#calendar-table").row_count:
                break
        assert isinstance(app.screen, CalendarScreen)
        app.screen.post_message(PimActionRequested("calendar", "related_messages", ("event-demo",)))
        # The preceding contact search already populated this table. Wait for
        # this calendar request's result before checking its unchanged assertions.
        for _ in range(100):
            await pilot.pause(0.05)
            if app.screen is app.screen_stack[0] and "Related mail for Synthetic lecture" in str(
                app.query_one("#status-bar", Static).content
            ):
                break
        assert app.screen is app.screen_stack[0]
        assert app.query_one("#message-table").row_count == 1
        assert "Related mail for Synthetic lecture" in str(
            app.query_one("#status-bar", Static).content
        )


@pytest.mark.asyncio
async def test_contact_palette_routes_execute_contextual_and_cross_area_actions(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.ui.selected_reference == "m-demo":
                break

        app._palette_contacts_search(
            CommandInvocation.from_mapping("contacts.search", {"query": "Synthetic"}),
            CommandContext(),
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-search", Input).value == "Synthetic":
                break
        assert isinstance(app.screen, ContactsScreen)
        assert app.screen.query_one("#contacts-search", Input).value == "Synthetic"
        app.pop_screen()
        await pilot.pause()

        directory_route = catalog_route("contacts.search_directory")
        app._catalog_screen_action(
            directory_route,
            CommandInvocation.from_mapping(
                directory_route.command_id, {"query": "directory@example.test"}
            ),
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                (isinstance(app.screen, ContactsScreen) and app.screen.is_mounted)
                and "lookup completed"
                in str(app.screen.query_one("#area-state", Static).content).casefold()
                and "Directory Result"
                in str(app.screen.query_one("#contacts-detail", Static).content)
            ):
                break
        assert isinstance(app.screen, ContactsScreen)
        assert app.screen.query_one("#contacts-search", Input).value == "directory@example.test"
        assert "Directory Result" in str(app.screen.query_one("#contacts-detail", Static).content)
        app.pop_screen()
        await pilot.pause()

        search_route = catalog_route("contacts.open_sender_contact")
        app._catalog_open(search_route, CommandInvocation(search_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-search", Input).value:
                break
        assert isinstance(app.screen, ContactsScreen)
        assert app.screen.query_one("#contacts-search", Input).value == "teacher@example.test"
        app.pop_screen()
        await pilot.pause()

        add_route = catalog_route("contacts.add_sender_to_contacts")
        app._catalog_open(add_route, CommandInvocation(add_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ContactEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ContactEditorScreen)
        assert app.screen.query_one("#contact-edit-name", Input).value == "Course Team"
        assert app.screen.query_one("#contact-edit-emails", Input).value == "teacher@example.test"
        app.screen.dismiss(None)
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()

        history_route = catalog_route("contacts.show_recent_recipients")
        app._catalog_open(history_route, CommandInvocation(history_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CorrespondentsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#correspondents-table").row_count:
                break
        assert isinstance(app.screen, CorrespondentsScreen)
        assert app.screen.order == "recent"
        assert app.screen.query_one("#correspondents-table").row_count == 1
        app.pop_screen()
        await pilot.pause()

        app._open_area("contacts")
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-table").row_count:
                break
        assert isinstance(app.screen, ContactsScreen)
        meeting_route = catalog_route("contacts.schedule_meeting_with_contact")
        app._catalog_screen_action(meeting_route, CommandInvocation(meeting_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        assert (
            app.screen.query_one("#calendar-edit-required", Input).value == "contact@example.test"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_delay", [0, 0.2])
async def test_calendar_palette_executes_availability_proposal_and_message_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookup_delay: float,
) -> None:
    original = FakeGateway.get_availability

    def delayed_lookup(self, *args, **kwargs):
        Event().wait(lookup_delay)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeGateway, "get_availability", delayed_lookup)
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.ui.selected_reference == "m-demo":
                break

        create_route = catalog_route("calendar.create_event_from_selected_email")
        app._catalog_open(create_route, CommandInvocation(create_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        assert (
            app.screen.query_one("#calendar-edit-subject", Input).value
            == "Follow up: Synthetic welcome"
        )
        assert (
            app.screen.query_one("#calendar-edit-required", Input).value == "teacher@example.test"
        )
        app.screen.dismiss(None)
        await pilot.pause()
        assert isinstance(app.screen, CalendarScreen)

        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.query_one("#calendar-table").row_count:
                break

        availability_route = catalog_route("calendar.check_availability")
        app._catalog_screen_action(
            availability_route,
            CommandInvocation(availability_route.command_id),
        )
        await pilot.pause()
        assert isinstance(app.screen, CalendarAvailabilityScreen)
        assert (
            app.screen.query_one("#availability-attendees", Input).value == "student@example.test"
        )
        app.screen.query_one("#availability-check", Button).press()
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, CalendarScreen) and app.screen.is_mounted) and str(
                app.screen.query_one("#calendar-detail", Static).content
            ).startswith("Availability · "):
                break
        assert isinstance(app.screen, CalendarScreen)
        assert "student@example.test" in str(
            app.screen.query_one("#calendar-detail", Static).content
        )

        free_route = catalog_route("calendar.find_free_time")
        app._catalog_screen_action(free_route, CommandInvocation(free_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, CalendarAvailabilityScreen)
        app.screen.query_one("#availability-check", Button).press()
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and "First common free slot" in str(
                app.screen.query_one("#calendar-detail", Static).content
            ):
                break
        assert "First common free slot" in str(
            app.screen.query_one("#calendar-detail", Static).content
        )

        proposal_route = catalog_route("calendar.propose_new_time")
        app._catalog_screen_action(
            proposal_route,
            CommandInvocation(proposal_route.command_id),
        )
        await pilot.pause()
        assert isinstance(app.screen, MeetingResponseScreen)
        assert app.screen.query_one("#meeting-response-proposed-start", Input).value
        app.screen.query_one("#meeting-response-send", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_kind", ["failed", "missing", "wrong_attendee", "short", "outside_window"]
)
async def test_calendar_free_time_never_confirms_incomplete_or_out_of_window_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
) -> None:
    def availability(self, attendees, *, start, end, interval_minutes):
        slots = int((end - start).total_seconds() // (interval_minutes * 60))
        if response_kind == "missing":
            return ()
        return (
            {
                "attendee": Address("unrequested@example.test")
                if response_kind == "wrong_attendee"
                else attendees[0],
                "available": response_kind != "failed",
                "error": "SyntheticDenied" if response_kind == "failed" else None,
                "merged": "0" * 2
                if response_kind == "short"
                else ("2" * slots + "00" if response_kind == "outside_window" else "0" * slots),
                "events": (),
            },
        )

    monkeypatch.setattr(FakeGateway, "get_availability", availability)
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("calendar")
        for _ in range(60):
            await pilot.pause(0.025)
            if isinstance(app.screen, CalendarScreen) and app.screen._highlighted:
                break
        screen = app.screen
        assert isinstance(screen, CalendarScreen)
        screen.action_find_free_time()
        await pilot.pause()
        assert isinstance(app.screen, CalendarAvailabilityScreen)
        app.screen.query_one("#availability-check", Button).press()
        for _ in range(80):
            await pilot.pause(0.025)
            if str(screen.query_one("#calendar-detail", Static).content).startswith(
                "Availability · "
            ):
                break
        detail = str(screen.query_one("#calendar-detail", Static).content)
        assert detail.startswith("Availability · ")
        assert "First common free slot" not in detail
        assert "could not be confirmed" in detail


@pytest.mark.asyncio
async def test_calendar_palette_views_apply_visible_anchor_and_local_date(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        today = datetime.now().astimezone().date()
        cases = (
            ("calendar.open_agenda", "agenda", None),
            ("calendar.open_today", "day", today),
            ("calendar.open_tomorrow", "day", today + timedelta(days=1)),
            ("calendar.open_this_week", "week", today),
            ("calendar.open_next_week", "week", today + timedelta(days=7)),
            ("calendar.open_month", "month", None),
        )
        for command_id, expected_view, expected_anchor in cases:
            route = catalog_route(command_id)
            invocation = CommandInvocation(route.command_id)
            if route.action is None:
                app._catalog_open(route, invocation)
            else:
                app._catalog_screen_action(route, invocation)
            for _ in range(50):
                await pilot.pause(0.05)
                if (isinstance(app.screen, CalendarScreen) and app.screen.is_mounted) and (
                    app.screen.query_one("#calendar-view", Select).value == expected_view
                    and (expected_anchor is None or app.screen.anchor_date == expected_anchor)
                ):
                    break
            assert isinstance(app.screen, CalendarScreen)
            assert app.screen.query_one("#calendar-view", Select).value == expected_view
            if expected_anchor is not None:
                assert app.screen.anchor_date == expected_anchor
            app.pop_screen()
            await pilot.pause()

        route = catalog_route("calendar.go_to_date")
        app._catalog_open(
            route,
            CommandInvocation.from_mapping(
                route.command_id,
                {"when": datetime(2026, 8, 30, 23, 30, tzinfo=UTC)},
            ),
        )
        for _ in range(50):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and app.screen.anchor_date == datetime(2026, 8, 31).date():
                break
        assert isinstance(app.screen, CalendarScreen)
        assert app.screen.anchor_date == datetime(2026, 8, 31).date()


@pytest.mark.asyncio
async def test_calendar_palette_queues_selection_actions_and_prefills_attendees(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    class SlowCalendarGateway(FakeGateway):
        def list_events(self, **kwargs: object) -> list[CalendarEvent]:
            started.set()
            assert release.wait(10)
            return super().list_events(**kwargs)  # type: ignore[arg-type]

    app = GUCmailApp(context_for(tmp_path), gateway=SlowCalendarGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        route = catalog_route("calendar.add_attendee")
        app._catalog_screen_action(
            route,
            CommandInvocation.from_mapping(
                route.command_id,
                {"person": "guest@example.test"},
            ),
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                started.is_set()
                and (isinstance(app.screen, CalendarScreen) and app.screen.is_mounted)
                and app.screen._pending_action is not None
            ):
                break
        assert isinstance(app.screen, CalendarScreen)
        assert app.screen._pending_action is not None
        release.set()
        for _ in range(60):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarEditorScreen)
                and app.screen.is_mounted
                and (
                    app.screen.query_one("#calendar-edit-required", Input).value
                    == "student@example.test, guest@example.test"
                )
                and app.screen.query_one("#calendar-edit-required", Input).has_focus
            ):
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        assert app.screen.query_one("#calendar-edit-required", Input).value == (
            "student@example.test, guest@example.test"
        )
        assert app.screen.query_one("#calendar-edit-required", Input).has_focus
        app.screen.dismiss(None)
        await pilot.pause()

        assert isinstance(app.screen, CalendarScreen)
        remove = catalog_route("calendar.remove_attendee")
        app._catalog_screen_action(
            remove,
            CommandInvocation.from_mapping(
                remove.command_id,
                {"person": "student@example.test"},
            ),
        )
        for _ in range(50):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarEditorScreen)
                and app.screen.is_mounted
                and (app.screen.query_one("#calendar-edit-required", Input).value == "")
            ):
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        assert app.screen.query_one("#calendar-edit-required", Input).value == ""
        app.screen.dismiss(None)
        await pilot.pause()

        assert isinstance(app.screen, CalendarScreen)
        cancel = catalog_route("calendar.cancel_meeting")
        app._catalog_screen_action(cancel, CommandInvocation(cancel.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        assert "cancellation notices" in str(
            app.screen.query_one("#operational-confirm-detail", Static).content
        )
        assert app.screen.query_one("#operational-confirm-yes", Button).label.plain == (
            "Cancel meeting"
        )
        app.screen.dismiss(False)


@pytest.mark.asyncio
async def test_calendar_palette_executes_crud_response_editor_and_interchange_routes(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("calendar")
        for _ in range(50):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#calendar-table", DataTable).row_count == 1:
                break
        assert isinstance(app.screen, CalendarScreen)

        create = catalog_route("calendar.create_event")
        app._catalog_screen_action(create, CommandInvocation(create.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        assert "Create event" in str(app.screen.query_one("#calendar-editor-title").render())
        app.screen.dismiss(None)
        await pilot.pause()

        for command_id, selector in (
            ("calendar.edit_event", None),
            ("calendar.add_reminder", "#calendar-edit-reminder"),
            ("calendar.change_reminder", "#calendar-edit-reminder"),
            ("calendar.make_recurring", "#calendar-edit-repeat"),
        ):
            route = catalog_route(command_id)
            app._catalog_screen_action(route, CommandInvocation(route.command_id))
            for _ in range(40):
                await pilot.pause(0.05)
                if (isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted) and (
                    selector is None or app.screen.query_one(selector).has_focus
                ):
                    break
            assert isinstance(app.screen, CalendarEditorScreen)
            if selector is not None:
                assert app.screen.query_one(selector).has_focus
            app.screen.dismiss(None)
            await pilot.pause()

        for command_id, response in (
            ("calendar.accept_invitation", "accept"),
            ("calendar.tentatively_accept", "tentative"),
            ("calendar.decline_invitation", "decline"),
        ):
            route = catalog_route(command_id)
            app._catalog_screen_action(route, CommandInvocation(route.command_id))
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, MeetingResponseScreen) and app.screen.is_mounted:
                    break
            assert isinstance(app.screen, MeetingResponseScreen)
            assert app.screen.query_one("#meeting-response-value", Select).value == response
            app.screen.dismiss(None)
            await pilot.pause()

        conflict = catalog_route("calendar.show_conflicts")
        app._catalog_screen_action(conflict, CommandInvocation(conflict.command_id))
        await pilot.pause()
        assert isinstance(app.screen, CalendarScreen)
        assert "No conflicts" in str(app.screen.query_one("#calendar-detail", Static).content)

        delete = catalog_route("calendar.delete_event")
        app._catalog_screen_action(delete, CommandInvocation(delete.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        assert "Only this bounded selection" in str(
            app.screen.query_one("#operational-confirm-detail", Static).content
        )
        app.screen.dismiss(False)
        await pilot.pause()

        export = catalog_route("calendar.export_event")
        app._catalog_screen_action(export, CommandInvocation(export.command_id))
        await pilot.pause()
        assert isinstance(app.screen, FilePathScreen)
        assert "Export selected events" in str(app.screen.query_one("#pim-file-title").render())
        app.screen.dismiss(None)
        await pilot.pause()

        import_route = catalog_route("calendar.import_ics")
        app._catalog_screen_action(import_route, CommandInvocation(import_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, FilePathScreen)
        assert "Import calendar events" in str(app.screen.query_one("#pim-file-title").render())
        app.screen.dismiss(None)


@pytest.mark.asyncio
async def test_task_palette_creates_from_message_and_edits_reminder(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.ui.selected_reference == "m-demo":
                break

        create_route = catalog_route("tasks.create_task_from_message")
        app._catalog_open(create_route, CommandInvocation(create_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, TaskEditorScreen)
        assert "Create task" in str(app.screen.query_one("#task-editor-title", Static).content)
        assert (
            app.screen.query_one("#task-edit-subject", Input).value
            == "Follow up: Synthetic welcome"
        )
        assert app.screen.query_one("#task-editor-body", TextArea).text == "Source message: m-demo"
        await pilot.pause()
        app.screen.query_one("#task-edit-reminder", Input).value = "2026-08-24T09:00:00+02:00"
        assert app.screen._value().reminder_at is not None
        assert app.screen._value().reminder_at.isoformat() == "2026-08-24T09:00:00+02:00"
        app.screen.dismiss(None)
        await pilot.pause()

        assert isinstance(app.screen, TasksScreen)
        app.screen.action_refresh()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.query_one("#tasks-table").row_count:
                break
        assert app.screen.query_one("#tasks-table").row_count == 1
        reminder_route = catalog_route("tasks.add_reminder")
        app._catalog_screen_action(reminder_route, CommandInvocation(reminder_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                (isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted)
                and app.focused is not None
                and app.focused.id == "task-edit-reminder"
            ):
                break
        assert isinstance(app.screen, TaskEditorScreen)
        assert app.focused is not None
        assert app.focused.id == "task-edit-reminder"


@pytest.mark.asyncio
async def test_base_task_palette_create_search_and_complete_execute(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        app._palette_task_create(CommandInvocation("tasks.create"), CommandContext())
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, TaskEditorScreen)
        app.screen.dismiss(None)

        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, TasksScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, TasksScreen)

        search_route = catalog_route("tasks.search_tasks")
        app._catalog_search(
            search_route,
            CommandInvocation.from_mapping(
                search_route.command_id,
                {"query": "Synthetic follow-up"},
            ),
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.query_one("#tasks-search", Input).value == "Synthetic follow-up":
                break
        assert app.screen.query_one("#tasks-search", Input).value == "Synthetic follow-up"

        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.query_one("#tasks-table").row_count:
                break
        assert app.screen.query_one("#tasks-table").row_count == 1
        app._palette_task_complete(CommandInvocation("tasks.complete"), CommandContext())
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.__class__.__name__ == "ConfirmationScreen":
                break
        assert app.screen.__class__.__name__ == "ConfirmationScreen"


@pytest.mark.asyncio
async def test_draft_attachment_workspace_executes_palette_handoffs_and_safe_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    ).create(
        DraftData(
            reference="dft-workspace",
            to=[Address("recipient@example.test")],
            subject="Workspace draft",
            body_text="Synthetic body",
        )
    )
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        route = catalog_route("drafts.show_unsynchronized_drafts")
        app._catalog_open(route, CommandInvocation(route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#draft-table").row_count:
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#draft-location", Select).value == "local"
        assert app.screen.query_one("#draft-table").row_count == 1
        for _ in range(40):
            if not app.screen._operation_active:
                break
            await pilot.pause(0.05)
        assert not app.screen._operation_active

        app.screen.query_one("#attachment-destination", Input).value = str(tmp_path)
        export_draft_route = catalog_route("drafts.export_draft")
        app._catalog_screen_action(
            export_draft_route,
            CommandInvocation(export_draft_route.command_id),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                tmp_path / "dft-workspace.draft.json"
            ).exists() and not app.screen._operation_active:
                break
        assert (tmp_path / "dft-workspace.draft.json").is_file()

        delete_route = catalog_route("drafts.delete_draft")
        app._catalog_screen_action(delete_route, CommandInvocation(delete_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.__class__.__name__ == "ConfirmationScreen":
                break
        assert app.screen.__class__.__name__ == "ConfirmationScreen"
        app.screen.dismiss(False)
        await pilot.pause()
        assert isinstance(app.screen, DraftAttachmentScreen)

        send_route = catalog_route("drafts.send_draft")
        app._catalog_screen_action(send_route, CommandInvocation(send_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ConfirmationScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        app.screen.dismiss(None)
        await pilot.pause()

        app._palette_open_attachments(
            CommandInvocation.from_mapping("attachments.open", {"reference": "m-demo"}),
            CommandContext(),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted) and (
                app.screen.query_one("#attachment-table").row_count
                and not app.screen._operation_active
            ):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#attachment-table").row_count == 1
        app.screen.query_one("#attachment-destination", Input).value = str(tmp_path)

        for command_id in (
            "attachments.list_attachments",
            "attachments.open_attachments_view",
        ):
            metadata_route = catalog_route(command_id)
            app._catalog_direct(
                metadata_route,
                CommandInvocation.from_mapping(command_id, {"reference": "m-demo"}),
            )
            for _ in range(30):
                await pilot.pause(0.05)
                if (
                    app.screen.query_one("#attachment-table").row_count == 1
                    and not app.screen._operation_active
                ):
                    break
            assert app.screen.query_one("#attachment-table").row_count == 1

        hash_route = catalog_route("attachments.hash_attachment")
        app._catalog_screen_action(hash_route, CommandInvocation(hash_route.command_id))
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if (tmp_path / "notes.txt").exists() and "SHA-256" in str(
                app.screen.query_one("#attachment-detail", Static).content
            ):
                break
            await pilot.pause(0.05)
        assert (tmp_path / "notes.txt").exists(), (
            str(app.screen.query_one("#area-state", Static).content),
            str(app.screen.query_one("#attachment-detail", Static).content),
        )
        assert (tmp_path / "notes.txt").read_bytes() == b"synthetic attachment"
        assert "SHA-256" in str(app.screen.query_one("#attachment-detail", Static).content)

        export_route = catalog_route("attachments.export_attachment_list")
        app._catalog_screen_action(export_route, CommandInvocation(export_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (tmp_path / "m-demo.attachments.json").exists():
                break
        assert (tmp_path / "m-demo.attachments.json").is_file()

        save_as_route = catalog_route("attachments.save_attachment_as")
        app._catalog_screen_action(save_as_route, CommandInvocation(save_as_route.command_id))
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "attachment-destination"

        opened: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app,
            "load_message",
            lambda reference, mode="auto": opened.append((reference, mode)),
        )
        app.ui.selected_reference = "different-message"
        containing_route = catalog_route("attachments.open_containing_message")
        app._catalog_direct(
            containing_route,
            CommandInvocation(containing_route.command_id),
        )
        assert opened == [("m-demo", "auto")]

        existing_downloads = set(tmp_path.glob("notes*.txt"))
        app._palette_handlers()["attachments.download"](
            CommandInvocation.from_mapping(
                "attachments.download",
                {"reference": "m-demo"},
            ),
            app._palette_context(),
        )
        for _ in range(60):
            await pilot.pause(0.05)
            new_downloads = set(tmp_path.glob("notes*.txt")) - existing_downloads
            if new_downloads:
                break
        assert len(new_downloads) == 1
        assert new_downloads.pop().read_bytes() == b"synthetic attachment"


@pytest.mark.asyncio
async def test_draft_palette_uses_local_search_age_and_durable_duplicate(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    service = DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )
    old = service.create(
        DraftData(reference="draft-old", subject="Old thesis draft", body_text="archive")
    )
    service.create(
        DraftData(reference="draft-current", subject="Current project draft", body_text="active")
    )
    old.updated_at = datetime.now(UTC) - timedelta(days=45)
    with context.state.connection() as connection:
        connection.execute(
            "UPDATE local_drafts SET draft_json=?, updated_at=? WHERE reference=?",
            (
                json.dumps(draft_to_dict(old), ensure_ascii=False, sort_keys=True),
                old.updated_at.isoformat(),
                old.reference,
            ),
        )

    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app._palette_handlers()["drafts.open_drafts"](
            CommandInvocation("drafts.open_drafts"),
            app._palette_context(),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted) and (
                app.screen.query_one("#draft-table").row_count == 2
            ):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#draft-table").row_count == 2
        assert app.screen.query_one("#draft-age", Select).value == ""
        app.pop_screen()
        await pilot.pause()

        search_route = catalog_route("drafts.search_drafts")
        app._catalog_open(
            search_route,
            CommandInvocation.from_mapping(search_route.command_id, {"query": "Current"}),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted) and (
                app.screen.query_one("#draft-filter", Input).value == "Current"
                and app.screen.query_one("#draft-table").row_count == 1
            ):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#draft-table").row_count == 1
        assert "Current project" in str(app.screen.query_one("#draft-detail", Static).content)
        app.pop_screen()
        await pilot.pause()

        old_route = catalog_route("drafts.show_old_drafts")
        app._catalog_open(old_route, CommandInvocation(old_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted) and (
                app.screen.query_one("#draft-age", Select).value == "30"
                and app.screen.query_one("#draft-table").row_count == 1
            ):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#draft-table").row_count == 1
        assert "Old thesis" in str(app.screen.query_one("#draft-detail", Static).content)
        app.pop_screen()
        await pilot.pause()

        edit_route = catalog_route("drafts.edit_draft")
        app._catalog_open(
            edit_route,
            CommandInvocation.from_mapping(edit_route.command_id, {"reference": "draft-current"}),
        )
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-subject", Input).value == "Current project draft"
        app.screen.dismiss(None)
        await pilot.pause()

        duplicate_route = catalog_route("drafts.duplicate_draft")
        app._catalog_open(
            duplicate_route,
            CommandInvocation.from_mapping(
                duplicate_route.command_id,
                {"reference": "draft-current"},
            ),
        )
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-subject", Input).value == "Current project draft"
        assert len(service.list()) == 3
        app.screen.dismiss(None)
        await pilot.pause()

        new_route = catalog_route("drafts.new_draft")
        app._catalog_application_action(new_route, CommandInvocation(new_route.command_id))
        for _ in range(150):
            await pilot.pause(0.02)
            if (isinstance(app.screen, ComposeScreen) and app.screen._status_checked
                    and not app.screen._attempt_pending):
                break
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen._status_checked and not app.screen._attempt_pending
        app.screen.query_one("#compose-subject", Input).value = "Created by draft command"
        app._palette_handlers()["draft.save"](
            CommandInvocation("draft.save"),
            app._palette_context(),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if len(service.list()) == 4 and not isinstance(app.screen, ComposeScreen):
                break
        assert len(service.list()) == 4

        service.write_recovery(
            DraftData(reference="draft-recovered", subject="Recovered generated route")
        )
        recover_route = catalog_route("drafts.recover_autosaved_draft")
        app._catalog_open(recover_route, CommandInvocation(recover_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-subject", Input).value == "Recovered generated route"
        app.screen.dismiss(None)
        await pilot.pause()

        app._catalog_open(edit_route, CommandInvocation(edit_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted
            ) and "Select a draft" in str(app.screen.query_one("#area-state", Static).content):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert "Select a draft" in str(app.screen.query_one("#area-state", Static).content)


def test_every_draft_palette_command_has_an_executable_and_honest_policy(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    handlers = app._palette_handlers()
    commands = tuple(
        command for command in build_core_command_registry().all() if command.category == "drafts"
    )

    assert len(commands) == 17
    assert all(command.handler_id in handlers for command in commands)
    connection_required = {"drafts.send_draft", "drafts.synchronize_drafts"}
    assert all(
        command.requires_connection and not command.offline
        for command in commands
        if command.command_id in connection_required
    )
    assert all(
        command.offline and not command.requires_connection
        for command in commands
        if command.command_id not in connection_required
    )


@pytest.mark.asyncio
async def test_attachment_storage_palette_runs_exact_analyses_on_synthetic_data(
    tmp_path: Path,
) -> None:
    digest = "a" * 64
    messages = (
        {
            "reference": "message-one",
            "subject": "First attachment owner",
            "folder_path": "inbox",
            "attachments": ({"name": "shared.pdf", "size": 2_048, "sha256": digest},),
        },
        {
            "reference": "message-two",
            "subject": "Second attachment owner",
            "folder_path": "archive",
            "attachments": ({"name": "copy.pdf", "size": 2_048, "sha256": digest},),
        },
    )
    known_attachments = tuple(
        {
            **attachment,
            "message_reference": message["reference"],
        }
        for message in messages
        for attachment in message["attachments"]
    )
    backend = StorageAreaBackend(
        quota_factory=None,
        message_loader=lambda _analysis, limit: messages[:limit],
        attachment_loader=lambda limit: known_attachments[:limit],
        local_paths={},
        export_directory=tmp_path,
        online=True,
        maximum_scan_items=20,
    )
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    screen = StorageScreen(backend, analysis_limit=20)

    async with app.run_test(size=(120, 35)) as pilot:
        app.push_screen(screen)
        for _ in range(30):
            if screen.is_mounted and screen._snapshot is not None:
                break
            await pilot.pause(0.05)
        assert screen._snapshot is not None

        app._palette_handlers()["storage.largest_attachments"](
            CommandInvocation("attachments.show_largest_attachments"),
            app._palette_context(),
        )
        for _ in range(40):
            if screen._analysis and screen._analysis.analysis_id == "largest-attachments":
                break
            await pilot.pause(0.05)
        assert screen._analysis is not None
        assert screen._analysis.analysis_id == "largest-attachments"
        assert [row.size_bytes for row in screen._analysis.rows] == [2_048, 2_048]

        duplicate_route = catalog_route("attachments.find_duplicate_attachments")
        app._catalog_screen_action(
            duplicate_route,
            CommandInvocation(duplicate_route.command_id),
        )
        for _ in range(40):
            if screen._analysis and screen._analysis.analysis_id == "duplicate-attachments":
                break
            await pilot.pause(0.05)
        assert screen._analysis is not None
        assert screen._analysis.analysis_id == "duplicate-attachments"
        assert len(screen._analysis.rows) == 1
        assert screen._analysis.rows[0].item_count == 2
        assert screen._analysis.rows[0].source == "trusted SHA-256 metadata"
        assert "not downloaded automatically" in screen._analysis.note

        statistics_route = catalog_route("attachments.show_attachment_storage_statistics")
        app._catalog_screen_action(
            statistics_route,
            CommandInvocation(statistics_route.command_id),
        )
        for _ in range(40):
            if screen._analysis and screen._analysis.analysis_id == "attachment-types":
                break
            await pilot.pause(0.05)
        assert screen._analysis is not None
        assert screen._analysis.analysis_id == "attachment-types"
        assert screen._analysis.rows[0].label == ".pdf"
        assert screen._analysis.rows[0].size_bytes == 4_096


@pytest.mark.asyncio
async def test_storage_palette_executes_each_analysis_focus_export_and_cleanup_plan(
    tmp_path: Path,
) -> None:
    class NoServerQuota:
        def get_quota(self) -> None:
            return None

    messages = (
        {
            "reference": "storage-one",
            "subject": "Large project package",
            "sender": "team@example.test",
            "folder_path": "Inbox/Projects",
            "received_at": "2026-08-20T09:00:00+00:00",
            "size_bytes": 8_000,
            "attachments": ({"name": "project.pdf", "size": 3_000},),
        },
        {
            "reference": "storage-two",
            "subject": "Second project package",
            "sender": "team@example.test",
            "folder_path": "Archive",
            "received_at": "2026-07-20T09:00:00+00:00",
            "size_bytes": 5_000,
            "attachments": ({"name": "notes.txt", "size": 1_000},),
        },
        {
            "reference": "storage-three",
            "subject": "Small independent note",
            "sender": "other@example.test",
            "folder_path": "Inbox",
            "received_at": "2026-06-20T09:00:00+00:00",
            "size_bytes": 500,
        },
    )
    cache = tmp_path / "cache"
    backups = tmp_path / "backups"
    cache.mkdir()
    backups.mkdir()
    (cache / "entry.bin").write_bytes(b"cache")
    index = tmp_path / "index.sqlite3"
    index.write_bytes(b"index-data")
    (backups / "manifest.json").write_bytes(b"backup-data")
    backend = StorageAreaBackend(
        quota_factory=lambda: QuotaService(NoServerQuota()),
        message_loader=lambda _analysis, limit: messages[:limit],
        local_paths={"Cache": cache, "Index": index, "Backups": backups},
        export_directory=tmp_path / "exports",
        online=True,
        maximum_scan_items=20,
    )
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    screen = StorageScreen(backend, analysis_limit=20)

    async def wait_ready(predicate, description: str) -> None:  # type: ignore[no-untyped-def]
        for _ in range(500):
            if not screen._operation_active and predicate():
                await pilot.pause()
                return
            await pilot.pause(0.02)
        raise AssertionError(f"Storage did not acknowledge {description}")

    async def run_analysis(
        command_id: str,
        expected: str,
        *,
        selected_row: str | None = None,
    ) -> None:
        route = catalog_route(command_id)
        # Several distinct palette commands intentionally share an analysis ID.
        # Clear the prior view so this helper waits for the newly requested worker.
        await wait_ready(lambda: True, "the preceding operation")
        screen._analysis = None
        app._catalog_screen_action(route, CommandInvocation(route.command_id))
        await wait_ready(
            lambda: (
                screen._analysis is not None
                and screen._analysis.analysis_id == expected
                and (selected_row is None or screen._selected_row == selected_row)
            ),
            f"{command_id}: {expected}",
        )

    async with app.run_test(size=(120, 35)) as pilot:
        app.push_screen(screen)
        await wait_ready(lambda: screen._snapshot is not None, "the storage snapshot")
        assert screen._snapshot is not None

        await run_analysis("cleanup.analyze_mailbox", "mailbox-overview")
        assert screen._analysis is not None
        assert {row.row_id for row in screen._analysis.rows} >= {
            "overview:mailbox-sample",
            "overview:attachments",
            "overview:largest-folder",
            "overview:largest-sender",
            "overview:largest-message",
        }
        screen._analysis = None
        app._palette_handlers()["cleanup.analyze"](
            CommandInvocation("cleanup.analyze"),
            app._palette_context(),
        )
        await wait_ready(
            lambda: (
                screen._analysis is not None and screen._analysis.analysis_id == "mailbox-overview"
            ),
            "the overview palette action",
        )
        assert screen._analysis is not None
        assert screen._analysis.analysis_id == "mailbox-overview"

        for command_id, analysis_id in (
            ("storage.show_storage_by_folder", "folders"),
            ("storage.show_storage_by_sender", "senders"),
            ("storage.show_storage_by_date", "dates"),
            ("storage.show_storage_by_attachment_type", "attachment-types"),
        ):
            await run_analysis(command_id, analysis_id)
            assert screen._analysis is not None and screen._analysis.rows

        for command_id, selected_row in (
            ("storage.show_local_cache_size", "local:cache"),
            ("storage.show_search_index_size", "local:index"),
            ("storage.show_backup_storage", "local:backups"),
        ):
            await run_analysis(command_id, "local", selected_row=selected_row)
            assert screen._selected_row == selected_row

        app._palette_handlers()["storage.largest_messages"](
            CommandInvocation("storage.largest_messages"),
            app._palette_context(),
        )
        await wait_ready(
            lambda: (
                screen._analysis is not None and screen._analysis.analysis_id == "largest-messages"
            ),
            "the largest messages palette action",
        )
        assert screen._analysis is not None
        assert screen._analysis.rows[0].row_id == "storage-one"

        await run_analysis("storage.analyze_reclaimable_space", "largest-messages")
        assert "Estimated reclaimable" in str(screen.query_one("#area-state", Static).render())

        screen.action_export()
        await wait_ready(
            lambda: (
                bool(tuple((tmp_path / "exports").glob("gucmail-storage-*.json")))
                and "Storage statistics exported"
                in str(screen.query_one("#area-state", Static).content)
            ),
            "the exported report",
        )
        assert len(tuple((tmp_path / "exports").glob("gucmail-storage-*.json"))) == 1

        planner = catalog_route("storage.open_cleanup_planner")
        app._catalog_screen_action(planner, CommandInvocation(planner.command_id))
        await wait_ready(
            lambda: (
                isinstance(app.screen, CleanupPlanScreen)
                and app.screen.is_mounted
                and bool(app.screen.query("#cleanup-plan-summary"))
            ),
            "the cleanup plan",
        )
        assert isinstance(app.screen, CleanupPlanScreen)
        assert len(app.screen.query("#cleanup-plan-summary")) == 1
        assert app.screen.plan.entries


@pytest.mark.asyncio
async def test_storage_open_and_quota_palette_routes_mount_the_real_offline_workspace(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)

    async with app.run_test(size=(100, 30)) as pilot:
        app._palette_handlers()["storage.open"](
            CommandInvocation("storage.open"),
            app._palette_context(),
        )
        for _ in range(80):
            if (
                isinstance(app.screen, StorageScreen) and app.screen.is_mounted
            ) and app.screen._snapshot is not None:
                break
            await pilot.pause(0.03)
        assert isinstance(app.screen, StorageScreen)
        assert app.screen._snapshot is not None
        assert not app.screen._snapshot.total_capacity_is_fallback
        assert app.screen._snapshot.total_capacity_bytes is None
        await pilot.press("escape")
        await pilot.pause()

        route = catalog_route("storage.show_mailbox_quota")
        app._catalog_open(route, CommandInvocation(route.command_id))
        for _ in range(80):
            if isinstance(app.screen, StorageScreen) and app.screen._snapshot is not None:
                detail = str(app.screen.query_one("#storage-detail", Static).render())
                if "Server-reported total capacity" in detail:
                    break
            await pilot.pause(0.03)
        assert isinstance(app.screen, StorageScreen)
        detail = str(app.screen.query_one("#storage-detail", Static).render())
        assert "Server-reported total capacity: Unknown" in detail
        assert "Prohibit send" in detail

        await pilot.press("escape")
        await pilot.pause()
        diagnostics_route = catalog_route("diagnostics.show_quota_source")
        app._palette_catalog_handler(
            CommandInvocation(diagnostics_route.command_id), app._palette_context()
        )
        for _ in range(80):
            if isinstance(app.screen, StorageScreen) and app.screen._snapshot is not None:
                detail = str(app.screen.query_one("#storage-detail", Static).render())
                if "Capacity source:" in detail:
                    break
            await pilot.pause(0.03)
        assert isinstance(app.screen, StorageScreen)
        detail = str(app.screen.query_one("#storage-detail", Static).render())
        assert "Server-reported total capacity: Unknown" in detail
        assert "Capacity source: Unknown" in detail
        assert "Prohibit send" in detail


@pytest.mark.asyncio
async def test_cleanup_plan_palette_routes_create_preview_and_execute_only_after_review(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    moved: list[str] = []
    messages = (
        {
            "reference": "cleanup-large",
            "subject": "Large review candidate",
            "sender": "sender@example.test",
            "folder_path": "Inbox",
            "size_bytes": 9_000,
        },
        {
            "reference": "cleanup-small",
            "subject": "Small review candidate",
            "sender": "sender@example.test",
            "folder_path": "Archive",
            "size_bytes": 1_000,
        },
    )
    backend = StorageAreaBackend(
        quota_factory=None,
        message_loader=lambda _analysis, limit: messages[:limit],
        state=context.state,
        cleanup_executor=lambda candidate: moved.append(candidate.reference),
        local_paths={},
        export_directory=tmp_path / "exports",
        online=True,
        maximum_scan_items=20,
    )
    app = GUCmailApp(context, gateway=FakeGateway())
    storage = StorageScreen(backend, analysis_limit=20)

    async def open_plan(command_id: str) -> CleanupPlanScreen:
        route = catalog_route(command_id)
        app._catalog_screen_action(route, CommandInvocation(route.command_id))
        for _ in range(120):
            current = app.screen
            if (
                isinstance(current, CleanupPlanScreen)
                and current.is_mounted
                and len(current.query("#cleanup-plan-execute"))
            ):
                return current
            await pilot.pause(0.03)
        raise AssertionError(f"{command_id} did not open a cleanup plan")

    async with app.run_test(size=(120, 35)) as pilot:
        app.push_screen(storage)
        for _ in range(80):
            if storage._snapshot is not None:
                break
            await pilot.pause(0.03)
        assert storage._snapshot is not None

        for command_id in (
            "cleanup.create_cleanup_plan",
            "cleanup.preview_cleanup_plan",
        ):
            plan = await open_plan(command_id)
            assert [entry.reference for entry in plan.plan.entries] == [
                "cleanup-large",
                "cleanup-small",
            ]
            assert moved == []
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is storage

        plan = await open_plan("cleanup.execute_cleanup_plan")
        execute = plan.query_one("#cleanup-plan-execute", Button)
        assert not execute.disabled
        execute.focus()
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(app.screen, OperationalConfirmationScreen) and app.screen.is_mounted:
                break
            await pilot.pause(0.03)
        assert isinstance(app.screen, OperationalConfirmationScreen)
        assert moved == []
        await pilot.press("y")
        for _ in range(120):
            if moved == ["cleanup-large", "cleanup-small"]:
                break
            await pilot.pause(0.03)
        assert moved == ["cleanup-large", "cleanup-small"]


def test_every_attachment_palette_command_has_an_executable_handler(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    handlers = app._palette_handlers()
    commands = tuple(
        command
        for command in build_core_command_registry().all()
        if command.category == "attachments"
    )

    assert len(commands) == 15
    assert all(command.handler_id in handlers for command in commands)
    duplicate = next(
        command
        for command in commands
        if command.command_id == "attachments.find_duplicate_attachments"
    )
    assert duplicate.offline and not duplicate.requires_connection
    assert dict(catalog_route(duplicate.command_id).filters) == {
        "analysis": "duplicate-attachments"
    }
    assert all(
        command.offline and not command.requires_connection
        for command in commands
        if command.command_id != "attachments.open_containing_message"
    )
    ocr = next(command for command in commands if command.command_id == "attachments.ocr")
    assert ocr.handler_id == "attachments.ocr" and ocr.offline
    assert ocr.cli_path == ("ocr", "status") and ocr.help_topic == "search.local"


@pytest.mark.asyncio
async def test_draft_palette_schedule_creates_durable_reviewed_job(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    ).create(
        DraftData(
            reference="dft-schedule",
            to=[Address("recipient@example.test")],
            subject="Schedule me",
            body_text="Synthetic body",
        )
    )
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("drafts")
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#draft-table").row_count:
                break
        assert isinstance(app.screen, DraftAttachmentScreen)

        route = catalog_route("drafts.schedule_draft")
        app._catalog_screen_action(route, CommandInvocation(route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, DraftScheduleScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, DraftScheduleScreen)
        app.screen.query_one("#draft-schedule-expression", Input).value = "tomorrow 09:00"
        app.screen._finish()

        for _ in range(60):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#scheduled-table").row_count:
                break
        assert isinstance(app.screen, ScheduledScreen)
        assert app.screen.query_one("#scheduled-table").row_count == 1
        records = app._scheduler_service().list(draft_reference="dft-schedule", limit=10)
        assert len(records) == 1
        assert records[0].message.require_final_confirmation is True


@pytest.mark.asyncio
async def test_scheduled_palette_pauses_and_resumes_selected_recurring_rule(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, gateway=FakeGateway())
    scheduler = app._scheduler_service()
    draft = scheduler.drafts.create(
        DraftData(
            reference="dft-recurring-tui",
            to=[Address("recipient@example.test")],
            subject="Recurring palette fixture",
            body_text="Synthetic recurring body",
        )
    )
    now = datetime.now(UTC)
    recurring = RecurringService(
        context.state,
        scheduler.drafts,
        scheduler,
        automation_enabled=True,
    )
    rule = recurring.create(
        draft.reference,
        RecurrenceRule(
            start_at=now.replace(microsecond=0) + timedelta(days=2),
            frequency=RecurrenceFrequency.DAILY,
            timezone="UTC",
        ),
        activate=True,
        reference="rec-tui",
        now=now,
    )
    scheduler.schedule_at(
        draft.reference,
        now + timedelta(days=2),
        recurrence_id=rule.reference,
        now=now,
    )

    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("scheduled")
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#scheduled-table").row_count:
                break
        assert isinstance(app.screen, ScheduledScreen)

        pause_route = catalog_route("scheduled.pause_recurring_message")
        app._catalog_screen_action(pause_route, CommandInvocation(pause_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                recurring.get(rule.reference).state is RecurringState.PAUSED
                and (isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted)
                and not app.screen._operation_active
            ):
                break
        assert recurring.get(rule.reference).state is RecurringState.PAUSED
        await pilot.pause(0.1)

        resume_route = catalog_route("scheduled.resume_recurring_message")
        app._catalog_screen_action(resume_route, CommandInvocation(resume_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause(0.05)
            if recurring.get(rule.reference).state is RecurringState.ACTIVE:
                break
        assert recurring.get(rule.reference).state is RecurringState.ACTIVE


@pytest.mark.asyncio
async def test_scheduled_palette_views_duplicate_send_run_and_pause(
    tmp_path: Path,
) -> None:
    class SendingGateway(FakeGateway):
        def __init__(self) -> None:
            self.sent_subjects: list[str] = []

        def send(self, draft: DraftData) -> str:
            self.sent_subjects.append(draft.subject)
            return f"sent-{len(self.sent_subjects)}"

    context = context_for(tmp_path)
    gateway = SendingGateway()
    app = GUCmailApp(context, gateway=gateway)
    scheduler = app._scheduler_service()
    now = datetime.now(UTC)
    future_draft = scheduler.drafts.create(
        DraftData(
            reference="draft-scheduled-future",
            to=[Address("recipient@example.test")],
            subject="Future palette message",
            body_text="Synthetic scheduled body",
        )
    )
    scheduler.schedule_at(
        future_draft.reference,
        now + timedelta(days=2),
        now=now,
        timezone="UTC",
        require_final_confirmation=True,
    )

    async with app.run_test(size=(120, 35)) as pilot:
        next_route = catalog_route("scheduled.show_next_scheduled_message")
        app._catalog_open(next_route, CommandInvocation(next_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted) and (
                app.screen.query_one("#scheduled-window", Select).value == "next"
                and app.screen.query_one("#scheduled-table").row_count == 1
            ):
                break
        assert isinstance(app.screen, ScheduledScreen)
        assert app.screen.query_one("#scheduled-window", Select).value == "next"
        assert "Future palette message" in str(
            app.screen.query_one("#scheduled-detail", Static).content
        )

        duplicate_route = catalog_route("scheduled.duplicate_scheduled_message")
        app._catalog_screen_action(
            duplicate_route,
            CommandInvocation.from_mapping(
                duplicate_route.command_id,
                {"when": "in 3 days"},
            ),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                len(app._scheduler_service().list(limit=10)) == 2
                and not app.screen._operation_active
            ):
                break
        assert len(app._scheduler_service().list(limit=10)) == 2
        assert not app.screen._operation_active

        send_route = catalog_route("scheduled.send_scheduled_message_now")
        app._catalog_screen_action(send_route, CommandInvocation(send_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(60):
            await pilot.pause(0.05)
            if gateway.sent_subjects:
                break
        assert gateway.sent_subjects == ["Future palette message"]
        for _ in range(60):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted
            ) and not app.screen._operation_active:
                break
        assert not app.screen._operation_active

        assert isinstance(app.screen, ScheduledScreen)
        pause_route = catalog_route("scheduled.pause_scheduler")
        app._catalog_screen_action(pause_route, CommandInvocation(pause_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if app._scheduler_service().is_paused() and not app.screen._operation_active:
                break
        assert app._scheduler_service().is_paused()
        assert not app.screen._operation_active
        resume_route = catalog_route("scheduled.resume_scheduler")
        app._catalog_screen_action(resume_route, CommandInvocation(resume_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if not app._scheduler_service().is_paused() and not app.screen._operation_active:
                break
        assert not app._scheduler_service().is_paused()
        assert not app.screen._operation_active

        due_draft = scheduler.drafts.create(
            DraftData(
                reference="draft-scheduled-due",
                to=[Address("recipient@example.test")],
                subject="Due palette message",
                body_text="Synthetic due body",
            )
        )
        scheduler.schedule_at(
            due_draft.reference,
            now - timedelta(minutes=1),
            now=now - timedelta(days=1),
            timezone="UTC",
        )
        run_route = catalog_route("scheduled.run_due_jobs")
        app._catalog_screen_action(run_route, CommandInvocation(run_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(60):
            await pilot.pause(0.05)
            if len(gateway.sent_subjects) == 2:
                break
        assert gateway.sent_subjects == ["Future palette message", "Due palette message"]

        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted
            ) and not app.screen._operation_active:
                break
        assert isinstance(app.screen, ScheduledScreen)
        app.pop_screen()
        await pilot.pause()

        for command_id, selector, expected in (
            ("scheduled.show_today_s_scheduled_messages", "#scheduled-window", "today"),
            (
                "scheduled.show_this_week_s_scheduled_messages",
                "#scheduled-window",
                "this_week",
            ),
            ("scheduled.show_failed_scheduled_messages", "#scheduled-state", "failed"),
            (
                "scheduled.show_uncertain_sends",
                "#scheduled-state",
                "send_state_uncertain",
            ),
        ):
            route = catalog_route(command_id)
            app._catalog_open(route, CommandInvocation(route.command_id))
            for _ in range(30):
                await pilot.pause(0.05)
                if (isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted) and (
                    app.screen.query_one(selector, Select).value == expected
                ):
                    break
            assert isinstance(app.screen, ScheduledScreen)
            assert app.screen.query_one(selector, Select).value == expected
            app.pop_screen()
            await pilot.pause()

        status_route = catalog_route("scheduled.open_scheduler_status")
        app._catalog_open(status_route, CommandInvocation(status_route.command_id))
        for _ in range(30):
            await pilot.pause(0.05)
            if (isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted) and str(
                app.screen.query_one("#scheduled-summary", Static).content
            ).startswith("Running"):
                break
        assert isinstance(app.screen, ScheduledScreen)
        assert "Running" in str(app.screen.query_one("#scheduled-summary", Static).content)


@pytest.mark.asyncio
async def test_schedule_current_draft_base_handler_reaches_review(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        app.action_compose()
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        app.screen.query_one("#compose-to", Input).value = "recipient@example.test"
        app.screen.query_one("#compose-subject", Input).value = "Schedule current handler"
        app._palette_handlers()["scheduled.schedule_current"](
            CommandInvocation.from_mapping(
                "scheduled.schedule_current",
                {"when": "in 2 days"},
            ),
            app._palette_context(),
        )
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, DraftScheduleScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, DraftScheduleScreen)
        assert app.screen.query_one("#draft-schedule-expression", Input).value == "in 2 days"


@pytest.mark.asyncio
async def test_palette_mutation_parameters_can_be_edited_before_confirmation(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        app.action_compose()
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        app.screen.query_one("#compose-to", Input).value = "recipient@example.test"
        app.screen.query_one("#compose-subject", Input).value = "Editable schedule"

        app.action_palette()
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        app.screen.query_one("#palette-input", Input).value = "send this tomorrow morning"
        await pilot.pause()
        assert "When: tomorrow at 8:00 AM" in str(
            app.screen.query_one("#palette-explanation", Static).content
        )
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, PaletteParameterReviewScreen)
        when_index = next(
            index for index, (name, _value) in enumerate(app.screen._parameters) if name == "when"
        )
        app.screen.query_one(f"#parameter-review-{when_index}", Input).value = "not a date"
        await pilot.click("#parameter-review-continue")
        await pilot.pause()
        assert isinstance(app.screen, PaletteParameterReviewScreen)
        assert "Could not understand the date" in str(
            app.screen.query_one("#parameter-review-error", Static).content
        )
        app.screen.query_one(f"#parameter-review-{when_index}", Input).value = "in 2 days"
        await pilot.pause()
        app.screen.query_one("#parameter-review-continue", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, ConfirmationScreen), str(
            app.screen.query_one("#parameter-review-error", Static).content
            if isinstance(app.screen, PaletteParameterReviewScreen)
            else app.screen
        )
        assert "When: in 2 days" in app.screen.detail
        app.screen.dismiss(True)
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, DraftScheduleScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, DraftScheduleScreen)
        assert app.screen.query_one("#draft-schedule-expression", Input).value == "in 2 days"


@pytest.mark.asyncio
async def test_context_aware_palette_executes_selected_object_workflows(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    ).create(DraftData(reference="context-draft", subject="Context draft", body_text="Body"))
    BackupService(context.paths.backup_dir, state=context.state).create(
        (
            BackupItem(
                "context-message",
                "message",
                {"subject": "Context archive", "body_text": "searchable evidence"},
            ),
        ),
        backup_id="context-backup",
    )
    app = GUCmailApp(context, gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(80):
            await pilot.pause(0.025)
            if app.ui.online and app.ui.selected_reference == "m-demo":
                break

        mail_context = app._palette_context()
        assert mail_context.get("selected_message") == "m-demo"
        app._palette_catalog_handler(CommandInvocation("compose.reply"), mail_context)
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ComposeScreen)
        await pilot.pause()
        assert app.screen.query_one("#compose-subject", Input).value.startswith("Re:")
        app.screen.dismiss(None)
        await pilot.pause()

        app._open_area("calendar")
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and app._palette_context().get("selected_event"):
                break
        assert isinstance(app.screen, CalendarScreen)
        event_context = app._palette_context()
        assert event_context.get("selected_event") == "event-demo"
        app._palette_catalog_handler(CommandInvocation("calendar.accept_invitation"), event_context)
        await pilot.pause()
        assert isinstance(app.screen, MeetingResponseScreen)
        app.screen.dismiss(None)
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()

        app._open_area("contacts")
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app._palette_context().get("selected_contact"):
                break
        assert isinstance(app.screen, ContactsScreen)
        contact_context = app._palette_context()
        assert contact_context.get("selected_contact") == "contact@example.test"
        app._palette_catalog_handler(
            CommandInvocation("contacts.compose_to_contact"), contact_context
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-to", Input).value == "contact@example.test"
        app.screen.dismiss(None)
        await pilot.pause()

        app._open_area("drafts")
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, DraftAttachmentScreen) and app.screen.is_mounted
            ) and app._palette_context().get("current_draft"):
                break
        assert isinstance(app.screen, DraftAttachmentScreen)
        draft_context = app._palette_context()
        assert draft_context.get("current_draft") == "context-draft"
        app._palette_handlers()["scheduled.schedule_current"](
            CommandInvocation("scheduled.schedule_current"), draft_context
        )
        await pilot.pause()
        assert isinstance(app.screen, DraftScheduleScreen)
        assert app.screen.query_one("#draft-schedule-expression", Input).value == "tomorrow 09:00"
        app.screen.dismiss(None)
        await pilot.pause()

        app._palette_catalog_handler(CommandInvocation("compose.send_after_delay"), draft_context)
        await pilot.pause()
        assert isinstance(app.screen, DraftScheduleScreen)
        assert app.screen.query_one("#draft-schedule-expression", Input).value == "in 10 minutes"
        app.screen.dismiss(None)
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()

        app._open_area("backups")
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, BackupScreen) and app.screen.is_mounted
            ) and app._palette_context().get("current_backup"):
                break
        assert isinstance(app.screen, BackupScreen)
        backup_context = app._palette_context()
        assert backup_context.get("current_backup") == "context-backup"
        app._palette_catalog_handler(
            CommandInvocation("search.search_backups", original_text="search"), backup_context
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, LocalSearchScreen) and app.screen.is_mounted
            ) and app.screen.selected_backup_id == "context-backup":
                break
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.selected_backup_id == "context-backup"
        assert app.screen.query_one("#local-search-mode", Select).value == "backups"
        assert app.screen.query_one("#local-search-query", Input).value == ""


@pytest.mark.asyncio
async def test_palette_executes_only_allowlisted_chains_after_real_preview(
    tmp_path: Path,
) -> None:
    mark_context = context_for(tmp_path / "mark")
    mark_gateway = ChainGateway()
    mark_app = GUCmailApp(mark_context, gateway=mark_gateway)

    async with mark_app.run_test(size=(120, 35)) as pilot:
        mark_app.action_palette()
        await pilot.pause()
        assert isinstance(mark_app.screen, PaletteScreen)
        mark_app.screen.query_one(
            "#palette-input", Input
        ).value = "find unread emails from student affairs and mark them read"
        for _ in range(20):
            await pilot.pause(0.025)
            if mark_app.screen._sequence is not None:
                break
        assert "Bounded sequence" in str(
            mark_app.screen.query_one("#palette-explanation", Static).content
        )
        assert not mark_gateway.marked
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(mark_app.screen, PaletteSequencePreviewScreen):
                break
        assert isinstance(mark_app.screen, PaletteSequencePreviewScreen)
        assert mark_app.screen.query_one("#sequence-results", DataTable).row_count == 1
        assert "Matched: 1 bounded" in str(
            mark_app.screen.query_one("#sequence-summary", Static).content
        )
        assert not mark_gateway.marked
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        await pilot.click("#sequence-continue")
        await pilot.pause()
        assert isinstance(mark_app.screen, ConfirmationScreen)
        assert "Exact local references" in mark_app.screen.detail
        assert not mark_gateway.marked
        mark_app.screen.dismiss(True)
        for _ in range(80):
            await pilot.pause(0.025)
            if mark_gateway.marked:
                break
        assert mark_gateway.marked == [("m-demo", True)]
        assert mark_gateway.search_queries[-1].sender == "Student Affairs"
        assert mark_gateway.search_queries[-1].is_read is False

    backup_context = context_for(tmp_path / "backup")
    backup_gateway = ChainGateway()
    backup_app = GUCmailApp(backup_context, gateway=backup_gateway)
    backup_destination = tmp_path / "chain-backups"

    async with backup_app.run_test(size=(120, 35)) as pilot:
        backup_app.action_palette()
        await pilot.pause()
        assert isinstance(backup_app.screen, PaletteScreen)
        backup_app.screen.query_one(
            "#palette-input", Input
        ).value = "find emails larger than 5 MB and then back them up"
        for _ in range(20):
            await pilot.pause(0.025)
            if backup_app.screen._sequence is not None:
                break
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(backup_app.screen, PaletteSequencePreviewScreen):
                break
        assert isinstance(backup_app.screen, PaletteSequencePreviewScreen)
        summary = str(backup_app.screen.query_one("#sequence-summary", Static).content)
        assert "Matched: 1 bounded" in summary
        assert "5.7 MiB known" in summary
        backup_app.screen.query_one("#sequence-destination", Input).value = str(backup_destination)
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        await pilot.click("#sequence-continue")
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(backup_app.screen, BackupScreen):
                break
        assert isinstance(backup_app.screen, BackupScreen)
        assert backup_app.screen.selected_messages == ("m-demo",)
        assert backup_app.screen.query_one("#backup-destination", Input).value == str(
            backup_destination.resolve()
        )
        assert backup_gateway.search_queries[-1].minimum_size == 5_000_000
        assert backup_gateway.search_queries[-1].sender is None
        backup_app.screen.action_create()
        await pilot.pause()
        assert isinstance(backup_app.screen, OperationalConfirmationScreen)
        backup_app.screen.dismiss(True)
        for _ in range(120):
            await pilot.pause(0.025)
            if BackupService(backup_destination, state=backup_context.state).list(limit=10):
                break
        records = BackupService(backup_destination, state=backup_context.state).list(limit=10)
        assert len(records) == 1
        assert records[0].item_count == 1

    attachment_context = context_for(tmp_path / "attachment")
    attachment_gateway = ChainGateway()
    attachment_app = GUCmailApp(attachment_context, gateway=attachment_gateway)
    attachment_destination = tmp_path / "chain-downloads"

    async with attachment_app.run_test(size=(120, 35)) as pilot:
        attachment_app.action_palette()
        await pilot.pause()
        assert isinstance(attachment_app.screen, PaletteScreen)
        attachment_app.screen.query_one(
            "#palette-input", Input
        ).value = "find all PDFs from Ahmed then save them to Downloads"
        for _ in range(20):
            await pilot.pause(0.025)
            if attachment_app.screen._sequence is not None:
                break
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(attachment_app.screen, PaletteSequencePreviewScreen):
                break
        assert isinstance(attachment_app.screen, PaletteSequencePreviewScreen)
        assert attachment_app.screen.query_one("#sequence-results", DataTable).row_count == 1
        assert not attachment_destination.exists()
        attachment_app.screen.query_one("#sequence-destination", Input).value = str(
            attachment_destination
        )
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        await pilot.click("#sequence-continue")
        await pilot.pause()
        assert isinstance(attachment_app.screen, ConfirmationScreen)
        assert "safely rename" in attachment_app.screen.detail
        attachment_app.screen.dismiss(True)
        downloaded = attachment_destination / "synthetic-report.pdf"
        for _ in range(120):
            await pilot.pause(0.025)
            if downloaded.exists():
                break
        assert downloaded.read_bytes() == b"synthetic pdf"
        assert attachment_gateway.search_queries[-1].sender == "Ahmed"
        assert attachment_gateway.search_queries[-1].attachment_name == ".pdf"


def test_every_scheduled_palette_command_has_an_executable_and_honest_policy(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    handlers = app._palette_handlers()
    commands = tuple(
        command
        for command in build_core_command_registry().all()
        if command.category == "scheduled"
    )

    assert len(commands) == 19
    assert all(command.handler_id in handlers for command in commands)
    online_commands = {
        "scheduled.run_due_jobs",
        "scheduled.run_scheduler_now",
        "scheduled.send_scheduled_message_now",
    }
    assert all(
        command.requires_connection and not command.offline and command.changes_server
        for command in commands
        if command.command_id in online_commands
    )
    assert all(
        command.offline and not command.requires_connection
        for command in commands
        if command.command_id not in online_commands
    )
    assert dict(catalog_route("scheduled.show_next_scheduled_message").filters) == {
        "limit": "1",
        "window": "next",
    }
    assert dict(catalog_route("scheduled.show_today_s_scheduled_messages").filters) == {
        "window": "today"
    }
    assert dict(catalog_route("scheduled.show_this_week_s_scheduled_messages").filters) == {
        "window": "this_week"
    }


@pytest.mark.asyncio
async def test_composer_palette_controls_execute_real_draft_fields_and_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    context.config_store.save(replace(context.config, external_editor="synthetic-editor"))
    context = reloaded_context(context)

    class FakeEditor:
        def __init__(self, drafts: DraftService, command: str) -> None:
            assert command == "synthetic-editor"
            self.drafts = drafts

        def edit(self, reference: str) -> SimpleNamespace:
            draft = self.drafts.get(reference)
            draft.body_text = "Externally edited body"
            saved = self.drafts.save(draft, expected_revision=draft.revision)
            return SimpleNamespace(draft=saved)

    monkeypatch.setattr("gucmail.tui.app.ExternalDraftEditor", FakeEditor)
    app = GUCmailApp(context, gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.ui.selected_reference == "m-demo":
                break

        forward_route = catalog_route("compose.forward_as_attachment")
        app._catalog_screen_action(forward_route, CommandInvocation(forward_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#compose-attachments", Input).value:
                break
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        mime_path = Path(app.screen.query_one("#compose-attachments", Input).value)
        assert mime_path.suffix == ".eml"
        assert mime_path.read_bytes().endswith(b"Hello\r\n")

        add_route = catalog_route("compose.add_attachment")
        app._catalog_screen_action(add_route, CommandInvocation(add_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, FilePathScreen)
        app.screen.dismiss(None)
        await pilot.pause()
        await _ready_composer_for_editing(pilot)

        remove_route = catalog_route("compose.remove_attachment")
        app._catalog_screen_action(remove_route, CommandInvocation(remove_route.command_id))
        await pilot.pause()
        assert app.screen.query_one("#compose-attachments", Input).value == ""

        read_route = catalog_route("compose.request_read_receipt")
        delivery_route = catalog_route("compose.request_delivery_receipt")
        app._catalog_screen_action(read_route, CommandInvocation(read_route.command_id))
        app._catalog_screen_action(delivery_route, CommandInvocation(delivery_route.command_id))
        assert app.screen.query_one("#compose-read-receipt", Checkbox).value
        assert app.screen.query_one("#compose-delivery-receipt", Checkbox).value

        app.screen.query_one("#compose-body", TextArea).text = "<unsafe & plain>"
        mode_route = catalog_route("compose.toggle_html_plain_text")
        app._catalog_screen_action(mode_route, CommandInvocation(mode_route.command_id))
        assert app.screen.query_one("#compose-body-mode", Select).value == "html"
        assert "&lt;unsafe &amp; plain&gt;" in app.screen.query_one("#compose-body", TextArea).text

        editor_route = catalog_route("compose.open_external_editor")
        app._catalog_screen_action(editor_route, CommandInvocation(editor_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if app.screen.query_one("#compose-body", TextArea).text == "Externally edited body":
                break
        assert app.screen.query_one("#compose-body-mode", Select).value == "plain"
        assert app.screen.query_one("#compose-body", TextArea).text == "Externally edited body"
        await _ready_composer_for_editing(pilot)
        payload = app.screen._payload(action="save")
        assert payload["request_read_receipt"] is True
        assert payload["request_delivery_receipt"] is True
        app.screen.query_one("#compose-to", Input).value = "recipient@example.test"

        delay_route = catalog_route("compose.send_after_delay")
        app._catalog_screen_action(delay_route, CommandInvocation(delay_route.command_id))
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, DraftScheduleScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, DraftScheduleScreen)
        assert app.screen.query_one("#draft-schedule-expression", Input).value == "in 10 minutes"
        app.screen.query_one("#draft-schedule-expression", Input).value = "in 30 minutes"
        app.screen._finish()
        for _ in range(60):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ScheduledScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#scheduled-table").row_count:
                break
        assert isinstance(app.screen, ScheduledScreen)
        records = app._scheduler_service().list(limit=10)
        assert len(records) == 1
        assert records[0].message.require_final_confirmation

        before = records[0].message.scheduled_for
        app.screen.query_one("#scheduled-time", Input).value = "in 2 days"
        reschedule_route = catalog_route("compose.reschedule")
        app._catalog_screen_action(
            reschedule_route,
            CommandInvocation(reschedule_route.command_id),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                app._scheduler_service().list(limit=1)[0].message.scheduled_for != before
                and app.screen.view_state is ViewState.READY
                and not app.screen._operation_active
                and not app.screen.mutation_pending
            ):
                break
        assert app._scheduler_service().list(limit=1)[0].message.scheduled_for != before
        assert app.screen.view_state is ViewState.READY and not app.screen.mutation_pending

        cancel_route = catalog_route("compose.cancel_scheduled_send")
        app._catalog_screen_action(cancel_route, CommandInvocation(cancel_route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause(0.05)
            # Durable cancellation precedes the worker/UI acknowledgement. Do
            # not hand off to another mutation while its shared guard is held.
            if (
                app._scheduler_service().list(limit=1)[0].message.state.value == "cancelled"
                and app.screen.view_state is ViewState.READY
                and not app.screen._operation_active
                and not app.screen.mutation_pending
                and not app_write_coordinator(app).pending
            ):
                break
        assert app._scheduler_service().list(limit=1)[0].message.state.value == "cancelled"
        assert app.screen.view_state is ViewState.READY
        assert not app.screen._operation_active and not app.screen.mutation_pending
        assert not app_write_coordinator(app).pending

        for command_id, expected in (
            ("compose.send_later", "tomorrow 09:00"),
            ("compose.schedule_draft", "tomorrow 09:00"),
        ):
            route = catalog_route(command_id)
            app._catalog_screen_action(route, CommandInvocation(route.command_id))
            for _ in range(40):
                await pilot.pause(0.05)
                if isinstance(app.screen, DraftScheduleScreen) and app.screen.is_mounted:
                    break
            assert isinstance(app.screen, DraftScheduleScreen)
            await pilot.pause()
            assert app.screen.query_one("#draft-schedule-expression", Input).value == expected
            app.screen.dismiss(None)
            await pilot.pause()
            assert isinstance(app.screen, ComposeScreen)
            app.screen.dismiss(None)
            await pilot.pause()


@pytest.mark.asyncio
async def test_palette_saved_search_browser_executes_exact_query_and_records_run(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    backend = StateSavedSearchBackend(context.state, timezone=context.config.timezone)
    backend.create("Unread from teacher", 'from:"teacher@example.test" AND unread:true')
    app = GUCmailApp(context, gateway=SavedSearchGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        advanced = catalog_route("search.advanced_search")
        app._catalog_search(
            advanced,
            CommandInvocation.from_mapping(
                advanced.command_id,
                {"query": 'from:"teacher@example.test" AND unread:true'},
            ),
        )
        search_worker = next(
            worker for worker in app.workers if worker.name == "_search_expression"
        )
        await search_worker.wait()
        await pilot.pause()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.query_one("#message-table").row_count == 1:
                break
        assert app.query_one("#message-table").row_count == 1
        assert app._current_search_text == 'from:"teacher@example.test" AND unread:true'
        assert context.state.list_search_history(limit=1)[0]["source"] == "palette"

        route = catalog_route("search.search_saved_queries")
        app._catalog_open(route, CommandInvocation(route.command_id))
        await pilot.pause()
        assert isinstance(app.screen, SavedSearchScreen)
        assert app.screen.query_one("#saved-search-table").row_count == 1

        app.screen.action_run()
        for _ in range(30):
            await pilot.pause(0.05)
            current_record = context.state.get_search_record("Unread from teacher")
            if (
                app.screen is app.screen_stack[0]
                and current_record is not None
                and current_record["run_count"] == 1
            ):
                break
        assert app.screen is app.screen_stack[0]
        assert app.query_one("#message-table").row_count == 1
        record = context.state.get_search_record("Unread from teacher")
        assert record is not None
        assert record["last_result_count"] == 1
        assert record["run_count"] == 1
        assert context.state.list_search_history(limit=5)[0]["source"] == "saved"

        app._current_search_text = "unread:true"
        create_route = catalog_route("search.save_current_search")
        app._catalog_open(create_route, CommandInvocation(create_route.command_id))
        for _ in range(10):
            await pilot.pause(0.05)
            if isinstance(app.screen, SavedSearchEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, SavedSearchEditorScreen)
        await pilot.pause()
        assert app.screen.query_one("#saved-search-editor-query", TextArea).text == "unread:true"


@pytest.mark.asyncio
async def test_saved_search_browser_consumes_disabled_history_setting(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.history_enabled", False)
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(79, 26)) as pilot:
        app._open_saved_searches(mode="recent")
        await pilot.pause()
        assert isinstance(app.screen, SavedSearchScreen)
        backend = app.screen.backend
        assert isinstance(backend, StateSavedSearchBackend)
        assert backend.history_enabled is False
        assert (
            "disabled"
            in str(app.screen.query_one("#saved-search-empty", Static).content).casefold()
        )


@pytest.mark.asyncio
async def test_catalog_local_search_route_executes_federated_offline_sources(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    SearchIndex(context.paths.index_database).upsert(
        IndexDocument(
            "indexed-enrollment",
            subject="Synthetic enrollment notice",
            body="Enrollment instructions are indexed locally.",
        )
    )
    BackupService(context.paths.backup_dir).create(
        (
            BackupItem(
                "archived-enrollment",
                "task",
                {"title": "Archived enrollment task"},
                folder_path="Tasks",
            ),
        ),
        backup_id="search-route-backup",
    )
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(120, 35)) as pilot:
        route = catalog_route("search.search_all_data")
        invocation = CommandInvocation.from_mapping(
            route.command_id,
            {"query": "enrollment"},
            original_text="search all data for enrollment",
        )
        app._catalog_screen_action(route, invocation)
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, LocalSearchScreen):
                table = app.screen.query_one("#local-search-table")
                if table.row_count == 2:
                    break

        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-table").row_count == 2
        source = str(app.screen.query_one("#local-search-source", Static).content)
        assert "local-index, backup" in source


@pytest.mark.asyncio
async def test_offline_palette_routes_execute_local_workflows_without_exchange(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    backups = BackupService(context.paths.backup_dir)
    backups.create(
        (
            BackupItem(
                "old-message",
                "message",
                {"subject": "Superseded local snapshot"},
                folder_path="Inbox/Old",
            ),
        ),
        backup_id="aaa-older-complete",
    )
    backups.create(
        (
            BackupItem(
                "latest-project",
                "message",
                {"subject": "Enrollment project update"},
                folder_path="Inbox/Projects",
            ),
            BackupItem(
                "latest-contact",
                "contact",
                {"display_name": "Synthetic Contact"},
                folder_path="Contacts",
            ),
        ),
        backup_id="zzz-latest-complete",
    )
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(120, 35)) as pilot:
        handlers = app._palette_handlers()
        offline_catalog = handlers["catalog.offline"]

        offline_catalog(CommandInvocation("offline.leave_offline_mode"), CommandContext())
        assert app.offline is False
        assert app.ui.online is False
        assert app.gateway is None
        assert "reconnect on demand" in str(app.query_one("#status-bar", Static).content).casefold()

        handlers["offline.enter"](CommandInvocation("offline.enter"), CommandContext())
        assert app.offline is True
        assert app.ui.online is False
        assert app.gateway is None

        offline_catalog(CommandInvocation("offline.show_offline_status"), CommandContext())
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, AreaScreen):
                status_text = str(app.screen.query_one("#area-content", Static).content)
                if "No Exchange request was made" in status_text:
                    break
        assert isinstance(app.screen, AreaScreen)
        status_text = str(app.screen.query_one("#area-content", Static).content)
        assert "Offline mode: ENABLED" in status_text
        assert "Complete backups: 2 of 2" in status_text
        assert "zzz-latest-complete" in status_text
        assert "No Exchange request was made" in status_text
        app.screen.dismiss(None)
        await pilot.pause()

        offline_catalog(
            CommandInvocation.from_mapping("offline.search_offline_mail", {"query": "enrollment"}),
            CommandContext(),
        )
        for _ in range(100):
            await pilot.pause(0.025)
            if isinstance(app.screen, LocalSearchScreen):
                results = app.screen.query_one("#local-search-table", DataTable)
                if results.row_count == 1:
                    break
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-mode", Select).value == "backups"
        assert app.screen.query_one("#local-search-table", DataTable).row_count == 1
        assert (
            "backup" in str(app.screen.query_one("#local-search-source", Static).content).casefold()
        )
        app.screen.dismiss(None)
        await pilot.pause()

        offline_catalog(CommandInvocation("offline.open_latest_backup_offline"), CommandContext())
        for _ in range(160):
            await pilot.pause(0.025)
            if (
                (isinstance(app.screen, BackupBrowseScreen) and app.screen.is_mounted)
                and app.screen.query("#backup-browse-table")
                and app.screen.query_one("#backup-browse-table", DataTable).row_count == 2
            ):
                break
        assert isinstance(app.screen, BackupBrowseScreen)
        assert app.screen.query_one("#backup-browse-table", DataTable).row_count == 2
        assert app.screen.backup_id == "zzz-latest-complete"
        assert {item.reference for item in app.screen.items} == {
            "latest-contact",
            "latest-project",
        }
        app.screen.dismiss(None)
        await pilot.pause()

        offline_catalog(
            CommandInvocation.from_mapping(
                "offline.browse_offline_folders", {"folder": "Projects"}
            ),
            CommandContext(),
        )
        for _ in range(160):
            await pilot.pause(0.025)
            if (
                (isinstance(app.screen, BackupFolderBrowseScreen) and app.screen.is_mounted)
                and app.screen.query("#backup-folder-table")
                and app.screen.query_one("#backup-folder-table", DataTable).row_count == 2
            ):
                break
        assert isinstance(app.screen, BackupFolderBrowseScreen)
        assert app.screen.backup_id == "zzz-latest-complete"
        assert app.screen.query_one("#backup-folder-table", DataTable).row_count == 2
        source = str(app.screen.query_one("#backup-folder-source", Static).content)
        detail = str(app.screen.query_one("#backup-folder-detail", Static).content)
        assert "immutable local backup" in source
        assert "Inbox/Projects" in detail

        app.screen.action_open_folder()
        for _ in range(160):
            await pilot.pause(0.025)
            if (
                (isinstance(app.screen, BackupBrowseScreen) and app.screen.is_mounted)
                and app.screen.query("#backup-browse-table")
                and app.screen.query_one("#backup-browse-table", DataTable).row_count == 1
            ):
                break
        assert isinstance(app.screen, BackupBrowseScreen)
        assert app.screen.query_one("#backup-browse-table", DataTable).row_count == 1
        assert tuple(item.reference for item in app.screen.items) == ("latest-project",)


@pytest.mark.asyncio
async def test_palette_attachment_fuzzy_and_exact_searches_execute_local_index(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    SearchIndex(context.paths.index_database).upsert_many(
        (
            IndexDocument(
                "attachment-hit",
                subject="Synthetic attachment",
                sender="registrar@example.test",
                attachment_names="registration-form.pdf",
                attachment_text="student enrollment confirmation",
                received_at=datetime(2026, 8, 20, tzinfo=UTC),
                size=3_000,
            ),
            IndexDocument(
                "body-only",
                subject="Student enrollment confirmation",
                body="The same words are only in ordinary mail text.",
            ),
        )
    )
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(120, 35)) as pilot:
        handler = app._palette_handlers()["attachments.search"]
        handler(
            CommandInvocation.from_mapping(
                "search.search_attachments",
                {"query": "student enrollment confirmation"},
            ),
            CommandContext(),
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, LocalSearchScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#local-search-table").row_count == 1:
                break
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-mode", Select).value == "attachments"
        assert app.screen.query_one("#local-search-table").row_count == 1

        app.screen.dismiss(None)
        await pilot.pause()
        handler(
            CommandInvocation.from_mapping(
                "search.search_attachments",
                {
                    "sender": "registrar",
                    "date_range": DateRange(
                        datetime(2026, 8, 1, tzinfo=UTC),
                        datetime(2026, 9, 1, tzinfo=UTC),
                        "UTC",
                        "August 2026",
                    ),
                    "size": SizeConstraint(2_000, 4_000, "between 2 and 4 KB"),
                    "attachment_type": "pdf",
                },
            ),
            CommandContext(),
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, LocalSearchScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#local-search-table").row_count == 1:
                break
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-table").row_count == 1

        app.screen.dismiss(None)
        await pilot.pause()
        exact_route = catalog_route("search.exact_search")
        app._catalog_search(
            exact_route,
            CommandInvocation.from_mapping(
                exact_route.command_id,
                {"query": "student enrollment confirmation"},
            ),
        )
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, LocalSearchScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#local-search-table").row_count == 2:
                break
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-exact", Checkbox).value
        assert not app.screen.query_one("#local-search-fuzzy", Checkbox).value

        app.screen.dismiss(None)
        await pilot.pause()
        fuzzy_route = catalog_route("search.fuzzy_search")
        app._catalog_search(
            fuzzy_route,
            CommandInvocation.from_mapping(
                fuzzy_route.command_id,
                {"query": "enrolment confirmtion"},
            ),
        )
        await pilot.pause()
        assert isinstance(app.screen, LocalSearchScreen)
        assert app.screen.query_one("#local-search-fuzzy", Checkbox).value
        assert not app.screen.query_one("#local-search-exact", Checkbox).value


@pytest.mark.asyncio
async def test_catalog_mail_search_preserves_clean_query_and_maps_field_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    captured: list[SearchQuery] = []

    async with app.run_test(size=(120, 35)) as pilot:
        monkeypatch.setattr(app, "_search_mail", lambda query, _label: captured.append(query))
        current = catalog_route("search.search_current_folder")
        app._catalog_search(
            current,
            CommandInvocation.from_mapping(current.command_id, {"query": "enrollment"}),
        )
        assert captured[-1].text == "enrollment"
        assert captured[-1].folder == app.ui.folder

        attachment = catalog_route("search.search_attachment_filename")
        app._catalog_search(
            attachment,
            CommandInvocation.from_mapping(attachment.command_id, {"query": "report.pdf"}),
        )
        assert captured[-1].attachment_name == "report.pdf"
        assert captured[-1].text is None

        importance = catalog_route("search.search_by_importance")
        app._catalog_search(
            importance,
            CommandInvocation.from_mapping(importance.command_id, {"query": "low"}),
        )
        assert captured[-1].importance is Importance.LOW
        assert captured[-1].text is None

        sender = catalog_route("search.search_sender")
        app._catalog_search(
            sender,
            CommandInvocation.from_mapping(sender.command_id, {"query": "teacher@example.test"}),
        )
        assert captured[-1].sender == "teacher@example.test"
        assert captured[-1].text is None

        date_route = catalog_route("search.search_by_date")
        day = datetime(2026, 8, 20, tzinfo=UTC)
        app._catalog_search(
            date_route,
            CommandInvocation.from_mapping(date_route.command_id, {"when": day}),
        )
        assert captured[-1].after == day
        assert captured[-1].before == day + timedelta(days=1)

        size_route = catalog_route("search.search_by_size")
        app._catalog_search(
            size_route,
            CommandInvocation.from_mapping(
                size_route.command_id,
                {"size": SizeConstraint(2_000, 4_000, "between 2 and 4 KB")},
            ),
        )
        assert captured[-1].minimum_size == 2_000
        assert captured[-1].maximum_size == 4_000

        expressions: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app,
            "_run_advanced_palette_search",
            lambda query, *, label: expressions.append((query, label)),
        )
        headers = catalog_route("search.search_headers")
        app._catalog_search(
            headers,
            CommandInvocation.from_mapping(headers.command_id, {"query": "X-Route: alpha"}),
        )
        assert expressions == [('header:"X-Route: alpha"', "Header search")]

        app._messages = (
            MessageSummary(
                reference="match",
                subject="Enrollment notice",
                sender=None,
                received_at=None,
            ),
            MessageSummary(
                reference="other",
                subject="Synthetic receipt",
                sender=None,
                received_at=None,
            ),
        )
        app._render_message_rows(app._messages)
        refine = catalog_route("search.search_within_results")
        app._catalog_direct(
            refine,
            CommandInvocation.from_mapping(refine.command_id, {"query": "enrollment"}),
        )
        await pilot.pause()
        assert [item.reference for item in app._messages] == ["match"]
        assert "without a network request" in str(app.query_one("#status-bar", Static).content)

        refreshes: list[bool] = []
        monkeypatch.setattr(app, "refresh_mailbox", lambda: refreshes.append(True))
        app._current_search_text = "subject:enrollment"
        app.ui.search = "enrollment"
        app.query_one("#message-filter", Input).value = "enrollment"
        clear = catalog_route("search.clear_search")
        app._catalog_direct(clear, CommandInvocation(clear.command_id))
        assert app._current_search_text == ""
        assert app.ui.search == ""
        assert app.query_one("#message-filter", Input).value == ""
        assert refreshes == [True]


@pytest.mark.asyncio
async def test_cleanup_search_routes_apply_review_only_age_and_classification_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    captured: list[tuple[SearchQuery, str]] = []

    async with app.run_test(size=(100, 30)):
        monkeypatch.setattr(
            app,
            "_search_mail",
            lambda query, label: captured.append((query, label)),
        )

        cases = (
            ("cleanup.find_old_unread_mail", "find old unread mail"),
            ("cleanup.find_stale_newsletters", "find stale newsletters"),
            (
                "cleanup.find_unnecessary_sent_attachments",
                "find unnecessary sent attachments",
            ),
        )
        for command_id, phrase in cases:
            route = catalog_route(command_id)
            app._catalog_search(
                route,
                CommandInvocation.from_mapping(
                    command_id,
                    {},
                    original_text=phrase,
                ),
            )

    old_unread, old_unread_label = captured[0]
    assert old_unread.text is None
    assert old_unread.is_read is False
    assert old_unread.before is not None
    # Age filters use local calendar days; UTC elapsed hours differ across DST.
    local_cutoff = datetime.now(ZoneInfo("Africa/Cairo")) - timedelta(days=30)
    assert abs(old_unread.before - local_cutoff) < timedelta(minutes=1)
    assert old_unread_label == "Old unread mail (older than 30 days)"

    newsletters, newsletter_label = captured[1]
    assert newsletters.text == "unsubscribe"
    assert newsletters.before is not None
    assert abs(
        newsletters.before - (datetime.now(ZoneInfo("Africa/Cairo")) - timedelta(days=90))
    ) < timedelta(minutes=1)
    assert "unsubscribe heuristic" in newsletter_label

    sent, sent_label = captured[2]
    assert sent.text is None
    assert sent.folder == "sent"
    assert sent.has_attachments is True
    assert sent.before is not None
    assert "review candidates" in sent_label


@pytest.mark.asyncio
async def test_cleanup_old_drafts_route_opens_the_persisted_local_age_view(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    route = catalog_route("cleanup.find_old_drafts")

    async with app.run_test(size=(100, 30)) as pilot:
        app._catalog_open(route, CommandInvocation(route.command_id))
        for _ in range(80):
            if isinstance(app.screen, DraftAttachmentScreen) and len(
                app.screen.query("#draft-age")
            ):
                age = app.screen.query_one("#draft-age", Select).value
                if age == "30":
                    break
            await pilot.pause(0.03)

        assert isinstance(app.screen, DraftAttachmentScreen)
        assert app.screen.query_one("#draft-age", Select).value == "30"


@pytest.mark.asyncio
async def test_search_category_routes_execute_calendar_contacts_and_tasks(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())

    async with app.run_test(size=(120, 35)) as pilot:
        handlers = app._palette_handlers()
        handlers["calendar.search"](
            CommandInvocation.from_mapping(
                "search.search_calendar",
                {"query": "Synthetic lecture"},
            ),
            CommandContext(),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#calendar-search", Input).value == "Synthetic lecture":
                break
        assert isinstance(app.screen, CalendarScreen)
        assert app.screen.query_one("#calendar-search", Input).value == "Synthetic lecture"
        app.screen.dismiss(None)
        await pilot.pause()

        handlers["contacts.search"](
            CommandInvocation.from_mapping(
                "search.search_contacts",
                {"query": "Synthetic Contact"},
            ),
            CommandContext(),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#contacts-search", Input).value == "Synthetic Contact":
                break
        assert isinstance(app.screen, ContactsScreen)
        assert app.screen.query_one("#contacts-search", Input).value == "Synthetic Contact"
        app.screen.dismiss(None)
        await pilot.pause()

        tasks = catalog_route("search.search_tasks")
        app._catalog_search(
            tasks,
            CommandInvocation.from_mapping(tasks.command_id, {"query": "Synthetic follow-up"}),
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, TasksScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#tasks-search", Input).value == "Synthetic follow-up":
                break
        assert isinstance(app.screen, TasksScreen)
        assert app.screen.query_one("#tasks-search", Input).value == "Synthetic follow-up"


@pytest.mark.asyncio
async def test_settings_opens_real_theme_and_branding_browsers(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(140, 42)) as pilot:
        app._open_area("settings")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        app.screen.query_one("#settings-more", Button).press()
        await pilot.pause()
        app.screen.query_one("#settings-browse-themes", Button).press()
        for _ in range(40):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, ThemeBrowserScreen) and app.screen.is_mounted
            ) and app.screen.query("#theme-category #label"):
                break
        assert isinstance(app.screen, ThemeBrowserScreen)
        await pilot.press("escape")
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, SettingsScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, SettingsScreen)
        app.screen.query_one("#settings-browse-branding", Button).press()
        for _ in range(40):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, BrandingBrowserScreen) and app.screen.is_mounted
            ) and app.screen.query("#branding-category #label"):
                break
        assert isinstance(app.screen, BrandingBrowserScreen)


@pytest.mark.asyncio
async def test_settings_palette_search_categories_transfer_and_confirmed_resets(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    service.set("appearance.density", "compact")
    service.set("appearance.banner_visible", False)
    app = GUCmailApp(reloaded_context(context), offline=True)
    handlers = app._palette_handlers()

    async with app.run_test(size=(140, 42)) as pilot:
        handlers["settings.open"](CommandInvocation("settings.open"), app._palette_context())
        for _ in range(40):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, SettingsScreen) and app.screen.is_mounted
            ) and app.screen.query_one("#settings-form", SettingsForm).row_count:
                break
        assert isinstance(app.screen, SettingsScreen)

        handlers["settings.search"](
            CommandInvocation.from_mapping(
                "settings.search",
                {"query": "row spacing"},
            ),
            app._palette_context(),
        )
        await pilot.pause()
        assert app.screen.query_one("#settings-search", Input).value == "row spacing"
        assert any(
            setting.setting_id == "appearance.density" for setting in app.screen._visible_settings
        )

        handlers["appearance.settings"](
            CommandInvocation("appearance.settings"), app._palette_context()
        )
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._selected_category == "Appearance"

        categories = {
            "settings.open_layout_settings": "Layout",
            "settings.open_mail_settings": "Mail",
            "settings.open_composer_settings": "Composer & Drafts",
            "settings.open_calendar_settings": "Calendar",
            "settings.open_storage_settings": "Storage",
            "settings.open_backup_settings": "Backup",
            "settings.open_search_settings": "Search & Indexing",
            "settings.open_connection_settings": "Connection",
            "settings.open_privacy_settings": "Privacy & Security",
            "settings.open_advanced_settings": "Advanced",
        }
        for command_id, category in categories.items():
            app._palette_catalog_handler(CommandInvocation(command_id), app._palette_context())
            await pilot.pause()
            assert app.screen._selected_category == category
            assert app.screen._visible_settings

        export_route = catalog_route("settings.export_settings")
        app._palette_catalog_handler(
            CommandInvocation(export_route.command_id), app._palette_context()
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, SettingsTransferScreen):
                payload = app.screen.query_one("#settings-transfer-payload", TextArea).text
                if payload:
                    break
        assert isinstance(app.screen, SettingsTransferScreen)
        exported = json.loads(app.screen.query_one("#settings-transfer-payload", TextArea).text)
        assert exported["application"] == "gucmail"
        assert "integrations.webhook_secret" not in exported["settings"]

        app.screen.dismiss(None)
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        import_route = catalog_route("settings.import_settings")
        app._palette_catalog_handler(
            CommandInvocation(import_route.command_id), app._palette_context()
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, SettingsTransferScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, SettingsTransferScreen)
        assert app.screen.query_one("#settings-transfer-mode", Select).value == "import"
        assert not app.screen.query_one("#settings-generate-export", Button).display
        assert app.screen.query_one("#settings-preview-import", Button).display
        assert not app.screen.query_one("#settings-transfer-payload", TextArea).read_only

        import_payload = {
            "schema_version": 1,
            "application": "gucmail",
            "settings": {"appearance.density": "spacious"},
        }
        app.screen.query_one("#settings-transfer-payload", TextArea).load_text(
            json.dumps(import_payload)
        )
        assert await pilot.click("#settings-preview-import")
        for _ in range(40):
            await pilot.pause(0.025)
            if not app.screen.query_one("#settings-apply-import", Button).disabled:
                break
        assert not app.screen.query_one("#settings-apply-import", Button).disabled
        assert await pilot.click("#settings-apply-import")
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, OperationalConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, OperationalConfirmationScreen)
        import_screen = next(
            screen for screen in app.screen_stack if isinstance(screen, SettingsTransferScreen)
        )
        assert await pilot.click("#operational-confirm-yes")
        await pilot.pause()
        import_worker = import_screen._active_worker
        if import_worker is not None:
            await import_worker.wait()
        await pilot.pause()
        for _ in range(40):
            await pilot.pause(0.025)
            if service.value("appearance.density").effective_value == "spacious":
                break
        assert service.value("appearance.density").effective_value == "spacious", (
            type(app.screen).__name__,
            str(app.screen.query_one("#area-state", Static).content),
        )

        # Persistence precedes the worker's notification/readback. Closing its
        # screen at the file-write boundary would discard that completion.
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen.action_close()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        for _ in range(60):
            await pilot.pause(0.025)
            if (
                not app.screen._operation_active
                and app.screen._catalog is not None
                and app.screen._catalog.revision == context.config_store.load_document().revision
            ):
                break
        reset_setting = catalog_route("settings.reset_setting")
        app._catalog_screen_action(
            reset_setting,
            CommandInvocation.from_mapping(
                reset_setting.command_id,
                {"setting": "appearance.density"},
            ),
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, OperationalConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, OperationalConfirmationScreen)
        assert await pilot.click("#operational-confirm-yes")
        await pilot.pause()
        await app.workers.wait_for_complete()
        for _ in range(40):
            await pilot.pause(0.025)
            if service.value("appearance.density").effective_value == "theme":
                break
        assert service.value("appearance.density").effective_value == "theme", (
            type(app.screen).__name__,
            str(app.screen.query_one("#area-state", Static).content),
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if (
                (isinstance(app.screen, SettingsScreen) and app.screen.is_mounted)
                and not app.screen._operation_active
                and app.screen._catalog is not None
                and app.screen._catalog.revision == context.config_store.load_document().revision
            ):
                break

        reset_category = catalog_route("settings.reset_category")
        app._catalog_screen_action(
            reset_category,
            CommandInvocation.from_mapping(
                reset_category.command_id,
                {"settings_category": "Appearance"},
            ),
        )
        for _ in range(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, OperationalConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, OperationalConfirmationScreen)
        assert await pilot.click("#operational-confirm-yes")
        await pilot.pause()
        await app.workers.wait_for_complete()
        for _ in range(40):
            await pilot.pause(0.025)
            if service.value("appearance.banner_visible").effective_value is True:
                break
        assert service.value("appearance.banner_visible").effective_value is True

        handlers["notifications.settings"](
            CommandInvocation("notifications.settings"), app._palette_context()
        )
        await pilot.pause()
        assert isinstance(app.screen, NotificationSettingsScreen)


def test_palette_preserves_runtime_context_and_plans_medium_confidence_confirmation() -> None:
    runtime = CommandContext.from_mapping(
        {"current_area": "mail", "selected_message": "m-demo"},
        online=True,
        expert_mode=True,
        services=("monitoring",),
        handler_ids=("search.mail",),
    )
    screen = PaletteScreen("Africa/Cairo", command_context=runtime)
    invocation = CommandInvocation.from_mapping(
        "search.mail",
        {"query": "ambiguous local phrase"},
        confidence=0.75,
    )

    assert screen.runtime_context == runtime
    plan = screen.engine.plan(invocation, context=screen.runtime_context)
    assert plan.availability.available is True
    assert plan.confirmation_required is True


def test_every_search_palette_command_has_an_executable_handler_and_truthful_scope(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    handlers = app._palette_handlers()
    commands = tuple(
        command for command in universal_palette_registry().all() if command.category == "search"
    )

    assert commands
    assert all((command.handler_id or command.command_id) in handlers for command in commands)
    local_ids = {
        "search.fuzzy_search",
        "search.exact_search",
        "search.search_attachments",
        "search.search_backups",
        "search.search_all_data",
        "search.semantic_search",
        "search.similar_message_search",
        "search.duplicate_message_search",
        "search.near_duplicate_search",
    }
    for command_id in local_ids:
        command = universal_palette_registry().get(command_id)
        assert command.offline
        assert not command.requires_connection
    for command_id in {
        "search.search_current_folder",
        "search.search_all_mail",
        "search.search_headers",
        "search.search_tasks",
    }:
        command = universal_palette_registry().get(command_id)
        assert command.requires_connection
        assert not command.offline


def test_palette_projects_every_supported_structured_search_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    captured: list[tuple[SearchQuery, str]] = []

    def capture(query: SearchQuery, label: str) -> None:
        captured.append((query, label))

    monkeypatch.setattr(app, "_search_mail", capture)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)
    invocation = CommandInvocation.from_mapping(
        "search.mail",
        {
            "query": "unread PDFs from Course Team last month",
            "sender": "teacher@example.test",
            "recipient": "student@example.test",
            "folder": "Inbox/Courses",
            "date_range": DateRange(start, end, "UTC", "last month"),
            "size": SizeConstraint(1_000, 2_000, "between 1 and 2 KB"),
            "attachment_type": "pdf",
            "read_state": "unread",
            "flagged": True,
            "important": True,
            "has_attachments": True,
            "categories": ("Course",),
            "internet_message_id": "<synthetic@example.test>",
        },
    )

    app._palette_search(invocation, CommandContext())

    query, label = captured[0]
    assert label == "unread PDFs from Course Team last month"
    assert query.text is None
    assert query.sender == "teacher@example.test"
    assert query.recipient == "student@example.test"
    assert query.folder == "Inbox/Courses"
    assert query.after == start
    assert query.before == end
    assert query.minimum_size == 1_000
    assert query.maximum_size == 2_000
    assert query.attachment_name == ".pdf"
    assert query.is_read is False
    assert query.flagged is True
    assert query.importance is Importance.HIGH
    assert query.has_attachments is True
    assert query.categories == ("Course",)
    assert query.internet_message_id == "<synthetic@example.test>"


def test_tui_product_area_loaders_use_bounded_service_contracts(tmp_path: Path) -> None:
    from dataclasses import replace

    gateway = FakeGateway()
    # This loader asks for the next 30 days from now, unlike the Calendar day's
    # midnight-to-midnight window. Keep its fixture inside that requested range.
    upcoming = datetime.now(UTC) + timedelta(hours=1)
    gateway._calendar_fixture = replace(
        gateway.get_event("event-demo"), start=upcoming, end=upcoming + timedelta(hours=1)
    )
    app = GUCmailApp(context_for(tmp_path), gateway=gateway, offline=True)

    assert "Synthetic lecture" in app._calendar_area()
    assert "Synthetic Contact" in app._contacts_area()
    assert "Synthetic follow-up" in app._tasks_area()
    assert app._scheduled_area() == "No local scheduled messages."
    assert app._backups_area() == "No local backups."
    assert "Mailbox total capacity is unknown unless Exchange reports it." in (app._storage_area())
    activity = app._activity_area()
    assert "Monitoring configuration: disabled" in activity
    assert "No local notification delivery history." in activity
    settings = app._settings_area()
    assert "mode:" in settings
    assert "demo@example.test" not in settings


@pytest.mark.asyncio
async def test_offline_product_area_never_resolves_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions = 0

    def forbidden_gateway(self: AppContext, **_kwargs: object) -> object:
        nonlocal resolutions
        resolutions += 1
        raise AssertionError("offline mode attempted to resolve a gateway")

    monkeypatch.setattr(AppContext, "gateway", forbidden_gateway)
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_area("calendar")
        await pilot.pause()

        assert resolutions == 0
        from gucmail.tui.offline_pim_screen import OfflinePimScreen

        assert isinstance(app.screen, OfflinePimScreen)
        assert app.screen.archive is None
        assert not app.screen._backups


@pytest.mark.asyncio
async def test_tui_opens_service_backed_pim_screens(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    cases = (
        ("calendar", CalendarScreen, "#calendar-table"),
        ("contacts", ContactsScreen, "#contacts-table"),
        ("tasks", TasksScreen, "#tasks-table"),
    )
    async with app.run_test(size=(120, 35)) as pilot:
        for area, screen_type, table_selector in cases:
            app._open_area(area)
            for _ in range(30):
                await pilot.pause(0.05)
                if app.screen.query_one(table_selector).row_count:  # type: ignore[attr-defined]
                    break
            assert isinstance(app.screen, screen_type)
            assert app.screen.query_one(table_selector).row_count == 1  # type: ignore[attr-defined]
            await pilot.press("escape")


@pytest.mark.asyncio
async def test_area_refresh_ignores_stale_worker_result(tmp_path: Path) -> None:
    first_started = Event()
    release_first = Event()
    call_lock = Lock()
    calls = 0

    def loader() -> str:
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            release_first.wait(timeout=5)
            return "stale result"
        return "fresh result"

    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            screen = AreaScreen("Synthetic race", loader)
            app.push_screen(screen)
            for _ in range(100):
                if first_started.is_set():
                    break
                await pilot.pause(0.01)
            assert first_started.is_set()

            screen.action_refresh()
            for _ in range(100):
                content = str(screen.query_one("#area-content", Static).content)
                if "fresh result" in content:
                    break
                await pilot.pause(0.01)
            assert "fresh result" in str(screen.query_one("#area-content", Static).content)

            release_first.set()
            await pilot.pause(0.1)
            assert "fresh result" in str(screen.query_one("#area-content", Static).content)
    finally:
        release_first.set()


@pytest.mark.asyncio
async def test_area_close_ignores_late_worker_result(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    def loader() -> str:
        started.set()
        release.wait(timeout=5)
        return "late result"

    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            screen = AreaScreen("Synthetic close", loader)
            app.push_screen(screen)
            for _ in range(100):
                if started.is_set():
                    break
                await pilot.pause(0.01)
            assert started.is_set()

            screen.action_close()
            await pilot.pause()
            assert app.screen is not screen

            release.set()
            await pilot.pause(0.1)
            assert app.screen is not screen
    finally:
        release.set()


@pytest.mark.asyncio
async def test_empty_composer_escape_and_backspace_close(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        assert isinstance(app.screen, ComposeScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ComposeScreen)

        await pilot.press("c")
        assert isinstance(app.screen, ComposeScreen)
        await pilot.press("backspace")
        await pilot.pause()
        assert not isinstance(app.screen, ComposeScreen)


@pytest.mark.asyncio
async def test_edited_composer_close_autosaves_and_confirms(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway(), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        composer = app.screen
        assert isinstance(composer, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        composer.query_one("#compose-subject", Input).value = "Recovered subject"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert list(app.context.paths.recovery_dir.glob("*.json"))

        await pilot.press("y")
        await pilot.pause()
        assert not isinstance(app.screen, ComposeScreen | ConfirmationScreen)


@pytest.mark.asyncio
async def test_composer_reopen_edit_and_recovery_preserve_server_identity(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    stored = drafts.create(
        DraftData(
            reference="dft-local-synthetic",
            to=[Address("synthetic@example.test")],
            subject="Before editing",
            body_text="Synthetic body",
            server_id="draft_remote_synthetic",
            server_change_key="current-server-key",
            location=DraftLocation.SYNCHRONIZED,
        )
    )
    stored = drafts.save(replace(stored, subject="Second revision"))
    app = GUCmailApp(context, gateway=FakeGateway(), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_compose(stored)
        await pilot.pause()
        composer = app.screen
        assert isinstance(composer, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        composer.query_one("#compose-subject", Input).value = "Edited in composer"
        payload = composer._payload(action="save")
        rebuilt = app._draft_from_payload(payload)
        assert rebuilt.server_id == stored.server_id
        assert rebuilt.server_change_key == stored.server_change_key
        assert rebuilt.revision == stored.revision
        assert rebuilt.location is DraftLocation.SYNCHRONIZED
        assert app.recover_composer_before_close(payload)
        reopened = drafts.read_recovery(stored.reference)
        assert reopened.server_id == stored.server_id
        assert reopened.server_change_key == stored.server_change_key
        assert reopened.subject == "Edited in composer"


@pytest.mark.asyncio
async def test_composer_save_does_not_overwrite_concurrent_revision(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    original = drafts.create(DraftData(reference="dft-concurrent", subject="Original"))
    app = GUCmailApp(context, gateway=FakeGateway(), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_compose(original)
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        await _ready_composer_for_editing(pilot)
        payload = app.screen._payload(action="save")
        drafts.save(replace(original, subject="Changed by another editor"))
        app.submit_composer(payload)
        await app.workers.wait_for_complete()
        assert drafts.get(original.reference).subject == "Changed by another editor"


@pytest.mark.asyncio
async def test_composer_duplicate_submit_sends_once_and_late_autosave_cannot_revive_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = Event(), Event()
    sent: list[str] = []

    class SendingGateway(FakeGateway):
        def send(self, draft: DraftData) -> dict[str, bool]:
            sent.append(draft.reference)
            started.set()
            assert release.wait(5)
            return {"accepted": True}

    context = context_for(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    draft = drafts.create(
        DraftData(
            reference="dft-submit",
            to=[Address("synthetic@example.test")],
            subject="Synthetic send",
            body_text="Synthetic body",
        )
    )
    app = GUCmailApp(context, gateway=SendingGateway())
    statuses: list[str] = []
    original_status = app._set_status

    def record_status(value: str) -> None:
        statuses.append(value)
        original_status(value)

    monkeypatch.setattr(app, "_set_status", record_status)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            app._open_compose(draft)
            await pilot.pause()
            assert isinstance(app.screen, ComposeScreen)
            await _ready_composer_for_editing(pilot)
            payload = app.screen._payload(action="send")
            plan = ComposeService().plan_send(app._draft_from_payload(payload))
            payload["confirmation_scope"] = plan.confirmation_scope
            app._write_composer_recovery(payload)
            submit_worker = app.submit_composer(payload)
            assert await asyncio.to_thread(started.wait, 5), (
                statuses,
                submit_worker.state,
                submit_worker.error,
            )
            app.submit_composer(payload)
            await pilot.pause(0.05)
            release.set()
            await app.workers.wait_for_complete()
            assert sent == [draft.reference]
            assert not drafts.exists(draft.reference)
            assert not drafts.recovery_path(draft.reference).exists()
            app._write_composer_recovery(payload)
            assert not drafts.recovery_path(draft.reference).exists()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_composer_discard_deletes_server_reference_before_removing_local_copy(
    tmp_path: Path,
) -> None:
    deleted: list[str] = []

    class DiscardGateway(FakeGateway):
        def delete_message(self, reference: str, permanent: bool = False) -> str:
            assert not permanent
            deleted.append(reference)
            return reference

    context = context_for(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    draft = drafts.create(
        DraftData(
            reference="dft-local-discard",
            subject="Synthetic",
            server_id="draft-server-discard",
            server_change_key="server-key",
            location=DraftLocation.SYNCHRONIZED,
        )
    )
    app = GUCmailApp(context, gateway=DiscardGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_compose(draft)
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        async with asyncio.timeout(15):
            while app.screen._attempt_pending:
                await asyncio.sleep(0.005)
        app._preview_discard_composer(app.screen)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        await pilot.press("y")
        await app.workers.wait_for_complete()
        assert deleted == [draft.server_id]
        assert not drafts.exists(draft.reference)


@pytest.mark.asyncio
async def test_persisted_preferences_reach_operational_screens(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    values = {
        "calendar.default_view": "week",
        "calendar.week_start": "sunday",
        "contacts.sort_order": "company",
        "tasks.default_view": "due",
        "tasks.show_completed": False,
        "search.fuzzy_enabled": False,
        "search.fuzzy_threshold": 91,
        "accessibility.reduced_motion": True,
    }
    for key, value in values.items():
        settings.set(key, value)
    app = GUCmailApp(context, gateway=FakeGateway())
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.animation_level == "none"
        app._open_area("calendar")
        await pilot.pause()
        assert app.screen.view_kind.value == "week"
        assert app.screen.first_weekday == 6
        assert app.screen.query_one("#calendar-view", Select).value == "week"
        app.pop_screen()
        await pilot.pause()
        app._open_area("contacts")
        await pilot.pause()
        app.screen._contacts_loaded(
            (
                ContactInfo(reference="one", display_name="A", company="Zulu"),
                ContactInfo(reference="two", display_name="B", company="Alpha"),
            )
        )
        assert [contact.reference for contact in app.screen._contacts] == ["two", "one"]
        app.pop_screen()
        await pilot.pause()
        app._open_area("tasks")
        await pilot.pause()
        assert app.screen.query_one("#tasks-view", Select).value == "due"
        app.screen._tasks_loaded(
            (
                TaskInfo(
                    reference="open",
                    subject="Due synthetic",
                    status="not_started",
                    due_date=datetime.now().date(),
                ),
                TaskInfo(
                    reference="complete",
                    subject="Completed synthetic",
                    status="completed",
                    percent_complete=100,
                    due_date=datetime.now().date(),
                ),
            )
        )
        assert [task.reference for task in app.screen._tasks] == ["open"]
        app.pop_screen()
        await pilot.pause()
        app._open_local_search()
        await pilot.pause()
        assert not app.screen.query_one("#local-search-fuzzy", Checkbox).value
        assert app.screen.minimum_score == 91


@pytest.mark.asyncio
async def test_compose_preferences_disable_automatic_recovery_and_server_sync(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("composer.local_recovery", False)
    settings.set("composer.server_draft_sync", False)
    settings.set("contacts.autocomplete", False)
    gateway = FakeGateway()
    app = GUCmailApp(context, gateway=gateway)
    draft = DraftData(
        reference="dft-preference",
        to=[Address("synthetic@example.test")],
        subject="Synthetic",
        body_text="Local only",
    )
    async with app.run_test(size=(120, 35)) as pilot:
        app._open_compose(draft)
        await pilot.pause()
        composer = app.screen
        await _ready_composer_for_editing(pilot)
        assert composer.query_one("#compose-to", Input).suggester is None
        composer._request_autosave()
        await pilot.pause()
        service = DraftService(context.state, context.paths.recovery_dir)
        assert not service.recovery_path(draft.reference).exists()
        worker = app.submit_composer(composer._payload(action="save"))
        await worker.wait()
        assert service.get(draft.reference).subject == "Synthetic"
        assert service.get(draft.reference).server_id is None


@pytest.mark.asyncio
async def test_mark_read_preference_reaches_gateway_only_when_enabled(tmp_path: Path) -> None:
    class ReadGateway(FakeGateway):
        def __init__(self):
            self.read_arguments = []
            self.marked = []

        def show_message(self, reference, mark_read=False):
            self.read_arguments.append(mark_read)
            return super().show_message(reference)

        def mark_read(self, reference, read=True):
            self.marked.append((reference, read))

    context = context_for(tmp_path)
    gateway = ReadGateway()
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(150):
            if app.ui.selected_reference == "m-demo" and app.ui.busy is None:
                break
            await pilot.pause(0.02)
        assert app.ui.selected_reference == "m-demo"
        app.load_message("m-demo")
        for _ in range(150):
            if app._message_detail_reference == "m-demo" and app.ui.busy is None:
                break
            await pilot.pause(0.02)
        assert app._message_detail_reference == "m-demo"
        await pilot.pause()
        assert gateway.read_arguments[-1] is False
        assert not gateway.marked
        SettingsService(context.config_store).set("mail.mark_read_on_open", True)
        app.load_message("m-demo")
        for _ in range(150):
            if gateway.marked and not app._automatic_read_inflight:
                break
            await pilot.pause(0.02)
        assert gateway.read_arguments == [False, False]
        assert gateway.marked == [("m-demo", True)]
        assert app._message_detail.summary.is_read


@pytest.mark.asyncio
async def test_search_scope_and_ascii_preferences_reach_ui(tmp_path: Path, monkeypatch) -> None:
    from gucmail.services.local_search import LocalSearchMode
    from gucmail.tui.themes import ColorCapability

    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("accessibility.unicode_fallback", True)
    settings.set("search.default_scope", "backups")
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app._apply_theme_structure(app.active_theme)
        assert app.query_one("#navigation").styles.border_left[0] in {"ascii", "none"}
        opened = []
        monkeypatch.setattr(app, "_open_local_search", lambda **kwargs: opened.append(kwargs))
        await pilot.press("/")
        assert opened[-1]["mode"] is LocalSearchMode.BACKUPS
        monkeypatch.setattr(app, "_terminal_color_capability", lambda: ColorCapability.COLORS_16)
        app._activate_theme(app.active_theme)
        from gucmail.tui.themes import adapt_theme

        assert app._runtime_theme == adapt_theme(app.active_theme, ColorCapability.COLORS_16)



async def _ready_composer_for_editing(pilot) -> ComposeScreen:
    """Wait for the actual happy-path edit state; never relax pending guards."""
    async with asyncio.timeout(15):
        while True:
            composer = pilot.app.screen
            if (
                isinstance(composer, ComposeScreen)
                and composer.is_mounted
                and composer._status_checked
                and not composer._attempt_pending
                and not composer._submission_pending
                and not composer._save_preflight_pending
                and not app_write_coordinator(pilot.app).pending
            ):
                break
            await pilot.pause(0.005)
    assert not composer._attempt_blocked
    assert not composer.query_one("#compose-to", Input).disabled
    return composer
