"""Generated command, settings, shortcut, and NLP reference material."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gucmail.commands import CommandSpec
from gucmail.help.models import HelpRuntimeContext
from gucmail.help.models import ShortcutHelpRecord as ShortcutHelpRecord


@dataclass(frozen=True, slots=True)
class DocumentedNlpExample:
    """Static Help example and its asserted parser outcome."""

    text: str
    interpretation_kind: str
    command_id: str | None


DOCUMENTED_NLP_EXAMPLES: tuple[DocumentedNlpExample, ...] = (
    DocumentedNlpExample("find unread mail from Ahmed Example", "command", "search.mail"),
    DocumentedNlpExample("open help about backups", "command", "help.search"),
    DocumentedNlpExample("show calendar this week", "command", "calendar.open_this_week"),
    DocumentedNlpExample("move these", "incomplete", None),
)


_BASE_SHORTCUTS: tuple[ShortcutHelpRecord, ...] = (
    ShortcutHelpRecord(
        "global.palette.primary",
        "Global",
        "ctrl+k",
        "Open Command Palette",
        True,
        "palette.primary_shortcut",
        "palette.open",
    ),
    ShortcutHelpRecord(
        "global.palette.secondary",
        "Global",
        "ctrl+p",
        "Open Command Palette",
        True,
        "palette.secondary_shortcut",
        "palette.open",
    ),
    ShortcutHelpRecord(
        "global.help", "Global", "?", "Open contextual Help", command_id="help.open"
    ),
    ShortcutHelpRecord(
        "global.theme.previous",
        "Global",
        "ctrl+alt+left",
        "Previous theme",
        True,
        "appearance.previous_theme_shortcut",
    ),
    ShortcutHelpRecord(
        "global.theme.next",
        "Global",
        "ctrl+alt+right",
        "Next theme",
        True,
        "appearance.next_theme_shortcut",
    ),
    ShortcutHelpRecord("global.refresh", "Global", "f5", "Refresh the current view"),
    ShortcutHelpRecord("global.focus.next", "Global", "tab", "Move focus forward"),
    ShortcutHelpRecord("global.focus.previous", "Global", "shift+tab", "Move focus backward"),
    ShortcutHelpRecord("global.back", "Global", "escape", "Close or return without applying"),
    ShortcutHelpRecord(
        "mail.open", "Mail", "enter", "Open selected message", command_id="mail.open_message"
    ),
    ShortcutHelpRecord("mail.compose", "Mail", "c", "Compose a message", command_id="compose.new"),
    ShortcutHelpRecord("mail.reply", "Mail", "r", "Reply", command_id="mail.reply"),
    ShortcutHelpRecord("mail.reply_all", "Mail", "a", "Reply all", command_id="mail.reply_all"),
    ShortcutHelpRecord("mail.forward", "Mail", "f", "Forward", command_id="mail.forward"),
    ShortcutHelpRecord(
        "mail.delete", "Mail", "d", "Delete with confirmation", command_id="mail.delete"
    ),
    ShortcutHelpRecord("mail.search", "Mail", "/", "Search mail", command_id="search.mail"),
    ShortcutHelpRecord("help.search", "Help", "ctrl+f", "Focus Help search"),
    ShortcutHelpRecord("help.back", "Help", "alt+left", "Previous Help page"),
    ShortcutHelpRecord("help.forward", "Help", "alt+right", "Next Help page"),
    ShortcutHelpRecord("help.home", "Help", "ctrl+home", "Help home"),
    ShortcutHelpRecord("help.favorite", "Help", "ctrl+d", "Favorite current topic"),
    ShortcutHelpRecord("settings.search", "Settings", "ctrl+f", "Focus Settings search"),
    ShortcutHelpRecord("settings.apply", "Settings", "ctrl+s", "Apply reviewed changes"),
    ShortcutHelpRecord("settings.cancel", "Settings", "ctrl+z", "Cancel staged changes"),
    ShortcutHelpRecord("calendar.create", "Calendar", "n", "Create event"),
    ShortcutHelpRecord("calendar.edit", "Calendar", "e", "Edit event"),
    ShortcutHelpRecord("calendar.delete", "Calendar", "d", "Delete selected event"),
    ShortcutHelpRecord("calendar.details", "Calendar", "enter", "Open event details"),
    ShortcutHelpRecord("calendar.previous", "Calendar", "left", "Previous period"),
    ShortcutHelpRecord("calendar.next", "Calendar", "right", "Next period"),
    ShortcutHelpRecord("calendar.today", "Calendar", "t", "Go to today"),
    ShortcutHelpRecord("contacts.create", "Contacts", "n", "Create contact"),
    ShortcutHelpRecord("contacts.edit", "Contacts", "e", "Edit contact"),
    ShortcutHelpRecord("contacts.delete", "Contacts", "d", "Delete selected contact"),
    ShortcutHelpRecord("contacts.details", "Contacts", "enter", "Open contact details"),
    ShortcutHelpRecord("tasks.create", "Tasks", "n", "Create task"),
    ShortcutHelpRecord("tasks.edit", "Tasks", "e", "Edit task"),
    ShortcutHelpRecord("tasks.delete", "Tasks", "d", "Delete selected task"),
    ShortcutHelpRecord("tasks.complete", "Tasks", "x", "Complete task"),
    ShortcutHelpRecord("tasks.details", "Tasks", "enter", "Open task details"),
    ShortcutHelpRecord("backup.estimate", "Backup", "e", "Estimate backup"),
    ShortcutHelpRecord("backup.create", "Backup", "c", "Create backup"),
    ShortcutHelpRecord("backup.resume", "Backup", "u", "Resume backup"),
    ShortcutHelpRecord("backup.verify", "Backup", "v", "Verify backup"),
    ShortcutHelpRecord("backup.browse", "Backup", "enter", "Browse backup"),
    ShortcutHelpRecord("scheduled.edit", "Scheduled", "e", "Edit scheduled draft"),
    ShortcutHelpRecord("scheduled.reschedule", "Scheduled", "s", "Reschedule message"),
    ShortcutHelpRecord("scheduled.duplicate", "Scheduled", "d", "Duplicate schedule"),
    ShortcutHelpRecord("scheduled.history", "Scheduled", "h", "Open schedule history"),
    ShortcutHelpRecord("appearance.search", "Appearance", "ctrl+f", "Search themes or branding"),
    ShortcutHelpRecord("appearance.preview", "Appearance", "enter", "Preview selection"),
    ShortcutHelpRecord("appearance.apply", "Appearance", "ctrl+enter", "Apply preview"),
    ShortcutHelpRecord("appearance.favorite", "Appearance", "f", "Toggle favorite"),
)


def shortcut_records(context: HelpRuntimeContext | None = None) -> tuple[ShortcutHelpRecord, ...]:
    """Return current bindings, applying only explicit non-secret overrides."""

    if context is not None and context.shortcut_assignments is not None:
        return context.shortcut_assignments
    settings = context.settings if context is not None else {}
    overrides = context.shortcuts if context is not None else {}
    records: list[ShortcutHelpRecord] = []
    for record in _BASE_SHORTCUTS:
        key = overrides.get(record.shortcut_id)
        if key is None and record.command_id:
            key = overrides.get(record.command_id)
        if key is None and record.setting_id:
            configured = settings.get(record.setting_id)
            if isinstance(configured, str):
                key = configured
        records.append(
            ShortcutHelpRecord(
                shortcut_id=record.shortcut_id,
                scope=record.scope,
                key=key.strip() if key is not None else record.key,
                action=record.action,
                customizable=record.customizable,
                setting_id=record.setting_id,
                command_id=record.command_id,
            )
        )
    return tuple(records)


def shortcut_conflicts(
    records: Sequence[ShortcutHelpRecord],
) -> Mapping[tuple[str, str], tuple[str, ...]]:
    """Return same-scope duplicate keys; reuse across different screens is intentional."""

    grouped: dict[tuple[str, str], list[ShortcutHelpRecord]] = {}
    for record in records:
        if record.key:
            grouped.setdefault(
                (
                    (record.context_id or record.context or record.scope).casefold(),
                    record.key.casefold(),
                ),
                [],
            ).append(record)
    return {
        key: tuple(record.shortcut_id for record in values)
        for key, values in grouped.items()
        if len({record.binding_action or record.action for record in values}) > 1
    }


def command_reference_markdown(commands: Sequence[CommandSpec]) -> str:
    """Render every canonical field that users need without hand-maintained drift."""

    rows = [
        "| Command and purpose | Inputs | Shortcut / CLI | Availability and effects | Related |",
        "|---|---|---|---|---|",
    ]
    for command in sorted(
        commands,
        key=lambda item: (item.category.casefold(), item.name.casefold(), item.command_id),
    ):
        parameters = (
            ", ".join(_parameter_label(parameter) for parameter in command.parameters) or "None"
        )
        cli = " ".join(command.cli_path or ())
        launch = f"`gucmail {cli}`" if cli else "Palette/TUI"
        shortcut = f"`{command.shortcut}` · " if command.shortcut else ""
        availability = "Offline" if command.offline else "Connected mailbox"
        effects = []
        if command.changes_server:
            effects.append("server change")
        if command.changes_local:
            effects.append("local change")
        if not effects:
            effects.append("read-only")
        if command.confirmation_required:
            effects.append("confirmation")
        services = ", ".join(command.required_services)
        if services:
            effects.append(f"needs {services}")
        related = (
            ", ".join(
                f"`{identifier}`"
                for identifier in (*command.related_commands, *command.follow_up_commands)
            )
            or "—"
        )
        command_label = (
            f"[`{command.command_id}`](command:{command.command_id}) — "
            f"**{_escape(command.name)}**"
            f"<br>{_escape(command.description)}"
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    command_label,
                    _escape(parameters),
                    shortcut + launch,
                    f"{availability}; {', '.join(effects)}",
                    related,
                )
            )
            + " |"
        )
    return "\n".join(rows)


def natural_language_reference_markdown(commands: Sequence[CommandSpec]) -> str:
    """Render one regression-covered phrase per command, grouped by category."""

    from gucmail.nlp import load_command_corpus

    corpus = load_command_corpus()
    phrase_by_id = {group.command_id: group.phrases[0] for group in corpus.groups if group.phrases}
    categories: dict[str, list[CommandSpec]] = {}
    for command in commands:
        if command.command_id in phrase_by_id:
            categories.setdefault(command.category, []).append(command)
    blocks: list[str] = []
    for category in sorted(categories, key=str.casefold):
        blocks.extend((f"### {_escape(category.title())}", ""))
        for command in sorted(categories[category], key=lambda item: item.name.casefold()):
            phrase = phrase_by_id[command.command_id]
            blocks.append(f"- `{_escape(phrase)}` → `{command.command_id}`")
        blocks.append("")
    return "\n".join(blocks).rstrip()


def settings_reference_markdown(context: HelpRuntimeContext | None = None) -> str:
    """Render settings metadata plus current safe values from the supplied local context."""

    from gucmail.settings import DEFAULT_SETTINGS

    current = context.settings if context is not None else {}
    rows = [
        "| Setting | Purpose | Default / current | Options and effect | Help |",
        "|---|---|---|---|---|",
    ]
    for spec in sorted(
        DEFAULT_SETTINGS,
        key=lambda setting: (setting.category.casefold(), setting.label.casefold()),
    ):
        default = "[stored securely]" if spec.sensitive else _value(spec.default)
        if spec.sensitive:
            present = spec.setting_id in current and current[spec.setting_id] not in (
                None,
                "",
                (),
                {},
            )
            current_value = "configured" if present else "not shown"
        else:
            current_value = _value(current.get(spec.setting_id, spec.default))
        constraints: list[str] = []
        if spec.choices:
            constraints.append("options: " + ", ".join(spec.choices))
        if spec.minimum is not None or spec.maximum is not None:
            minimum = spec.minimum if spec.minimum is not None else "..."
            maximum = spec.maximum if spec.maximum is not None else "..."
            constraints.append(f"range: {minimum}-{maximum}")
        constraints.append(f"apply: {spec.apply_semantics.value}")
        setting_label = (
            f"[`{spec.setting_id}`](setting:{spec.setting_id}) — "
            f"**{_escape(spec.label)}**"
            f"<br>{_escape(spec.category)} / {spec.scope.value}"
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    setting_label,
                    _escape(spec.description),
                    f"`{_escape(default)}` / `{_escape(current_value)}`",
                    _escape("; ".join(constraints)),
                    f"[{_escape(spec.help_id)}](help:{spec.help_id})",
                )
            )
            + " |"
        )
    return "\n".join(rows)


def shortcuts_reference_markdown(context: HelpRuntimeContext | None = None) -> str:
    records = shortcut_records(context)
    conflicts = shortcut_conflicts(records)
    rows = [
        "| Scope / context | Current shortcut | Action | Customizable | Conflict |",
        "|---|---|---|---|---|",
    ]
    for record in records:
        conflict = (
            (record.context_id or record.context or record.scope).casefold(),
            record.key.casefold(),
        ) in conflicts
        rows.append(
            "| "
            + " | ".join(
                (
                    _escape(record.scope + (f" / {record.context}" if record.context else "")),
                    f"`{_escape(record.key)}`" if record.key else "Disabled",
                    _escape(record.action),
                    "Yes" if record.customizable else "No",
                    "Yes — review Settings" if conflict else "No",
                )
            )
            + " |"
        )
    return "\n".join(rows)


def _parameter_label(parameter: object) -> str:
    from gucmail.commands import ParameterSpec

    if not isinstance(parameter, ParameterSpec):
        return str(parameter)
    label = parameter.name
    if parameter.required:
        label += " (required)"
    elif parameter.has_default:
        label += f"={parameter.default}"
    else:
        label += " (optional)"
    if parameter.multiple:
        label += " repeated"
    return label


def _value(value: object) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (tuple, list)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
    )
