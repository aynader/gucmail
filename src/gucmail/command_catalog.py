"""Core universal-command catalog used before UI-specific handlers are bound.

These commands are semantic actions, not CLI argument vectors.  Several entries are presets over
the same eventual handler (for example, opening Inbox and opening Sent Items).  That keeps the
catalog broad without duplicating mailbox operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from gucmail.commands import (
    CommandRegistry,
    CommandSpec,
    ParameterSpec,
    ParameterType,
    VisibilityLevel,
)
from gucmail.models import SafetyClass


@dataclass(frozen=True, slots=True)
class _Seed:
    command_id: str
    name: str
    category: str
    aliases: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...] = ()
    safety: SafetyClass = SafetyClass.READ_ONLY
    changes_server: bool = False
    changes_local: bool = False
    confirmation: bool = False
    preview: bool = False
    offline: bool = True
    requires_connection: bool = False
    context: tuple[str, ...] = ()
    cli_path: tuple[str, ...] | None = None
    visibility: VisibilityLevel = VisibilityLevel.BASIC
    help_topic: str | None = None
    shortcut: str | None = None
    availability: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    misspellings: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    handler_id: str | None = None


QUERY = ParameterSpec("query", ParameterType.QUERY, "Text or structured search criteria.")
PERSON = ParameterSpec("person", ParameterType.PERSON, "Resolved person or address.")
SENDER = ParameterSpec("sender", ParameterType.PERSON, "Resolved sender.")
RECIPIENT = ParameterSpec("recipient", ParameterType.PERSON, "Resolved recipient.")
FOLDER = ParameterSpec("folder", ParameterType.FOLDER, "Resolved mailbox folder.")
REFERENCE = ParameterSpec("reference", ParameterType.REFERENCE, "Selected local item reference.")
SELECTION = ParameterSpec("selection", ParameterType.REFERENCE, "Current selection.", multiple=True)
WHEN = ParameterSpec("when", ParameterType.DATETIME, "Resolved future date and time.")
DATE_RANGE = ParameterSpec("date_range", ParameterType.DATE_RANGE, "Resolved inclusive date range.")
SIZE = ParameterSpec("size", ParameterType.SIZE_RANGE, "Parsed message or attachment size filter.")
ATTACHMENT_TYPE = ParameterSpec(
    "attachment_type",
    ParameterType.ENUM,
    "Attachment kind.",
    choices=(
        "archive",
        "document",
        "image",
        "pdf",
        "presentation",
        "spreadsheet",
    ),
)
READ_STATE = ParameterSpec(
    "read_state", ParameterType.ENUM, "Read-state filter.", choices=("read", "unread")
)
FLAGGED = ParameterSpec("flagged", ParameterType.BOOLEAN, "Flag-state filter.")
IMPORTANT = ParameterSpec("important", ParameterType.BOOLEAN, "Importance filter.")
IMPORTANCE_LEVEL = ParameterSpec(
    "importance",
    ParameterType.ENUM,
    "Message importance.",
    choices=("low", "normal", "high"),
)
CATEGORY = ParameterSpec("category", ParameterType.STRING, "Mailbox category name.")
HAS_ATTACHMENTS = ParameterSpec(
    "has_attachments", ParameterType.BOOLEAN, "Attachment-presence filter."
)
EXCLUSIONS = ParameterSpec(
    "exclusions",
    ParameterType.EXCLUSION,
    "Explicit negative search clauses.",
    multiple=True,
)
RANKING = ParameterSpec(
    "ranking", ParameterType.RANKING, "Result ordering and optional bounded count."
)
THREAD_RESPONSE = ParameterSpec(
    "thread_response",
    ParameterType.THREAD_RESPONSE,
    "Conversation-response ownership filter.",
)
THEME = ParameterSpec("theme", ParameterType.STRING, "Theme name or family.")
WEBHOOK_NAME = ParameterSpec("name", ParameterType.STRING, "Configured webhook name.")
THEME_PATH = ParameterSpec(
    "path",
    ParameterType.PATH,
    "Local JSON theme file path.",
    required=True,
)
SETTING_NAME = ParameterSpec(
    "setting",
    ParameterType.STRING,
    "Stable setting ID or human-readable setting name.",
)
SETTINGS_CATEGORY = ParameterSpec(
    "settings_category",
    ParameterType.STRING,
    "Human-readable settings category.",
)
SUBJECT = ParameterSpec("subject", ParameterType.STRING, "Message, event, or task subject.")
END = ParameterSpec("end", ParameterType.DATETIME, "Resolved event end date and time.")


# This is the product specification's minimum universal-command inventory.  It lives beside the
# structured command seeds so the registry remains the one canonical source consumed by Help,
# autocomplete, NLP, and presentation layers.  Coverage rows use one real, category-scoped
# in-process handler family; availability is derived from connection, context, settings, and
# capabilities instead of being hard-coded as an implementation placeholder.
MINIMUM_COMMAND_ACTIONS: dict[str, tuple[str, ...]] = {
    "mail": (
        "Open Inbox",
        "Open Sent",
        "Open Drafts",
        "Open Deleted Items",
        "Open Junk",
        "Open Archive",
        "Open Outbox",
        "Open Scheduled",
        "Open Unread",
        "Open Flagged",
        "Open a specific folder",
        "Open previous folder",
        "Open next folder",
        "Return to last location",
        "Refresh current folder",
        "Refresh mailbox",
        "Synchronize folder",
        "Synchronize mailbox",
        "Open selected message",
        "Open selected message in reader",
        "Open selected message in floating reader",
        "Open selected message externally",
        "Open message details",
        "Open raw headers",
        "Open raw MIME",
        "Copy sender address",
        "Copy message ID",
        "Copy subject",
        "Copy selected message as text",
        "Mark read",
        "Mark unread",
        "Mark all visible read",
        "Mark all selected read",
        "Flag",
        "Unflag",
        "Set importance",
        "Add category",
        "Remove category",
        "Move message",
        "Copy message",
        "Delete message",
        "Restore message",
        "Permanently delete",
        "Select all",
        "Select unread",
        "Select read",
        "Select messages from sender",
        "Select messages matching current search",
        "Export selected messages",
        "Back up selected messages",
    ),
    "compose": (
        "Compose message",
        "Compose to contact",
        "Compose to selected sender",
        "Compose to selected recipients",
        "Reply",
        "Reply all",
        "Forward",
        "Forward as attachment",
        "Edit current draft",
        "Duplicate current draft",
        "Save draft",
        "Save local draft",
        "Save server draft",
        "Send",
        "Send later",
        "Send after delay",
        "Send now",
        "Schedule draft",
        "Cancel scheduled send",
        "Reschedule",
        "Add attachment",
        "Remove attachment",
        "Insert signature",
        "Change signature",
        "Toggle HTML/plain text",
        "Set importance",
        "Request read receipt",
        "Request delivery receipt",
        "Open external editor",
        "Discard draft",
        "Recover draft",
    ),
    "search": (
        "Search mail",
        "Search current folder",
        "Search all mail",
        "Search backups",
        "Search attachments",
        "Search calendar",
        "Search contacts",
        "Search tasks",
        "Search all data",
        "Advanced search",
        "Fuzzy search",
        "Exact search",
        "Semantic search",
        "Similar-message search",
        "Duplicate-message search",
        "Near-duplicate search",
        "Search sender",
        "Search recipient",
        "Search subject",
        "Search body",
        "Search headers",
        "Search attachment filename",
        "Search by date",
        "Search by size",
        "Search by folder",
        "Search by category",
        "Search by importance",
        "Search unread",
        "Search flagged",
        "Search with attachments",
        "Search saved queries",
        "Save current search",
        "Open recent search",
        "Clear search",
        "Search within results",
    ),
    "folders": (
        "Open folder",
        "Create folder",
        "Rename folder",
        "Move folder",
        "Delete folder",
        "Empty folder",
        "Show folder details",
        "Show folder storage",
        "Show folder unread count",
        "Expand folder tree",
        "Collapse folder tree",
        "Expand all folders",
        "Collapse all folders",
        "Refresh folder tree",
    ),
    "attachments": (
        "List attachments",
        "Open attachments view",
        "Download attachment",
        "Download all attachments",
        "Save attachment as",
        "Show largest attachments",
        "Find duplicate attachments",
        "Search attachment content",
        "Open containing message",
        "Hash attachment",
        "Export attachment list",
        "Show attachment storage statistics",
    ),
    "drafts": (
        "Open drafts",
        "New draft",
        "Edit draft",
        "Duplicate draft",
        "Recover autosaved draft",
        "Delete draft",
        "Search drafts",
        "Show old drafts",
        "Show unsynchronized drafts",
        "Show draft conflicts",
        "Synchronize drafts",
        "Schedule draft",
        "Send draft",
        "Export draft",
    ),
    "scheduled": (
        "Open scheduled messages",
        "Show next scheduled message",
        "Show today's scheduled messages",
        "Show this week's scheduled messages",
        "Show failed scheduled messages",
        "Show uncertain sends",
        "Schedule current draft",
        "Reschedule",
        "Cancel schedule",
        "Pause recurring message",
        "Resume recurring message",
        "Send scheduled message now",
        "Duplicate scheduled message",
        "Open scheduler status",
        "Run scheduler now",
        "Run due jobs",
        "Pause scheduler",
        "Resume scheduler",
    ),
    "calendar": (
        "Open calendar",
        "Open agenda",
        "Open today",
        "Open tomorrow",
        "Open this week",
        "Open next week",
        "Open month",
        "Go to date",
        "Create event",
        "Edit event",
        "Delete event",
        "Cancel meeting",
        "Accept invitation",
        "Tentatively accept",
        "Decline invitation",
        "Propose new time",
        "Add attendee",
        "Remove attendee",
        "Add reminder",
        "Change reminder",
        "Make recurring",
        "Show conflicts",
        "Check availability",
        "Find free time",
        "Search calendar",
        "Export event",
        "Import ICS",
        "Open related messages",
        "Create event from selected email",
    ),
    "contacts": (
        "Open contacts",
        "Search contacts",
        "Create contact",
        "Edit contact",
        "Delete contact",
        "Merge contacts",
        "Find duplicate contacts",
        "Open sender contact",
        "Add sender to contacts",
        "Show recent recipients",
        "Show frequent correspondents",
        "Search directory",
        "Resolve email address",
        "Compose to contact",
        "Schedule meeting with contact",
        "Show correspondence history",
        "Import vCard",
        "Export vCard",
    ),
    "tasks": (
        "Open tasks",
        "Create task",
        "Edit task",
        "Complete task",
        "Reopen task",
        "Delete task",
        "Search tasks",
        "Show overdue tasks",
        "Show tasks due today",
        "Show tasks due this week",
        "Create task from message",
        "Add reminder",
        "Change due date",
        "Change priority",
    ),
    "backup": (
        "Open backup center",
        "Create backup",
        "Create full backup",
        "Create incremental backup",
        "Back up current folder",
        "Back up selected messages",
        "Back up search results",
        "Back up cluster",
        "Estimate backup size",
        "Resume backup",
        "Pause backup",
        "Cancel backup",
        "Verify backup",
        "Inspect backup",
        "Open backup",
        "Browse backup offline",
        "Restore-plan",
        "Restore selected items",
        "Export backup",
        "Prune old backups",
        "Open backup folder",
    ),
    "storage": (
        "Open storage",
        "Show mailbox quota",
        "Show mailbox usage",
        "Show remaining mailbox space",
        "Show storage by folder",
        "Show storage by sender",
        "Show storage by date",
        "Show storage by attachment type",
        "Show largest messages",
        "Show largest attachments",
        "Show local cache size",
        "Show search-index size",
        "Show backup storage",
        "Analyze reclaimable space",
        "Open cleanup planner",
    ),
    "cleanup": (
        "Analyze mailbox",
        "Find large messages",
        "Find duplicate messages",
        "Find duplicate attachments",
        "Find old unread mail",
        "Find stale newsletters",
        "Find old drafts",
        "Find unnecessary sent attachments",
        "Find largest senders",
        "Estimate reclaimable space",
        "Create cleanup plan",
        "Preview cleanup plan",
        "Execute cleanup plan",
        "Undo recent move/delete when possible",
    ),
    "monitoring": (
        "Start monitoring",
        "Stop monitoring",
        "Pause monitoring",
        "Resume monitoring",
        "Show monitoring status",
        "Monitor Inbox",
        "Monitor folder",
        "Monitor sender",
        "Monitor subject",
        "Monitor important messages",
        "Monitor messages with attachments",
        "Show last synchronization",
        "Force synchronization",
        "Show watcher logs",
    ),
    "notifications": (
        "Open notification settings",
        "Enable desktop notifications",
        "Disable desktop notifications",
        "Test desktop notification",
        "Create notification rule",
        "Edit notification rule",
        "Delete notification rule",
        "Mute notifications",
        "Enable quiet hours",
        "Show notification history",
        "Hide notification previews",
        "Show sender only",
        "Show full preview",
    ),
    "integrations": (
        "Open integrations",
        "Add webhook",
        "Remove webhook",
        "Test webhook",
        "Add Discord webhook",
        "Test Discord webhook",
        "Add Slack-compatible webhook",
        "Configure Teams webhook",
        "Show webhook delivery history",
        "Retry failed webhook",
        "Pause webhook",
        "Resume webhook",
        "Show webhook configuration",
        "Copy webhook event schema",
    ),
    "appearance": (
        "Open theme settings",
        "Search themes",
        "Apply theme",
        "Preview theme",
        "Next theme",
        "Previous theme",
        "Favorite theme",
        "Create custom theme",
        "Duplicate theme",
        "Import theme",
        "Export theme",
        "Reset appearance",
        "Change ASCII banner",
        "Hide ASCII banner",
        "Show ASCII banner",
        "Change density",
        "Toggle monochrome",
        "Toggle high contrast",
    ),
    "layout": (
        "Make message list wider",
        "Make message list narrower",
        "Make preview wider",
        "Make preview narrower",
        "Collapse preview",
        "Expand preview",
        "Toggle preview",
        "Open floating reader",
        "Close floating reader",
        "Toggle sidebar",
        "Make sidebar wider",
        "Make sidebar narrower",
        "Reset layout",
        "Switch to compact layout",
        "Switch to reading layout",
        "Switch to list-only layout",
        "Save layout preset",
        "Load layout preset",
    ),
    "settings": (
        "Open Settings",
        "Search settings",
        "Open Appearance settings",
        "Open Layout settings",
        "Open Mail settings",
        "Open Composer settings",
        "Open Calendar settings",
        "Open Notification settings",
        "Open Storage settings",
        "Open Backup settings",
        "Open Search settings",
        "Open Connection settings",
        "Open Privacy settings",
        "Open Advanced settings",
        "Reset setting",
        "Reset category",
        "Export settings",
        "Import settings",
    ),
    "activity": (
        "Open Activity",
        "Show errors",
        "Show recent operations",
        "Show webhook activity",
        "Show scheduled-send activity",
        "Show backup activity",
        "Show connection activity",
        "Clear local activity history",
        "Export sanitized activity log",
    ),
    "diagnostics": (
        "Run doctor",
        "Check connection",
        "Reconnect",
        "Show server version",
        "Show EWS capabilities",
        "Show EWS operations",
        "Show quota source",
        "Show cache health",
        "Repair cache",
        "Rebuild index",
        "Verify local database",
        "Open logs",
        "Export sanitized diagnostics",
    ),
    "offline": (
        "Enter offline mode",
        "Leave offline mode",
        "Open latest backup offline",
        "Search offline mail",
        "Browse offline folders",
        "Show offline status",
    ),
    "help": (
        "Open Help",
        "Search Help",
        "Show keyboard shortcuts",
        "Explain command palette",
        "Explain natural-language commands",
        "Explain backups",
        "Explain notifications",
        "Explain webhooks",
        "Explain scheduled messages",
        "Explain mailbox quota",
        "Explain a selected setting",
        "Show command examples",
        "Show command reference",
    ),
}


_SEEDS = (
    _Seed(
        "mail.open_inbox",
        "Open Inbox",
        "mail",
        ("inbox", "go to inbox"),
        (FOLDER,),
        cli_path=("messages", "list"),
    ),
    _Seed(
        "mail.open_sent",
        "Open Sent Items",
        "mail",
        ("sent mail", "open sent"),
        (FOLDER,),
        cli_path=("messages", "list"),
    ),
    _Seed(
        "mail.open_drafts",
        "Open Drafts",
        "mail",
        ("drafts", "unfinished mail"),
        (FOLDER,),
        cli_path=("drafts", "list"),
    ),
    _Seed(
        "mail.open_deleted",
        "Open Deleted Items",
        "mail",
        ("trash", "open deleted"),
        (FOLDER,),
        cli_path=("messages", "list"),
    ),
    _Seed(
        "mail.open_junk",
        "Open Junk",
        "mail",
        ("junk mail", "spam folder"),
        (FOLDER,),
        cli_path=("messages", "list"),
    ),
    _Seed(
        "mail.open_archive",
        "Open Archive",
        "mail",
        ("archive", "archived mail"),
        (FOLDER,),
        cli_path=("messages", "list"),
    ),
    _Seed(
        "mail.open_unread",
        "Open Unread Mail",
        "mail",
        ("show unread", "unread mail"),
        (READ_STATE,),
        cli_path=("search",),
    ),
    _Seed(
        "mail.open_flagged",
        "Open Flagged Mail",
        "mail",
        ("show flagged", "flagged mail"),
        (FLAGGED,),
        cli_path=("search",),
    ),
    _Seed(
        "mail.refresh",
        "Refresh Mailbox",
        "mail",
        ("refresh mail", "sync mailbox"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "mail.open_message",
        "Open Selected Message",
        "mail",
        ("read this", "open message"),
        (REFERENCE,),
        context=("selected_message",),
        cli_path=("messages", "show"),
    ),
    _Seed(
        "mail.reply",
        "Reply to Selected Message",
        "mail",
        ("reply", "answer this"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("reply",),
        help_topic="mail.compose",
    ),
    _Seed(
        "mail.reply_all",
        "Reply All",
        "mail",
        ("reply everyone", "answer everyone"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("reply-all",),
        help_topic="mail.compose",
    ),
    _Seed(
        "mail.forward",
        "Forward Selected Message",
        "mail",
        ("forward this", "send this on"),
        (REFERENCE, RECIPIENT),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("forward",),
        help_topic="mail.compose",
    ),
    _Seed(
        "mail.mark_read",
        "Mark Read",
        "mail",
        ("mark this read", "make read"),
        (SELECTION,),
        SafetyClass.MUTATING,
        True,
        False,
        False,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "mark-read"),
    ),
    _Seed(
        "mail.mark_unread",
        "Mark Unread",
        "mail",
        ("mark this unread", "make unread"),
        (SELECTION,),
        SafetyClass.MUTATING,
        True,
        False,
        False,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "mark-unread"),
    ),
    _Seed(
        "mail.flag",
        "Flag Message",
        "mail",
        ("flag this", "add flag"),
        (SELECTION,),
        SafetyClass.MUTATING,
        True,
        False,
        False,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "flag"),
    ),
    _Seed(
        "mail.unflag",
        "Remove Message Flag",
        "mail",
        ("unflag this", "remove flag"),
        (SELECTION,),
        SafetyClass.MUTATING,
        True,
        False,
        False,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "unflag"),
    ),
    _Seed(
        "mail.move",
        "Move Message",
        "mail",
        ("move this", "file this under"),
        (SELECTION, FOLDER),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "move"),
    ),
    _Seed(
        "mail.delete",
        "Move Message to Deleted Items",
        "mail",
        ("delete this message", "move this to trash"),
        (SELECTION,),
        SafetyClass.DESTRUCTIVE,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "delete"),
    ),
    _Seed(
        "mail.restore",
        "Restore Message",
        "mail",
        ("restore this", "put this back"),
        (SELECTION,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_message",),
        ("messages", "restore"),
    ),
    _Seed(
        "compose.new",
        "Compose Message",
        "compose",
        ("new email", "write mail"),
        (RECIPIENT, QUERY),
        offline=True,
    ),
    _Seed(
        "draft.save",
        "Save Current Draft",
        "drafts",
        ("save draft", "draft this"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
        True,
        False,
        ("current_draft",),
    ),
    _Seed(
        "draft.recover",
        "Recover Draft",
        "drafts",
        ("recover draft", "resume draft", "restore autosave"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        True,
        False,
        (),
        ("drafts", "recover"),
    ),
    _Seed(
        "draft.send_status",
        "Inspect Draft Send Outcome",
        "drafts",
        ("draft send status", "was this draft sent", "inspect send attempt"),
        (replace(REFERENCE, required=True, context_source="current_draft"),),
        cli_path=("drafts", "send-status"),
        help_topic="mail.drafts",
        examples=("gucmail drafts send-status dft-example",),
    ),
    _Seed(
        "scheduled.open",
        "Open Scheduled Messages",
        "scheduled",
        ("scheduled mail", "show scheduled", "open scheduled mail"),
        cli_path=("scheduled", "list"),
    ),
    _Seed(
        "scheduled.schedule_current",
        "Schedule Current Draft",
        "scheduled",
        ("send this later", "schedule this draft"),
        (REFERENCE, WHEN),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        True,
        False,
        ("current_draft",),
        ("drafts", "schedule"),
    ),
    _Seed(
        "scheduled.cancel",
        "Cancel Scheduled Message",
        "scheduled",
        ("cancel scheduled mail", "unschedule this"),
        (REFERENCE,),
        SafetyClass.DESTRUCTIVE,
        False,
        True,
        True,
        True,
        True,
        False,
        ("scheduled_message",),
        ("scheduled", "cancel"),
    ),
    _Seed(
        "search.mail",
        "Search Mail",
        "search",
        ("find email", "search messages"),
        (
            QUERY,
            SUBJECT,
            ParameterSpec("body", ParameterType.STRING, "Words in the message body."),
            SENDER,
            RECIPIENT,
            FOLDER,
            DATE_RANGE,
            SIZE,
            HAS_ATTACHMENTS,
            ATTACHMENT_TYPE,
            READ_STATE,
            FLAGGED,
            IMPORTANT,
            EXCLUSIONS,
            RANKING,
            THREAD_RESPONSE,
        ),
        cli_path=("search",),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "folders.open",
        "Open Folder",
        "folders",
        ("go to folder", "show folder"),
        (FOLDER,),
        cli_path=("folders", "list"),
    ),
    _Seed(
        "folders.create",
        "Create Folder",
        "folders",
        ("new folder", "make folder"),
        (FOLDER,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        (),
        ("folders", "create"),
    ),
    _Seed(
        "attachments.ocr",
        "Open Local Image OCR",
        "attachments",
        ("local image OCR", "recognize image text", "extract local image text"),
        cli_path=("ocr", "status"),
        help_topic="search.local",
    ),
    _Seed(
        "attachments.open",
        "Open Attachments",
        "attachments",
        ("show attachments", "attachment list"),
        (REFERENCE,),
        context=("selected_message",),
        cli_path=("attachments", "list"),
    ),
    _Seed(
        "attachments.download",
        "Download Attachments",
        "attachments",
        ("save attachments", "download files"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        True,
        False,
        ("selected_message",),
        ("attachments", "save-all"),
    ),
    _Seed(
        "attachments.search",
        "Search Attachments",
        "attachments",
        ("find attachments", "search attachment content"),
        (
            QUERY,
            SENDER,
            DATE_RANGE,
            SIZE,
            ATTACHMENT_TYPE,
            EXCLUSIONS,
            RANKING,
            THREAD_RESPONSE,
        ),
        requires_connection=False,
        offline=True,
    ),
    _Seed(
        "calendar.open",
        "Open Calendar",
        "calendar",
        ("calendar", "show calendar"),
        cli_path=("calendar", "agenda"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "calendar.tomorrow",
        "Open Tomorrow in Calendar",
        "calendar",
        ("calendar tomorrow", "meetings tomorrow"),
        (DATE_RANGE,),
        cli_path=("calendar", "day"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "calendar.create_tomorrow",
        "Create Event Tomorrow",
        "calendar",
        ("create event tomorrow", "new event tomorrow"),
        (SUBJECT, PERSON),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        (),
        None,
    ),
    _Seed(
        "calendar.show_tomorrow_events",
        "Show Tomorrow's Events",
        "calendar",
        ("show tomorrow events", "tomorrow events"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "calendar.create",
        "Create Calendar Event",
        "calendar",
        ("new event", "create meeting"),
        (WHEN, END, SUBJECT, PERSON, QUERY),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        (),
        ("calendar", "create"),
    ),
    _Seed(
        "calendar.search",
        "Search Calendar",
        "calendar",
        ("find meeting", "search events"),
        (QUERY, PERSON, DATE_RANGE),
        cli_path=("calendar", "search"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "contacts.open",
        "Open Contacts",
        "contacts",
        ("contacts", "address book"),
        cli_path=("contacts", "list"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "contacts.search",
        "Search Contacts",
        "contacts",
        ("find contact", "look up person"),
        (QUERY, PERSON),
        cli_path=("contacts", "search"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "contacts.create",
        "Create Contact",
        "contacts",
        ("new contact", "save contact"),
        (PERSON,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        (),
        ("contacts", "create"),
    ),
    _Seed(
        "tasks.open",
        "Open Tasks",
        "tasks",
        ("tasks", "task list"),
        cli_path=("tasks", "list"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "tasks.create",
        "Create Task",
        "tasks",
        ("new task", "add task"),
        (QUERY, WHEN),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        (),
        ("tasks", "create"),
    ),
    _Seed(
        "tasks.complete",
        "Complete Task",
        "tasks",
        ("finish task", "mark task complete"),
        (REFERENCE,),
        SafetyClass.MUTATING,
        True,
        False,
        True,
        True,
        False,
        True,
        ("selected_task",),
        ("tasks", "complete"),
    ),
    _Seed(
        "tasks.show_local_task_history",
        "Show local Task history",
        "tasks",
        ("task history", "local task history", "show task history"),
        (REFERENCE,),
        context=("selected_task",),
        cli_path=("tasks", "history"),
        handler_id="catalog.tasks",
    ),
    _Seed(
        "backup.open",
        "Open Backup Center",
        "backup",
        ("show backups", "open backups", "backup center", "bkp"),
        cli_path=("backup", "list"),
    ),
    _Seed(
        "backup.create",
        "Create Mailbox Backup",
        "backup",
        ("back up mail", "make backup", "bkp"),
        (FOLDER, SELECTION),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        False,
        True,
        (),
        ("backup", "create"),
    ),
    _Seed(
        "backup.verify",
        "Verify Backup",
        "backup",
        ("check backup", "verify latest backup", "bkp"),
        (REFERENCE,),
        cli_path=("backup", "verify"),
    ),
    _Seed(
        "storage.open",
        "Open Storage",
        "storage",
        ("mailbox storage", "show quota"),
        cli_path=("quota", "show"),
        requires_connection=False,
        offline=True,
    ),
    _Seed(
        "insights.unanswered",
        "Review Unanswered Conversations",
        "search",
        ("review unanswered mail", "unanswered conversation review"),
        cli_path=("insights", "unanswered"),
        requires_connection=True,
        offline=False,
        help_topic="search.overview",
    ),
    _Seed(
        "storage.largest_messages",
        "Show Largest Messages",
        "storage",
        ("largest emails", "biggest messages"),
        (FOLDER,),
        cli_path=("insights", "storage"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "storage.largest_attachments",
        "Show Largest Attachments",
        "storage",
        ("largest attachments", "biggest files"),
        (FOLDER,),
        cli_path=("insights", "storage"),
        requires_connection=False,
        offline=True,
    ),
    _Seed(
        "cleanup.analyze",
        "Analyze Mailbox for Cleanup",
        "cleanup",
        ("clean mailbox", "free space safely"),
        (FOLDER, SIZE, DATE_RANGE),
        cli_path=("cleanup", "analyze"),
        requires_connection=True,
        offline=False,
    ),
    _Seed(
        "monitoring.start",
        "Start Monitoring",
        "monitoring",
        ("watch inbox", "start watcher"),
        (FOLDER, SENDER),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        False,
        True,
    ),
    _Seed(
        "monitoring.stop",
        "Stop Monitoring",
        "monitoring",
        ("stop watcher", "disable monitoring"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
    ),
    _Seed(
        "monitoring.status", "Show Monitoring Status", "monitoring", ("watch status", "last sync")
    ),
    _Seed(
        "notifications.settings",
        "Open Notification Settings",
        "notifications",
        ("notification settings", "configure alerts"),
    ),
    _Seed(
        "notifications.test",
        "Test Notification",
        "notifications",
        ("test desktop notification", "send test alert"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
    ),
    _Seed(
        "contacts.groups",
        "Manage Contact Groups",
        "contacts",
        ("recipient lists", "local groups", "personal contact groups"),
        offline=True,
        cli_path=("contact-groups", "list"),
    ),
    _Seed(
        "notifications.automatic_replies",
        "Manage Automatic Replies",
        "notifications",
        ("out of office", "automatic replies", "oof"),
        offline=False,
        requires_connection=True,
        cli_path=("automatic-replies", "show"),
    ),
    _Seed(
        "notifications.inbox_rules",
        "Manage Exchange Inbox Rules",
        "notifications",
        ("exchange rules", "inbox policies", "mailbox rules"),
        offline=False,
        requires_connection=True,
        cli_path=("inbox-rules", "list"),
    ),
    _Seed(
        "notifications.history",
        "Show Notification History",
        "notifications",
        ("recent notifications", "alert history"),
        cli_path=("notifications", "history"),
    ),
    _Seed(
        "integrations.open",
        "Open Integrations",
        "integrations",
        ("integrations", "webhook settings"),
    ),
    _Seed(
        "integrations.webhook_test",
        "Test Webhook",
        "integrations",
        ("test webhook", "webhook test"),
        (WEBHOOK_NAME,),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        False,
        True,
        (),
        ("webhooks", "test"),
    ),
    _Seed(
        "integrations.discord_test",
        "Test Discord Webhook",
        "integrations",
        ("test discord", "discord webhook test"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
        False,
        True,
    ),
    _Seed(
        "appearance.settings",
        "Open Appearance Settings",
        "appearance",
        ("appearance settings", "theme settings"),
    ),
    _Seed(
        "appearance.apply_theme",
        "Apply Theme",
        "appearance",
        ("change theme", "switch colors"),
        (THEME,),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "appearance.toggle_banner",
        "Toggle ASCII Banner",
        "appearance",
        ("hide banner", "show guc logo"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "layout.preview_wider",
        "Make Preview Wider",
        "layout",
        ("preview wider", "bigger reader"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "layout.preview_narrower",
        "Make Preview Narrower",
        "layout",
        ("preview narrower", "smaller reader"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "layout.toggle_preview",
        "Toggle Preview Pane",
        "layout",
        ("hide preview", "show preview"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "layout.reset",
        "Reset Layout",
        "layout",
        ("default layout", "restore layout"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        True,
        True,
    ),
    _Seed("settings.open", "Open Settings", "settings", ("settings", "preferences")),
    _Seed(
        "settings.search",
        "Search Settings",
        "settings",
        ("find setting", "search preferences"),
        (QUERY,),
    ),
    _Seed("activity.open", "Open Activity", "activity", ("activity log", "recent operations")),
    _Seed(
        "diagnostics.doctor",
        "Run Doctor",
        "diagnostics",
        ("run diagnostics", "fix guc mail"),
        cli_path=("doctor",),
    ),
    _Seed(
        "diagnostics.connection",
        "Check Connection",
        "diagnostics",
        ("connection broken", "test exchange connection"),
        cli_path=("doctor",),
    ),
    _Seed("diagnostics.logs", "Open Logs", "diagnostics", ("show logs", "error log")),
    _Seed(
        "diagnostics.ews_capabilities",
        "Show EWS Capabilities",
        "diagnostics",
        ("ews features", "server capabilities"),
        cli_path=("ews", "capabilities"),
    ),
    _Seed(
        "ews.delegates",
        "Show Mailbox Delegates",
        "ews",
        ("mailbox delegates", "show delegates", "delegation information"),
        offline=False,
        requires_connection=True,
        visibility=VisibilityLevel.ADVANCED,
        cli_path=("ews", "delegates"),
        help_topic="advanced.ews",
    ),
    _Seed(
        "ews.request_preview",
        "Preview Typed EWS Operation",
        "ews",
        ("preview ews request", "ews request preview"),
        (QUERY,),
        visibility=VisibilityLevel.EXPERT,
        cli_path=("ews", "request-preview"),
        help_topic="advanced.ews",
    ),
    _Seed(
        "offline.enter",
        "Enter Offline Mode",
        "offline",
        ("work offline", "disconnect mode"),
        (),
        SafetyClass.MUTATING,
        False,
        True,
        False,
        True,
    ),
    _Seed(
        "offline.browse",
        "Browse Offline Backup",
        "offline",
        ("offline mail", "open backup offline"),
        (REFERENCE,),
    ),
    _Seed("help.open", "Open Help", "help", ("help", "help center")),
    _Seed(
        "help.tutorials",
        "Guided Tutorials",
        "help",
        ("guided tutorials", "practice lessons", "restart tutorial", "first-time tutorial"),
        help_topic="start.interface",
    ),
    _Seed("help.search", "Search Help", "help", ("how do i", "find help"), (QUERY,)),
    _Seed("help.shortcuts", "Show Keyboard Shortcuts", "help", ("keyboard shortcuts", "hotkeys")),
)


_HELP_BY_CATEGORY = {
    "activity": "reference.commands",
    "appearance": "appearance.themes",
    "attachments": "attachments.safety",
    "backup": "backup.overview",
    "calendar": "calendar.overview",
    "cleanup": "cleanup.overview",
    "compose": "mail.compose",
    "contacts": "contacts.overview",
    "diagnostics": "diagnostics.doctor",
    "drafts": "mail.drafts",
    "ews": "advanced.ews",
    "folders": "mail.folders",
    "help": "reference.commands",
    "integrations": "integrations.webhooks",
    "layout": "appearance.layout",
    "mail": "mail.reading",
    "monitoring": "monitoring.overview",
    "notifications": "notifications.rules",
    "offline": "backup.offline",
    "scheduled": "mail.scheduled",
    "search": "search.overview",
    "settings": "settings.overview",
    "storage": "storage.quota",
    "tasks": "tasks.overview",
}

_SHORTCUT_BY_ID = {
    "compose.new": "c",
    "help.open": "?",
    "mail.delete": "d",
    "mail.forward": "f",
    "mail.open_inbox": "g i",
    "mail.open_message": "enter",
    "mail.reply": "r",
    "mail.reply_all": "a",
    "search.mail": "/",
}


_BASE_COMMAND_IDS_BY_PHRASE: dict[tuple[str, str], set[str]] = {}
for _seed in _SEEDS:
    for _phrase in (_seed.name, *_seed.aliases):
        _phrase_key = " ".join(re.findall(r"[a-z0-9]+", _phrase.casefold()))
        _BASE_COMMAND_IDS_BY_PHRASE.setdefault((_seed.category, _phrase_key), set()).add(
            _seed.command_id
        )


def minimum_action_command_id(category: str, name: str) -> str:
    """Return the stable ID used for one minimum-inventory action.

    The conversion is intentionally simple and covered by contract tests.  Once published, these
    IDs must not be changed merely to improve wording.
    """

    phrase_key = " ".join(re.findall(r"[a-z0-9]+", name.casefold()))
    matches = _BASE_COMMAND_IDS_BY_PHRASE.get((category, phrase_key), set())
    if len(matches) == 1:
        return next(iter(matches))
    fragment = phrase_key.replace(" ", "_")
    return f"{category}.{fragment}"


def _coverage_parameters(name: str) -> tuple[ParameterSpec, ...]:
    normalized = name.casefold()
    parameters: list[ParameterSpec] = []

    def include(parameter: ParameterSpec) -> None:
        if all(existing.name != parameter.name for existing in parameters):
            parameters.append(parameter)

    if (
        normalized.startswith(("search ", "find "))
        or normalized in {"advanced search", "exact search", "fuzzy search"}
        or " matching " in normalized
    ):
        include(QUERY)
    if normalized in {"duplicate draft", "edit draft"}:
        include(REFERENCE)
    if normalized in {"import theme", "export theme"}:
        include(THEME_PATH)
    if normalized == "reset setting":
        include(SETTING_NAME)
    if normalized == "reset category":
        include(SETTINGS_CATEGORY)
    if "folder" in normalized:
        include(FOLDER)
    if normalized == "copy message":
        include(FOLDER)
    if normalized == "set importance":
        include(IMPORTANCE_LEVEL)
    if normalized == "search by importance":
        include(IMPORTANCE_LEVEL)
    if normalized in {"add category", "remove category"}:
        include(CATEGORY)
    if normalized == "search by category":
        include(CATEGORY)
    if normalized == "search by size":
        include(SIZE)
    if any(
        word in normalized
        for word in ("attendee", "contact", "correspondent", "recipient", "sender")
    ):
        include(PERSON)
    if any(
        word in normalized
        for word in (" date", "delay", "due ", "reminder", "schedule", " time", "today", "week")
    ):
        include(WHEN)
    if any(word in normalized for word in ("current draft", "selected", "message details")):
        include(SELECTION if "messages" in normalized else REFERENCE)
    return tuple(parameters)


def _coverage_context(name: str) -> tuple[str, ...]:
    normalized = name.casefold()
    if normalized in {
        "add category",
        "copy message",
        "permanently delete",
        "remove category",
        "set importance",
    }:
        return ("selected_message",)
    if "current draft" in normalized:
        return ("current_draft",)
    if "selected contact" in normalized:
        return ("selected_contact",)
    if any(word in normalized for word in ("selected email", "selected message", "sender")):
        return ("selected_message",)
    if "invitation" in normalized:
        return ("selected_event",)
    if "current search" in normalized or "within results" in normalized:
        return ("current_search",)
    return ()


def _coverage_policy(
    category: str,
    name: str,
) -> tuple[SafetyClass, bool, bool, bool, bool, bool, VisibilityLevel]:
    normalized = name.casefold()
    first = normalized.split(maxsplit=1)[0]
    destructive = first in {"cancel", "delete", "discard", "empty", "prune", "remove"} or (
        normalized.startswith(("clear local", "execute cleanup", "permanently delete"))
    )
    mutation_verbs = {
        "accept",
        "add",
        "back",
        "cancel",
        "change",
        "clear",
        "compose",
        "configure",
        "create",
        "decline",
        "delete",
        "disable",
        "discard",
        "download",
        "duplicate",
        "edit",
        "empty",
        "enable",
        "execute",
        "export",
        "favorite",
        "flag",
        "force",
        "hide",
        "import",
        "insert",
        "make",
        "mark",
        "merge",
        "monitor",
        "move",
        "mute",
        "new",
        "pause",
        "permanently",
        "propose",
        "prune",
        "rebuild",
        "recover",
        "reopen",
        "remove",
        "rename",
        "repair",
        "request",
        "reset",
        "reschedule",
        "restore",
        "resume",
        "retry",
        "run",
        "save",
        "schedule",
        "send",
        "set",
        "show",
        "start",
        "stop",
        "switch",
        "synchronize",
        "tentatively",
        "toggle",
        "undo",
        "unflag",
    }
    # Plain "show" commands are observational.  The settings-like show actions below alter a
    # presentation/privacy preference and are the only show variants treated as mutations.
    mutates = first in mutation_verbs and not normalized.startswith("show ")
    mutates = mutates or normalized in {
        "copy message",
        "show ascii banner",
        "show full preview",
        "show sender only",
    }
    safety = (
        SafetyClass.DESTRUCTIVE
        if destructive
        else SafetyClass.MUTATING
        if mutates
        else SafetyClass.READ_ONLY
    )
    server_categories = {"calendar", "compose", "contacts", "drafts", "folders", "mail", "tasks"}
    server_change = mutates and category in server_categories
    if category == "scheduled" and normalized in {
        "run due jobs",
        "run scheduler now",
        "send scheduled message now",
    }:
        server_change = True
    if normalized.startswith(("save local", "open external", "export ")):
        server_change = False
    local_change = mutates and not server_change
    requires_connection = category in {
        "attachments",
        "calendar",
        "compose",
        "contacts",
        "drafts",
        "folders",
        "mail",
        "monitoring",
        "storage",
        "tasks",
    }
    if category == "search" and normalized in {
        "advanced search",
        "search all mail",
        "search attachment filename",
        "search body",
        "search by category",
        "search by date",
        "search by folder",
        "search by importance",
        "search by size",
        "search current folder",
        "search flagged",
        "search headers",
        "search mail",
        "search recipient",
        "search sender",
        "search subject",
        "search tasks",
        "search unread",
        "search with attachments",
    }:
        requires_connection = True
    local_categories = {
        "activity",
        "appearance",
        "backup",
        "help",
        "layout",
        "offline",
        "settings",
    }
    if "offline" in normalized or category in local_categories:
        requires_connection = False
    local_attachment_commands = {
        "download all attachments",
        "download attachment",
        "export attachment list",
        "find duplicate attachments",
        "hash attachment",
        "list attachments",
        "open attachments view",
        "save attachment as",
        "search attachment content",
        "show attachment storage statistics",
        "show largest attachments",
    }
    if category == "attachments" and normalized in local_attachment_commands:
        requires_connection = False
    local_draft_commands = {
        "delete draft",
        "duplicate draft",
        "edit draft",
        "export draft",
        "new draft",
        "recover autosaved draft",
        "schedule draft",
        "search drafts",
        "show draft conflicts",
        "show old drafts",
        "show unsynchronized drafts",
    }
    if category == "drafts" and normalized in local_draft_commands:
        requires_connection = False
    # Defining a watcher scope or predicate only edits local notification
    # configuration.  Exchange is contacted later by an explicitly started watcher.
    if category == "monitoring" and normalized.startswith("monitor "):
        requires_connection = False
    if category == "scheduled" and normalized in {
        "run due jobs",
        "run scheduler now",
        "send scheduled message now",
    }:
        requires_connection = True
    if category == "storage" and normalized in {
        "show mailbox quota",
        "show local cache size",
        "show search-index size",
        "show backup storage",
    }:
        # These views remain useful offline: configured capacity provenance and local
        # filesystem sizes do not depend on Exchange. Usage/remaining and mailbox
        # analytics below continue to require a connection.
        requires_connection = False
    if category == "cleanup":
        local_read_only = {
            "find duplicate attachments",
            "find old drafts",
        }
        server_mutations = {
            "execute cleanup plan",
            "undo recent move/delete when possible",
        }
        safety = SafetyClass.READ_ONLY
        server_change = False
        local_change = False
        confirmation = False
        requires_connection = normalized not in local_read_only
        if normalized in server_mutations:
            safety = SafetyClass.MUTATING
            server_change = True
            confirmation = True
            requires_connection = True
    confirmation = destructive or normalized.startswith(
        ("execute ", "import ", "restore ", "send ", "synchronize ")
    )
    confirmation = confirmation or (
        category == "scheduled" and normalized in {"run due jobs", "run scheduler now"}
    )
    if category == "cleanup" and normalized in {
        "execute cleanup plan",
        "undo recent move/delete when possible",
    }:
        confirmation = True
    if category == "monitoring":
        read_only = {
            "show monitoring status",
            "show last synchronization",
            "show watcher logs",
        }
        local_controls = {"pause monitoring", "stop monitoring"}
        connected_controls = {
            "start monitoring",
            "resume monitoring",
            "force synchronization",
        }
        if normalized in read_only:
            safety = SafetyClass.READ_ONLY
            local_change = False
            server_change = False
            confirmation = False
            requires_connection = False
        elif normalized.startswith("monitor "):
            safety = SafetyClass.MUTATING
            local_change = True
            server_change = False
            confirmation = False
            requires_connection = False
        elif normalized in local_controls:
            safety = SafetyClass.MUTATING
            local_change = True
            server_change = False
            requires_connection = False
        elif normalized in connected_controls:
            safety = SafetyClass.MUTATING
            local_change = True
            server_change = False
            requires_connection = True
    # Backup workflows deliberately mix local repository operations with bounded mailbox
    # reads and, for restore, an eventual mailbox mutation.  Treating the entire category
    # as offline-capable made the palette promise actions that the Backup screen correctly
    # refused while offline.  Keep the policy aligned with the operation that is actually
    # executed rather than with the screen that hosts it.
    if category == "backup":
        create_commands = {
            "create backup",
            "create full backup",
            "create incremental backup",
            "back up current folder",
            "back up selected messages",
            "back up search results",
            "back up cluster",
        }
        if normalized in create_commands:
            safety = SafetyClass.MUTATING
            server_change = False
            local_change = True
            confirmation = True
            requires_connection = True
        elif normalized == "estimate backup size":
            safety = SafetyClass.READ_ONLY
            server_change = False
            local_change = False
            confirmation = False
            requires_connection = True
        elif normalized == "resume backup":
            safety = SafetyClass.MUTATING
            server_change = False
            local_change = True
            confirmation = True
            requires_connection = True
        elif normalized in {"pause backup", "cancel backup"}:
            # Both actions are cooperative stops that retain a resumable checkpoint.
            safety = SafetyClass.MUTATING
            server_change = False
            local_change = True
            confirmation = False
            requires_connection = False
        elif normalized == "restore selected items":
            safety = SafetyClass.MUTATING
            server_change = True
            local_change = False
            confirmation = True
            requires_connection = True
    visibility = (
        VisibilityLevel.EXPERT
        if normalized.startswith(
            (
                "clear local",
                "execute cleanup",
                "permanently delete",
                "repair cache",
                "rebuild index",
            )
        )
        else VisibilityLevel.ADVANCED
        if category
        in {
            "activity",
            "backup",
            "cleanup",
            "diagnostics",
            "integrations",
            "monitoring",
            "offline",
            "storage",
        }
        or any(word in normalized for word in ("raw mime", "raw headers", "semantic"))
        else VisibilityLevel.BASIC
    )
    return (
        safety,
        server_change,
        local_change,
        confirmation,
        requires_connection,
        not requires_connection,
        visibility,
    )


def _keywords(seed: _Seed) -> tuple[str, ...]:
    values = [seed.category, *re.findall(r"[a-z0-9]+", seed.name.casefold())]
    values.extend(word for alias in seed.aliases for word in alias.casefold().split())
    return tuple(dict.fromkeys(values))


def _coverage_help_topic(category: str, name: str) -> str:
    if category == "mail" and name.casefold() == "permanently delete":
        return "privacy.credentials"
    return _HELP_BY_CATEGORY[category]


def _command_subcategory(name: str) -> str:
    """Derive a stable, human-facing workflow group from an action name."""

    normalized = name.casefold()
    groups = (
        (("open ", "show ", "view ", "list ", "browse ", "go ", "return "), "Navigation & views"),
        (("search ", "find ", "filter ", "sort ", "group "), "Search & discovery"),
        (("compose ", "reply", "forward", "send ", "notify "), "Communication"),
        (("create ", "new ", "add ", "import ", "duplicate "), "Create & import"),
        (("save ", "download ", "export ", "back up ", "restore "), "Data transfer"),
        (
            ("refresh ", "synchronize ", "rebuild ", "verify ", "test ", "check ", "diagnose "),
            "Operations & diagnostics",
        ),
        (
            ("delete ", "remove ", "empty ", "clear ", "discard ", "cancel ", "reset "),
            "Cleanup & removal",
        ),
        (
            (
                "select ",
                "copy ",
                "mark ",
                "flag",
                "unflag",
                "set ",
                "toggle ",
                "change ",
                "edit ",
                "rename ",
                "move ",
                "enable ",
                "disable ",
                "start ",
                "stop ",
            ),
            "Manage & organize",
        ),
    )
    for prefixes, label in groups:
        if normalized.startswith(prefixes):
            return label
    return "Tools & workflow"


def _command_misspellings(name: str) -> tuple[str, ...]:
    """Return one deterministic, plausible typo without creating a second corpus."""

    typo_tokens = {
        "attachment": "attchment",
        "attachments": "attchments",
        "backup": "bakup",
        "calendar": "calender",
        "compose": "comopse",
        "contact": "contcat",
        "contacts": "contcats",
        "diagnostics": "diagnotics",
        "folder": "fodler",
        "folders": "fodlers",
        "inbox": "inbxo",
        "message": "mesage",
        "messages": "mesages",
        "notification": "notifcation",
        "notifications": "notifcations",
        "scheduled": "schedueld",
        "search": "serch",
        "settings": "setings",
        "storage": "stroage",
    }
    words = name.casefold().split()
    for index, word in enumerate(words):
        replacement = typo_tokens.get(word)
        if replacement:
            candidate = list(words)
            candidate[index] = replacement
            return (" ".join(candidate),)
    first = words[0] if words else ""
    if len(first) >= 4:
        candidate = list(words)
        candidate[0] = first[0] + first[2] + first[1] + first[3:]
        return (" ".join(candidate),)
    return (name.casefold() + " command",)


_RELATED_SETTINGS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "activity": ("privacy.logging_level",),
    "appearance": ("appearance.theme", "appearance.branding"),
    "attachments": (
        "mail.download_directory",
        "mail.attachment_collision",
        "mail.max_attachment_bytes",
    ),
    "backup": ("backup.default_destination", "backup.default_kind"),
    "calendar": ("calendar.timezone", "calendar.default_view"),
    "cleanup": ("storage.cache_limit_mb", "search.default_scope"),
    "compose": ("composer.signature", "composer.autosave_seconds"),
    "contacts": ("contacts.sort_order", "contacts.autocomplete"),
    "diagnostics": ("privacy.logging_level", "advanced.raw_soap_enabled"),
    "drafts": ("composer.local_recovery", "composer.server_draft_sync"),
    "ews": ("advanced.raw_soap_enabled", "connection.endpoint"),
    "folders": ("mail.default_folder", "mail.folder_expansion"),
    "help": ("help.skill_level", "help.contextual_suggestions"),
    "integrations": ("integrations.notification_privacy",),
    "layout": ("layout.preview_visible", "layout.reader_mode"),
    "mail": ("mail.default_folder", "mail.page_size"),
    "monitoring": ("monitoring.enabled", "monitoring.poll_interval_seconds"),
    "notifications": ("monitoring.desktop_notifications", "monitoring.quiet_hours_enabled"),
    "offline": ("storage.cache_enabled", "privacy.cache_message_bodies"),
    "scheduled": ("composer.undo_send_seconds", "calendar.timezone"),
    "search": ("search.default_scope", "search.fuzzy_enabled"),
    "settings": ("advanced.mode",),
    "storage": ("storage.prohibit_send_mb", "storage.cache_limit_mb"),
    "tasks": ("tasks.default_view", "tasks.show_completed"),
}


# Short discovery phrases promised by Secondpass are intentionally shared by the small command
# family they describe.  Keeping these on the canonical registry makes the behavior identical in
# the TUI, Help autocomplete, and the presentation-neutral palette engine.  These are aliases, not
# hidden parser rules, so exact matching remains inspectable and deterministic.
_DISCOVERY_ALIASES: dict[str, tuple[str, ...]] = {
    "attachments.open": ("atta", "attch"),
    "attachments.show_largest_attachments": ("atta", "attch"),
    "attachments.search": ("atta", "attch"),
    "attachments.download": ("atta", "attch"),
    "backup.open": ("bkup",),
    "backup.create": ("bkup",),
    "backup.verify": ("bkup",),
    "notifications.settings": ("notif", "ntf"),
    "notifications.enable_desktop_notifications": ("notif", "ntf"),
    "notifications.test": ("notif", "ntf"),
    "notifications.history": ("notif", "ntf"),
    "calendar.tomorrow": ("cal tom",),
    "calendar.create_tomorrow": ("cal tom",),
    "calendar.show_tomorrow_events": ("cal tom",),
}

DISCOVERY_QUERY_GROUPS: dict[str, tuple[str, ...]] = {
    "atta": (
        "attachments.open",
        "attachments.show_largest_attachments",
        "attachments.search",
        "attachments.download",
    ),
    "attch": (
        "attachments.open",
        "attachments.show_largest_attachments",
        "attachments.search",
        "attachments.download",
    ),
    "bkp": ("backup.create", "backup.open", "backup.verify"),
    "bkup": ("backup.create", "backup.open", "backup.verify"),
    "notif": (
        "notifications.settings",
        "notifications.enable_desktop_notifications",
        "notifications.test",
        "notifications.history",
    ),
    "ntf": (
        "notifications.settings",
        "notifications.enable_desktop_notifications",
        "notifications.test",
        "notifications.history",
    ),
    "cal tom": (
        "calendar.tomorrow",
        "calendar.create_tomorrow",
        "calendar.show_tomorrow_events",
    ),
}


# A short verb can deliberately expand to several concrete actions when a compatible object is
# selected.  The tuple order is the presentation order promised by Secondpass; the parser still
# chooses only the first action, while the palette exposes the complete family for an explicit
# keyboard selection.
CONTEXTUAL_QUERY_GROUPS: dict[tuple[str, str], tuple[str, ...]] = {
    ("selected_message", "reply"): (
        "mail.reply",
        "mail.reply_all",
        "compose.reply",
    ),
    ("current_draft", "send later"): (
        "scheduled.schedule_current",
        "compose.send_after_delay",
    ),
    ("selected_event", "accept"): ("calendar.accept_invitation",),
    ("selected_contact", "email"): ("contacts.compose_to_contact",),
    ("current_backup", "search"): ("search.search_backups",),
}


def build_core_command_registry() -> CommandRegistry:
    specs = [
        CommandSpec(
            command_id=seed.command_id,
            name=seed.name,
            description=f"{seed.name}.",
            long_description=f"{seed.name}. Review any requested values before continuing.",
            category=seed.category,
            keywords=_keywords(seed),
            aliases=seed.aliases,
            misspellings=seed.misspellings,
            examples=seed.examples or seed.aliases,
            parameters=seed.parameters,
            context_requirements=seed.context,
            availability_rules=seed.availability,
            shortcut=seed.shortcut or _SHORTCUT_BY_ID.get(seed.command_id),
            safety=seed.safety,
            changes_server=seed.changes_server,
            changes_local=seed.changes_local,
            confirmation_required=seed.confirmation,
            preview_supported=seed.preview,
            offline=seed.offline,
            requires_connection=seed.requires_connection,
            required_services=seed.required_services,
            visibility=seed.visibility,
            cli_path=seed.cli_path,
            help_topic=seed.help_topic or _HELP_BY_CATEGORY[seed.category],
            handler_id=seed.handler_id or seed.command_id,
        )
        for seed in _SEEDS
    ]
    existing_ids = {spec.command_id for spec in specs}
    existing_indexes = {spec.command_id: index for index, spec in enumerate(specs)}
    existing_by_name = {spec.name.casefold(): spec for spec in specs}
    for category, names in MINIMUM_COMMAND_ACTIONS.items():
        for name in names:
            command_id = minimum_action_command_id(category, name)
            if command_id in existing_ids:
                # A minimum-inventory phrase may intentionally resolve to a
                # broader canonical command (for example ``Open Unread`` ->
                # ``Open Unread Mail``).  Keep the single stable command ID,
                # but preserve the specification wording as an exact alias so
                # Help, NLP, and contract validation all expose it verbatim.
                index = existing_indexes[command_id]
                current = specs[index]
                documented = {
                    " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
                    for value in (current.name, *current.aliases)
                }
                normalized_name = " ".join(re.findall(r"[a-z0-9]+", name.casefold()))
                if normalized_name not in documented:
                    specs[index] = replace(
                        current,
                        aliases=tuple(dict.fromkeys((*current.aliases, name))),
                    )
                continue
            preset = existing_by_name.get(name.casefold())
            if preset is not None:
                specs.append(
                    CommandSpec(
                        command_id=command_id,
                        name=name,
                        description=preset.description,
                        long_description=preset.long_description,
                        category=category,
                        keywords=tuple(dict.fromkeys((category, *preset.keywords))),
                        aliases=tuple(
                            dict.fromkeys(
                                (
                                    name.casefold(),
                                    command_id.replace(".", " ").replace("_", " "),
                                    *preset.aliases,
                                )
                            )
                        ),
                        parameters=preset.parameters,
                        context_requirements=preset.context_requirements,
                        availability_rules=preset.availability_rules,
                        shortcut=preset.shortcut,
                        safety=preset.safety,
                        changes_server=preset.changes_server,
                        changes_local=preset.changes_local,
                        confirmation_required=preset.confirmation_required,
                        preview_supported=preset.preview_supported,
                        offline=preset.offline,
                        requires_connection=preset.requires_connection,
                        required_services=preset.required_services,
                        visibility=preset.visibility,
                        cli_path=preset.cli_path,
                        help_topic=_coverage_help_topic(category, name),
                        handler_id=(
                            "drafts.open_drafts"
                            if category == "drafts" and name.casefold() == "open drafts"
                            else preset.handler_id
                        ),
                    )
                )
                existing_ids.add(command_id)
                existing_indexes[command_id] = len(specs) - 1
                continue
            (
                safety,
                changes_server,
                changes_local,
                confirmation,
                requires_connection,
                offline,
                visibility,
            ) = _coverage_policy(category, name)
            specs.append(
                CommandSpec(
                    command_id=command_id,
                    name=name,
                    description=f"{name} from the {category} workflow.",
                    long_description=(
                        f"Use this command to {name.casefold()}. It is routed through the shared "
                        f"{category} application service or its operational TUI screen so the "
                        "palette and direct UI use the same safety checks."
                    ),
                    category=category,
                    keywords=tuple(
                        dict.fromkeys((category, *re.findall(r"[a-z0-9]+", name.casefold())))
                    ),
                    aliases=(
                        name.casefold(),
                        command_id.replace(".", " ").replace("_", " "),
                    ),
                    examples=(name,),
                    parameters=_coverage_parameters(name),
                    context_requirements=_coverage_context(name),
                    availability_rules=(),
                    safety=safety,
                    changes_server=changes_server,
                    changes_local=changes_local,
                    confirmation_required=confirmation,
                    preview_supported=confirmation,
                    offline=offline,
                    requires_connection=requires_connection,
                    visibility=visibility,
                    help_topic=_coverage_help_topic(category, name),
                    handler_id=f"catalog.{category}",
                )
            )
            existing_ids.add(command_id)
            existing_indexes[command_id] = len(specs) - 1
    specs = [
        replace(
            spec,
            aliases=tuple(
                dict.fromkeys((*spec.aliases, *_DISCOVERY_ALIASES.get(spec.command_id, ())))
            ),
        )
        for spec in specs
    ]
    specs = [
        replace(
            spec,
            name="Open Reply Draft",
            description="Open a reply draft for the selected message.",
            long_description=(
                "Prepare and open a reply draft for the selected message without sending it."
            ),
            context_requirements=("selected_message",),
            parameters=(REFERENCE,),
        )
        if spec.command_id == "compose.reply"
        else spec
        for spec in specs
    ]
    # Preserve command IDs and handlers while distinguishing folder navigation from the
    # draft workspace. Old folder phrases remain compatible, without duplicate aliases.
    presentation = {
        "mail.open_drafts": ("Open Drafts folder", ("open drafts", "drafts", "unfinished mail")),
        "drafts.open_drafts": (
            "Manage drafts",
            ("open draft manager", "manage drafts", "local drafts"),
        ),
        "search.search_all_data": (
            "Open Local Search",
            ("open local search", "local search", "search all data", "search search all data"),
        ),
    }
    specs = [
        replace(
            spec, name=presentation[spec.command_id][0], aliases=presentation[spec.command_id][1]
        )
        if spec.command_id in presentation
        else spec
        for spec in specs
    ]
    commands_by_group: dict[tuple[str, str], list[CommandSpec]] = {}
    commands_by_category: dict[str, list[CommandSpec]] = {}
    for spec in specs:
        subcategory = _command_subcategory(spec.name)
        commands_by_group.setdefault((spec.category, subcategory), []).append(spec)
        commands_by_category.setdefault(spec.category, []).append(spec)

    enriched: list[CommandSpec] = []
    for spec in specs:
        subcategory = _command_subcategory(spec.name)
        peers = [
            peer.command_id
            for peer in commands_by_group[(spec.category, subcategory)]
            if peer.command_id != spec.command_id
        ]
        if len(peers) < 4:
            peers.extend(
                peer.command_id
                for peer in commands_by_category[spec.category]
                if peer.command_id != spec.command_id and peer.command_id not in peers
            )
        if not peers:
            peers.extend(
                peer.command_id
                for peer in specs
                if peer.command_id != spec.command_id and peer.help_topic == spec.help_topic
            )
        if not peers:
            peers.extend(peer.command_id for peer in specs if peer.command_id != spec.command_id)
        enriched.append(
            replace(
                spec,
                subcategory=subcategory,
                misspellings=spec.misspellings or _command_misspellings(spec.name),
                examples=spec.examples or (spec.name,),
                related_commands=tuple(peers[:4]),
                follow_up_commands=tuple(peers[:2]),
                related_settings=_RELATED_SETTINGS_BY_CATEGORY.get(spec.category, ()),
            )
        )
    registry = CommandRegistry(enriched)
    registry.assert_valid()
    return registry


CORE_COMMANDS = build_core_command_registry()
