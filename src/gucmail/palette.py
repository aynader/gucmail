"""Universal, deterministic command-palette core.

The module is presentation-neutral: it ranks and interprets commands, produces safe previews, and
dispatches only through explicitly bound in-process handlers.  It never invokes a subprocess,
loads credentials, opens a mailbox connection, or sends palette text to an external service.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from gucmail.command_catalog import (
    CONTEXTUAL_QUERY_GROUPS,
    CORE_COMMANDS,
    DISCOVERY_QUERY_GROUPS,
)
from gucmail.commands import (
    CliProjection,
    CommandAvailability,
    CommandContext,
    CommandInvocation,
    CommandPlan,
    CommandPlanner,
    CommandRegistry,
    CommandSpec,
)
from gucmail.dates import ResolvedDateTime
from gucmail.models import SafetyClass
from gucmail.nlp import (
    InterpretationKind,
    NaturalLanguageParser,
    NaturalLanguageResult,
    NaturalLanguageSequence,
)


class PaletteKind(StrEnum):
    COMMAND = "command"
    SEARCH = "search_fallback"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"


class SuggestionSource(StrEnum):
    NATURAL_LANGUAGE = "natural_language"
    COMMAND = "command"
    RECENT = "recent"
    SUGGESTED = "suggested"
    RECENT_SEARCH = "recent_search"
    RECENT_FOLDER = "recent_folder"
    RECENT_PERSON = "recent_person"
    SEARCH_FALLBACK = "search_fallback"


@dataclass(frozen=True, slots=True)
class PaletteSuggestion:
    """One locally ranked, display-ready palette row."""

    command: CommandSpec
    score: float
    source: SuggestionSource
    availability: CommandAvailability
    invocation: CommandInvocation | None = None
    reason: str = ""
    group: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class PaletteInterpretation:
    """Compatibility view over the typed interpreter.

    ``command`` is populated only for a small set of syntactically proven legacy CLI projections.
    New callers should use ``invocation`` and ``plan`` instead.  An empty command tuple therefore
    means "no CLI projection", never "guess some arguments".
    """

    original: str
    kind: PaletteKind
    command: tuple[str, ...]
    explanation: str
    mutation: bool = False
    destructive: bool = False
    confirmation_required: bool = False
    resolved_time: ResolvedDateTime | None = None
    confidence: float = 1.0
    invocation: CommandInvocation | None = None
    cli_available: bool = False
    cli_reason: str = ""


UsageScoreHook = Callable[[str], float]
UsageRecordHook = Callable[[str], None]


class PaletteEngine:
    """Registry-backed interpretation, fuzzy ranking, and planning facade."""

    def __init__(
        self,
        registry: CommandRegistry = CORE_COMMANDS,
        *,
        cli_registry: CommandRegistry | None = None,
        timezone: str = "Africa/Cairo",
        now: datetime | None = None,
        fuzzy_threshold: float = 0.68,
        usage_score: UsageScoreHook | None = None,
        record_usage: UsageRecordHook | None = None,
    ) -> None:
        self.registry = registry
        self.parser = NaturalLanguageParser(
            registry,
            timezone=timezone,
            now=now,
            fuzzy_threshold=fuzzy_threshold,
        )
        self.planner = CommandPlanner(registry, cli_registry=cli_registry)
        self._usage_score = usage_score or (lambda _command_id: 0.0)
        self._record_usage = record_usage

    def interpret(
        self,
        text: str,
        *,
        context: CommandContext | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
    ) -> NaturalLanguageResult:
        runtime = context or CommandContext()
        result = self.parser.interpret(
            text,
            context=dict(runtime.values),
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
        )
        invocation = result.invocation
        if invocation is not None and invocation.command_id in {
            "search.search_current_folder",
            "search.search_all_mail",
            "search.mail",
            "search.advanced_search",
        }:
            command = self.registry.get(invocation.command_id)
            names = {
                " ".join(name.casefold().split())
                for name in (command.name, *command.aliases, *command.misspellings)
            }
            if " ".join(text.casefold().split()) in names:
                values = invocation.as_dict()
                values.pop("query", None)
                result = replace(
                    result,
                    invocation=CommandInvocation.from_mapping(
                        invocation.command_id,
                        values,
                        original_text=invocation.original_text,
                        confidence=invocation.confidence,
                        explanation=invocation.explanation,
                    ),
                )
        return result

    def suggest(
        self,
        text: str,
        *,
        context: CommandContext | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
        recent_command_ids: Sequence[str] = (),
        limit: int = 20,
    ) -> tuple[PaletteSuggestion, ...]:
        """Return stable local suggestions with context and explicit-usage ranking boosts."""

        runtime = context or CommandContext()
        limit = max(0, limit)
        interpretation = self.interpret(
            text,
            context=runtime,
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
        )
        candidate_scores = {
            candidate.command_id: (candidate.score, candidate.reason)
            for candidate in interpretation.candidates
        }
        if interpretation.invocation is not None:
            current = candidate_scores.get(interpretation.invocation.command_id, (0.0, ""))
            candidate_scores[interpretation.invocation.command_id] = (
                max(current[0], interpretation.confidence),
                interpretation.explanation,
            )
        for command_id in self._discovery_candidates(text):
            candidate_scores[command_id] = (
                1.0,
                "Documented command-family abbreviation",
            )
        for index, command_id in enumerate(self._contextual_candidates(text, runtime)):
            # Context is strong enough to place the promised family ahead of generic exact/fuzzy
            # matches, but unrelated commands remain in the result set below it.
            contextual_score = 1.25 - index * 0.01
            current = candidate_scores.get(command_id, (0.0, ""))
            candidate_scores[command_id] = (
                max(current[0], contextual_score),
                "Selected-object command family",
            )
        query_empty = not text.strip()
        suggested = self._suggested_candidates(runtime) if query_empty else {}
        for command_id, (_invocation, _display_name, reason, index) in suggested.items():
            current = candidate_scores.get(command_id, (0.0, ""))
            candidate_scores[command_id] = (max(current[0], 1.5 - index * 0.01), reason)
        recent_rank = {
            command_id: index for index, command_id in enumerate(dict.fromkeys(recent_command_ids))
        }
        current_area = runtime.get("current_area", "") or ""
        rows: list[tuple[float, str, PaletteSuggestion]] = []
        for command in self.registry.all():
            if query_empty and command.command_id in suggested:
                base, reason = candidate_scores[command.command_id]
            elif query_empty:
                base = 0.35
                reason = "Available command"
            elif command.command_id in candidate_scores:
                base, reason = candidate_scores[command.command_id]
            else:
                continue
            source = SuggestionSource.COMMAND
            if (
                interpretation.invocation
                and command.command_id == interpretation.invocation.command_id
            ):
                source = (
                    SuggestionSource.SEARCH_FALLBACK
                    if interpretation.kind is InterpretationKind.SEARCH_FALLBACK
                    else SuggestionSource.NATURAL_LANGUAGE
                )
            if command.command_id in suggested:
                source = SuggestionSource.SUGGESTED
            score = base
            if command.category == current_area:
                score += 0.08
                reason += "; current area"
            if command.command_id in recent_rank:
                score += max(0.02, 0.07 - recent_rank[command.command_id] * 0.005)
                if source is SuggestionSource.COMMAND:
                    source = SuggestionSource.RECENT
                reason += "; recent"
            with contextlib.suppress(Exception):
                score += max(0.0, min(float(self._usage_score(command.command_id)), 1.0)) * 0.06
                # A persistence hook may be temporarily unavailable; command discovery must remain
                # instant and deterministic instead of failing the palette.
            invocation = (
                interpretation.invocation
                if interpretation.invocation
                and interpretation.invocation.command_id == command.command_id
                else None
            )
            display_name: str | None = None
            group: str | None = None
            if command.command_id in suggested:
                invocation, display_name, _suggested_reason, _index = suggested[command.command_id]
                group = "Suggested actions"
            availability = self.registry.availability(
                command.command_id,
                runtime,
                invocation=invocation,
            )
            if availability.available:
                score += 0.025
            rows.append(
                (
                    score,
                    command.command_id,
                    PaletteSuggestion(
                        command=command,
                        score=min(score, 1.0),
                        source=source,
                        availability=availability,
                        invocation=invocation,
                        reason=reason,
                        group=group,
                        display_name=display_name,
                    ),
                )
            )
        rows.sort(key=lambda row: (-row[0], row[1]))
        # Several commands can share a visible name (e.g. "Search calendar" exists in
        # Calendar and in Search). Show each name once: the best-ranked one wins.
        seen: set[str] = set()
        unique: list[PaletteSuggestion] = []
        for _score, _command_id, suggestion in rows:
            label = (suggestion.display_name or suggestion.command.name).casefold()
            if label in seen:
                continue
            seen.add(label)
            unique.append(suggestion)
        return tuple(unique[:limit])

    def _suggested_candidates(
        self,
        runtime: CommandContext,
    ) -> dict[str, tuple[CommandInvocation, str, str, int]]:
        """Build deterministic, side-effect-free suggestions from supplied local context."""

        candidates: list[tuple[str, Mapping[str, object], str, str]] = []
        selected = runtime.get("selected_message")
        if selected:
            candidates.append(
                ("mail.reply", {"reference": selected}, "Reply", "Selected message action")
            )
            if runtime.get("selected_message_has_attachments") == "true":
                candidates.append(
                    (
                        "attachments.download",
                        {"reference": selected},
                        "Download attachments",
                        "Selected message has attachments",
                    )
                )
            sender = runtime.get("selected_sender")
            if sender:
                candidates.append(
                    (
                        "contacts.open_sender_contact",
                        {"person": sender},
                        "Open sender contact",
                        "Selected message sender",
                    )
                )
            candidates.append(
                (
                    "search.similar_message_search",
                    {},
                    "Search related messages",
                    "Selected message action",
                )
            )
        if runtime.get("mailbox_near_limit") == "true":
            candidates.extend(
                (
                    (
                        "storage.largest_messages",
                        {},
                        "Show largest messages",
                        "Mailbox usage is near its configured limit",
                    ),
                    (
                        "storage.analyze_reclaimable_space",
                        {},
                        "Analyze reclaimable space",
                        "Mailbox usage is near its configured limit",
                    ),
                    (
                        "folders.empty_folder",
                        {"folder": "trash"},
                        "Empty Deleted Items",
                        "Mailbox usage is near its configured limit",
                    ),
                    (
                        "backup.create",
                        {},
                        "Create backup",
                        "Mailbox usage is near its configured limit",
                    ),
                )
            )

        known = {command.command_id for command in self.registry.all()}
        return {
            command_id: (
                CommandInvocation.from_mapping(
                    command_id,
                    parameters,
                    original_text=display_name,
                    explanation=reason,
                ),
                display_name,
                reason,
                index,
            )
            for index, (command_id, parameters, display_name, reason) in enumerate(candidates)
            if command_id in known
        }

    def _discovery_candidates(self, text: str) -> tuple[str, ...]:
        """Return one documented short-query family, including useful prefixes."""

        normalized = " ".join(
            re.findall(r"[a-z0-9@.+:-]+", unicodedata.normalize("NFKC", text).casefold())
        )
        known = {command.command_id for command in self.registry.all()}
        groups: dict[str, tuple[str, ...]] = {}
        for phrase, command_ids in DISCOVERY_QUERY_GROUPS.items():
            key = phrase.casefold()
            available_ids = tuple(command_id for command_id in command_ids if command_id in known)
            if available_ids:
                groups[key] = available_ids
        exact = groups.get(normalized)
        if exact is not None:
            return exact
        if len(normalized) < 3:
            return ()
        matching = {
            command_ids
            for phrase, command_ids in groups.items()
            if " " not in normalized
            and " " not in phrase
            and (phrase.startswith(normalized) or normalized.startswith(phrase))
        }
        return next(iter(matching)) if len(matching) == 1 else ()

    def _contextual_candidates(
        self,
        text: str,
        context: CommandContext,
    ) -> tuple[str, ...]:
        """Expand an exact short verb only for the selected object's documented family."""

        normalized = " ".join(
            re.findall(r"[a-z0-9@.+:-]+", unicodedata.normalize("NFKC", text).casefold())
        )
        known = {command.command_id for command in self.registry.all()}
        for (requirement, phrase), command_ids in CONTEXTUAL_QUERY_GROUPS.items():
            if normalized == phrase and context.get(requirement):
                return tuple(command_id for command_id in command_ids if command_id in known)
        return ()

    def interpret_sequence(
        self,
        text: str,
        *,
        context: CommandContext | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
        maximum_steps: int = 4,
    ) -> NaturalLanguageSequence:
        """Preview one bounded, allowlisted result-handoff sequence without executing it."""

        runtime = context or CommandContext()
        return self.parser.interpret_sequence(
            text,
            context=dict(runtime.values),
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
            maximum_steps=maximum_steps,
        )

    def plan(
        self,
        invocation: CommandInvocation,
        *,
        context: CommandContext | None = None,
    ) -> CommandPlan:
        return self.planner.prepare(invocation, context or CommandContext())

    def record_success(self, invocation: CommandInvocation) -> None:
        """Record usage only after the caller reports successful execution."""

        if self._record_usage is not None:
            self._record_usage(invocation.command_id)


def universal_palette_registry() -> CommandRegistry:
    """Return the canonical semantic registry used by palette, Help, and TUI integrations."""

    return CORE_COMMANDS


def interpret_typed_palette(
    text: str,
    *,
    timezone: str = "Africa/Cairo",
    now: datetime | None = None,
    context: Mapping[str, str] | CommandContext | None = None,
    folder_candidates: Sequence[str] = (),
    person_candidates: Sequence[str] = (),
) -> NaturalLanguageResult:
    """Interpret palette text into a typed invocation with no execution side effects."""

    runtime = (
        context
        if isinstance(context, CommandContext)
        else CommandContext.from_mapping(context, online=True)
    )
    return PaletteEngine(CORE_COMMANDS, timezone=timezone, now=now).interpret(
        text,
        context=runtime,
        folder_candidates=folder_candidates,
        person_candidates=person_candidates,
    )


def interpret_typed_palette_sequence(
    text: str,
    *,
    timezone: str = "Africa/Cairo",
    now: datetime | None = None,
    context: Mapping[str, str] | CommandContext | None = None,
    folder_candidates: Sequence[str] = (),
    person_candidates: Sequence[str] = (),
    maximum_steps: int = 4,
) -> NaturalLanguageSequence:
    """Return a stable, side-effect-free palette sequence preview.

    Only parser-allowlisted result handoffs are split.  The return value carries typed
    invocations, handoff names, confirmation state, and whether the entire sequence is allowed.
    """

    runtime = (
        context
        if isinstance(context, CommandContext)
        else CommandContext.from_mapping(context, online=True)
    )
    return PaletteEngine(CORE_COMMANDS, timezone=timezone, now=now).interpret_sequence(
        text,
        context=runtime,
        folder_candidates=folder_candidates,
        person_candidates=person_candidates,
        maximum_steps=maximum_steps,
    )


def interpret_palette(
    text: str,
    *,
    timezone: str = "Africa/Cairo",
    now: datetime | None = None,
) -> PaletteInterpretation:
    """Backward-compatible wrapper backed entirely by the canonical typed system."""

    result = interpret_typed_palette(text, timezone=timezone, now=now)
    invocation = result.invocation
    if invocation is None:
        return PaletteInterpretation(
            original=text.strip(),
            kind=(
                PaletteKind.AMBIGUOUS
                if result.kind is InterpretationKind.AMBIGUOUS
                else PaletteKind.INCOMPLETE
            ),
            command=(),
            explanation=result.explanation,
            confidence=result.confidence,
        )
    command = CORE_COMMANDS.get(invocation.command_id)
    # Keep the legacy surface maximally safe: dangerous free-form text remains a transparent
    # search even though the richer typed API can offer a cleanup-analysis suggestion.
    safe_cleanup_fallback = (
        command.command_id == "cleanup.analyze" and "Dangerous language" in invocation.explanation
    )
    if safe_cleanup_fallback:
        cli = CliProjection(True, ("search", text.strip()), "Safe legacy search fallback.")
        kind = PaletteKind.SEARCH
        mutation = False
        destructive = False
        confirmation = False
    else:
        cli = _legacy_cli_projection(command, invocation)
        kind = (
            PaletteKind.SEARCH
            if result.kind is InterpretationKind.SEARCH_FALLBACK
            else PaletteKind.COMMAND
        )
        mutation = command.mutates
        destructive = command.safety in {SafetyClass.DESTRUCTIVE, SafetyClass.PRIVILEGED}
        confirmation = command.confirmation_required or result.confidence < 0.82
    resolved_time = result.entities.when
    return PaletteInterpretation(
        original=text.strip(),
        kind=kind,
        command=cli.argv or (),
        explanation=result.explanation,
        mutation=mutation,
        destructive=destructive,
        confirmation_required=confirmation,
        resolved_time=resolved_time,
        confidence=result.confidence,
        invocation=invocation,
        cli_available=cli.available,
        cli_reason=cli.reason,
    )


def _legacy_cli_projection(
    command: CommandSpec,
    invocation: CommandInvocation,
) -> CliProjection:
    """Project only compatibility cases whose current CLI grammar is fixed and unambiguous."""

    values = invocation.as_dict()
    if command.command_id == "search.mail":
        residual_slots = {"exclusions", "ranking", "thread_response"} & values.keys()
        if residual_slots:
            return CliProjection(
                False,
                reason=(
                    "Use the typed invocation; the legacy CLI projection cannot preserve: "
                    + ", ".join(sorted(residual_slots))
                ),
            )
        filters: list[str] = []
        read_state = values.get("read_state")
        if read_state in {"read", "unread"}:
            filters.append(f"{read_state}:true")
        attachment_type = values.get("attachment_type")
        if isinstance(attachment_type, str):
            filters.append(f"attachment:{attachment_type}")
        elif isinstance(values.get("has_attachments"), bool):
            filters.append(f"attachments:{str(bool(values['has_attachments'])).casefold()}")
        for key in ("flagged", "important"):
            value = values.get(key)
            if isinstance(value, bool):
                filters.append(f"{key}:{str(value).casefold()}")
        if filters:
            return CliProjection(
                True,
                ("search", " ".join(filters)),
                "Validated structured search filters.",
            )
        query = values.get("query", invocation.original_text or "")
        if isinstance(query, str):
            return CliProjection(True, ("search", query), "Validated search positional argument.")
    if command.command_id == "help.search":
        query = values.get("query", invocation.original_text or "")
        if isinstance(query, str):
            return CliProjection(True, ("search", query), "Validated search positional argument.")
    if command.command_id == "scheduled.schedule_current":
        reference = values.get("reference")
        when = values.get("when")
        resolved = getattr(when, "utc", None)
        if isinstance(reference, str) and resolved is not None and hasattr(resolved, "isoformat"):
            return CliProjection(
                True,
                ("drafts", "schedule", reference, "--at", resolved.isoformat()),
                "Validated scheduled-draft arguments.",
            )
    if command.cli_path is not None and not values:
        return CliProjection(True, command.cli_path, "Command has no projected parameters.")
    return CliProjection(
        False, reason="Use the typed invocation; no lossless legacy CLI projection exists."
    )


