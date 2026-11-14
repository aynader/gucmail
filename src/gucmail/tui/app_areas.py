"""Main window: opening the other areas (calendar, contacts, backups, ...)."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Event
from typing import Any, cast
from zoneinfo import ZoneInfo

from textual import work
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Input,
)

from gucmail.commands import (
    CommandInvocation,
)
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.formatting import human_bytes
from gucmail.models import (
    DraftData,
    MessageSummary,
    NotificationEvent,
    SearchQuery,
)
from gucmail.notification_policies import MonitoringProfile
from gucmail.secret_store import SecretStore
from gucmail.security import sanitize_terminal
from gucmail.services.automatic_replies import AutomaticReplyService
from gucmail.services.backup import BackupService
from gucmail.services.calendar import CalendarService
from gucmail.services.calendar_display import CalendarDisplayPreferences
from gucmail.services.calendar_preferences import (
    CalendarOperationPreferences,
)
from gucmail.services.calendar_working_time import CalendarWorkingTime
from gucmail.services.contact_groups import ContactGroupService, LocalContactGroups
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import ContactsService
from gucmail.services.contacts_display import ContactsDisplayPreferences
from gucmail.services.delegates import DelegateService
from gucmail.services.drafts import DraftService
from gucmail.services.gateway import DeferredGateway as _DeferredGateway
from gucmail.services.inbox_rules import InboxRuleService
from gucmail.services.index import SearchIndex
from gucmail.services.local_search import LocalSearchMode, LocalSearchService
from gucmail.services.message_export import MessageExportService
from gucmail.services.messages import MessageService
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.notification_simulation import (
    NotificationSample,
    NotificationSimulationService,
)
from gucmail.services.notifications import (
    DeliveryResult,
    NotificationDispatcher,
    notification_event_schema,
)
from gucmail.services.ocr import OcrService
from gucmail.services.pim_preferences import CalendarCreationPreferences, TaskCreationPreferences
from gucmail.services.quota import (
    QuotaService,
    quota_service_from_settings,
)
from gucmail.services.recurring import RecurringService
from gucmail.services.scheduler import SchedulerService
from gucmail.services.semantic import SemanticStore, account_identity
from gucmail.services.semantic_search import SemanticSearchService
from gucmail.services.startup_runtime import StartupService
from gucmail.services.tasks import TaskService
from gucmail.services.tasks_display import TaskDisplayPreferences
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.settings import SettingsContext, SettingsService
from gucmail.tui.activity_screen import ActivityScreen
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _safe_failure
from gucmail.tui.app_palette import PaletteMixin
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from gucmail.tui.area_backends import (
    ActivityAreaBackend,
    BackupAreaBackend,
    SettingsAreaBackend,
    StorageAreaBackend,
)
from gucmail.tui.area_common import (
    OperationProgress,
    ProgressReporter,
)
from gucmail.tui.automatic_replies_screen import AutomaticRepliesScreen
from gucmail.tui.backup_screen import BackupScreen
from gucmail.tui.branding_screen import BrandingBrowserScreen
from gucmail.tui.contact_groups_screen import (
    ContactGroupsScreen,
)
from gucmail.tui.delegates_screen import DelegatesScreen
from gucmail.tui.diagnostics_screen import DiagnosticsScreen, LocalDiagnosticsBackend
from gucmail.tui.help_screen import HelpScreen
from gucmail.tui.inbox_rules_screen import InboxRulesScreen
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.local_search_screen import (
    LocalSearchScreen,
    ServiceLocalSearchBackend,
)
from gucmail.tui.notification_settings_screen import (
    NotificationSettingsScreen,
)
from gucmail.tui.ocr_screen import OcrScreen
from gucmail.tui.offline_pim_screen import OfflinePimScreen
from gucmail.tui.palette_routes import (
    RouteTarget,
)
from gucmail.tui.pim_screens import (
    CalendarScreen,
    CalendarServiceBackend,
    ContactsScreen,
    ContactsServiceBackend,
    CorrespondentsScreen,
    TasksScreen,
    TasksServiceBackend,
)
from gucmail.tui.saved_search_screen import (
    SavedSearchScreen,
    StateSavedSearchBackend,
)
from gucmail.tui.scheduled_screen import ScheduledScreen, SchedulerServiceBackend
from gucmail.tui.settings_screen import (
    SettingsScreen,
)
from gucmail.tui.shell_widgets import AreaScreen
from gucmail.tui.startup_screen import StartupScreen
from gucmail.tui.storage_screen import StorageScreen


class AreasMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _quota_service(self, gateway: Any) -> QuotaService:
        settings = SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )

        def setting(setting_id: str, fallback: object) -> object:
            try:
                return settings.value(setting_id).effective_value
            except ConfigurationError:
                return fallback

        return quota_service_from_settings(gateway, setting)

    def _calendar_area(self) -> str:
        view = CalendarService(
            self._ensure_gateway(),
            display_timezone=self.context.config.timezone,
            default_limit=min(self.context.config.page_size, 100),
        ).upcoming(days=30, limit=min(self.context.config.page_size, 100))
        lines = [
            f"{event.start.isoformat(sep=' ', timespec='minutes')}  "
            f"{event.subject or '(no subject)'}"
            + (f"  ·  {event.location}" if event.location else "")
            for event in view.events
        ]
        return self._bounded_lines(lines, empty="No events in the next 30 days.")

    def _contacts_area(self) -> str:
        contacts = ContactsService(
            self._ensure_gateway(),
            default_limit=min(self.context.config.page_size, 100),
        ).list(limit=min(self.context.config.page_size, 100))
        lines = [
            f"{contact.display_name or '(unnamed contact)'}"
            + (
                "  ·  " + ", ".join(address.email for address in contact.emails)
                if contact.emails
                else ""
            )
            for contact in contacts
        ]
        return self._bounded_lines(lines, empty="No contacts in this bounded view.")

    def _tasks_area(self) -> str:
        tasks = TaskService(
            self._ensure_gateway(),
            default_limit=min(self.context.config.page_size, 100),
        ).list(limit=min(self.context.config.page_size, 100))
        lines = [
            f"[{task.status}] {task.subject or '(no subject)'}"
            + (f"  ·  due {task.due_date.isoformat()}" if task.due_date else "")
            for task in tasks
        ]
        return self._bounded_lines(lines, empty="No tasks in this bounded view.")

    def _scheduler_service(self) -> SchedulerService:
        def sender(draft: DraftData, **_kwargs: Any) -> object:
            return self._ensure_gateway().send(draft)

        return SchedulerService(
            self.context.state,
            DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            ),
            sender,
            timezone=self.context.config.timezone,
            retry_attempts=self.context.config.retry_attempts,
            retry_base_seconds=self.context.config.retry_base_seconds,
            retry_max_seconds=self.context.config.retry_max_seconds,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )

    def _scheduled_area(self) -> str:
        records = self._scheduler_service().list(limit=100)
        lines = [
            f"[{record.message.state.value}] {record.message.scheduled_for.isoformat()}  "
            f"{record.message.reference}"
            for record in records
        ]
        return self._bounded_lines(lines, empty="No local scheduled messages.")

    def _backups_area(self) -> str:
        backups = BackupService(self.context.paths.backup_dir, state=self.context.state).list(
            limit=100,
            inspect_corrupt_sizes=False,
        )
        lines = [
            f"[{backup.state}] {backup.backup_id}  ·  {backup.item_count} item(s)  ·  "
            f"{human_bytes(backup.stored_bytes)}" + ("  ·  encrypted" if backup.encrypted else "")
            for backup in backups
        ]
        return self._bounded_lines(lines, empty="No local backups.")

    def _storage_area(self) -> str:
        reports = QuotaService.local_disk_usage(
            {
                "data": self.context.paths.data_dir,
                "cache": self.context.paths.cache_dir,
                "search index": self.context.paths.index_database,
                "backups": self.context.paths.backup_dir,
                "downloads": self.context.config.downloads,
            }
        )
        lines = [
            (
                f"{report.label}: {human_bytes(report.free_bytes)} free of "
                f"{human_bytes(report.total_bytes)}  ·  {report.path}"
                if report.available
                else f"{report.label}: unavailable"
            )
            for report in reports
        ]
        lines.append(
            "Mailbox total capacity is unknown unless Exchange reports it. "
            "Open Storage to refresh the bounded usage estimate and independent thresholds."
        )
        return "\n".join(lines)

    def _activity_area(self) -> str:
        history = NotificationDispatcher([], state=self.context.state).history(limit=100)
        self.context.state.initialize()
        with self.context.state.connection() as connection:
            checkpoints = connection.execute(
                """
                SELECT scope, last_success_at, last_error, updated_at
                FROM sync_checkpoints
                WHERE scope LIKE 'monitor:%'
                ORDER BY updated_at DESC LIMIT 20
                """
            ).fetchall()
        lines = [
            "Monitoring configuration: "
            + ("enabled" if self.context.config.monitoring_enabled else "disabled"),
            f"Configured poll interval: {self.context.config.poll_interval_seconds} seconds",
            (
                "Watcher strategy: detected when gucmail watch starts; "
                "no background process is assumed."
            ),
            f"Saved monitoring checkpoints: {len(checkpoints)} (showing up to 20)",
        ]
        lines.extend(
            f"- {str(row['scope']).rsplit(':', 1)[-1]}: "
            f"last success {row['last_success_at'] or 'never'}; "
            f"last error {'recorded (details omitted)' if row['last_error'] else 'none'}; "
            f"updated {row['updated_at'] or 'never'}"
            for row in checkpoints
        )
        lines.extend(("", "Recent notification outcomes (up to 100):"))
        notification_lines = [
            f"[{row['state']}] {row['created_at']}  {row['provider']}  {row['event_id']}"
            + (f"  ·  {row['detail']}" if row.get("detail") else "")
            for row in history
        ]
        lines.extend(notification_lines or ["No local notification delivery history."])
        return "\n".join(lines)

    def _contacts_backend(self) -> ContactsServiceBackend:
        from gucmail.services.contact_history import ContactHistoryIndex
        from gucmail.services.contact_preferences import ContactOperationPreferences

        page_size = min(max(self.context.config.page_size, 1), 200)
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        return ContactsServiceBackend(
            ContactsService(
                deferred_gateway,
                correspondence_gateway=deferred_gateway,
                default_limit=page_size,
                operation_preferences=lambda: ContactOperationPreferences.from_settings(
                    self._settings_service()
                ),
                history_index=ContactHistoryIndex(self.context.state),
            )
        )

    def _open_unanswered(self) -> None:
        from gucmail.services.unanswered import UnansweredService
        from gucmail.tui.unanswered_screen import UnansweredScreen

        def open_message(reference: str) -> None:
            self.ui.selected_reference = reference
            self.load_message(reference, mode="full")

        self.push_screen(
            UnansweredScreen(
                UnansweredService(_DeferredGateway(self._ensure_gateway)),
                address=self.context.config.email or "",
                open_message=open_message,
            )
        )

    def _open_correspondents(self, order: str) -> None:
        self.push_screen(
            CorrespondentsScreen(
                self._contacts_backend(),
                order=order,
                operation_preferences=lambda: ContactOperationPreferences.from_settings(
                    self._settings_service()
                ),
                page_size=min(max(self.context.config.page_size, 20), 100),
                offline=self.offline,
            )
        )

    def _open_backup_area(
        self,
        *,
        selected_messages: Sequence[str] = (),
        default_kind: str | None = None,
        default_folders: Sequence[str] = (),
        default_destination: str | None = None,
    ) -> None:
        online = not self.offline
        configured_destination = self._setting_value("backup.default_destination", None)
        backup_destination = (
            default_destination.strip()
            if isinstance(default_destination, str) and default_destination.strip()
            else str(configured_destination).strip()
            if isinstance(configured_destination, str) and configured_destination.strip()
            else str(self.context.paths.backup_dir)
        )
        configured_retention = self._setting_value("backup.retention_count", 5)
        backup_retention = (
            int(configured_retention) if isinstance(configured_retention, int | float) else 5
        )
        self.push_screen(
            BackupScreen(
                BackupAreaBackend(
                    backup_destination,
                    state=self.context.state,
                    source_factory=(lambda: self._ensure_gateway()) if online else None,
                ),
                default_destination=backup_destination,
                default_kind=(
                    "selected-messages"
                    if selected_messages
                    else default_kind or str(self._setting_value("backup.default_kind", "full"))
                ),
                default_encrypted=bool(self._setting_value("backup.encryption_enabled", False)),
                default_verify_after_create=bool(
                    self._setting_value("backup.verify_after_create", True)
                ),
                retention_count=backup_retention,
                default_folders=default_folders,
                available_folders=tuple(
                    dict.fromkeys(
                        ("inbox", "sent", "drafts", "deleted", "junk", *self._folder_paths.values())
                    )
                ),
                selected_messages=selected_messages,
                offline=self.offline,
            )
        )

    def _backup_repository_path(self) -> Path:
        configured = self._setting_value("backup.default_destination", None)
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve(strict=False)
        return self.context.paths.backup_dir

    def _open_local_search(
        self,
        *,
        mode: LocalSearchMode | str | None = None,
        query: str = "",
        reference: str | None = None,
        fuzzy: bool | None = None,
        exact: bool = False,
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        attachment_extension: str | None = None,
        backup_id: str | None = None,
        auto_run: bool = False,
    ) -> None:
        service = LocalSearchService(
            SearchIndex(self.context.paths.index_database),
            BackupService(self._backup_repository_path(), state=self.context.state),
            semantic_enabled=bool(self._setting_value("search.semantic_enabled", False)),
            semantic=self._semantic_service(),
        )
        self.push_screen(
            LocalSearchScreen(
                ServiceLocalSearchBackend(service),
                initial_mode=mode if mode is not None else self._last_search_scope,
                initial_query=query,
                initial_reference=reference,
                initial_fuzzy=(
                    bool(self._setting_value("search.fuzzy_enabled", True))
                    if fuzzy is None and not exact
                    else bool(fuzzy)
                ),
                minimum_score=int(str(self._setting_value("search.fuzzy_threshold", 82))),
                initial_exact=exact,
                initial_sender=sender,
                initial_after=after,
                initial_before=before,
                initial_minimum_size=minimum_size,
                initial_maximum_size=maximum_size,
                initial_attachment_extension=attachment_extension,
                initial_backup_id=backup_id,
                auto_run=auto_run,
            )
        )

    def _semantic_service(self) -> SemanticSearchService:
        return SemanticSearchService(
            SearchIndex(self.context.paths.index_database),
            SemanticStore(
                self.context.paths.data_dir / "semantic.sqlite3",
                identity=account_identity(
                    self.context.config.email or "", self.context.config.endpoint
                ),
                enabled=bool(self._setting_value("search.semantic_enabled", False)),
            ),
        )

    def action_local_ocr(self) -> None:
        self.push_screen(OcrScreen(OcrService()))

    def _close_runtime_for_cache_repair(self) -> None:
        """Release the reusable provider handle before clearing its derived identity cache."""

        if self._gateway_injected:
            reset = getattr(self.gateway, "reset_session", None)
            if callable(reset):
                reset()
        else:
            self.context.close()
            self.gateway = None

    def _open_area(self, area: str) -> None:
        if area == "layouts":
            self.action_layout_presets()
            return
        online = not self.offline
        page_size = min(max(self.context.config.page_size, 1), 200)
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        if area in {"drafts", "attachments"}:
            self._open_draft_attachment_workspace()
            return
        if area == "saved_searches":
            self._open_saved_searches()
            return
        if area == "local_search":
            self._open_local_search()
            return
        if self.offline and area in {"calendar", "contacts", "tasks"}:
            self.push_screen(
                OfflinePimScreen(
                    BackupService(self.context.paths.backup_dir, state=self.context.state),
                    area,
                    page_size=page_size,
                )
            )
            return
        if area == "calendar":
            try:
                display = CalendarDisplayPreferences.from_settings(self._settings_service())
                CalendarOperationPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Calendar preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                CalendarScreen(
                    CalendarServiceBackend(
                        CalendarService(
                            deferred_gateway,
                            display_timezone=display.timezone,
                            default_limit=page_size,
                        )
                    ),
                    timezone=display.timezone,
                    display_preferences=lambda: CalendarDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    operation_preferences=lambda: CalendarOperationPreferences.from_settings(
                        self._settings_service()
                    ),
                    creation_preferences=lambda: CalendarCreationPreferences.from_settings(
                        self._settings_service()
                    ),
                    working_time_preferences=lambda: CalendarWorkingTime.from_settings(
                        self._settings_service()
                    ),
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "delegates":
            if self.offline:
                self._set_status("Mailbox delegates require an Exchange connection.")
                return
            self.push_screen(
                DelegatesScreen(DelegateService(deferred_gateway), page_size=page_size)
            )
            return
        if area == "inbox_rules":
            if self.offline:
                self._set_status("Inbox rules require a live Exchange connection.")
                return
            self.push_screen(InboxRulesScreen(InboxRuleService(deferred_gateway)))
            return
        if area == "automatic_replies":
            if self.offline:
                self._set_status("Automatic replies require a live Exchange connection.")
                return
            self.push_screen(AutomaticRepliesScreen(AutomaticReplyService(deferred_gateway)))
            return
        if area == "contact_groups":
            self.push_screen(
                ContactGroupsScreen(
                    ContactGroupService(LocalContactGroups(self.context.state)),
                    ContactGroupService(deferred_gateway) if online else None,
                    page_size=page_size,
                )
            )
            return
        if area == "contacts":
            try:
                ContactsDisplayPreferences.from_settings(self._settings_service())
                ContactOperationPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Contacts preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                ContactsScreen(
                    self._contacts_backend(),
                    display_preferences=lambda: ContactsDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    operation_preferences=lambda: ContactOperationPreferences.from_settings(
                        self._settings_service()
                    ),
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "tasks":
            from gucmail.services.task_history import TaskHistory

            try:
                task_display = TaskDisplayPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Task preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                TasksScreen(
                    TasksServiceBackend(
                        TaskService(deferred_gateway, default_limit=page_size),
                        show_completed=task_display.show_completed,
                        timezone=task_display.timezone,
                    ),
                    history_reader=TaskHistory(self.context.state).page,
                    view=task_display.default_view.value,
                    timezone=task_display.timezone,
                    creation_preferences=lambda: TaskCreationPreferences.from_settings(
                        self._settings_service()
                    ),
                    display_preferences=lambda: TaskDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    show_completed=task_display.show_completed,
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "backups":
            self._open_backup_area()
            return
        if area == "storage":
            self.push_screen(
                StorageScreen(
                    StorageAreaBackend.from_context(self.context, online=online),
                    offline=self.offline,
                    analysis_limit=min(max(self.context.config.page_size, 25), 200),
                )
            )
            return
        if area == "diagnostics":
            self._open_diagnostics_area()
            return
        if area == "settings":
            self.push_screen(
                SettingsScreen(
                    SettingsAreaBackend.from_context(
                        self.context,
                        preview_handlers={
                            "layout.message_columns": self._preview_message_columns,
                            "appearance.theme": self._preview_settings_theme,
                        },
                        on_applied=self._settings_applied_callback,
                    ),
                    offline=self.offline,
                )
            )
            return
        if area == "startup":
            self.push_screen(StartupScreen(StartupService(self.context)))
            return
        if area == "notifications":
            webhook_secrets = SecretStore("webhooks")
            self.push_screen(
                NotificationSettingsScreen(
                    NotificationConfigurationService(self.context.state),
                    webhook_registry=WebhookRegistry(
                        self.context.state,
                        secret_store=webhook_secrets,
                    ),
                    webhook_delivery=None if self.offline else self._deliver_webhook_test,
                    recent_simulation_source=None if self.offline else self._recent_rule_sample,
                    automatic_replies_available=not self.offline,
                    desktop_setup=self._open_desktop_setup,
                )
            )
            return
        if area == "activity":
            self.push_screen(
                ActivityScreen(
                    ActivityAreaBackend.from_context(self.context, offline=self.offline),
                    offline=self.offline,
                    timezone=self.context.config.timezone,
                    datetime_format=self.context.config.datetime_format,
                )
            )
            return
        if area == "scheduled":
            scheduler = self._scheduler_service()
            self.push_screen(
                ScheduledScreen(
                    SchedulerServiceBackend(
                        scheduler,
                        online=online,
                        recurring=RecurringService(
                            self.context.state,
                            scheduler.drafts,
                            scheduler,
                            automation_enabled=True,
                        ),
                    ),
                    page_size=min(max(self.context.config.page_size, 50), 500),
                    offline=self.offline,
                )
            )
            return
        if area == "themes":
            self.push_screen(
                ThemeBrowserScreen(self.theme_browser_backend),
                lambda _result: self._refresh_appearance_settings(),
            )
            return
        if area == "branding":
            self.push_screen(
                BrandingBrowserScreen(self.branding_browser_backend),
                lambda _result: self._refresh_appearance_settings(),
            )
            return
        loaders: dict[str, tuple[str, Callable[[], str]]] = {}
        try:
            title, loader = loaders[area]
        except KeyError:
            self._set_status(f"Unknown application area: {sanitize_terminal(area)}")
            return
        self.push_screen(AreaScreen(title, loader))

    def _open_help(
        self,
        *,
        initial_page_id: str | None = None,
        initial_query: str = "",
        active_view: str | None = None,
    ) -> None:
        active = (
            active_view or type(self.screen).__name__.removesuffix("Screen").casefold() or "mail"
        )
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context(active),
                initial_page_id=initial_page_id,
                initial_query=initial_query,
                store=self._help_state_store(),
            )
        )

    def _open_tutorials(self, lesson_id: str = "navigation") -> None:
        from gucmail.help.tutorials import TutorialProgress
        from gucmail.tui.tutorial_screen import TutorialScreen

        self.push_screen(
            TutorialScreen(
                TutorialProgress(self.context.state),
                initial_lesson=lesson_id,
                timezone=self.context.config.timezone,
            )
        )

    @work(thread=True, exclusive=True, group="pim-related-mail")
    def _search_calendar_related_messages(self, references: tuple[str, ...]) -> None:
        try:
            CalendarOperationPreferences.from_settings(self._settings_service()).require_related()
            references = tuple(dict.fromkeys(references))[:5]
            if not references:
                return
            gateway = self._ensure_gateway()
            calendar = CalendarService(
                gateway,
                display_timezone=self.context.config.timezone,
                default_limit=min(max(self.context.config.page_size, 1), 100),
            )
            messages = MessageService(gateway)
            result_limit = min(max(self.context.config.page_size, 1), 100)
            per_event_limit = max(1, result_limit // len(references))
            found: dict[str, MessageSummary] = {}
            subjects: list[str] = []
            summaries: list[str] = []
            for reference in references:
                CalendarOperationPreferences.from_settings(
                    self._settings_service()
                ).require_related()
                value = calendar.get(reference)
                subject = value.subject.strip()
                if not subject:
                    continue
                subjects.append(subject)
                CalendarOperationPreferences.from_settings(
                    self._settings_service()
                ).require_related()
                page = messages.search_messages(
                    SearchQuery(subject=subject),
                    limit=per_event_limit,
                )
                summaries.append(page.execution_summary)
                for message in page.items:
                    found.setdefault(message.reference, message)
                    if len(found) >= result_limit:
                        break
                if len(found) >= result_limit:
                    break
            if not subjects:
                self.call_from_thread(
                    self._set_status,
                    "Selected calendar events have no subject to match against mail.",
                )
                return
            label = (
                f"Related mail for {subjects[0]}"
                if len(subjects) == 1
                else f"Related mail for {len(subjects)} selected events"
            )
            execution = f"{len(subjects)} bounded server-side subject search(es); " + (
                summaries[-1] if summaries else "no residual evaluation"
            )
            self.call_from_thread(
                self._render_calendar_related_search,
                tuple(found.values()),
                label,
                execution,
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Related-mail search unavailable"),
            )

    def _render_calendar_related_search(
        self, messages: tuple[MessageSummary, ...], label: str, execution: str
    ) -> None:
        try:
            CalendarOperationPreferences.from_settings(self._settings_service()).require_related()
        except ConfigurationError as exc:
            self._set_status(sanitize_terminal(str(exc)))
            return
        self._render_palette_search(messages, label, execution)

    def _open_storage_analysis(self, analysis_id: str) -> None:
        screen = self._route_screen(RouteTarget.STORAGE)
        if isinstance(screen, StorageScreen):
            screen.queue_analysis(analysis_id)
            return
        self._open_area("storage")

        def analyze() -> None:
            mounted = self._route_screen(RouteTarget.STORAGE)
            if isinstance(mounted, StorageScreen):
                mounted.queue_analysis(analysis_id)

        self.set_timer(0.08, analyze)

    def _open_activity_with_control(self, control: str) -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_watcher_control(control)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_watcher_control(control)

        self.set_timer(0.08, execute)

    def _open_activity_with_notification_test(self, provider: str) -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_notification_test(provider)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_notification_test(provider)

        self.set_timer(0.08, execute)

    def _recent_rule_sample(
        self, profile: MonitoringProfile, cancel: Event, progress: ProgressReporter
    ) -> NotificationSample:
        service = NotificationSimulationService(
            MessageService(self._ensure_gateway(), state=self.context.state),
            account_id=self.context.config.email or "configured-account",
        )
        return service.recent(
            profile,
            cancel=cancel,
            progress=lambda completed, total: progress(
                OperationProgress(
                    "Sampling recent mail metadata",
                    completed=completed,
                    total=total,
                )
            ),
        )

    def _deliver_webhook_test(
        self,
        config: WebhookConfig,
        event: NotificationEvent,
    ) -> DeliveryResult:
        webhook_secrets = SecretStore("webhooks")
        with WebhookNotificationProvider(
            replace(config, enabled=True),
            state=self.context.state,
            secret_store=webhook_secrets,
        ) as provider:
            return provider.notify(event)

    def _open_notification_webhook_action(
        self,
        action: str,
        *,
        name: str | None = None,
        provider_kind: str | None = None,
    ) -> None:
        def execute(mounted: Screen[Any] | None = None) -> None:
            mounted = mounted or self._route_screen(RouteTarget.NOTIFICATIONS)
            if not isinstance(mounted, NotificationSettingsScreen):
                return
            if action == "test":
                mounted.action_test_webhook(name, provider_kind, confirmed=True)
            elif action == "show":
                mounted.action_show_webhook_configuration(name)

        current = self._route_screen(RouteTarget.NOTIFICATIONS)
        if isinstance(current, NotificationSettingsScreen):
            execute(current)
            return
        self._open_area("notifications")
        self.set_timer(0.08, execute)

    def _open_activity_with_delivery_retry(self, category: str = "webhook") -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_delivery_retry(category)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_delivery_retry(category)

        self.set_timer(0.08, execute)

    def _open_activity_filtered(self, category: str, text: str = "") -> None:
        def apply_filter(mounted: Screen[Any] | None = None) -> None:
            mounted = mounted or self._route_screen(RouteTarget.ACTIVITY)
            if not isinstance(mounted, ActivityScreen):
                return
            mounted.query_one("#activity-category", Select).value = category
            mounted.query_one("#activity-search", Input).value = text
            mounted.action_refresh()

        current = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(current, ActivityScreen):
            apply_filter(current)
            return
        self._open_area("activity")
        self.set_timer(0.08, apply_filter)

    def action_copy_webhook_event_schema(self) -> None:
        document = json.dumps(notification_event_schema(), ensure_ascii=False, indent=2)
        self.copy_to_clipboard(document)
        self._set_status("Copied webhook event JSON Schema to the clipboard")

    def _open_saved_searches(
        self,
        *,
        mode: str = "saved",
        create: bool = False,
    ) -> None:
        initial_query = self._current_search_text
        if not initial_query:
            try:
                page_filter = self.query_one("#message-filter", Input).value.strip()
            except NoMatches:
                page_filter = ""
            if page_filter:
                initial_query = f'text:"{page_filter.replace(chr(34), chr(92) + chr(34))}"'
        self.push_screen(
            SavedSearchScreen(
                StateSavedSearchBackend(
                    self.context.state,
                    timezone=self.context.config.timezone,
                    history_enabled=bool(self._setting_value("search.history_enabled", True)),
                ),
                initial_query=initial_query,
                mode=mode,
                create_on_mount=create,
            )
        )

    def _open_diagnostics_area(self, initial_action: str | None = None) -> None:
        online = not self.offline
        self.push_screen(
            DiagnosticsScreen(
                LocalDiagnosticsBackend(
                    self.context,
                    gateway_factory=self._ensure_gateway if online else None,
                    close_runtime=self._close_runtime_for_cache_repair,
                ),
                default_export_path=str(self.context.config.downloads / "gucmail-diagnostics.json"),
                default_folder=self.ui.folder,
                offline=self.offline,
                initial_action=initial_action,
            )
        )

    def _open_diagnostics_view(self, view: str) -> None:
        current = self._route_screen(RouteTarget.DIAGNOSTICS)
        if isinstance(current, DiagnosticsScreen):
            current.configure_view(view)
            return
        self._open_area("diagnostics")

        def configure() -> None:
            screen = self._route_screen(RouteTarget.DIAGNOSTICS)
            if isinstance(screen, DiagnosticsScreen):
                screen.configure_view(view)

        self.set_timer(0.05, configure)

    def _open_desktop_setup(self) -> None:
        from gucmail.services.windows_desktop import desktop_setup_service
        from gucmail.tui.desktop_setup_screen import DesktopSetupScreen

        self.push_screen(
            DesktopSetupScreen(lambda operation: desktop_setup_service(self.context, operation))
        )

    def _calendar_invocation_date(self, invocation: CommandInvocation) -> date | None:
        zone = ZoneInfo(self.context.config.timezone)
        for key in ("when", "date", "date_range"):
            value = invocation.get(key)
            if isinstance(value, datetime):
                return value.astimezone(zone).date() if value.tzinfo else value.date()
            if isinstance(value, date):
                return value
            for attribute in ("utc", "start"):
                candidate = getattr(value, attribute, None)
                if isinstance(candidate, datetime):
                    return (
                        candidate.astimezone(zone).date() if candidate.tzinfo else candidate.date()
                    )
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value.strip())
                except ValueError:
                    pass
        return None

    @staticmethod
    def _contacts_invocation_query(invocation: CommandInvocation) -> str:
        query = (
            PaletteMixin._palette_scalar(invocation.get("query"))
            or PaletteMixin._palette_scalar(invocation.get("person"))
            or PaletteMixin._palette_scalar(invocation.get("email"))
            or invocation.original_text
            or ""
        ).strip()
        for prefix in (
            "search contacts for ",
            "search contacts ",
            "search directory for ",
            "search directory ",
            "resolve email address for ",
            "resolve email address ",
        ):
            if query.casefold().startswith(prefix):
                return query[len(prefix) :].strip()
        return query

    def _message_mutation_service(self) -> MessageService:
        return MessageService(cast(Any, _DeferredGateway(self._ensure_gateway)))

    def _message_export_service(self) -> MessageExportService:
        maximum = max(int(self.context.config.max_attachment_bytes), 1)
        return MessageExportService(
            cast(Any, _DeferredGateway(self._ensure_gateway)),
            maximum_message_bytes=maximum,
            maximum_total_bytes=max(maximum, 1024 * 1024 * 1024),
        )
