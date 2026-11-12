"""Presentation-neutral routes for the generated universal-palette inventory.

``command_catalog`` deliberately groups the long-tail command inventory under one handler
family per product area (``catalog.mail``, ``catalog.calendar``, and so on).  This module is the
other half of that contract: it resolves every one of those command IDs to a typed, immutable
route without importing Textual, opening the state database, or resolving credentials.

Routes are intentionally conservative.  They point at an operational screen/action already in
the TUI, an existing direct mailbox/layout adapter, a Help page, or an exact Settings location.
When the current TUI has no safe operational surface, the route is an explicit capability gate
with a concrete prerequisite.  There is no generic "open an area" or "not implemented" fallback.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from gucmail.command_catalog import CORE_COMMANDS
from gucmail.commands import CommandRegistry, CommandSpec
from gucmail.models import SafetyClass


class RouteKind(StrEnum):
    """How a presentation layer should realize a palette route."""

    OPEN_SCREEN = "open_screen"
    SCREEN_ACTION = "screen_action"
    APPLICATION_ACTION = "application_action"
    DIRECT_ACTION = "direct_action"
    SEARCH = "search"
    SETTINGS = "settings"
    HELP = "help"
    CAPABILITY_GATE = "capability_gate"


class RouteTarget(StrEnum):
    """Stable presentation-neutral target names understood by the TUI adapter."""

    APPLICATION = "application"
    MAILBOX = "mailbox"
    FOLDER_TREE = "folder_tree"
    MESSAGE_READER = "message_reader"
    COMPOSER = "composer"
    SEARCH = "search"
    LOCAL_SEARCH = "local_search"
    ATTACHMENTS = "attachments"
    DRAFTS = "drafts"
    SCHEDULED = "scheduled"
    CALENDAR = "calendar"
    CONTACTS = "contacts"
    TASKS = "tasks"
    BACKUP = "backup"
    STORAGE = "storage"
    ACTIVITY = "activity"
    NOTIFICATIONS = "notifications"
    SETTINGS = "settings"
    THEMES = "themes"
    BRANDING = "branding"
    HELP = "help"
    DIAGNOSTICS = "diagnostics"
    OFFLINE_BACKUP = "offline_backup"


class PaletteRouteCatalogError(ValueError):
    """Raised when a catalog route is incomplete or contradicts command metadata."""


@dataclass(frozen=True, slots=True)
class PaletteRoute:
    """One validated route from canonical command metadata to an operational surface.

    ``filters`` is an ordered tuple rather than a dictionary so the object remains deeply
    immutable and produces deterministic previews.  Values are symbolic adapter inputs; no
    mailbox data is captured here.
    """

    command_id: str
    kind: RouteKind
    target: RouteTarget
    safety: SafetyClass
    confirmation_required: bool
    action: str | None = None
    intent: str | None = None
    query: str | None = None
    filters: tuple[tuple[str, str], ...] = ()
    help_topic: str | None = None
    settings_category: str | None = None
    setting_id: str | None = None
    capability: str | None = None
    config_key: str | None = None
    gate_reason: str | None = None

    @property
    def is_gate(self) -> bool:
        return self.kind is RouteKind.CAPABILITY_GATE

    @property
    def action_method(self) -> str | None:
        """Return the Textual method spelling for a screen/application action."""

        if self.action is None or self.kind not in {
            RouteKind.APPLICATION_ACTION,
            RouteKind.SCREEN_ACTION,
        }:
            return None
        return f"action_{self.action}"

    def filter_values(self) -> dict[str, str]:
        """Return a fresh mutable copy suitable for an adapter invocation."""

        return dict(self.filters)


_COMMANDS: dict[str, CommandSpec] = {
    command.command_id: command
    for command in CORE_COMMANDS.all()
    if (command.handler_id or "").startswith("catalog.")
}


def _pairs(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple((key, value) for key, value in values.items() if value)


def _route(
    command_id: str,
    kind: RouteKind,
    target: RouteTarget,
    *,
    action: str | None = None,
    intent: str | None = None,
    query: str | None = None,
    filters: tuple[tuple[str, str], ...] = (),
    help_topic: str | None = None,
    settings_category: str | None = None,
    setting_id: str | None = None,
    capability: str | None = None,
    config_key: str | None = None,
    gate_reason: str | None = None,
) -> PaletteRoute:
    command = _COMMANDS[command_id]
    return PaletteRoute(
        command_id=command_id,
        kind=kind,
        target=target,
        safety=command.safety,
        confirmation_required=command.confirmation_required,
        action=action,
        intent=intent,
        query=query,
        filters=filters,
        help_topic=help_topic,
        settings_category=settings_category,
        setting_id=setting_id,
        capability=capability,
        config_key=config_key,
        gate_reason=gate_reason,
    )


def _open(
    command_id: str,
    target: RouteTarget,
    intent: str,
    *,
    filters: tuple[tuple[str, str], ...] = (),
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.OPEN_SCREEN,
        target,
        intent=intent,
        filters=filters,
    )


def _screen_action(
    command_id: str,
    target: RouteTarget,
    action: str,
    *,
    intent: str | None = None,
    filters: tuple[tuple[str, str], ...] = (),
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.SCREEN_ACTION,
        target,
        action=action,
        intent=intent,
        filters=filters,
    )


def _app_action(
    command_id: str,
    action: str,
    *,
    intent: str | None = None,
    filters: tuple[tuple[str, str], ...] = (),
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.APPLICATION_ACTION,
        RouteTarget.APPLICATION,
        action=action,
        intent=intent,
        filters=filters,
    )


def _direct(
    command_id: str,
    target: RouteTarget,
    action: str,
    *,
    intent: str | None = None,
    filters: tuple[tuple[str, str], ...] = (),
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.DIRECT_ACTION,
        target,
        action=action,
        intent=intent,
        filters=filters,
    )


def _search(
    command_id: str,
    *,
    target: RouteTarget = RouteTarget.SEARCH,
    intent: str,
    query: str | None = None,
    filters: tuple[tuple[str, str], ...] = (),
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.SEARCH,
        target,
        action="search",
        intent=intent,
        query=query,
        filters=filters,
    )


def _settings(
    command_id: str,
    category: str,
    *,
    setting_id: str | None = None,
    intent: str = "edit",
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.SETTINGS,
        RouteTarget.SETTINGS,
        action="focus_search",
        intent=intent,
        settings_category=category,
        setting_id=setting_id,
    )


def _help(command_id: str, topic: str, *, query: str | None = None) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.HELP,
        RouteTarget.HELP,
        intent="open_topic",
        query=query,
        help_topic=topic,
    )


def _gate(
    command_id: str,
    target: RouteTarget,
    capability: str,
    reason: str,
    *,
    config_key: str | None = None,
) -> PaletteRoute:
    return _route(
        command_id,
        RouteKind.CAPABILITY_GATE,
        target,
        intent="require_runtime_capability",
        capability=capability,
        config_key=config_key,
        gate_reason=reason,
    )


def _catalog_ids(category: str) -> frozenset[str]:
    return frozenset(
        command.command_id
        for command in _COMMANDS.values()
        if command.handler_id == f"catalog.{category}"
    )


def _checked(category: str, routes: Iterable[PaletteRoute]) -> dict[str, PaletteRoute]:
    result: dict[str, PaletteRoute] = {}
    for route in routes:
        if route.command_id in result:
            raise PaletteRouteCatalogError(f"Duplicate route for {route.command_id}.")
        result[route.command_id] = route
    expected = _catalog_ids(category)
    actual = frozenset(result)
    if expected != actual:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise PaletteRouteCatalogError(
            f"{category} route coverage mismatch; missing: {missing}; extra: {extra}."
        )
    return result


def _mail_routes() -> dict[str, PaletteRoute]:
    routes = [
        _direct(
            "mail.open_outbox",
            RouteTarget.MAILBOX,
            "open_folder",
            intent="standard_folder",
            filters=_pairs(folder="outbox"),
        ),
        _open("mail.open_scheduled", RouteTarget.SCHEDULED, "list_scheduled_messages"),
        _direct(
            "mail.open_a_specific_folder",
            RouteTarget.MAILBOX,
            "open_folder",
            intent="resolve_folder_parameter",
        ),
        _direct("mail.open_previous_folder", RouteTarget.FOLDER_TREE, "navigate_previous_folder"),
        _direct("mail.open_next_folder", RouteTarget.FOLDER_TREE, "navigate_next_folder"),
        _app_action("mail.return_to_last_location", "navigate_back"),
        _app_action("mail.refresh_current_folder", "refresh", intent="current_folder"),
        _app_action("mail.synchronize_folder", "refresh", intent="current_folder_and_tree"),
        _app_action("mail.synchronize_mailbox", "refresh", intent="mailbox_and_folder_tree"),
        _direct(
            "mail.open_selected_message_in_reader",
            RouteTarget.MAILBOX,
            "open_message",
            intent="full_reader",
        ),
        _app_action("mail.open_selected_message_in_floating_reader", "detach_reader"),
        _direct(
            "mail.open_selected_message_externally",
            RouteTarget.MESSAGE_READER,
            "open_selected_externally",
            intent="registered_os_handler_non_shell",
        ),
        _direct(
            "mail.open_message_details",
            RouteTarget.MAILBOX,
            "open_message",
            intent="advanced_details",
        ),
        _direct(
            "mail.open_raw_headers",
            RouteTarget.MAILBOX,
            "open_message_section",
            filters=_pairs(section="headers"),
        ),
        _direct(
            "mail.open_raw_mime",
            RouteTarget.MAILBOX,
            "open_message_section",
            filters=_pairs(section="raw_mime"),
        ),
        _direct(
            "mail.copy_sender_address",
            RouteTarget.MAILBOX,
            "copy_message_field",
            filters=_pairs(field="sender_address"),
        ),
        _direct(
            "mail.copy_message_id",
            RouteTarget.MAILBOX,
            "copy_message_field",
            filters=_pairs(field="internet_message_id"),
        ),
        _direct(
            "mail.copy_subject",
            RouteTarget.MAILBOX,
            "copy_message_field",
            filters=_pairs(field="subject"),
        ),
        _direct(
            "mail.copy_selected_message_as_text",
            RouteTarget.MAILBOX,
            "copy_message_text",
            intent="sanitized_plain_text",
        ),
        _direct(
            "mail.mark_all_visible_read",
            RouteTarget.MAILBOX,
            "mark_bounded_read",
            filters=_pairs(scope="visible"),
        ),
        _direct(
            "mail.mark_all_selected_read",
            RouteTarget.MAILBOX,
            "mark_bounded_read",
            filters=_pairs(scope="selected"),
        ),
        _direct(
            "mail.set_importance", RouteTarget.MAILBOX, "set_importance", intent="selected_message"
        ),
        _direct(
            "mail.add_category", RouteTarget.MAILBOX, "add_category", intent="selected_message"
        ),
        _direct(
            "mail.remove_category",
            RouteTarget.MAILBOX,
            "remove_category",
            intent="selected_message",
        ),
        _direct("mail.copy_message", RouteTarget.MAILBOX, "copy_message", intent="choose_folder"),
        _app_action("mail.delete_message", "delete", intent="recoverable_deleted_items_move"),
        _direct(
            "mail.permanently_delete",
            RouteTarget.MAILBOX,
            "permanently_delete_message",
            intent="one_selected_message_high_friction",
        ),
    ]
    for command_id, selection in (
        ("mail.select_all", "visible"),
        ("mail.select_unread", "visible_unread"),
        ("mail.select_read", "visible_read"),
        ("mail.select_messages_from_sender", "visible_same_sender"),
        ("mail.select_messages_matching_current_search", "current_search_results"),
    ):
        routes.append(
            _direct(
                command_id,
                RouteTarget.MAILBOX,
                "select_visible_messages",
                filters=_pairs(mode=selection),
            )
        )
    routes.extend(
        (
            _direct(
                "mail.export_selected_messages",
                RouteTarget.MAILBOX,
                "export_selected_messages",
                intent="review_then_raw_eml_export",
            ),
            _direct(
                "mail.back_up_selected_messages",
                RouteTarget.BACKUP,
                "backup_selected_messages",
                intent="prefilled_selected_message_backup",
            ),
        )
    )
    return _checked("mail", routes)


def _compose_routes() -> dict[str, PaletteRoute]:
    routes = [
        _app_action("compose.compose_to_contact", "compose", intent="prefill_selected_contact"),
        _app_action(
            "compose.compose_to_selected_sender", "compose", intent="prefill_selected_sender"
        ),
        _app_action(
            "compose.compose_to_selected_recipients",
            "compose",
            intent="prefill_selected_recipients",
        ),
        _app_action("compose.reply", "reply"),
        _app_action("compose.forward", "forward"),
        _screen_action(
            "compose.forward_as_attachment",
            RouteTarget.COMPOSER,
            "forward_as_attachment",
        ),
        _open("compose.edit_current_draft", RouteTarget.COMPOSER, "resume_current_draft"),
        _open("compose.duplicate_current_draft", RouteTarget.COMPOSER, "duplicate_current_draft"),
        _direct("compose.save_draft", RouteTarget.COMPOSER, "save", intent="local_then_server"),
        _direct("compose.save_local_draft", RouteTarget.COMPOSER, "save", intent="local_only"),
        _direct("compose.save_server_draft", RouteTarget.COMPOSER, "save", intent="server_sync"),
        _direct("compose.send", RouteTarget.COMPOSER, "review_send", intent="current_draft"),
        _direct("compose.send_now", RouteTarget.COMPOSER, "review_send", intent="bypass_schedule"),
        _direct("compose.insert_signature", RouteTarget.COMPOSER, "insert_signature"),
        _settings("compose.change_signature", "Composer & Drafts", setting_id="composer.signature"),
        _direct("compose.set_importance", RouteTarget.COMPOSER, "set_importance"),
        _direct(
            "compose.discard_draft", RouteTarget.COMPOSER, "close", intent="discard_after_recovery"
        ),
    ]
    routes.extend(
        (
            _screen_action(
                "compose.send_later",
                RouteTarget.COMPOSER,
                "schedule_draft",
                filters=_pairs(expression="tomorrow 09:00"),
            ),
            _screen_action(
                "compose.send_after_delay",
                RouteTarget.COMPOSER,
                "schedule_draft",
                filters=_pairs(expression="in 10 minutes"),
            ),
            _screen_action(
                "compose.schedule_draft",
                RouteTarget.COMPOSER,
                "schedule_draft",
            ),
            _screen_action(
                "compose.cancel_scheduled_send",
                RouteTarget.SCHEDULED,
                "cancel_schedule",
            ),
            _screen_action(
                "compose.reschedule",
                RouteTarget.SCHEDULED,
                "reschedule",
            ),
        )
    )
    for command_id, control in (
        ("compose.add_attachment", "add"),
        ("compose.remove_attachment", "remove"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.COMPOSER,
                f"{control}_attachment",
            )
        )
    routes.extend(
        (
            _screen_action(
                "compose.toggle_html_plain_text",
                RouteTarget.COMPOSER,
                "toggle_html_plain_text",
            ),
            _screen_action(
                "compose.request_read_receipt",
                RouteTarget.COMPOSER,
                "request_read_receipt",
            ),
            _screen_action(
                "compose.request_delivery_receipt",
                RouteTarget.COMPOSER,
                "request_delivery_receipt",
            ),
            _screen_action(
                "compose.open_external_editor",
                RouteTarget.COMPOSER,
                "open_external_editor",
            ),
        )
    )
    return _checked("compose", routes)


def _search_routes() -> dict[str, PaletteRoute]:
    routes = [
        _search(
            "search.search_current_folder",
            intent="mail_query",
            filters=_pairs(scope="current_folder"),
        ),
        _search("search.search_all_mail", intent="mail_query", filters=_pairs(scope="all_mail")),
        _screen_action(
            "search.search_backups",
            RouteTarget.LOCAL_SEARCH,
            "search",
            intent="offline_backup_content",
            filters=_pairs(mode="backups"),
        ),
        _search("search.search_tasks", target=RouteTarget.TASKS, intent="task_query"),
        _screen_action(
            "search.search_all_data",
            RouteTarget.LOCAL_SEARCH,
            "search",
            intent="federated_local_sources",
            filters=_pairs(mode="all"),
        ),
        _search("search.advanced_search", intent="advanced_mail_builder"),
        _search("search.fuzzy_search", intent="mail_query", filters=_pairs(fuzzy="true")),
        _search("search.exact_search", intent="mail_query", filters=_pairs(exact="true")),
        _screen_action(
            "search.semantic_search",
            RouteTarget.LOCAL_SEARCH,
            "search",
            intent="optional_local_semantic_status",
            filters=_pairs(mode="semantic"),
        ),
    ]
    for command_id, mode in (
        ("search.similar_message_search", "similar"),
        ("search.duplicate_message_search", "duplicates"),
        (
            "search.near_duplicate_search",
            "near_duplicates",
        ),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.LOCAL_SEARCH,
                "search",
                intent="bounded_local_analysis",
                filters=_pairs(mode=mode),
            )
        )
    for command_id, field in (
        ("search.search_sender", "sender"),
        ("search.search_recipient", "recipient"),
        ("search.search_subject", "subject"),
        ("search.search_body", "body"),
        ("search.search_headers", "headers"),
        ("search.search_attachment_filename", "attachment_name"),
        ("search.search_by_date", "date_range"),
        ("search.search_by_size", "size_range"),
        ("search.search_by_folder", "folder"),
        ("search.search_by_category", "category"),
        ("search.search_by_importance", "importance"),
    ):
        routes.append(
            _search(command_id, intent="mail_field_query", filters=_pairs(prompt_field=field))
        )
    routes.extend(
        (
            _search("search.search_unread", intent="mail_query", filters=_pairs(is_read="false")),
            _search("search.search_flagged", intent="mail_query", filters=_pairs(flagged="true")),
            _search(
                "search.search_with_attachments",
                intent="mail_query",
                filters=_pairs(has_attachments="true"),
            ),
            _open(
                "search.search_saved_queries",
                RouteTarget.SEARCH,
                "search.saved_search_browser",
                filters=_pairs(mode="saved"),
            ),
            _open(
                "search.save_current_search",
                RouteTarget.SEARCH,
                "search.saved_search_editor",
                filters=_pairs(mode="saved", create="true"),
            ),
            _open(
                "search.open_recent_search",
                RouteTarget.SEARCH,
                "search.history_browser",
                filters=_pairs(mode="recent"),
            ),
            _direct(
                "search.clear_search", RouteTarget.SEARCH, "clear", intent="mail_filter_and_results"
            ),
            _direct(
                "search.search_within_results",
                RouteTarget.SEARCH,
                "refine_results",
                intent="bounded_current_results",
            ),
        )
    )
    return _checked("search", routes)


def _folder_routes() -> dict[str, PaletteRoute]:
    routes = [
        _direct("folders.rename_folder", RouteTarget.FOLDER_TREE, "rename_selected_folder"),
        _direct("folders.move_folder", RouteTarget.FOLDER_TREE, "move_selected_folder"),
        _direct("folders.delete_folder", RouteTarget.FOLDER_TREE, "delete_selected_folder"),
        _direct("folders.empty_folder", RouteTarget.FOLDER_TREE, "empty_selected_folder"),
        _direct("folders.show_folder_details", RouteTarget.FOLDER_TREE, "show_selected_details"),
        _screen_action(
            "folders.show_folder_storage",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="folders", selection="current_folder"),
        ),
        _direct(
            "folders.show_folder_unread_count",
            RouteTarget.FOLDER_TREE,
            "show_selected_unread_count",
        ),
        _direct("folders.expand_folder_tree", RouteTarget.FOLDER_TREE, "expand_selected"),
        _direct("folders.collapse_folder_tree", RouteTarget.FOLDER_TREE, "collapse_selected"),
        _direct("folders.expand_all_folders", RouteTarget.FOLDER_TREE, "expand_all_bounded"),
        _direct("folders.collapse_all_folders", RouteTarget.FOLDER_TREE, "collapse_all"),
        _app_action("folders.refresh_folder_tree", "refresh", intent="folder_tree"),
    ]
    return _checked("folders", routes)


def _attachment_routes() -> dict[str, PaletteRoute]:
    routes = [
        _direct("attachments.list_attachments", RouteTarget.ATTACHMENTS, "list_metadata"),
        _direct("attachments.open_attachments_view", RouteTarget.ATTACHMENTS, "list_metadata"),
        _screen_action(
            "attachments.download_attachment", RouteTarget.ATTACHMENTS, "save_attachment"
        ),
        _screen_action(
            "attachments.download_all_attachments",
            RouteTarget.ATTACHMENTS,
            "save_all_attachments",
        ),
        _screen_action(
            "attachments.save_attachment_as",
            RouteTarget.ATTACHMENTS,
            "save_attachment_as",
        ),
        _screen_action(
            "attachments.find_duplicate_attachments",
            RouteTarget.STORAGE,
            "analyze",
            intent="duplicate_attachment_analysis",
            filters=_pairs(analysis="duplicate-attachments"),
        ),
        _direct(
            "attachments.open_containing_message",
            RouteTarget.MAILBOX,
            "open_message",
            intent="containing_message",
        ),
        _screen_action("attachments.hash_attachment", RouteTarget.ATTACHMENTS, "hash_attachment"),
        _screen_action(
            "attachments.export_attachment_list",
            RouteTarget.ATTACHMENTS,
            "export_attachment_metadata",
        ),
        _screen_action(
            "attachments.show_attachment_storage_statistics",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="attachment-types"),
        ),
    ]
    return _checked("attachments", routes)


def _draft_routes() -> dict[str, PaletteRoute]:
    routes = [
        _app_action("drafts.new_draft", "compose"),
        _open("drafts.edit_draft", RouteTarget.COMPOSER, "resume_draft_reference"),
        _open("drafts.duplicate_draft", RouteTarget.COMPOSER, "duplicate_draft_reference"),
        _open("drafts.recover_autosaved_draft", RouteTarget.COMPOSER, "recover_latest_autosave"),
        _screen_action("drafts.delete_draft", RouteTarget.DRAFTS, "delete_local_draft"),
        _open(
            "drafts.search_drafts",
            RouteTarget.DRAFTS,
            "draft_search",
        ),
        _open(
            "drafts.show_old_drafts",
            RouteTarget.DRAFTS,
            "filtered_draft_list",
            filters=_pairs(age_days="30"),
        ),
    ]
    routes.extend(
        (
            _open(
                "drafts.show_unsynchronized_drafts",
                RouteTarget.DRAFTS,
                "filtered_draft_list",
                filters=_pairs(location="local"),
            ),
            _open(
                "drafts.show_draft_conflicts",
                RouteTarget.DRAFTS,
                "filtered_draft_list",
                filters=_pairs(location="conflicted"),
            ),
            _screen_action("drafts.synchronize_drafts", RouteTarget.DRAFTS, "sync_draft"),
            _screen_action("drafts.schedule_draft", RouteTarget.DRAFTS, "schedule_draft"),
            _screen_action("drafts.send_draft", RouteTarget.DRAFTS, "send_draft"),
            _screen_action("drafts.export_draft", RouteTarget.DRAFTS, "export_draft"),
        )
    )
    return _checked("drafts", routes)


def _scheduled_routes() -> dict[str, PaletteRoute]:
    routes = [
        _open(
            "scheduled.show_next_scheduled_message",
            RouteTarget.SCHEDULED,
            "list_scheduled_messages",
            filters=_pairs(window="next", limit="1"),
        ),
        _open(
            "scheduled.show_today_s_scheduled_messages",
            RouteTarget.SCHEDULED,
            "list_scheduled_messages",
            filters=_pairs(window="today"),
        ),
        _open(
            "scheduled.show_this_week_s_scheduled_messages",
            RouteTarget.SCHEDULED,
            "list_scheduled_messages",
            filters=_pairs(window="this_week"),
        ),
        _open(
            "scheduled.show_failed_scheduled_messages",
            RouteTarget.SCHEDULED,
            "list_scheduled_messages",
            filters=_pairs(state="failed"),
        ),
        _open(
            "scheduled.show_uncertain_sends",
            RouteTarget.SCHEDULED,
            "list_scheduled_messages",
            filters=_pairs(state="uncertain"),
        ),
        _open("scheduled.open_scheduler_status", RouteTarget.SCHEDULED, "show_scheduler_status"),
    ]
    routes.extend(
        (
            _screen_action("scheduled.reschedule", RouteTarget.SCHEDULED, "reschedule"),
            _screen_action("scheduled.cancel_schedule", RouteTarget.SCHEDULED, "cancel_schedule"),
            _screen_action(
                "scheduled.send_scheduled_message_now", RouteTarget.SCHEDULED, "send_now"
            ),
            _screen_action(
                "scheduled.duplicate_scheduled_message", RouteTarget.SCHEDULED, "duplicate"
            ),
            _screen_action("scheduled.run_scheduler_now", RouteTarget.SCHEDULED, "run_due"),
            _screen_action("scheduled.run_due_jobs", RouteTarget.SCHEDULED, "run_due"),
            _screen_action("scheduled.pause_scheduler", RouteTarget.SCHEDULED, "pause"),
            _screen_action("scheduled.resume_scheduler", RouteTarget.SCHEDULED, "resume"),
        )
    )
    routes.extend(
        (
            _screen_action(
                "scheduled.pause_recurring_message",
                RouteTarget.SCHEDULED,
                "pause_recurring",
            ),
            _screen_action(
                "scheduled.resume_recurring_message",
                RouteTarget.SCHEDULED,
                "resume_recurring",
            ),
        )
    )
    return _checked("scheduled", routes)


def _calendar_routes() -> dict[str, PaletteRoute]:
    routes = [
        _open(
            "calendar.open_agenda",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(view="agenda"),
        ),
        _screen_action(
            "calendar.open_today",
            RouteTarget.CALENDAR,
            "today",
            filters=_pairs(view="day"),
        ),
        _open(
            "calendar.open_tomorrow",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(view="day", anchor="tomorrow"),
        ),
        _open(
            "calendar.open_this_week",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(view="week", anchor="today"),
        ),
        _open(
            "calendar.open_next_week",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(view="week", anchor="next_week"),
        ),
        _open(
            "calendar.open_month",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(view="month"),
        ),
        _open(
            "calendar.go_to_date",
            RouteTarget.CALENDAR,
            "calendar_view",
            filters=_pairs(anchor="parameter"),
        ),
        _screen_action("calendar.create_event", RouteTarget.CALENDAR, "create"),
        _screen_action("calendar.edit_event", RouteTarget.CALENDAR, "edit"),
        _screen_action("calendar.delete_event", RouteTarget.CALENDAR, "delete"),
        _screen_action(
            "calendar.cancel_meeting",
            RouteTarget.CALENDAR,
            "cancel_meeting",
            intent="meeting_cancellation",
        ),
    ]
    for command_id, response in (
        ("calendar.accept_invitation", "accept"),
        ("calendar.tentatively_accept", "tentative"),
        ("calendar.decline_invitation", "decline"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.CALENDAR,
                "respond",
                filters=_pairs(response=response),
            )
        )
    routes.append(
        _screen_action(
            "calendar.propose_new_time",
            RouteTarget.CALENDAR,
            "propose_new_time",
        )
    )
    for command_id, field in (
        ("calendar.add_attendee", "required_attendees"),
        ("calendar.remove_attendee", "required_attendees"),
        ("calendar.add_reminder", "reminder_minutes"),
        ("calendar.change_reminder", "reminder_minutes"),
        ("calendar.make_recurring", "recurrence"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.CALENDAR,
                "edit",
                intent="focus_editor_field",
                filters=_pairs(field=field),
            )
        )
    routes.append(_screen_action("calendar.show_conflicts", RouteTarget.CALENDAR, "show_conflicts"))
    routes.extend(
        (
            _screen_action(
                "calendar.check_availability",
                RouteTarget.CALENDAR,
                "check_availability",
            ),
            _screen_action(
                "calendar.find_free_time",
                RouteTarget.CALENDAR,
                "find_free_time",
            ),
        )
    )
    routes.extend(
        (
            _screen_action("calendar.export_event", RouteTarget.CALENDAR, "export"),
            _screen_action("calendar.import_ics", RouteTarget.CALENDAR, "import"),
            _screen_action(
                "calendar.open_related_messages",
                RouteTarget.CALENDAR,
                "related",
                intent="selected_event",
            ),
            _open(
                "calendar.create_event_from_selected_email",
                RouteTarget.CALENDAR,
                "create_from_message",
                filters=_pairs(source="selected_message"),
            ),
        )
    )
    return _checked("calendar", routes)


def _contact_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action("contacts.edit_contact", RouteTarget.CONTACTS, "edit"),
        _screen_action("contacts.delete_contact", RouteTarget.CONTACTS, "delete"),
        _screen_action("contacts.merge_contacts", RouteTarget.CONTACTS, "merge"),
        _screen_action("contacts.find_duplicate_contacts", RouteTarget.CONTACTS, "find_duplicates"),
        _open(
            "contacts.open_sender_contact",
            RouteTarget.CONTACTS,
            "search_contact",
            filters=_pairs(source="selected_sender"),
        ),
        _open(
            "contacts.add_sender_to_contacts",
            RouteTarget.CONTACTS,
            "create_prefilled_contact",
            filters=_pairs(source="selected_sender"),
        ),
        _open(
            "contacts.show_recent_recipients",
            RouteTarget.CONTACTS,
            "recipient_history",
            filters=_pairs(order="recent"),
        ),
        _open(
            "contacts.show_frequent_correspondents",
            RouteTarget.CONTACTS,
            "recipient_history",
            filters=_pairs(order="frequency"),
        ),
        _screen_action("contacts.search_directory", RouteTarget.CONTACTS, "directory"),
        _screen_action("contacts.resolve_email_address", RouteTarget.CONTACTS, "directory"),
        _screen_action("contacts.compose_to_contact", RouteTarget.CONTACTS, "compose"),
        _screen_action(
            "contacts.schedule_meeting_with_contact",
            RouteTarget.CONTACTS,
            "schedule_meeting",
        ),
        _screen_action(
            "contacts.show_correspondence_history",
            RouteTarget.CONTACTS,
            "correspondence",
        ),
        _screen_action("contacts.import_vcard", RouteTarget.CONTACTS, "import"),
        _screen_action("contacts.export_vcard", RouteTarget.CONTACTS, "export"),
    ]
    return _checked("contacts", routes)


def _task_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action("tasks.edit_task", RouteTarget.TASKS, "edit"),
        _screen_action("tasks.reopen_task", RouteTarget.TASKS, "reopen"),
        _screen_action("tasks.delete_task", RouteTarget.TASKS, "delete"),
        _search("tasks.search_tasks", target=RouteTarget.TASKS, intent="task_query"),
        _open(
            "tasks.show_overdue_tasks",
            RouteTarget.TASKS,
            "filtered_task_list",
            filters=_pairs(due="overdue"),
        ),
        _open(
            "tasks.show_tasks_due_today",
            RouteTarget.TASKS,
            "filtered_task_list",
            filters=_pairs(due="today"),
        ),
        _open(
            "tasks.show_tasks_due_this_week",
            RouteTarget.TASKS,
            "filtered_task_list",
            filters=_pairs(due="this_week"),
        ),
        _open(
            "tasks.create_task_from_message",
            RouteTarget.TASKS,
            "create_from_message",
            filters=_pairs(source="selected_message"),
        ),
        _screen_action("tasks.show_local_task_history", RouteTarget.TASKS, "history"),
    ]
    for command_id, field in (
        ("tasks.add_reminder", "reminder"),
        ("tasks.change_due_date", "due_date"),
        ("tasks.change_priority", "priority"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.TASKS,
                "edit",
                intent="focus_editor_field",
                filters=_pairs(field=field),
            )
        )
    return _checked("tasks", routes)


def _backup_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action("backup.create_backup", RouteTarget.BACKUP, "create"),
        _screen_action(
            "backup.create_full_backup",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(kind="full"),
        ),
        _screen_action(
            "backup.create_incremental_backup",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(kind="incremental"),
        ),
        _screen_action(
            "backup.back_up_current_folder",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(scope="current_folder"),
        ),
        _screen_action(
            "backup.back_up_selected_messages",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(scope="selected_messages"),
        ),
        _screen_action(
            "backup.back_up_search_results",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(scope="search_results"),
        ),
        _screen_action(
            "backup.back_up_cluster",
            RouteTarget.BACKUP,
            "create",
            filters=_pairs(scope="selected_cluster"),
        ),
        _screen_action("backup.estimate_backup_size", RouteTarget.BACKUP, "estimate"),
        _screen_action("backup.resume_backup", RouteTarget.BACKUP, "resume"),
        _screen_action(
            "backup.pause_backup", RouteTarget.BACKUP, "pause_backup", intent="leave_checkpoint"
        ),
        _screen_action(
            "backup.cancel_backup",
            RouteTarget.BACKUP,
            "cancel_backup",
            intent="leave_checkpoint",
        ),
        _screen_action("backup.inspect_backup", RouteTarget.BACKUP, "verify", intent="inspect"),
        _screen_action("backup.open_backup", RouteTarget.BACKUP, "browse"),
        _screen_action("backup.browse_backup_offline", RouteTarget.BACKUP, "browse"),
        _screen_action("backup.restore_plan", RouteTarget.BACKUP, "restore_plan"),
        _screen_action(
            "backup.restore_selected_items",
            RouteTarget.BACKUP,
            "restore_selected_items",
        ),
        _screen_action("backup.export_backup", RouteTarget.BACKUP, "export_backup"),
        _screen_action(
            "backup.prune_old_backups",
            RouteTarget.BACKUP,
            "prune_old_backups",
        ),
        _screen_action(
            "backup.open_backup_folder",
            RouteTarget.BACKUP,
            "open_backup_folder",
        ),
    ]
    return _checked("backup", routes)


def _storage_routes() -> dict[str, PaletteRoute]:
    routes = [
        _open("storage.show_mailbox_quota", RouteTarget.STORAGE, "quota_thresholds"),
        _open("storage.show_mailbox_usage", RouteTarget.STORAGE, "mailbox_usage"),
        _open("storage.show_remaining_mailbox_space", RouteTarget.STORAGE, "remaining_space"),
    ]
    analyses = {
        "storage.show_storage_by_folder": "folders",
        "storage.show_storage_by_sender": "senders",
        "storage.show_storage_by_date": "dates",
        "storage.show_storage_by_attachment_type": "attachment-types",
        "storage.show_local_cache_size": "local",
        "storage.show_search_index_size": "local",
        "storage.show_backup_storage": "local",
    }
    for command_id, analysis in analyses.items():
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.STORAGE,
                "analyze",
                filters=_pairs(analysis=analysis),
            )
        )
    routes.extend(
        (
            _screen_action(
                "storage.analyze_reclaimable_space",
                RouteTarget.STORAGE,
                "analyze",
                filters=_pairs(analysis="largest-messages", cleanup_candidates="true"),
            ),
            _screen_action(
                "storage.open_cleanup_planner",
                RouteTarget.STORAGE,
                "cleanup_plan",
                intent="after_analysis",
            ),
        )
    )
    return _checked("storage", routes)


def _cleanup_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action(
            "cleanup.analyze_mailbox",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="mailbox-overview"),
        ),
        _screen_action(
            "cleanup.find_large_messages",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="largest-messages"),
        ),
        _screen_action(
            "cleanup.find_duplicate_attachments",
            RouteTarget.STORAGE,
            "analyze",
            intent="duplicate_attachment_analysis",
            filters=_pairs(analysis="duplicate-attachments"),
        ),
        _search(
            "cleanup.find_old_unread_mail",
            intent="mail_query",
            filters=_pairs(
                is_read="false",
                age_days="30",
                label="Old unread mail (older than 30 days)",
            ),
        ),
        _search(
            "cleanup.find_stale_newsletters",
            intent="mail_query",
            filters=_pairs(
                classification="newsletter",
                age_days="90",
                label="Stale newsletters (unsubscribe heuristic; older than 90 days)",
            ),
        ),
        _open(
            "cleanup.find_old_drafts",
            RouteTarget.DRAFTS,
            "draft_search",
            filters=_pairs(age_days="30"),
        ),
        _search(
            "cleanup.find_unnecessary_sent_attachments",
            intent="mail_query",
            filters=_pairs(
                folder="sent",
                has_attachments="true",
                age_days="90",
                label="Old sent mail with attachments (review candidates; older than 90 days)",
            ),
        ),
        _screen_action(
            "cleanup.find_largest_senders",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="senders", order="size_desc"),
        ),
        _screen_action(
            "cleanup.estimate_reclaimable_space",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="largest-messages", cleanup_candidates="true"),
        ),
        _screen_action(
            "cleanup.create_cleanup_plan",
            RouteTarget.STORAGE,
            "cleanup_plan",
            intent="after_analysis",
        ),
        _screen_action(
            "cleanup.preview_cleanup_plan",
            RouteTarget.STORAGE,
            "cleanup_plan",
            intent="read_only_preview",
        ),
        _screen_action(
            "cleanup.find_duplicate_messages",
            RouteTarget.STORAGE,
            "analyze",
            filters=_pairs(analysis="duplicates"),
        ),
        _screen_action(
            "cleanup.execute_cleanup_plan",
            RouteTarget.STORAGE,
            "cleanup_plan",
            intent="review_then_execute",
        ),
        _screen_action(
            "cleanup.undo_recent_move_delete_when_possible",
            RouteTarget.ACTIVITY,
            "undo_recent_cleanup",
        ),
    ]
    return _checked("cleanup", routes)


def _monitoring_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action(
            "monitoring.pause_monitoring",
            RouteTarget.ACTIVITY,
            "control_watcher",
            filters=_pairs(control="pause"),
        ),
        _screen_action(
            "monitoring.resume_monitoring",
            RouteTarget.ACTIVITY,
            "control_watcher",
            filters=_pairs(control="resume"),
        ),
        _screen_action(
            "monitoring.monitor_inbox",
            RouteTarget.NOTIFICATIONS,
            "configure_folder_scope",
            filters=_pairs(scope="inbox"),
        ),
        _screen_action(
            "monitoring.monitor_folder",
            RouteTarget.NOTIFICATIONS,
            "configure_folder_scope",
            filters=_pairs(scope="add"),
        ),
    ]
    for command_id, predicate in (
        ("monitoring.monitor_sender", "sender"),
        ("monitoring.monitor_subject", "subject"),
        ("monitoring.monitor_important_messages", "importance"),
        ("monitoring.monitor_messages_with_attachments", "has_attachments"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.NOTIFICATIONS,
                "create_rule",
                filters=_pairs(predicate=predicate),
            )
        )
    routes.extend(
        (
            _open(
                "monitoring.show_last_synchronization",
                RouteTarget.ACTIVITY,
                "filtered_activity",
                filters=_pairs(
                    category="watcher",
                    text="Synchronization checkpoint",
                    order="newest",
                ),
            ),
            _screen_action(
                "monitoring.force_synchronization",
                RouteTarget.ACTIVITY,
                "control_watcher",
                filters=_pairs(control="sync"),
            ),
            _open(
                "monitoring.show_watcher_logs",
                RouteTarget.ACTIVITY,
                "filtered_activity",
                filters=_pairs(category="watcher", text="Watcher log"),
            ),
        )
    )
    return _checked("monitoring", routes)


def _notification_routes() -> dict[str, PaletteRoute]:
    routes = [
        _screen_action(
            "notifications.enable_desktop_notifications",
            RouteTarget.NOTIFICATIONS,
            "set_desktop_notifications",
            filters=_pairs(enabled="true"),
        ),
        _screen_action(
            "notifications.disable_desktop_notifications",
            RouteTarget.NOTIFICATIONS,
            "set_desktop_notifications",
            filters=_pairs(enabled="false"),
        ),
    ]
    for command_id, action in (
        ("notifications.create_notification_rule", "create_rule"),
        ("notifications.edit_notification_rule", "edit_rule"),
        ("notifications.delete_notification_rule", "delete_rule"),
    ):
        routes.append(
            _screen_action(
                command_id,
                RouteTarget.NOTIFICATIONS,
                action,
            )
        )
    routes.extend(
        (
            _screen_action(
                "notifications.mute_notifications",
                RouteTarget.NOTIFICATIONS,
                "mute_notifications",
            ),
            _screen_action(
                "notifications.enable_quiet_hours",
                RouteTarget.NOTIFICATIONS,
                "enable_quiet_hours",
            ),
            _screen_action(
                "notifications.hide_notification_previews",
                RouteTarget.NOTIFICATIONS,
                "set_notification_privacy",
                filters=_pairs(mode="private"),
            ),
            _screen_action(
                "notifications.show_sender_only",
                RouteTarget.NOTIFICATIONS,
                "set_notification_privacy",
                filters=_pairs(mode="sender_only"),
            ),
            _screen_action(
                "notifications.show_full_preview",
                RouteTarget.NOTIFICATIONS,
                "set_notification_privacy",
                filters=_pairs(mode="full"),
            ),
        )
    )
    return _checked("notifications", routes)


def _integration_routes() -> dict[str, PaletteRoute]:
    routes: list[PaletteRoute] = [
        _screen_action(
            "integrations.add_webhook",
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
            filters=_pairs(provider_kind="webhook"),
        ),
        _screen_action(
            "integrations.add_discord_webhook",
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
            filters=_pairs(provider_kind="discord"),
        ),
        _screen_action(
            "integrations.add_slack_compatible_webhook",
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
            filters=_pairs(provider_kind="slack"),
        ),
        _screen_action(
            "integrations.configure_teams_webhook",
            RouteTarget.NOTIFICATIONS,
            "configure_webhook",
            filters=_pairs(provider_kind="teams"),
        ),
        _screen_action(
            "integrations.remove_webhook",
            RouteTarget.NOTIFICATIONS,
            "remove_webhook",
        ),
        _screen_action(
            "integrations.pause_webhook",
            RouteTarget.NOTIFICATIONS,
            "pause_webhook",
        ),
        _screen_action(
            "integrations.resume_webhook",
            RouteTarget.NOTIFICATIONS,
            "resume_webhook",
        ),
    ]
    routes.extend(
        (
            _open(
                "integrations.show_webhook_delivery_history",
                RouteTarget.ACTIVITY,
                "filtered_activity",
                filters=_pairs(category="webhook"),
            ),
            _screen_action(
                "integrations.retry_failed_webhook",
                RouteTarget.ACTIVITY,
                "retry_delivery",
                filters=_pairs(category="webhook", state="retryable"),
            ),
            _screen_action(
                "integrations.show_webhook_configuration",
                RouteTarget.NOTIFICATIONS,
                "show_webhook_configuration",
            ),
            _app_action(
                "integrations.copy_webhook_event_schema",
                "copy_webhook_event_schema",
            ),
        )
    )
    return _checked("integrations", routes)


def _appearance_routes() -> dict[str, PaletteRoute]:
    routes = [
        _open("appearance.open_theme_settings", RouteTarget.THEMES, "theme_browser"),
        _screen_action("appearance.search_themes", RouteTarget.THEMES, "focus_search"),
        _screen_action("appearance.preview_theme", RouteTarget.THEMES, "preview"),
        _screen_action("appearance.next_theme", RouteTarget.THEMES, "next_theme"),
        _screen_action("appearance.previous_theme", RouteTarget.THEMES, "previous_theme"),
        _screen_action("appearance.favorite_theme", RouteTarget.THEMES, "favorite"),
        _screen_action(
            "appearance.create_custom_theme",
            RouteTarget.THEMES,
            "create_custom_theme",
            intent="new_custom_theme",
        ),
        _screen_action("appearance.duplicate_theme", RouteTarget.THEMES, "duplicate"),
        _screen_action("appearance.import_theme", RouteTarget.THEMES, "import_theme"),
        _screen_action("appearance.export_theme", RouteTarget.THEMES, "export_theme"),
        _settings("appearance.reset_appearance", "Appearance", intent="reset_category"),
        _open("appearance.change_ascii_banner", RouteTarget.BRANDING, "branding_browser"),
        _settings(
            "appearance.hide_ascii_banner",
            "Appearance",
            setting_id="appearance.banner_visible",
            intent="set_false",
        ),
        _settings(
            "appearance.show_ascii_banner",
            "Appearance",
            setting_id="appearance.banner_visible",
            intent="set_true",
        ),
        _settings(
            "appearance.change_density",
            "Appearance",
            setting_id="appearance.density",
            intent="cycle",
        ),
        _settings(
            "appearance.toggle_monochrome",
            "Accessibility",
            setting_id="accessibility.monochrome",
            intent="toggle",
        ),
        _settings(
            "appearance.toggle_high_contrast",
            "Accessibility",
            setting_id="accessibility.high_contrast",
            intent="toggle",
        ),
    ]
    return _checked("appearance", routes)


def _layout_routes() -> dict[str, PaletteRoute]:
    routes = [
        _app_action("layout.make_message_list_wider", "preview_narrower"),
        _app_action("layout.save_layout_preset", "layout_presets"),
        _app_action("layout.load_layout_preset", "layout_presets"),
        _app_action("layout.make_message_list_narrower", "preview_wider"),
        _app_action("layout.collapse_preview", "toggle_preview", intent="ensure_hidden"),
        _app_action("layout.expand_preview", "toggle_preview", intent="ensure_visible"),
        _app_action("layout.open_floating_reader", "detach_reader"),
        _direct(
            "layout.close_floating_reader",
            RouteTarget.MESSAGE_READER,
            "close_floating_reader",
        ),
        _app_action("layout.toggle_sidebar", "toggle_sidebar"),
        _direct(
            "layout.make_sidebar_wider",
            RouteTarget.APPLICATION,
            "resize_sidebar",
            filters=_pairs(direction="wider"),
        ),
        _direct(
            "layout.make_sidebar_narrower",
            RouteTarget.APPLICATION,
            "resize_sidebar",
            filters=_pairs(direction="narrower"),
        ),
    ]
    for command_id, preset in (
        ("layout.switch_to_compact_layout", "compact"),
        ("layout.switch_to_reading_layout", "reading"),
        ("layout.switch_to_list_only_layout", "list_only"),
    ):
        routes.append(
            _direct(
                command_id,
                RouteTarget.APPLICATION,
                "apply_layout_preset",
                filters=_pairs(preset=preset),
            )
        )
    return _checked("layout", routes)


def _settings_routes() -> dict[str, PaletteRoute]:
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
    routes = [
        _settings(command_id, category, intent="open_category")
        for command_id, category in categories.items()
    ]
    routes.extend(
        (
            _screen_action("settings.reset_setting", RouteTarget.SETTINGS, "reset_setting"),
            _screen_action("settings.reset_category", RouteTarget.SETTINGS, "reset_category"),
            _screen_action(
                "settings.export_settings",
                RouteTarget.SETTINGS,
                "open_transfer",
                intent="export",
            ),
            _screen_action(
                "settings.import_settings",
                RouteTarget.SETTINGS,
                "open_transfer",
                intent="import",
            ),
        )
    )
    return _checked("settings", routes)


def _activity_routes() -> dict[str, PaletteRoute]:
    routes = []
    for command_id, category in (
        ("activity.show_errors", "error"),
        ("activity.show_recent_operations", "all"),
        ("activity.show_webhook_activity", "webhook"),
        ("activity.show_scheduled_send_activity", "scheduler"),
        ("activity.show_backup_activity", "backup"),
        ("activity.show_connection_activity", "connection"),
    ):
        routes.append(
            _open(
                command_id,
                RouteTarget.ACTIVITY,
                "filtered_activity",
                filters=_pairs(category=category, order="newest"),
            )
        )
    routes.extend(
        (
            _screen_action(
                "activity.clear_local_activity_history",
                RouteTarget.ACTIVITY,
                "clear_local_activity_history",
            ),
            _screen_action(
                "activity.export_sanitized_activity_log", RouteTarget.ACTIVITY, "export"
            ),
        )
    )
    return _checked("activity", routes)


def _diagnostic_routes() -> dict[str, PaletteRoute]:
    routes = [
        _app_action("diagnostics.reconnect", "reconnect"),
        _open(
            "diagnostics.show_server_version",
            RouteTarget.DIAGNOSTICS,
            "connection_status",
            filters=_pairs(field="server_version"),
        ),
        _open(
            "diagnostics.show_ews_operations",
            RouteTarget.DIAGNOSTICS,
            "ews_capability_matrix",
            filters=_pairs(view="operations"),
        ),
        _open(
            "diagnostics.show_quota_source",
            RouteTarget.STORAGE,
            "quota_thresholds",
            filters=_pairs(detail="provenance"),
        ),
        _open(
            "diagnostics.show_cache_health",
            RouteTarget.DIAGNOSTICS,
            "cache_health",
        ),
        _screen_action(
            "diagnostics.repair_cache",
            RouteTarget.DIAGNOSTICS,
            "repair_cache",
            intent="preview_exact_disposable_scope",
        ),
        _screen_action(
            "diagnostics.rebuild_index",
            RouteTarget.DIAGNOSTICS,
            "rebuild_index",
            intent="bounded_cancellable_metadata_rebuild",
        ),
        _open(
            "diagnostics.verify_local_database",
            RouteTarget.DIAGNOSTICS,
            "database_integrity_check",
        ),
        _screen_action(
            "diagnostics.export_sanitized_diagnostics",
            RouteTarget.DIAGNOSTICS,
            "export_bundle",
            intent="explicit_destination_redaction_preview",
        ),
    ]
    return _checked("diagnostics", routes)


def _offline_routes() -> dict[str, PaletteRoute]:
    routes = [
        _direct(
            "offline.leave_offline_mode",
            RouteTarget.APPLICATION,
            "leave_offline_mode",
            intent="reconnect_on_demand",
        ),
        _screen_action(
            "offline.open_latest_backup_offline",
            RouteTarget.BACKUP,
            "browse",
            intent="latest_complete",
        ),
        _screen_action(
            "offline.search_offline_mail",
            RouteTarget.LOCAL_SEARCH,
            "search",
            intent="offline_backup_content",
            filters=_pairs(mode="backups"),
        ),
        _screen_action(
            "offline.browse_offline_folders", RouteTarget.BACKUP, "browse", intent="folder_tree"
        ),
        _open("offline.show_offline_status", RouteTarget.ACTIVITY, "offline_status"),
    ]
    return _checked("offline", routes)


def _help_routes() -> dict[str, PaletteRoute]:
    topics = {
        "help.explain_command_palette": "palette.overview",
        "help.explain_natural_language_commands": "tutorial.palette",
        "help.explain_backups": "backup.overview",
        "help.explain_notifications": "notifications.rules",
        "help.explain_webhooks": "integrations.webhooks",
        "help.explain_scheduled_messages": "mail.scheduled",
        "help.explain_mailbox_quota": "storage.quota",
        "help.explain_a_selected_setting": "settings.overview",
        "help.show_command_examples": "palette.overview",
        "help.show_command_reference": "reference.commands",
    }
    return _checked("help", (_help(command_id, topic) for command_id, topic in topics.items()))


def _build_catalog_routes() -> Mapping[str, PaletteRoute]:
    routes: dict[str, PaletteRoute] = {}
    builders = (
        _mail_routes,
        _compose_routes,
        _search_routes,
        _folder_routes,
        _attachment_routes,
        _draft_routes,
        _scheduled_routes,
        _calendar_routes,
        _contact_routes,
        _task_routes,
        _backup_routes,
        _storage_routes,
        _cleanup_routes,
        _monitoring_routes,
        _notification_routes,
        _integration_routes,
        _appearance_routes,
        _layout_routes,
        _settings_routes,
        _activity_routes,
        _diagnostic_routes,
        _offline_routes,
        _help_routes,
    )
    for builder in builders:
        for command_id, route in builder().items():
            if command_id in routes:
                raise PaletteRouteCatalogError(f"Duplicate route for {command_id}.")
            routes[command_id] = route
    return MappingProxyType(dict(sorted(routes.items())))


CATALOG_ROUTES: Mapping[str, PaletteRoute] = _build_catalog_routes()


def catalog_route(command_id: str) -> PaletteRoute:
    """Return the immutable route for a canonical ``catalog.*`` command."""

    try:
        return CATALOG_ROUTES[command_id]
    except KeyError as exc:
        raise KeyError(f"No catalog palette route for {command_id!r}.") from exc


def routes_for_handler(handler_id: str) -> tuple[PaletteRoute, ...]:
    """Return deterministic routes owned by one category-scoped handler family."""

    return tuple(
        route
        for command_id, route in CATALOG_ROUTES.items()
        if _COMMANDS[command_id].handler_id == handler_id
    )


def catalog_handler_ids() -> frozenset[str]:
    """Return the handler families an integrating executor should bind."""

    return frozenset(command.handler_id or "" for command in _COMMANDS.values())


_KNOWN_SCREEN_ACTIONS: Mapping[RouteTarget, frozenset[str]] = MappingProxyType(
    {
        RouteTarget.BACKUP: frozenset(
            {
                "browse",
                "cancel_backup",
                "create",
                "estimate",
                "export_backup",
                "open_backup_folder",
                "pause_backup",
                "prune_old_backups",
                "restore_plan",
                "restore_selected_items",
                "resume",
                "verify",
            }
        ),
        RouteTarget.ATTACHMENTS: frozenset(
            {
                "export_attachment_metadata",
                "hash_attachment",
                "save_all_attachments",
                "save_attachment",
                "save_attachment_as",
            }
        ),
        RouteTarget.CALENDAR: frozenset(
            {
                "create",
                "cancel_meeting",
                "delete",
                "edit",
                "export",
                "import",
                "related",
                "respond",
                "show_conflicts",
                "propose_new_time",
                "check_availability",
                "find_free_time",
                "today",
            }
        ),
        RouteTarget.CONTACTS: frozenset(
            {
                "compose",
                "correspondence",
                "delete",
                "directory",
                "edit",
                "export",
                "find_duplicates",
                "import",
                "merge",
                "schedule_meeting",
            }
        ),
        RouteTarget.COMPOSER: frozenset(
            {
                "add_attachment",
                "forward_as_attachment",
                "open_external_editor",
                "remove_attachment",
                "request_delivery_receipt",
                "request_read_receipt",
                "schedule_draft",
                "toggle_html_plain_text",
            }
        ),
        RouteTarget.DRAFTS: frozenset(
            {
                "delete_local_draft",
                "export_draft",
                "schedule_draft",
                "send_draft",
                "sync_draft",
            }
        ),
        RouteTarget.TASKS: frozenset({"delete", "edit", "history", "reopen"}),
        RouteTarget.SCHEDULED: frozenset(
            {
                "cancel_schedule",
                "duplicate",
                "pause",
                "pause_recurring",
                "reschedule",
                "resume",
                "resume_recurring",
                "run_due",
                "send_now",
            }
        ),
        RouteTarget.STORAGE: frozenset({"analyze", "cleanup_plan"}),
        RouteTarget.ACTIVITY: frozenset(
            {
                "clear_local_activity_history",
                "control_watcher",
                "export",
                "retry_delivery",
                "toggle_watcher",
                "undo_recent_cleanup",
            }
        ),
        RouteTarget.SETTINGS: frozenset(
            {"focus_search", "open_transfer", "reset_category", "reset_setting"}
        ),
        RouteTarget.THEMES: frozenset(
            {
                "create_custom_theme",
                "duplicate",
                "export_theme",
                "favorite",
                "focus_search",
                "import_theme",
                "next_theme",
                "preview",
                "previous_theme",
            }
        ),
        RouteTarget.DIAGNOSTICS: frozenset({"export_bundle", "rebuild_index", "repair_cache"}),
        RouteTarget.NOTIFICATIONS: frozenset(
            {
                "configure_webhook",
                "configure_folder_scope",
                "create_rule",
                "delete_rule",
                "edit_rule",
                "enable_quiet_hours",
                "mute_notifications",
                "pause_webhook",
                "remove_webhook",
                "resume_webhook",
                "set_desktop_notifications",
                "set_notification_privacy",
                "show_webhook_configuration",
            }
        ),
        RouteTarget.LOCAL_SEARCH: frozenset({"search"}),
    }
)

_KNOWN_APPLICATION_ACTIONS = frozenset(
    {
        "compose",
        "copy_webhook_event_schema",
        "delete",
        "detach_reader",
        "forward",
        "layout_presets",
        "navigate_back",
        "preview_narrower",
        "preview_wider",
        "reconnect",
        "refresh",
        "reply",
        "toggle_preview",
        "toggle_sidebar",
    }
)


def validate_catalog_routes(
    registry: CommandRegistry = CORE_COMMANDS,
) -> tuple[str, ...]:
    """Return every structural/cross-reference error in the route table."""

    errors: list[str] = []
    catalog_commands = {
        command.command_id: command
        for command in registry.all()
        if (command.handler_id or "").startswith("catalog.")
    }
    route_ids = set(CATALOG_ROUTES)
    command_ids = set(catalog_commands)
    for command_id in sorted(command_ids - route_ids):
        errors.append(f"{command_id}: missing catalog route")
    for command_id in sorted(route_ids - command_ids):
        errors.append(f"{command_id}: route has no catalog command")

    forbidden_gate_phrases = ("not implemented", "todo", "placeholder", "generic screen")
    for command_id, route in CATALOG_ROUTES.items():
        command = catalog_commands.get(command_id)
        if command is None:
            continue
        prefix = route.command_id
        if route.safety is not command.safety:
            errors.append(f"{prefix}: safety does not match canonical command")
        if route.confirmation_required != command.confirmation_required:
            errors.append(f"{prefix}: confirmation policy does not match canonical command")
        keys = [key for key, value in route.filters if key and value]
        if len(keys) != len(route.filters) or len(set(keys)) != len(keys):
            errors.append(f"{prefix}: filters must have unique non-empty keys and values")
        if route.kind is RouteKind.APPLICATION_ACTION:
            if route.target is not RouteTarget.APPLICATION:
                errors.append(f"{prefix}: application action has the wrong target")
            if route.action not in _KNOWN_APPLICATION_ACTIONS:
                errors.append(f"{prefix}: unknown GUCmailApp action {route.action!r}")
        elif route.kind is RouteKind.SCREEN_ACTION:
            if route.action not in _KNOWN_SCREEN_ACTIONS.get(route.target, frozenset()):
                errors.append(
                    f"{prefix}: unknown {route.target.value} screen action {route.action!r}"
                )
        elif route.kind is RouteKind.DIRECT_ACTION:
            if not route.action or not route.action.strip():
                errors.append(f"{prefix}: direct action is missing")
        elif route.kind is RouteKind.OPEN_SCREEN:
            if not route.intent or not route.intent.strip():
                errors.append(f"{prefix}: screen route lacks a concrete intent")
        elif route.kind is RouteKind.SEARCH:
            if route.action != "search" or not route.intent:
                errors.append(f"{prefix}: search route lacks a concrete search intent")
        elif route.kind is RouteKind.SETTINGS:
            if not route.settings_category:
                errors.append(f"{prefix}: settings route lacks a category")
            if route.action != "focus_search":
                errors.append(f"{prefix}: settings route must enter through focus_search")
        elif route.kind is RouteKind.HELP:
            if not route.help_topic:
                errors.append(f"{prefix}: Help route lacks a page ID")
        elif route.kind is RouteKind.CAPABILITY_GATE:
            reason = (route.gate_reason or "").strip()
            if not route.capability or not reason:
                errors.append(f"{prefix}: capability gate lacks a prerequisite or reason")
            if any(phrase in reason.casefold() for phrase in forbidden_gate_phrases):
                errors.append(f"{prefix}: capability gate uses placeholder wording")
        else:  # pragma: no cover - StrEnum closes this unless a future kind is added.
            errors.append(f"{prefix}: unknown route kind {route.kind!r}")
    return tuple(errors)


_ROUTE_ERRORS = validate_catalog_routes()
if _ROUTE_ERRORS:
    raise PaletteRouteCatalogError("Invalid palette routes:\n- " + "\n- ".join(_ROUTE_ERRORS))


__all__ = [
    "CATALOG_ROUTES",
    "PaletteRoute",
    "PaletteRouteCatalogError",
    "RouteKind",
    "RouteTarget",
    "catalog_handler_ids",
    "catalog_route",
    "routes_for_handler",
    "validate_catalog_routes",
]
