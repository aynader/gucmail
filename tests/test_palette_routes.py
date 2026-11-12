from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from gucmail.command_catalog import CORE_COMMANDS
from gucmail.models import SafetyClass
from gucmail.tui.activity_screen import ActivityScreen
from gucmail.tui.app import ComposeScreen, GUCmailApp
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from gucmail.tui.backup_screen import BackupScreen
from gucmail.tui.diagnostics_screen import DiagnosticsScreen
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from gucmail.tui.local_search_screen import LocalSearchScreen
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from gucmail.tui.palette_routes import (
    CATALOG_ROUTES,
    PaletteRoute,
    RouteKind,
    RouteTarget,
    catalog_handler_ids,
    catalog_route,
    routes_for_handler,
    validate_catalog_routes,
)
from gucmail.tui.pim_screens import CalendarScreen, ContactsScreen, TasksScreen
from gucmail.tui.scheduled_screen import ScheduledScreen
from gucmail.tui.settings_screen import SettingsScreen
from gucmail.tui.storage_screen import StorageScreen


def _catalog_commands():
    return tuple(
        command
        for command in CORE_COMMANDS.all()
        if (command.handler_id or "").startswith("catalog.")
    )


def test_every_catalog_command_has_one_validated_route() -> None:
    commands = _catalog_commands()
    assert len(commands) >= 300
    assert set(CATALOG_ROUTES) == {command.command_id for command in commands}
    assert validate_catalog_routes() == ()


def test_route_preserves_canonical_safety_and_confirmation() -> None:
    for command in _catalog_commands():
        route = CATALOG_ROUTES[command.command_id]
        assert route.safety is command.safety
        assert route.confirmation_required is command.confirmation_required

    destructive = {
        command.command_id
        for command in _catalog_commands()
        if command.safety in {SafetyClass.DESTRUCTIVE, SafetyClass.PRIVILEGED}
    }
    assert destructive
    assert all(CATALOG_ROUTES[command_id].confirmation_required for command_id in destructive)


def test_backup_palette_policy_matches_real_online_and_restore_effects() -> None:
    online_local_writes = {
        "backup.create_backup",
        "backup.create_full_backup",
        "backup.create_incremental_backup",
        "backup.back_up_current_folder",
        "backup.back_up_selected_messages",
        "backup.back_up_search_results",
        "backup.back_up_cluster",
        "backup.resume_backup",
    }
    for command_id in online_local_writes:
        command = CORE_COMMANDS.get(command_id)
        assert command.requires_connection and not command.offline
        assert command.changes_local and not command.changes_server
        assert command.confirmation_required

    estimate = CORE_COMMANDS.get("backup.estimate_backup_size")
    assert estimate.requires_connection and not estimate.offline
    assert not estimate.changes_local and not estimate.changes_server

    for command_id in ("backup.pause_backup", "backup.cancel_backup"):
        command = CORE_COMMANDS.get(command_id)
        assert command.offline and not command.requires_connection
        assert command.changes_local and not command.changes_server
        assert not command.confirmation_required

    restore = CORE_COMMANDS.get("backup.restore_selected_items")
    assert restore.requires_connection and not restore.offline
    assert restore.changes_server and restore.confirmation_required


def test_storage_palette_keeps_local_and_configured_fallback_views_offline() -> None:
    offline_views = {
        "storage.open",
        "storage.show_mailbox_quota",
        "storage.show_local_cache_size",
        "storage.show_search_index_size",
        "storage.show_backup_storage",
    }
    for command_id in offline_views:
        command = CORE_COMMANDS.get(command_id)
        assert command.offline and not command.requires_connection

    online_views = {
        "storage.show_mailbox_usage",
        "storage.show_remaining_mailbox_space",
        "storage.show_storage_by_folder",
        "storage.show_storage_by_sender",
        "storage.show_storage_by_date",
        "storage.show_storage_by_attachment_type",
        "storage.analyze_reclaimable_space",
    }
    for command_id in online_views:
        command = CORE_COMMANDS.get(command_id)
        assert command.requires_connection and not command.offline


def test_cleanup_palette_policy_matches_analysis_and_server_mutation_effects() -> None:
    local_views = {
        "cleanup.find_duplicate_attachments",
        "cleanup.find_old_drafts",
    }
    for command_id in local_views:
        command = CORE_COMMANDS.get(command_id)
        assert command.offline and not command.requires_connection
        assert command.safety is SafetyClass.READ_ONLY
        assert not command.changes_local and not command.changes_server

    online_analysis = {
        "cleanup.analyze_mailbox",
        "cleanup.find_large_messages",
        "cleanup.find_duplicate_messages",
        "cleanup.find_old_unread_mail",
        "cleanup.find_stale_newsletters",
        "cleanup.find_unnecessary_sent_attachments",
        "cleanup.find_largest_senders",
        "cleanup.estimate_reclaimable_space",
        "cleanup.create_cleanup_plan",
        "cleanup.preview_cleanup_plan",
    }
    for command_id in online_analysis:
        command = CORE_COMMANDS.get(command_id)
        assert command.requires_connection and not command.offline
        assert command.safety is SafetyClass.READ_ONLY
        assert not command.changes_local and not command.changes_server
        assert not command.confirmation_required

    for command_id in {
        "cleanup.execute_cleanup_plan",
        "cleanup.undo_recent_move_delete_when_possible",
    }:
        command = CORE_COMMANDS.get(command_id)
        assert command.requires_connection and not command.offline
        assert command.safety is SafetyClass.MUTATING
        assert command.changes_server and not command.changes_local
        assert command.confirmation_required


def test_monitoring_palette_policy_separates_local_control_from_online_work() -> None:
    for command_id in {
        "monitoring.monitor_inbox",
        "monitoring.monitor_folder",
        "monitoring.monitor_sender",
        "monitoring.monitor_subject",
        "monitoring.monitor_important_messages",
        "monitoring.monitor_messages_with_attachments",
        "monitoring.pause_monitoring",
    }:
        command = CORE_COMMANDS.get(command_id)
        assert command.offline and not command.requires_connection
        assert command.safety is SafetyClass.MUTATING
        assert command.changes_local and not command.changes_server

    for command_id in {
        "monitoring.status",
        "monitoring.show_last_synchronization",
        "monitoring.show_watcher_logs",
    }:
        command = CORE_COMMANDS.get(command_id)
        assert command.offline and not command.requires_connection
        assert command.safety is SafetyClass.READ_ONLY
        assert not command.changes_local and not command.changes_server

    for command_id in {
        "monitoring.resume_monitoring",
        "monitoring.force_synchronization",
    }:
        command = CORE_COMMANDS.get(command_id)
        assert command.requires_connection and not command.offline
        assert command.safety is SafetyClass.MUTATING
        assert command.changes_local and not command.changes_server

    start = CORE_COMMANDS.get("monitoring.start")
    assert start.requires_connection and not start.offline
    stop = CORE_COMMANDS.get("monitoring.stop")
    assert stop.offline and not stop.requires_connection


def test_notification_palette_routes_mutate_runtime_configuration_not_generic_settings() -> None:
    expected_actions = {
        "notifications.enable_desktop_notifications": "set_desktop_notifications",
        "notifications.disable_desktop_notifications": "set_desktop_notifications",
        "notifications.mute_notifications": "mute_notifications",
        "notifications.enable_quiet_hours": "enable_quiet_hours",
        "notifications.hide_notification_previews": "set_notification_privacy",
        "notifications.show_sender_only": "set_notification_privacy",
        "notifications.show_full_preview": "set_notification_privacy",
    }
    for command_id, action in expected_actions.items():
        route = catalog_route(command_id)
        command = CORE_COMMANDS.get(command_id)
        assert route.kind is RouteKind.SCREEN_ACTION
        assert route.target is RouteTarget.NOTIFICATIONS
        assert route.action == action
        assert command.offline and not command.requires_connection
        assert command.changes_local and not command.changes_server


def test_webhook_palette_routes_use_operational_surfaces_and_copy_action() -> None:
    expected_screen_actions = {
        "integrations.add_webhook": (RouteTarget.NOTIFICATIONS, "configure_webhook"),
        "integrations.remove_webhook": (RouteTarget.NOTIFICATIONS, "remove_webhook"),
        "integrations.add_discord_webhook": (
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
        ),
        "integrations.add_slack_compatible_webhook": (
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
        ),
        "integrations.configure_teams_webhook": (
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
        ),
        "integrations.retry_failed_webhook": (RouteTarget.ACTIVITY, "retry_delivery"),
        "integrations.pause_webhook": (RouteTarget.NOTIFICATIONS, "pause_webhook"),
        "integrations.resume_webhook": (RouteTarget.NOTIFICATIONS, "resume_webhook"),
        "integrations.show_webhook_configuration": (
            RouteTarget.NOTIFICATIONS,
            "show_webhook_configuration",
        ),
    }
    for command_id, (target, action) in expected_screen_actions.items():
        route = catalog_route(command_id)
        assert route.kind is RouteKind.SCREEN_ACTION
        assert route.target is target
        assert route.action == action

    history = catalog_route("integrations.show_webhook_delivery_history")
    assert history.kind is RouteKind.OPEN_SCREEN
    assert history.target is RouteTarget.ACTIVITY
    assert history.filter_values() == {"category": "webhook"}
    schema = catalog_route("integrations.copy_webhook_event_schema")
    assert schema.kind is RouteKind.APPLICATION_ACTION
    assert schema.action == "copy_webhook_event_schema"


def test_routes_have_no_placeholder_fallback() -> None:
    forbidden = ("not implemented", "todo", "placeholder", "generic screen")
    for route in CATALOG_ROUTES.values():
        rendered = " ".join(
            value
            for value in (
                route.action,
                route.intent,
                route.query,
                route.help_topic,
                route.settings_category,
                route.setting_id,
                route.capability,
                route.config_key,
                route.gate_reason,
            )
            if value
        ).casefold()
        assert not any(term in rendered for term in forbidden), route.command_id
        if route.kind is RouteKind.CAPABILITY_GATE:
            assert route.capability
            assert route.gate_reason
            assert len(route.gate_reason.split()) >= 7


def test_screen_and_application_actions_name_real_methods() -> None:
    screen_types = {
        RouteTarget.BACKUP: BackupScreen,
        RouteTarget.ATTACHMENTS: DraftAttachmentScreen,
        RouteTarget.DRAFTS: DraftAttachmentScreen,
        RouteTarget.CALENDAR: CalendarScreen,
        RouteTarget.CONTACTS: ContactsScreen,
        RouteTarget.COMPOSER: ComposeScreen,
        RouteTarget.TASKS: TasksScreen,
        RouteTarget.SCHEDULED: ScheduledScreen,
        RouteTarget.STORAGE: StorageScreen,
        RouteTarget.ACTIVITY: ActivityScreen,
        RouteTarget.SETTINGS: SettingsScreen,
        RouteTarget.THEMES: ThemeBrowserScreen,
        RouteTarget.DIAGNOSTICS: DiagnosticsScreen,
        RouteTarget.NOTIFICATIONS: NotificationSettingsScreen,
        RouteTarget.LOCAL_SEARCH: LocalSearchScreen,
    }
    for route in CATALOG_ROUTES.values():
        if route.kind is RouteKind.SCREEN_ACTION:
            assert route.action_method is not None
            assert hasattr(screen_types[route.target], route.action_method), route.command_id
        elif route.kind is RouteKind.APPLICATION_ACTION:
            assert route.action_method is not None
            assert hasattr(GUCmailApp, route.action_method), route.command_id


def test_handler_partitions_are_complete_and_deterministic() -> None:
    expected_handlers = {
        command.handler_id for command in _catalog_commands() if command.handler_id is not None
    }
    assert catalog_handler_ids() == expected_handlers
    seen: set[str] = set()
    for handler_id in sorted(expected_handlers):
        routes = routes_for_handler(handler_id)
        assert routes
        assert tuple(route.command_id for route in routes) == tuple(
            sorted(route.command_id for route in routes)
        )
        assert all(CORE_COMMANDS.get(route.command_id).handler_id == handler_id for route in routes)
        seen.update(route.command_id for route in routes)
    assert seen == set(CATALOG_ROUTES)


def test_route_metadata_is_deeply_immutable_at_the_boundary() -> None:
    route = catalog_route("storage.show_storage_by_folder")
    with pytest.raises(FrozenInstanceError):
        route.action = "delete"  # type: ignore[misc]
    with pytest.raises(TypeError):
        CATALOG_ROUTES[route.command_id] = route  # type: ignore[index]
    values = route.filter_values()
    values["analysis"] = "changed"
    assert route.filter_values()["analysis"] == "folders"


def test_unknown_route_has_an_actionable_key_error() -> None:
    with pytest.raises(KeyError, match="No catalog palette route"):
        catalog_route("mail.no_such_command")


@pytest.mark.parametrize(
    ("command_id", "kind", "target", "action", "intent_value"),
    (
        (
            "mail.open_outbox",
            RouteKind.DIRECT_ACTION,
            RouteTarget.MAILBOX,
            "open_folder",
            ("folder", "outbox"),
        ),
        (
            "calendar.accept_invitation",
            RouteKind.SCREEN_ACTION,
            RouteTarget.CALENDAR,
            "respond",
            ("response", "accept"),
        ),
        (
            "storage.show_storage_by_sender",
            RouteKind.SCREEN_ACTION,
            RouteTarget.STORAGE,
            "analyze",
            ("analysis", "senders"),
        ),
        (
            "appearance.hide_ascii_banner",
            RouteKind.SETTINGS,
            RouteTarget.SETTINGS,
            "focus_search",
            None,
        ),
        (
            "help.show_command_reference",
            RouteKind.HELP,
            RouteTarget.HELP,
            None,
            None,
        ),
        (
            "cleanup.execute_cleanup_plan",
            RouteKind.SCREEN_ACTION,
            RouteTarget.STORAGE,
            "cleanup_plan",
            None,
        ),
    ),
)
def test_representative_routes_are_specific(
    command_id: str,
    kind: RouteKind,
    target: RouteTarget,
    action: str | None,
    intent_value: tuple[str, str] | None,
) -> None:
    route = catalog_route(command_id)
    assert route.kind is kind
    assert route.target is target
    assert route.action == action
    if intent_value is not None:
        assert route.filter_values()[intent_value[0]] == intent_value[1]


def test_help_settings_and_search_routes_carry_concrete_destinations() -> None:
    for route in CATALOG_ROUTES.values():
        if route.kind is RouteKind.HELP:
            assert route.help_topic
        elif route.kind is RouteKind.SETTINGS:
            assert route.settings_category
            assert route.intent
        elif route.kind is RouteKind.SEARCH:
            assert route.intent
            assert route.action == "search"


def test_route_constructor_shape_is_explicit_for_integrators() -> None:
    fields = set(PaletteRoute.__dataclass_fields__)
    assert {
        "command_id",
        "kind",
        "target",
        "action",
        "intent",
        "query",
        "filters",
        "help_topic",
        "settings_category",
        "setting_id",
        "capability",
        "config_key",
        "gate_reason",
        "safety",
        "confirmation_required",
    } <= fields
