"""Typed command metadata shared by CLI, TUI, Help, and language interfaces.

The registry deliberately describes commands without executing them.  Presentation layers can
discover, rank, preview, and validate an invocation without importing mailbox credentials or
opening an Exchange session.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz

from gucmail.models import SafetyClass

COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ParameterType(StrEnum):
    """Portable parameter types used by all command front ends."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    EMAIL = "email"
    PERSON = "person"
    FOLDER = "folder"
    DATETIME = "datetime"
    DATE_RANGE = "date_range"
    SIZE = "size"
    SIZE_RANGE = "size_range"
    ENUM = "enum"
    REFERENCE = "reference"
    PATH = "path"
    QUERY = "query"
    EXCLUSION = "exclusion"
    RANKING = "ranking"
    THREAD_RESPONSE = "thread_response"


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One explicit negative search clause passed through every command surface."""

    field: str
    value: str
    expression: str

    def __post_init__(self) -> None:
        if self.field not in {
            "attachment_type",
            "folder",
            "has_attachments",
            "item_type",
            "sender",
            "text",
        }:
            raise ValueError(f"Unsupported exclusion field: {self.field}")
        if not self.value.strip():
            raise ValueError("An exclusion value cannot be empty.")


@dataclass(frozen=True, slots=True)
class Ranking:
    """Stable ordering and optional bounded result-count semantics."""

    field: str
    direction: str
    limit: int | None = None
    expression: str = ""

    def __post_init__(self) -> None:
        if self.field not in {
            "attachment_type_frequency",
            "received_at",
            "sender_frequency",
            "size",
            "thread_message_count",
        }:
            raise ValueError(f"Unsupported ranking field: {self.field}")
        if self.direction not in {"ascending", "descending"}:
            raise ValueError(f"Unsupported ranking direction: {self.direction}")
        if self.limit is not None and not 1 <= self.limit <= 1_000:
            raise ValueError("A ranking limit must be between 1 and 1000.")


@dataclass(frozen=True, slots=True)
class ThreadResponseState:
    """Conversation-response ownership for index-backed search evaluation."""

    state: str
    expression: str

    def __post_init__(self) -> None:
        if self.state not in {"waiting_on_me", "waiting_on_them"}:
            raise ValueError(f"Unsupported thread-response state: {self.state}")


class VisibilityLevel(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"


class AvailabilityState(StrEnum):
    """Runtime availability of a command in the current application context."""

    AVAILABLE = "available"
    NEEDS_CONTEXT = "needs_context"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One typed input accepted by a :class:`CommandSpec`."""

    name: str
    type: ParameterType = ParameterType.STRING
    description: str = ""
    required: bool = False
    has_default: bool = False
    default: object | None = None
    choices: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    multiple: bool = False
    minimum: float | None = None
    maximum: float | None = None
    context_source: str | None = None


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Canonical metadata for one user-facing command or parameterized command preset."""

    command_id: str
    name: str
    description: str
    category: str
    keywords: tuple[str, ...]
    aliases: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...] = ()
    long_description: str = ""
    subcategory: str | None = None
    misspellings: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    context_requirements: tuple[str, ...] = ()
    availability_rules: tuple[str, ...] = ()
    shortcut: str | None = None
    safety: SafetyClass = SafetyClass.READ_ONLY
    changes_server: bool = False
    changes_local: bool = False
    confirmation_required: bool = False
    preview_supported: bool = False
    offline: bool = False
    requires_connection: bool = False
    required_services: tuple[str, ...] = ()
    visibility: VisibilityLevel = VisibilityLevel.BASIC
    related_commands: tuple[str, ...] = ()
    follow_up_commands: tuple[str, ...] = ()
    related_settings: tuple[str, ...] = ()
    cli_path: tuple[str, ...] | None = None
    help_topic: str | None = None
    handler_id: str | None = None

    @property
    def mutates(self) -> bool:
        return self.safety is not SafetyClass.READ_ONLY or self.changes_server or self.changes_local


SlotValue = object


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """A command ID and typed parameters, independent from CLI argument syntax."""

    command_id: str
    parameters: tuple[tuple[str, SlotValue], ...] = ()
    original_text: str | None = None
    confidence: float = 1.0
    explanation: str = ""

    @classmethod
    def from_mapping(
        cls,
        command_id: str,
        parameters: Mapping[str, SlotValue] | None = None,
        *,
        original_text: str | None = None,
        confidence: float = 1.0,
        explanation: str = "",
    ) -> CommandInvocation:
        values = tuple(sorted((parameters or {}).items(), key=lambda item: item[0]))
        return cls(
            command_id=command_id,
            parameters=values,
            original_text=original_text,
            confidence=confidence,
            explanation=explanation,
        )

    def as_dict(self) -> dict[str, SlotValue]:
        return dict(self.parameters)

    def get(self, name: str, default: SlotValue = None) -> SlotValue:
        return self.as_dict().get(name, default)


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Presentation-neutral facts used to rank and gate commands.

    ``handler_ids`` is optional on purpose.  A metadata-only consumer such as Help can omit it;
    an executing surface should pass the handlers it actually bound so nonfunctional buttons are
    reported as unavailable instead of appearing to work.
    """

    online: bool = False
    offline_mode: bool = False
    expert_mode: bool = False
    services: frozenset[str] = frozenset()
    values: tuple[tuple[str, str], ...] = ()
    handler_ids: frozenset[str] | None = None

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str] | None = None,
        *,
        online: bool = False,
        offline_mode: bool = False,
        expert_mode: bool = False,
        services: Iterable[str] = (),
        handler_ids: Iterable[str] | None = None,
    ) -> CommandContext:
        return cls(
            online=online,
            offline_mode=offline_mode,
            expert_mode=expert_mode,
            services=frozenset(services),
            values=tuple(sorted((values or {}).items())),
            handler_ids=None if handler_ids is None else frozenset(handler_ids),
        )

    def get(self, name: str, default: str | None = None) -> str | None:
        return dict(self.values).get(name, default)


@dataclass(frozen=True, slots=True)
class CommandAvailability:
    """Why a command can or cannot be executed right now."""

    state: AvailabilityState
    reasons: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()
    missing_services: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.state is AvailabilityState.AVAILABLE


@dataclass(frozen=True, slots=True)
class CliProjection:
    """A conservative projection to an installed CLI leaf.

    The palette never launches this vector.  It is useful for previews, documentation, and copy
    actions.  A projection is withheld whenever preserving the typed semantics cannot be proven.
    """

    available: bool
    argv: tuple[str, ...] | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """Validated execution preview shared by TUI and other in-process front ends."""

    command: CommandSpec
    invocation: CommandInvocation
    availability: CommandAvailability
    preview: tuple[str, ...]
    confirmation_required: bool
    cli: CliProjection
    validation_errors: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.availability.available and not self.validation_errors


class CommandExecutionError(RuntimeError):
    """Base error for safe in-process command dispatch."""


class CommandUnavailableError(CommandExecutionError):
    """Raised when an invocation has no executable handler in the supplied context."""


class CommandConfirmationRequired(CommandExecutionError):
    """Raised when a caller tries to bypass a required confirmation preview."""


class RegistryValidationError(ValueError):
    """Raised when canonical command metadata contains inconsistent references."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Invalid command registry:\n- " + "\n- ".join(self.errors))


class CommandRegistry:
    """Validated, deterministic collection of canonical commands."""

    def __init__(self, commands: Iterable[CommandSpec] = ()) -> None:
        self._commands: dict[str, CommandSpec] = {}
        duplicates: list[str] = []
        for command in commands:
            if command.command_id in self._commands:
                duplicates.append(command.command_id)
                continue
            self._commands[command.command_id] = command
        if duplicates:
            raise RegistryValidationError(
                f"duplicate command ID: {command_id}" for command_id in sorted(set(duplicates))
            )

    def get(self, command_id: str) -> CommandSpec:
        try:
            return self._commands[command_id]
        except KeyError as exc:
            raise KeyError(f"Unknown command: {command_id}") from exc

    def all(self) -> tuple[CommandSpec, ...]:
        return tuple(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)

    def merge(self, *registries: CommandRegistry) -> CommandRegistry:
        commands = list(self.all())
        for registry in registries:
            commands.extend(registry.all())
        return CommandRegistry(commands)

    def aliases(self, text: str) -> tuple[CommandSpec, ...]:
        normalized = _normalize_search_text(text)
        return tuple(
            command
            for command in self._commands.values()
            if normalized
            in {
                _normalize_search_text(command.name),
                *(_normalize_search_text(alias) for alias in command.aliases),
                *(_normalize_search_text(alias) for alias in command.misspellings),
            }
        )

    def search(self, query: str, *, limit: int = 20) -> tuple[CommandSpec, ...]:
        """Return deterministic local fuzzy results without executing availability checks."""

        normalized = _normalize_search_text(query)
        if not normalized:
            return self.all()[:limit]
        ranked: list[tuple[float, str, CommandSpec]] = []
        for command in self._commands.values():
            candidates = (
                command.command_id,
                command.name,
                *command.aliases,
                *command.misspellings,
                *command.keywords,
                command.long_description,
                command.subcategory or "",
                " ".join(command.cli_path or ()),
                *command.related_settings,
            )
            score = max(
                fuzz.WRatio(normalized, _normalize_search_text(item)) for item in candidates
            )
            ranked.append((float(score), command.command_id, command))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[: max(0, limit)])

    def validate(
        self,
        *,
        cli_paths: Iterable[tuple[str, ...]] | None = None,
        help_topics: Iterable[str] | None = None,
        setting_ids: Iterable[str] | None = None,
    ) -> tuple[str, ...]:
        """Return all metadata and cross-reference errors instead of failing at the first one."""

        known_ids = set(self._commands)
        known_cli = set(cli_paths) if cli_paths is not None else None
        known_help = set(help_topics) if help_topics is not None else None
        known_settings = set(setting_ids) if setting_ids is not None else None
        errors: list[str] = []
        for command in self._commands.values():
            prefix = command.command_id
            if not COMMAND_ID_RE.fullmatch(command.command_id):
                errors.append(f"{prefix}: invalid stable command ID")
            for field_name, value in (
                ("name", command.name),
                ("description", command.description),
                ("category", command.category),
            ):
                if not value.strip():
                    errors.append(f"{prefix}: missing {field_name}")
            if not command.keywords:
                errors.append(f"{prefix}: missing search keywords")
            if not command.aliases:
                errors.append(f"{prefix}: missing aliases")
            if command.offline and command.requires_connection:
                errors.append(f"{prefix}: cannot be offline and require a connection")
            if command.safety is SafetyClass.READ_ONLY and (
                command.changes_local or command.changes_server
            ):
                errors.append(f"{prefix}: read-only command declares a data mutation")
            if command.safety is not SafetyClass.READ_ONLY and not (
                command.changes_local or command.changes_server
            ):
                errors.append(f"{prefix}: mutating command does not identify its mutation target")
            if command.safety in {SafetyClass.DESTRUCTIVE, SafetyClass.PRIVILEGED} and not (
                command.confirmation_required and command.preview_supported
            ):
                errors.append(
                    f"{prefix}: destructive/privileged command lacks preview confirmation"
                )
            parameter_names: set[str] = set()
            for parameter in command.parameters:
                parameter_prefix = f"{prefix}.{parameter.name}"
                if not parameter.name or parameter.name in parameter_names:
                    errors.append(f"{parameter_prefix}: missing or duplicate parameter name")
                parameter_names.add(parameter.name)
                if parameter.required and parameter.has_default:
                    errors.append(f"{parameter_prefix}: required parameter cannot have a default")
                if (
                    parameter.minimum is not None
                    and parameter.maximum is not None
                    and parameter.minimum > parameter.maximum
                ):
                    errors.append(f"{parameter_prefix}: invalid numeric range")
                if parameter.type is ParameterType.ENUM and not parameter.choices:
                    errors.append(f"{parameter_prefix}: enum has no choices")
            for relation in (*command.related_commands, *command.follow_up_commands):
                if relation not in known_ids:
                    errors.append(f"{prefix}: references unknown command {relation}")
            if (
                known_cli is not None
                and command.cli_path is not None
                and command.cli_path not in known_cli
            ):
                errors.append(f"{prefix}: references unknown CLI path {' '.join(command.cli_path)}")
            if (
                known_help is not None
                and command.help_topic is not None
                and command.help_topic not in known_help
            ):
                errors.append(f"{prefix}: references unknown Help topic {command.help_topic}")
            if known_settings is not None:
                for setting_id in command.related_settings:
                    if setting_id not in known_settings:
                        errors.append(f"{prefix}: references unknown setting {setting_id}")
        return tuple(errors)

    def assert_valid(
        self,
        *,
        cli_paths: Iterable[tuple[str, ...]] | None = None,
        help_topics: Iterable[str] | None = None,
        setting_ids: Iterable[str] | None = None,
    ) -> None:
        errors = self.validate(
            cli_paths=cli_paths,
            help_topics=help_topics,
            setting_ids=setting_ids,
        )
        if errors:
            raise RegistryValidationError(errors)

    def validate_invocation(self, invocation: CommandInvocation) -> tuple[str, ...]:
        try:
            command = self.get(invocation.command_id)
        except KeyError:
            return (f"unknown command ID: {invocation.command_id}",)
        supplied = invocation.as_dict()
        expected = {parameter.name: parameter for parameter in command.parameters}
        errors = [
            f"{command.command_id}: unknown parameter {name}"
            for name in supplied
            if name not in expected
        ]
        errors.extend(
            f"{command.command_id}: missing required parameter {parameter.name}"
            for parameter in command.parameters
            if parameter.required and parameter.name not in supplied
        )
        for name, value in supplied.items():
            parameter = expected.get(name)
            if parameter is not None and not _parameter_accepts(parameter, value):
                errors.append(
                    f"{command.command_id}: parameter {name} does not accept {type(value).__name__}"
                )
        return tuple(errors)

    def availability(
        self,
        command_id: str,
        context: CommandContext,
        *,
        invocation: CommandInvocation | None = None,
    ) -> CommandAvailability:
        """Evaluate declared requirements without contacting Exchange or loading credentials."""

        command = self.get(command_id)
        reasons: list[str] = []
        supplied = invocation.as_dict() if invocation is not None else {}
        missing_context = tuple(
            requirement
            for requirement in command.context_requirements
            if not _context_requirement_met(requirement, context, supplied)
        )
        missing_services = tuple(
            service for service in command.required_services if service not in context.services
        )
        if command.requires_connection and not context.online:
            reasons.append("An Exchange connection is required.")
        if context.offline_mode and not command.offline:
            reasons.append("This command is unavailable in offline mode.")
        if command.visibility is VisibilityLevel.EXPERT and not context.expert_mode:
            reasons.append("Expert mode is required.")
        if missing_services:
            reasons.append("Required service is not running: " + ", ".join(missing_services))
        for rule in command.availability_rules:
            if rule == "not_implemented":
                reasons.append("This operation is documented but is not implemented by this build.")
            elif rule.startswith("unavailable:"):
                reasons.append(rule.partition(":")[2].strip() or "This command is unavailable.")
            elif rule.startswith("service:"):
                service = rule.partition(":")[2]
                if service and service not in context.services and service not in missing_services:
                    missing_services = (*missing_services, service)
                    reasons.append(f"Required service is not running: {service}")
        if context.handler_ids is not None:
            handler_id = command.handler_id or command.command_id
            if handler_id not in context.handler_ids:
                reasons.append("No in-process handler is bound for this command.")
        if reasons:
            return CommandAvailability(
                AvailabilityState.UNAVAILABLE,
                tuple(dict.fromkeys(reasons)),
                missing_context,
                missing_services,
            )
        if missing_context:
            return CommandAvailability(
                AvailabilityState.NEEDS_CONTEXT,
                ("Select or supply: " + ", ".join(missing_context),),
                missing_context,
                missing_services,
            )
        return CommandAvailability(AvailabilityState.AVAILABLE)


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    safety: SafetyClass
    changes_server: bool
    changes_local: bool
    confirmation_required: bool
    preview_supported: bool
    offline: bool
    requires_connection: bool


PolicyResolver = Callable[[tuple[str, ...], Any], CommandPolicy]


def discover_typer_registry(
    typer_app: Any,
    *,
    policy_resolver: PolicyResolver | None = None,
) -> CommandRegistry:
    """Discover the installed Typer tree without invoking a command or starting a subprocess."""

    from typer.core import TyperGroup
    from typer.main import get_command

    root = get_command(typer_app)
    resolver = policy_resolver or _default_policy
    commands: list[CommandSpec] = []

    def walk(command: Any, path: tuple[str, ...]) -> None:
        if isinstance(command, TyperGroup):
            for name, child in command.commands.items():
                walk(child, (*path, name))
            return
        if not path:
            return
        command_id = "cli." + ".".join(part.replace("-", "_") for part in path)
        leaf_words = path[-1].replace("-", " ")
        group_words = path[0].replace("-", " ")
        display_name = " ".join(part.replace("-", " ") for part in path).title()
        description = (command.help or command.short_help or f"Run {' '.join(path)}.").strip()
        policy = resolver(path, command)
        aliases = _unique(
            (
                " ".join(path),
                leaf_words,
                f"{group_words} {leaf_words}",
            )
        )
        keywords = _unique((*path, *leaf_words.split(), *group_words.split()))
        commands.append(
            CommandSpec(
                command_id=command_id,
                name=display_name,
                description=description,
                long_description=description,
                category=path[0].replace("-", "_"),
                subcategory=path[-2].replace("-", "_") if len(path) > 2 else None,
                keywords=keywords,
                aliases=aliases,
                parameters=tuple(_click_parameter(parameter) for parameter in command.params),
                safety=policy.safety,
                changes_server=policy.changes_server,
                changes_local=policy.changes_local,
                confirmation_required=policy.confirmation_required,
                preview_supported=policy.preview_supported,
                offline=policy.offline,
                requires_connection=policy.requires_connection,
                cli_path=path,
                handler_id=command_id,
            )
        )

    walk(root, ())
    registry = CommandRegistry(commands)
    registry.assert_valid()
    return registry


def discover_cli_paths(typer_app: Any) -> frozenset[tuple[str, ...]]:
    return frozenset(
        command.cli_path
        for command in discover_typer_registry(typer_app).all()
        if command.cli_path is not None
    )


def _click_parameter(parameter: Any) -> ParameterSpec:
    from typer.core import TyperOption

    parameter_type = _click_parameter_type(parameter)
    aliases: tuple[str, ...] = ()
    if isinstance(parameter, TyperOption):
        aliases = _unique((*parameter.opts, *parameter.secondary_opts))
    required = bool(parameter.required)
    default = parameter.default
    has_default = default is not None and not required
    choices = tuple(str(item) for item in getattr(parameter.type, "choices", ()) or ())
    return ParameterSpec(
        name=str(parameter.name or "value").replace("-", "_"),
        type=parameter_type,
        description=(getattr(parameter, "help", None) or "").strip(),
        required=required,
        has_default=has_default,
        default=default if has_default else None,
        choices=choices,
        aliases=aliases,
        multiple=bool(getattr(parameter, "multiple", False) or parameter.nargs != 1),
    )


def _click_parameter_type(parameter: Any) -> ParameterType:
    from typer.core import TyperArgument

    name = str(getattr(parameter.type, "name", "text") or "text").casefold()
    if isinstance(parameter, TyperArgument) and "path" in name:
        return ParameterType.PATH
    if "bool" in name:
        return ParameterType.BOOLEAN
    if "int" in name:
        return ParameterType.INTEGER
    if "float" in name:
        return ParameterType.NUMBER
    if "choice" in name:
        return ParameterType.ENUM
    if "path" in name or "file" in name:
        return ParameterType.PATH
    return ParameterType.STRING


def _default_policy(path: tuple[str, ...], _command: Any) -> CommandPolicy:
    words = {part.casefold() for part in path}
    leaf = path[-1].casefold()
    destructive_verbs = {
        "cancel",
        "clear",
        "delete",
        "empty",
        "execute",
        "prune",
        "uninstall",
    }
    mutating_verbs = {
        "add",
        "build",
        "cancel",
        "categorize",
        "complete",
        "copy",
        "create",
        "disable",
        "duplicate",
        "edit",
        "enable",
        "flag",
        "forward",
        "import",
        "incremental",
        "install",
        "login",
        "logout",
        "mark-read",
        "mark-unread",
        "materialize",
        "merge",
        "move",
        "optimize",
        "pause",
        "rebuild",
        "recover",
        "remove",
        "rename",
        "reopen",
        "reply",
        "reply-all",
        "reschedule",
        "restore",
        "resume",
        "run",
        "run-once",
        "save",
        "send",
        "send-now",
        "set",
        "skip",
        "sync",
        "undo",
        "unflag",
        "update",
    }
    local_groups = {
        "backup",
        "cache",
        "cleanup",
        "clusters",
        "config",
        "drafts",
        "index",
        "notifications",
        "recipients",
        "recurring",
        "saved-searches",
        "scheduled",
        "scheduler",
        "semantic",
        "webhooks",
    }
    server_groups = {
        "attachments",
        "calendar",
        "contacts",
        "directory",
        "folders",
        "messages",
        "quota",
        "tasks",
    }
    online_roots = {"forward", "reply", "reply-all", "search", "send", "watch"}
    is_destructive = leaf in destructive_verbs
    is_mutating = is_destructive or leaf in mutating_verbs or bool(words & mutating_verbs)
    changes_local = is_mutating and path[0] in local_groups
    changes_server = is_mutating and (path[0] in server_groups or path[0] in online_roots)
    if is_mutating and not (changes_local or changes_server):
        changes_local = True
    safety = (
        SafetyClass.DESTRUCTIVE
        if is_destructive
        else SafetyClass.MUTATING
        if is_mutating
        else SafetyClass.READ_ONLY
    )
    requires_connection = path[0] in server_groups or path[0] in online_roots
    offline = not requires_connection
    return CommandPolicy(
        safety=safety,
        changes_server=changes_server,
        changes_local=changes_local,
        confirmation_required=is_destructive
        or leaf in {"send", "send-now", "reply", "reply-all", "forward"},
        preview_supported=is_destructive
        or leaf in {"send", "send-now", "reply", "reply-all", "forward"},
        offline=offline,
        requires_connection=requires_connection,
    )


def _parameter_accepts(parameter: ParameterSpec, value: object) -> bool:
    if value is None:
        return not parameter.required
    values = value if parameter.multiple and isinstance(value, (list, tuple)) else (value,)
    if parameter.multiple and not isinstance(value, (list, tuple)):
        return False
    primitive_types: dict[ParameterType, tuple[type[object], ...]] = {
        ParameterType.INTEGER: (int,),
        ParameterType.NUMBER: (int, float),
        ParameterType.BOOLEAN: (bool,),
        ParameterType.STRING: (str,),
        ParameterType.EMAIL: (str,),
        ParameterType.PATH: (str,),
        ParameterType.QUERY: (str,),
        ParameterType.REFERENCE: (str,),
        ParameterType.ENUM: (str,),
        ParameterType.EXCLUSION: (Exclusion,),
        ParameterType.RANKING: (Ranking,),
        ParameterType.THREAD_RESPONSE: (ThreadResponseState,),
    }
    accepted = primitive_types.get(parameter.type)
    for item in values:
        if accepted is not None and not isinstance(item, accepted):
            return False
        if parameter.choices and str(item) not in parameter.choices:
            return False
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if parameter.minimum is not None and item < parameter.minimum:
                return False
            if parameter.maximum is not None and item > parameter.maximum:
                return False
    return True


def _normalize_search_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold().replace("_", " ")))


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _context_requirement_met(
    requirement: str,
    context: CommandContext,
    supplied: Mapping[str, object],
) -> bool:
    if context.get(requirement):
        return True
    parameter_hints = {
        "current_draft": ("reference",),
        "scheduled_message": ("reference",),
        "selected_contact": ("person", "reference"),
        "selected_event": ("reference",),
        "selected_message": ("reference", "selection"),
        "selected_task": ("reference",),
    }
    return any(supplied.get(name) for name in parameter_hints.get(requirement, (requirement,)))


def project_cli_invocation(
    invocation: CommandInvocation,
    *,
    registry: CommandRegistry,
    cli_registry: CommandRegistry | None,
) -> CliProjection:
    """Build a CLI vector only when the installed parameter schema proves it is valid.

    This function is deliberately conservative.  Unsupported semantic slots make projection
    unavailable instead of producing an attractive but invalid argument vector.  It does not run
    the CLI and is never an execution path for the palette.
    """

    try:
        source = registry.get(invocation.command_id)
    except KeyError as exc:
        return CliProjection(False, reason=str(exc))
    if source.cli_path is None:
        return CliProjection(False, reason="This semantic command has no CLI linkage.")
    if cli_registry is None:
        return CliProjection(False, reason="The installed CLI schema was not supplied.")
    targets = tuple(
        command for command in cli_registry.all() if command.cli_path == source.cli_path
    )
    if len(targets) != 1:
        return CliProjection(False, reason="The linked CLI command is missing or ambiguous.")
    target = targets[0]
    converted, conversion_error = _convert_cli_parameters(source, target, invocation.as_dict())
    if conversion_error:
        return CliProjection(False, reason=conversion_error)

    argv: list[str] = list(source.cli_path)
    for parameter in target.parameters:
        if parameter.name not in converted:
            if parameter.required:
                return CliProjection(
                    False,
                    reason=f"CLI parameter {parameter.name!r} is still required.",
                )
            continue
        raw_value = converted[parameter.name]
        raw_values = (
            tuple(raw_value)
            if parameter.multiple and isinstance(raw_value, (tuple, list))
            else (raw_value,)
        )
        if parameter.multiple and not isinstance(raw_value, (tuple, list)):
            raw_values = (raw_value,)
        if parameter.type is ParameterType.BOOLEAN:
            if not isinstance(raw_value, bool):
                return CliProjection(False, reason=f"CLI flag {parameter.name!r} needs a boolean.")
            option = _boolean_option(parameter, raw_value)
            if option == "":
                continue
            if option is None:
                return CliProjection(
                    False,
                    reason=f"CLI flag {parameter.name!r} cannot represent {raw_value!r}.",
                )
            argv.append(option)
            continue
        option = next((alias for alias in parameter.aliases if alias.startswith("--")), None)
        for value in raw_values:
            rendered = _cli_scalar(value)
            if rendered is None:
                return CliProjection(
                    False,
                    reason=f"CLI parameter {parameter.name!r} has no safe scalar representation.",
                )
            if option:
                argv.append(option)
            argv.append(rendered)
    return CliProjection(True, tuple(argv), "Validated against the installed CLI parameter schema.")


def _convert_cli_parameters(
    source: CommandSpec,
    target: CommandSpec,
    supplied: Mapping[str, object],
) -> tuple[dict[str, object], str | None]:
    target_names = {parameter.name for parameter in target.parameters}
    converted: dict[str, object] = {}
    aliases: dict[str, tuple[str, ...]] = {
        "folder": ("destination", "name", "parent"),
        "person": ("display_name", "required", "to"),
        "query": ("text", "subject", "operation"),
        "recipient": ("to", "required"),
        "reference": ("references", "message", "identifier"),
        "selection": ("references", "reference"),
        "when": ("due", "start"),
    }
    search_text_present = source.command_id == "search.mail" and "query" in supplied
    for name, value in supplied.items():
        target_name = (
            name
            if name in target_names
            else next(
                (candidate for candidate in aliases.get(name, ()) if candidate in target_names),
                None,
            )
        )
        if target_name is not None:
            if target_name == "display_name":
                display = getattr(value, "display", None)
                if isinstance(display, str) and display:
                    value = display
            elif target_name == "due":
                local = getattr(value, "local", None)
                if local is not None and hasattr(local, "date"):
                    value = local.date().isoformat()
            converted[target_name] = value
            continue
        if name == "date_range" and {"after", "before"} <= target_names:
            start = getattr(value, "start", None)
            end = getattr(value, "end", None)
            if start is None or end is None:
                return {}, "The date range has no start and end values."
            converted["after"] = start.date().isoformat()
            converted["before"] = end.date().isoformat()
            continue
        if name == "read_state" and "is_read" in target_names:
            if value not in {"read", "unread"}:
                return {}, "Read state is not representable by the linked CLI command."
            converted["is_read"] = value == "read"
            continue
        # Search text retains the original phrase, so parsed helper slots are redundant.  Omitting
        # them is safer than inventing flags absent from the installed CLI.
        if search_text_present:
            continue
        return {}, f"Semantic parameter {name!r} is not supported by the linked CLI command."
    return converted, None


def _boolean_option(parameter: ParameterSpec, value: bool) -> str | None:
    positive = next((alias for alias in parameter.aliases if alias.startswith("--")), None)
    negative = next(
        (
            alias
            for alias in parameter.aliases[1:]
            if alias.startswith("--") and ("no-" in alias or "un" in alias)
        ),
        None,
    )
    if value:
        return positive
    if negative:
        return negative
    if parameter.has_default and parameter.default is False:
        return ""
    return None


def _cli_scalar(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    canonical = getattr(value, "canonical", None)
    if isinstance(canonical, str):
        return canonical
    email = getattr(value, "email", None)
    if isinstance(email, str) and email:
        return email
    display = getattr(value, "display", None)
    if isinstance(display, str):
        return display
    utc = getattr(value, "utc", None)
    if isinstance(utc, object) and hasattr(utc, "isoformat"):
        rendered = utc.isoformat()
        return rendered if isinstance(rendered, str) else None
    return None


class CommandPlanner:
    """Create a safe, side-effect-free command preview."""

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        cli_registry: CommandRegistry | None = None,
        medium_confidence: float = 0.82,
    ) -> None:
        self.registry = registry
        self.cli_registry = cli_registry
        self.medium_confidence = medium_confidence

    def prepare(self, invocation: CommandInvocation, context: CommandContext) -> CommandPlan:
        command = self.registry.get(invocation.command_id)
        errors = self.registry.validate_invocation(invocation)
        availability = self.registry.availability(
            command.command_id,
            context,
            invocation=invocation,
        )
        confirmation_required = (
            command.confirmation_required
            or command.safety in {SafetyClass.DESTRUCTIVE, SafetyClass.PRIVILEGED}
            or invocation.confidence < self.medium_confidence
        )
        preview = [command.name, command.description]
        if invocation.parameters:
            preview.append(
                "Parameters: "
                + ", ".join(
                    f"{name}={_safe_preview_value(value)}" for name, value in invocation.parameters
                )
            )
        effects: list[str] = []
        if command.changes_server:
            effects.append("Exchange mailbox data")
        if command.changes_local:
            effects.append("local application data")
        preview.append("Changes: " + (" and ".join(effects) if effects else "none"))
        if availability.reasons:
            preview.append("Unavailable: " + " ".join(availability.reasons))
        if errors:
            preview.append("Invalid: " + "; ".join(errors))
        return CommandPlan(
            command=command,
            invocation=invocation,
            availability=availability,
            preview=tuple(preview),
            confirmation_required=confirmation_required,
            cli=project_cli_invocation(
                invocation,
                registry=self.registry,
                cli_registry=self.cli_registry,
            ),
            validation_errors=errors,
        )


ExecutionHandler = Callable[[CommandInvocation, CommandContext], object]


class CommandExecutor:
    """Small in-process dispatcher; intentionally has no subprocess execution fallback."""

    def __init__(
        self,
        registry: CommandRegistry,
        handlers: Mapping[str, ExecutionHandler] | None = None,
        *,
        cli_registry: CommandRegistry | None = None,
    ) -> None:
        self.registry = registry
        self._handlers: dict[str, ExecutionHandler] = dict(handlers or {})
        self.planner = CommandPlanner(registry, cli_registry=cli_registry)

    @property
    def handler_ids(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def bind(self, handler_id: str, handler: ExecutionHandler) -> None:
        if not callable(handler):
            raise TypeError("Command handler must be callable.")
        self._handlers[handler_id] = handler

    def prepare(self, invocation: CommandInvocation, context: CommandContext) -> CommandPlan:
        executing_context = CommandContext(
            online=context.online,
            offline_mode=context.offline_mode,
            expert_mode=context.expert_mode,
            services=context.services,
            values=context.values,
            handler_ids=self.handler_ids,
        )
        return self.planner.prepare(invocation, executing_context)

    def execute(
        self,
        invocation: CommandInvocation,
        context: CommandContext,
        *,
        confirmed: bool = False,
    ) -> object:
        plan = self.prepare(invocation, context)
        if not plan.executable:
            detail = "; ".join((*plan.availability.reasons, *plan.validation_errors))
            raise CommandUnavailableError(detail or "Command is unavailable.")
        if plan.confirmation_required and not confirmed:
            raise CommandConfirmationRequired("Review the command preview and confirm execution.")
        handler_id = plan.command.handler_id or plan.command.command_id
        handler = self._handlers.get(handler_id)
        if handler is None:  # Defensive: availability normally catches this.
            raise CommandUnavailableError("No in-process handler is bound for this command.")
        return handler(invocation, context)


def _safe_preview_value(value: object) -> str:
    if isinstance(value, (tuple, list)):
        rendered = "[" + ", ".join(_safe_preview_value(item) for item in value[:8]) + "]"
    elif isinstance(value, Exclusion):
        rendered = f"{value.field}!={value.value}"
    elif isinstance(value, Ranking):
        limit = f" limit={value.limit}" if value.limit is not None else ""
        rendered = f"{value.field} {value.direction}{limit}"
    elif isinstance(value, ThreadResponseState):
        rendered = value.state
    else:
        scalar = _cli_scalar(value)
        rendered = scalar if scalar is not None else type(value).__name__
    rendered = re.sub(r"[\x00-\x1f\x7f]", " ", rendered)
    return rendered[:160]
