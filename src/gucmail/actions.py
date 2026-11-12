"""Legacy action facade backed by the canonical universal-command registry.

``ActionSpec`` and ``DEFAULT_ACTIONS`` remain import-compatible with the first release.  All
safety, availability, shortcut, and Help metadata is projected from ``CORE_COMMANDS`` so the
compatibility API cannot drift into a second command catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from gucmail.command_catalog import CORE_COMMANDS
from gucmail.commands import CommandRegistry, CommandSpec, VisibilityLevel
from gucmail.models import SafetyClass


class UserMode(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"


_MODE_BY_VISIBILITY = {
    VisibilityLevel.BASIC: UserMode.BASIC,
    VisibilityLevel.ADVANCED: UserMode.ADVANCED,
    VisibilityLevel.EXPERT: UserMode.EXPERT,
}


# Stable first-release IDs are aliases only.  The values are canonical command IDs and therefore
# the sole source of behavioral metadata.  Keeping this small mapping is what lets old callers
# migrate without preserving the obsolete parallel action definitions.
LEGACY_ACTION_ALIASES: dict[str, str] = {
    "messages.list": "mail.open_inbox",
    "messages.open": "mail.open_message",
    "messages.reply": "mail.reply",
    "messages.reply_all": "mail.reply_all",
    "messages.forward": "mail.forward",
    "messages.delete": "mail.delete",
    "messages.hard_delete": "mail.permanently_delete",
    "drafts.open": "drafts.open_drafts",
    "scheduled.open": "scheduled.open",
    "calendar.open": "calendar.open",
    "contacts.open": "contacts.open",
    "tasks.open": "tasks.open",
    "search.open": "search.mail",
    "backup.create": "backup.create",
    "cleanup.plan": "cleanup.analyze",
    "cleanup.execute": "cleanup.execute_cleanup_plan",
    "ews.request_preview": "ews.request_preview",
}


_LEGACY_LABELS = {
    "messages.list": "List messages",
    "messages.open": "Open message",
    "messages.reply": "Reply",
    "messages.reply_all": "Reply all",
    "messages.forward": "Forward",
    "messages.delete": "Move to Deleted",
    "messages.hard_delete": "Permanently delete",
    "drafts.open": "Open draft",
    "scheduled.open": "Open scheduled messages",
    "calendar.open": "Open calendar",
    "contacts.open": "Open contacts",
    "tasks.open": "Open tasks",
    "search.open": "Search",
    "backup.create": "Create backup",
    "cleanup.plan": "Plan cleanup",
    "cleanup.execute": "Execute cleanup plan",
    "ews.request_preview": "Preview typed EWS operation",
}


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """First-release view of one canonical command."""

    name: str
    label: str
    area: str
    mode: UserMode
    safety: SafetyClass
    online: bool
    offline: bool
    confirmation: bool = False
    shortcut: str | None = None
    help_topic: str | None = None

    @property
    def canonical_id(self) -> str:
        return LEGACY_ACTION_ALIASES.get(self.name, self.name)

    def to_command_spec(self) -> CommandSpec:
        """Project this legacy ID without cloning behavioral metadata."""

        try:
            canonical = CORE_COMMANDS.get(self.canonical_id)
        except KeyError:
            # Retain the public constructor's historical behavior for third-party ActionSpec
            # instances while all built-in actions take the canonical path above.
            visibility = {
                UserMode.BASIC: VisibilityLevel.BASIC,
                UserMode.ADVANCED: VisibilityLevel.ADVANCED,
                UserMode.EXPERT: VisibilityLevel.EXPERT,
            }[self.mode]
            mutates = self.safety is not SafetyClass.READ_ONLY
            words = tuple(dict.fromkeys((self.area, *self.label.casefold().split())))
            return CommandSpec(
                command_id=self.name,
                name=self.label,
                description=f"{self.label} through the shared application action layer.",
                category=self.area,
                keywords=words,
                aliases=(self.label, self.name.replace(".", " ").replace("_", " ")),
                shortcut=self.shortcut,
                safety=self.safety,
                changes_server=mutates,
                confirmation_required=self.confirmation,
                preview_supported=self.confirmation,
                offline=self.offline,
                requires_connection=self.online and not self.offline,
                visibility=visibility,
                help_topic=self.help_topic,
                handler_id=self.name,
            )
        return replace(
            canonical,
            command_id=self.name,
            name=self.label,
            related_commands=(),
            follow_up_commands=(),
            aliases=tuple(
                dict.fromkeys(
                    (
                        self.label,
                        self.name.replace(".", " ").replace("_", " "),
                        *canonical.aliases,
                    )
                )
            ),
        )


def _legacy_action(legacy_id: str, canonical_id: str) -> ActionSpec:
    command = CORE_COMMANDS.get(canonical_id)
    return ActionSpec(
        name=legacy_id,
        label=_LEGACY_LABELS[legacy_id],
        area=command.category,
        mode=_MODE_BY_VISIBILITY[command.visibility],
        safety=command.safety,
        online=command.requires_connection,
        offline=command.offline,
        confirmation=command.confirmation_required,
        shortcut=command.shortcut,
        help_topic=command.help_topic,
    )


class ActionRegistry:
    """Compatibility lookup whose rows are derived from a canonical registry."""

    def __init__(
        self,
        aliases: dict[str, str] | None = None,
        *,
        commands: CommandRegistry = CORE_COMMANDS,
    ) -> None:
        mappings = aliases or LEGACY_ACTION_ALIASES
        # Validate against the supplied registry before constructing the immutable projection.
        for canonical_id in mappings.values():
            commands.get(canonical_id)
        self._actions = tuple(
            _legacy_action(legacy_id, canonical_id) for legacy_id, canonical_id in mappings.items()
        )
        self._by_name = {action.name: action for action in self._actions}

    def get(self, name: str) -> ActionSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"Unknown action: {name}") from exc

    def visible(self, mode: UserMode) -> tuple[ActionSpec, ...]:
        rank = {UserMode.BASIC: 0, UserMode.ADVANCED: 1, UserMode.EXPERT: 2}
        return tuple(action for action in self._actions if rank[action.mode] <= rank[mode])

    def all(self) -> tuple[ActionSpec, ...]:
        return self._actions

    def as_command_registry(self) -> CommandRegistry:
        registry = CommandRegistry(action.to_command_spec() for action in self.all())
        registry.assert_valid()
        return registry


DEFAULT_ACTIONS = ActionRegistry()
DEFAULT_ACTION_COMMANDS = DEFAULT_ACTIONS.as_command_registry()
