"""Deterministic, privacy-local natural-language command interpretation.

The parser combines a registry-backed phrase corpus, fuzzy intent ranking, and typed entity
extraction.  It never calls a language model or network service and never turns uncertain text
into a mailbox mutation.
"""

from __future__ import annotations

import json
import re
import unicodedata
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from importlib import resources
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from gucmail.command_catalog import (
    CONTEXTUAL_QUERY_GROUPS,
    CORE_COMMANDS,
    DISCOVERY_QUERY_GROUPS,
)
from gucmail.commands import (
    CommandInvocation,
    CommandRegistry,
    CommandSpec,
    Exclusion,
    ParameterType,
    Ranking,
    ThreadResponseState,
)
from gucmail.dates import ResolvedDateTime, parse_datetime_expression
from gucmail.models import SafetyClass

EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})(?![\w.-])")
SIZE_RE = re.compile(
    r"(?:(between)\s+)?(\d+(?:\.\d+)?)\s*(bytes?|kb|kib|mb|mib|gb|gib)"
    r"(?:\s+(?:and|to)\s+(\d+(?:\.\d+)?)\s*(bytes?|kb|kib|mb|mib|gb|gib))?",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
AMBIGUOUS_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")

COMMON_TYPOS = {
    "ahmd": "ahmed",
    "attchment": "attachment",
    "attchments": "attachments",
    "attchmnts": "attachments",
    "atta": "attachment",
    "backuo": "backup",
    "bakup": "backup",
    "bkp": "backup",
    "biger": "bigger",
    "calender": "calendar",
    "cal": "calendar",
    "contacs": "contacts",
    "discrd": "discord",
    "emals": "emails",
    "frm": "from",
    "inbxo": "inbox",
    "itms": "items",
    "largst": "largest",
    "loadng": "loading",
    "msg": "message",
    "msgs": "messages",
    "notif": "notification",
    "notifcations": "notifications",
    "opn": "open",
    "preveiw": "preview",
    "scheduel": "schedule",
    "studnt": "student",
    "affrs": "affairs",
    "serch": "search",
    "setings": "settings",
    "sho": "show",
    "strt": "start",
    "storag": "storage",
    "tomorow": "tomorrow",
    "tom": "tomorrow",
    "unraed": "unread",
    "unred": "unread",
    "webhok": "webhook",
    "wk": "week",
}

FOLDER_ALIASES = {
    "archive": "archive",
    "archived": "archive",
    "bin": "deleted",
    "deleted": "deleted",
    "deleted items": "deleted",
    "draft": "drafts",
    "drafts": "drafts",
    "inbox": "inbox",
    "junk": "junk",
    "outbox": "outbox",
    "recycle bin": "deleted",
    "sent": "sent",
    "sent items": "sent",
    "sent mail": "sent",
    "outgoing history": "sent",
    "spam": "junk",
    "trash": "deleted",
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

ATTACHMENT_TYPES = {
    "archive": {"archive", "archives", "zip", "zips"},
    "document": {"doc", "docs", "docx", "document", "documents", "word"},
    "image": {"gif", "image", "images", "jpeg", "jpg", "photo", "photos", "png"},
    "pdf": {"pdf", "pdfs"},
    "presentation": {"powerpoint", "ppt", "pptx", "presentation", "slides"},
    "spreadsheet": {"excel", "sheet", "spreadsheet", "xls", "xlsx"},
}

DANGEROUS_WORDS = {
    "bulk",
    "cancel",
    "delete",
    "empty",
    "erase",
    "overwrite",
    "purge",
    "remove",
    "replace",
    "restore",
    "send",
    "send all",
    "wipe",
}


class InterpretationKind(StrEnum):
    COMMAND = "command"
    SEARCH_FALLBACK = "search_fallback"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    original: str
    normalized: str
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonEntity:
    display: str
    email: str | None = None
    source: str = "text"
    confidence: float = 1.0
    alternatives: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return bool(self.alternatives)


@dataclass(frozen=True, slots=True)
class FolderEntity:
    display: str
    canonical: str
    source: str = "alias"
    confidence: float = 1.0
    alternatives: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return bool(self.alternatives)


@dataclass(frozen=True, slots=True)
class DateRange:
    start: datetime | None
    end: datetime | None
    timezone: str
    expression: str


@dataclass(frozen=True, slots=True)
class SizeConstraint:
    minimum_bytes: int | None = None
    maximum_bytes: int | None = None
    expression: str = ""


@dataclass(frozen=True, slots=True)
class ParsedEntities:
    person: PersonEntity | None = None
    folder: FolderEntity | None = None
    date_range: DateRange | None = None
    when: ResolvedDateTime | None = None
    size: SizeConstraint | None = None
    attachment_type: str | None = None
    read_state: str | None = None
    flagged: bool | None = None
    important: bool | None = None
    has_attachments: bool | None = None
    exclusions: tuple[Exclusion, ...] = ()
    ranking: Ranking | None = None
    thread_response: ThreadResponseState | None = None
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandCandidate:
    command_id: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class _SemanticIntent:
    command_id: str
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class NaturalLanguageResult:
    original: str
    normalized: str
    kind: InterpretationKind
    invocation: CommandInvocation | None
    candidates: tuple[CommandCandidate, ...]
    entities: ParsedEntities
    explanation: str
    preview: tuple[str, ...] = ()

    @property
    def confidence(self) -> float:
        if self.invocation is not None:
            return self.invocation.confidence
        return self.candidates[0].score if self.candidates else 0.0


@dataclass(frozen=True, slots=True)
class NaturalLanguageSequence:
    """A bounded, preview-only sequence of allowlisted composable commands.

    ``handoffs`` names the value passed from the preceding result into each later step.  The
    interpreter never executes a sequence and never invents an unbounded pipeline.
    """

    original: str
    steps: tuple[NaturalLanguageResult, ...]
    handoffs: tuple[str, ...] = ()
    confirmation_required: bool = False
    explanation: str = ""
    allowed: bool = True

    @property
    def complete(self) -> bool:
        return (
            self.allowed
            and bool(self.steps)
            and all(step.invocation is not None for step in self.steps)
        )


@dataclass(frozen=True, slots=True)
class CorpusGroup:
    command_id: str
    phrases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCorpus:
    schema: str
    groups: tuple[CorpusGroup, ...]

    @property
    def phrase_count(self) -> int:
        return sum(len(group.phrases) for group in self.groups)


def normalize_text(text: str) -> NormalizedInput:
    original = text.strip()
    normalized_unicode = unicodedata.normalize("NFKC", original).casefold()
    normalized_unicode = normalized_unicode.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").replace(
        "`", "'"
    )
    normalized_unicode = re.sub(r"(?<=\w)'(?=\w)", "", normalized_unicode)
    raw_tokens = re.findall(r"[\w@.+:-]+", normalized_unicode, re.UNICODE)
    tokens = tuple(COMMON_TYPOS.get(token, token) for token in raw_tokens)
    return NormalizedInput(original=original, normalized=" ".join(tokens), tokens=tokens)


def _command_id_phrase(command_id: str) -> str:
    """Encode separators so fallback corpus phrases stay unique across valid command IDs."""

    return "command id " + command_id.replace("_", " underscore ").replace("-", " dash ").replace(
        ".", " dot "
    )


def generate_command_corpus(
    registry: CommandRegistry,
    *,
    curated_groups: Sequence[CorpusGroup] = (),
) -> CommandCorpus:
    """Generate a deterministic phrase corpus from canonical command metadata.

    The packaged JSON is a curated augmentation for natural wording, not a second command list.
    New registry commands therefore receive a corpus group automatically and cannot silently be
    omitted.  When two presets expose the same human phrase, the first canonical command owns it;
    every later preset receives a stable command-ID phrase instead of creating ambiguity.
    """

    command_ids = {command.command_id for command in registry.all()}
    phrases_by_id: dict[str, list[str]] = {command_id: [] for command_id in command_ids}
    owners: dict[str, str] = {}
    discovery_phrases = {phrase.strip().casefold() for phrase in DISCOVERY_QUERY_GROUPS}

    def add(command_id: str, phrase: str) -> bool:
        normalized = normalize_text(phrase).normalized
        if not normalized:
            return False
        owner = owners.get(normalized)
        if owner is not None:
            return owner == command_id
        owners[normalized] = command_id
        phrases_by_id[command_id].append(phrase.strip())
        return True

    for group in curated_groups:
        if group.command_id not in command_ids:
            continue
        for phrase in group.phrases:
            if not add(group.command_id, phrase):
                owner = owners[normalize_text(phrase).normalized]
                if owner != group.command_id:
                    raise ValueError(
                        f"Command corpus phrase {phrase!r} is duplicated in {owner} "
                        f"and {group.command_id}."
                    )

    for command in registry.all():
        for phrase in (command.name, *command.aliases, *command.examples):
            # A short palette-family query intentionally has several owners and cannot also be
            # an exact one-command NLP corpus phrase.
            if phrase.strip().casefold() in discovery_phrases:
                continue
            add(command.command_id, phrase)
        if not phrases_by_id[command.command_id]:
            fallback = _command_id_phrase(command.command_id)
            if not add(command.command_id, fallback):
                raise ValueError(
                    f"Could not generate a unique phrase for command {command.command_id}."
                )

    groups = tuple(
        CorpusGroup(command.command_id, tuple(phrases_by_id[command.command_id]))
        for command in registry.all()
    )
    return CommandCorpus(schema="gucmail.command-corpus/v1", groups=groups)


def load_command_corpus(registry: CommandRegistry = CORE_COMMANDS) -> CommandCorpus:
    resource = resources.files("gucmail.data").joinpath("command_corpus.json")
    parsed = json.loads(resource.read_text(encoding="utf-8"))
    if parsed.get("schema") != "gucmail.command-corpus/v1":
        raise ValueError("Unsupported command-corpus schema.")
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list):
        raise ValueError("Command corpus must contain a groups list.")
    curated_groups: list[CorpusGroup] = []
    seen_groups: set[str] = set()
    registry_ids = {command.command_id for command in registry.all()}
    core_ids = {command.command_id for command in CORE_COMMANDS.all()}
    loading_core = registry_ids == core_ids
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("Command corpus groups must be objects.")
        command_id = str(raw_group.get("command_id", ""))
        if command_id in seen_groups:
            raise ValueError(f"Command corpus repeats group {command_id}.")
        seen_groups.add(command_id)
        raw_phrases = raw_group.get("phrases")
        if not isinstance(raw_phrases, list) or not raw_phrases:
            raise ValueError(f"Command corpus group {command_id} has no phrases.")
        phrases = tuple(str(phrase).strip() for phrase in raw_phrases)
        if any(not phrase for phrase in phrases):
            raise ValueError(f"Command corpus group {command_id} has an empty phrase.")
        if command_id in registry_ids:
            curated_groups.append(CorpusGroup(command_id=command_id, phrases=phrases))
        elif loading_core:
            raise ValueError(f"Command corpus references unknown command {command_id}.")
    corpus = generate_command_corpus(registry, curated_groups=curated_groups)
    if len(corpus.groups) != len(registry):
        raise ValueError("Generated command corpus does not cover the canonical registry.")
    return corpus


class NaturalLanguageParser:
    """Registry-backed interpreter with bounded local fuzzy matching."""

    def __init__(
        self,
        registry: CommandRegistry = CORE_COMMANDS,
        *,
        corpus: CommandCorpus | None = None,
        timezone: str = "Africa/Cairo",
        now: datetime | None = None,
        fuzzy_threshold: float = 0.68,
    ) -> None:
        self.registry = registry
        self.corpus = corpus or load_command_corpus(registry)
        self.timezone = timezone
        self.now = now
        self.fuzzy_threshold = fuzzy_threshold
        self._phrases: tuple[tuple[str, str], ...] = tuple(
            (normalize_text(phrase).normalized, group.command_id)
            for group in self.corpus.groups
            for phrase in group.phrases
        )
        exact: defaultdict[str, list[str]] = defaultdict(list)
        for phrase, command_id in self._phrases:
            if command_id not in exact[phrase]:
                exact[phrase].append(command_id)
        self._exact = {phrase: tuple(command_ids) for phrase, command_ids in exact.items()}
        self._command_phrases: tuple[tuple[str, str], ...] = tuple(
            (normalize_text(phrase).normalized, command.command_id)
            for command in self.registry.all()
            for phrase in (command.name, *command.aliases, *command.misspellings)
        )

    def interpret(
        self,
        text: str,
        *,
        context: Mapping[str, str] | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
    ) -> NaturalLanguageResult:
        normalized = normalize_text(text)
        if not normalized.normalized:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.INCOMPLETE,
                invocation=None,
                candidates=tuple(
                    CommandCandidate(command.command_id, 0.5, "Suggested command")
                    for command in self.registry.all()[:8]
                ),
                entities=ParsedEntities(),
                explanation="Type a command, task, or search phrase.",
            )

        if _explicit_folder_navigation_reference(normalized.original) is not None:
            candidate = CommandCandidate("folders.open", 1.0, "Explicit local folder identity")
            return self._build_result(
                normalized,
                "folders.open",
                (candidate,),
                confidence=0.99,
                reason="Explicit local folder reference",
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
                exact=True,
            )

        contextual = self._contextual_intent(normalized, context)
        if contextual is not None:
            candidates = self._promote_candidate(self._rank(normalized), contextual)
            return self._build_result(
                normalized,
                contextual.command_id,
                candidates,
                confidence=contextual.confidence,
                reason=contextual.reason,
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
                exact=False,
            )

        exact_ids = self._exact.get(normalized.normalized, ())
        if exact_ids:
            candidates = tuple(
                CommandCandidate(command_id, 1.0, "Exact phrase") for command_id in exact_ids
            )
            chosen = self._choose_exact(exact_ids, candidates)
            return self._build_result(
                normalized,
                chosen,
                candidates,
                confidence=0.99,
                reason="Exact documented phrase",
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
                exact=True,
            )

        incomplete_ids = self._incomplete_candidates(normalized.normalized)
        if incomplete_ids:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.INCOMPLETE,
                invocation=None,
                candidates=tuple(
                    CommandCandidate(command_id, 0.72 - index * 0.02, "Missing parameter")
                    for index, command_id in enumerate(incomplete_ids)
                ),
                entities=self._entities(
                    normalized.original,
                    None,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation=(
                    "The action is incomplete; choose a suggestion or supply the missing value."
                ),
            )

        candidates = self._rank(normalized)

        entity_only = self._entity_only_candidates(normalized)
        entity_only = self._context_ranked_entities(entity_only, context)
        contextual_only = normalized.normalized in {
            "this",
            "this email",
            "these",
            "selected",
            "current folder",
            "current search",
            "this contact",
            "this event",
            "this sender",
        }
        if entity_only and contextual_only:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=tuple(
                    CommandCandidate(command_id, 0.76 - index * 0.02, "Entity-only request")
                    for index, command_id in enumerate(entity_only)
                ),
                entities=self._entities(
                    normalized.original,
                    None,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation=(
                    "A person or contextual object was named without an action; choose what to do."
                ),
            )

        semantic = self._semantic_intent(normalized)
        if semantic is not None:
            candidates = self._promote_candidate(candidates, semantic)
            confidence = semantic.confidence
            command = self.registry.get(semantic.command_id)
            if self._has_dangerous_language(normalized.normalized) and command.mutates:
                # Force the shared planner's medium-confidence confirmation even when a generated
                # command's metadata does not independently require it.
                confidence = min(confidence, 0.8)
            return self._build_result(
                normalized,
                semantic.command_id,
                candidates,
                confidence=confidence,
                reason=semantic.reason,
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
                exact=False,
            )

        command_prefix = bool(candidates and candidates[0].score >= 0.9)
        if entity_only and not command_prefix:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=tuple(
                    CommandCandidate(command_id, 0.76 - index * 0.02, "Entity-only request")
                    for index, command_id in enumerate(entity_only)
                ),
                entities=self._entities(
                    normalized.original,
                    None,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation="A person was named without an action; choose what to do.",
            )

        if self._is_combined_mail_search(normalized):
            return self._build_result(
                normalized,
                "search.mail",
                candidates,
                confidence=0.9,
                reason="Combined mail filters",
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
                exact=False,
            )

        if self._has_dangerous_language(normalized.normalized):
            return self._safe_dangerous_result(
                normalized,
                candidates,
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
            )

        if not candidates or candidates[0].score < self.fuzzy_threshold:
            return self._fallback(
                normalized,
                candidates,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
            )
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < 0.035:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=candidates[:6],
                entities=self._entities(
                    normalized.original,
                    None,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation="Several commands are similarly likely; choose one before continuing.",
            )

        top = candidates[0]
        command = self.registry.get(top.command_id)
        if command.safety in {SafetyClass.DESTRUCTIVE, SafetyClass.PRIVILEGED}:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=candidates[:6],
                entities=self._entities(
                    normalized.original,
                    command,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation="A destructive action was only a fuzzy match; select it explicitly.",
            )
        return self._build_result(
            normalized,
            command.command_id,
            candidates,
            confidence=top.score,
            reason=top.reason,
            context=context,
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
            exact=False,
        )

    def interpret_sequence(
        self,
        text: str,
        *,
        context: Mapping[str, str] | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
        maximum_steps: int = 4,
    ) -> NaturalLanguageSequence:
        """Interpret a bounded allowlist of explicit or plain-``and`` result pipelines.

        The result is always a preview.  It never executes a step, and a mutating follow-up is
        marked for confirmation.  Unsupported chains remain a single harmless interpretation.
        """

        maximum_steps = max(1, min(maximum_steps, 4))
        split = self._split_allowlisted_sequence(text)
        if split is None:
            step = self.interpret(
                text,
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
            )
            return NaturalLanguageSequence(
                original=text,
                steps=(step,),
                explanation="No allowlisted result-handoff pattern was found.",
                allowed=False,
            )
        parts, requested_commands, handoffs = split
        if len(parts) > maximum_steps:
            step = self.interpret(
                text,
                context=context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
            )
            return NaturalLanguageSequence(
                original=text,
                steps=(step,),
                explanation=(
                    f"The sequence needs {len(parts)} steps but the caller allows only "
                    f"{maximum_steps}; no result handoff was created."
                ),
                allowed=False,
            )
        parts = parts[:maximum_steps]
        requested_commands = requested_commands[:maximum_steps]
        handoffs = handoffs[: max(0, maximum_steps - 1)]
        steps: list[NaturalLanguageResult] = []
        chain_context = dict(context or {})
        for index, (part, command_id) in enumerate(zip(parts, requested_commands, strict=True)):
            if index:
                chain_context["selected_message"] = "@previous.results"
                chain_context["current_search"] = "@previous.results"
            interpreted = self.interpret(
                part,
                context=chain_context,
                folder_candidates=folder_candidates,
                person_candidates=person_candidates,
            )
            if interpreted.invocation is None or interpreted.invocation.command_id != command_id:
                interpreted = self._build_result(
                    normalize_text(part),
                    command_id,
                    self._promote_candidate(
                        interpreted.candidates,
                        _SemanticIntent(command_id, 0.9, "Allowlisted chain step"),
                    ),
                    confidence=0.8 if index else 0.9,
                    reason="Allowlisted chain step",
                    context=chain_context,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                    exact=False,
                )
            steps.append(interpreted)
        complete = all(step.invocation is not None for step in steps)
        return NaturalLanguageSequence(
            original=text,
            steps=tuple(steps),
            handoffs=handoffs,
            confirmation_required=len(steps) > 1,
            explanation=(
                "Preview each bounded step; pass only the preceding result set and confirm the "
                "mutating follow-up."
                if complete
                else "At least one chain step needs clarification before anything can run."
            ),
            allowed=complete,
        )

    def _rank(
        self,
        normalized: NormalizedInput,
        *,
        exact_ids: Sequence[str] = (),
    ) -> tuple[CommandCandidate, ...]:
        scores: dict[str, tuple[float, str]] = {}
        for command_id in exact_ids:
            scores[command_id] = (1.0, "Exact phrase")
        category_cues = set(normalized.tokens) & {
            "activity",
            "appearance",
            "attachment",
            "attachments",
            "backup",
            "calendar",
            "cleanup",
            "contact",
            "contacts",
            "diagnostics",
            "draft",
            "drafts",
            "folder",
            "folders",
            "help",
            "integration",
            "integrations",
            "layout",
            "monitoring",
            "notification",
            "notifications",
            "scheduled",
            "settings",
            "storage",
            "tasks",
            "theme",
            "webhook",
        }
        for phrase, command_id in self._phrases:
            weighted = fuzz.WRatio(normalized.normalized, phrase) / 100.0
            unordered = fuzz.token_set_ratio(normalized.normalized, phrase) / 100.0
            ordered = fuzz.ratio(normalized.normalized, phrase) / 100.0
            score = weighted * 0.5 + unordered * 0.35 + ordered * 0.15
            command_prefix = command_id.partition(".")[0]
            if command_prefix in category_cues:
                score += 0.06
            if self.registry.get(command_id).safety in {
                SafetyClass.DESTRUCTIVE,
                SafetyClass.PRIVILEGED,
            } and not self._has_dangerous_language(normalized.normalized):
                score = min(score, 0.55)
            if score > scores.get(command_id, (0.0, ""))[0]:
                scores[command_id] = (min(score, 1.0), "Fuzzy phrase match")
        for candidate_text, command_id in self._command_phrases:
            weighted = fuzz.WRatio(normalized.normalized, candidate_text) / 100.0
            unordered = fuzz.token_set_ratio(normalized.normalized, candidate_text) / 100.0
            score = weighted * 0.65 + unordered * 0.35
            if command_id.partition(".")[0] in category_cues:
                score += 0.06
            if self.registry.get(command_id).safety in {
                SafetyClass.DESTRUCTIVE,
                SafetyClass.PRIVILEGED,
            } and not self._has_dangerous_language(normalized.normalized):
                score = min(score, 0.55)
            if score > scores.get(command_id, (0.0, ""))[0]:
                scores[command_id] = (min(score, 1.0), "Command-name or alias match")
        ranked = [
            CommandCandidate(command_id=command_id, score=score, reason=reason)
            for command_id, (score, reason) in scores.items()
        ]
        ranked.sort(key=lambda item: (-item.score, item.command_id))
        return tuple(ranked[:12])

    @staticmethod
    def _promote_candidate(
        candidates: Sequence[CommandCandidate], intent: _SemanticIntent
    ) -> tuple[CommandCandidate, ...]:
        promoted = CommandCandidate(intent.command_id, intent.confidence, intent.reason)
        remaining = tuple(
            candidate for candidate in candidates if candidate.command_id != intent.command_id
        )
        return (promoted, *remaining[:11])

    @staticmethod
    def _entity_only_candidates(normalized: NormalizedInput) -> tuple[str, ...]:
        text = normalized.normalized
        if text in {
            "this",
            "this email",
            "these",
            "selected",
            "current folder",
            "current search",
            "this contact",
            "this event",
            "this sender",
        }:
            return (
                "mail.open_message",
                "search.mail",
                "attachments.open",
                "backup.create",
            )
        if (
            len(normalized.tokens) == 1
            and not set(normalized.tokens)
            & {
                "backup",
                "calendar",
                "contact",
                "contacts",
                "draft",
                "help",
                "inbox",
                "mail",
                "message",
                "notifications",
                "quota",
                "sent",
                "settings",
                "storage",
                "task",
                "tasks",
                "theme",
            }
            and re.fullmatch(r"[\w.+@-]+(?:\s+[\w.+@-]+){0,2}", text)
        ):
            return (
                "search.mail",
                "contacts.search",
                "compose.new",
                "calendar.search",
            )
        return ()

    @staticmethod
    def _context_ranked_entities(
        candidates: Sequence[str], context: Mapping[str, str] | None
    ) -> tuple[str, ...]:
        """Use current area only to rank ambiguous actions, never to change their meaning."""

        current_area = (context or {}).get("current_area", "").casefold()
        preferred_prefix = {
            "calendar": "calendar.",
            "compose": "compose.",
            "contacts": "contacts.",
            "mail": "search.",
            "search": "search.",
        }.get(current_area)
        if preferred_prefix is None:
            return tuple(candidates)
        return tuple(
            sorted(
                candidates,
                key=lambda command_id: (
                    not command_id.startswith(preferred_prefix),
                    tuple(candidates).index(command_id),
                ),
            )
        )

    @staticmethod
    def _contextual_intent(
        normalized: NormalizedInput,
        context: Mapping[str, str] | None,
    ) -> _SemanticIntent | None:
        """Resolve short verbs only when the selected object makes them unambiguous."""

        runtime = context or {}
        for (requirement, phrase), command_ids in CONTEXTUAL_QUERY_GROUPS.items():
            if runtime.get(requirement) and normalized.normalized == phrase:
                return _SemanticIntent(
                    command_ids[0],
                    0.96,
                    f"Selected {requirement.removeprefix('selected_').replace('_', ' ')} action",
                )
        if runtime.get("selected_event"):
            command_id = {
                "accept": "calendar.accept_invitation",
                "decline": "calendar.decline_invitation",
                "tentative": "calendar.tentatively_accept",
            }.get(normalized.normalized)
            if command_id is not None:
                return _SemanticIntent(command_id, 0.96, "Selected invitation response")
        return None

    @staticmethod
    def _semantic_intent(normalized: NormalizedInput) -> _SemanticIntent | None:
        """Resolve broad intent and operation before corpus-wide fuzzy ranking.

        These rules identify command families and leave values to the typed entity stages.  They
        intentionally use word-boundary clauses rather than an exact-phrase grammar, so filler,
        ordering, and small perturbations do not change semantic meaning.
        """

        text = normalized.normalized
        tokens = set(normalized.tokens)

        def has(pattern: str) -> bool:
            return re.search(pattern, text) is not None

        def intent(command_id: str, reason: str, confidence: float = 0.91) -> _SemanticIntent:
            return _SemanticIntent(command_id, confidence, reason)

        if has(r"^(?:search|find)\s+(?:subject|body)\s+\S") or has(
            r"^(?:search|find)\s+(?:in\s+)?"
            r"(?:deleted items|trash|inbox|drafts|sent items|junk)\s+for\s+\S"
        ):
            return intent("search.mail", "Explicit field or folder search", 0.97)

        # Explicit search exclusions must win over category nouns inside the excluded clause
        # (for example, excluding calendar invitations is still a mail search).
        if has(r"\b(?:not from|not in|without|except|excluding|but not)\b") and (
            tokens & {"email", "emails", "everything", "mail", "message", "messages", "pdfs"}
        ):
            return intent("search.mail", "Natural-language search exclusion", 0.93)

        # Questions about operation stay in the embedded Help system.  Diagnostic and storage
        # questions are handled below by their stronger domain vocabulary.
        if has(r"\b(?:run|start).*(?:windows|computer).*(?:starts?|startup|login|boot)\b"):
            return intent("help.search", "Windows-startup Help question", 0.94)
        if has(r"\bopen help (?:about|for)\b") or has(r"^(?:how do|how can|what does)\b"):
            return intent("help.search", "Natural-language Help question", 0.94)
        if has(r"\b(?:keyboard shortcuts?|hotkeys?)\b"):
            return intent("help.shortcuts", "Shortcut Help request", 0.95)
        if has(r"\b(?:natural commands? examples?|command examples?)\b"):
            return intent("help.show_command_examples", "Command-example Help request", 0.94)
        if text in {"help", "open help", "help me"}:
            return intent("help.open", "Help navigation", 0.96)
        if has(r"\bshow (?:all )?commands?\b"):
            return intent("help.show_command_reference", "Command-reference request", 0.94)

        # Diagnostics outrank mailbox free-text search for recognizable failure language.
        if has(r"\b(?:mail|connection) (?:is )?(?:broken|stopped|not|isnt).*(?:load|work)") or has(
            r"\bwhy (?:isnt|wont|didnt) mail (?:load|loading|work)"
        ):
            return intent("diagnostics.connection", "Connection-diagnostic language", 0.95)
        if has(r"\b(?:check|test) (?:the )?connection\b"):
            return intent("diagnostics.connection", "Connection check", 0.95)
        if has(r"\breconnect\b"):
            return intent("diagnostics.reconnect", "Reconnect request", 0.94)
        if has(r"\b(?:run )?doctor\b"):
            return intent("diagnostics.doctor", "Doctor request", 0.95)
        if has(r"\b(?:fix|repair) (?:the )?cache\b"):
            return intent("diagnostics.repair_cache", "Cache repair request", 0.93)
        if has(r"\b(?:check|verify) (?:the )?(?:database|db)\b"):
            return intent("diagnostics.verify_local_database", "Database verification", 0.93)
        if has(r"\bserver version\b"):
            return intent("diagnostics.show_server_version", "Server-version request", 0.94)
        if has(r"\b(?:ews|exchange).*(?:features|operations|capabilities|available)\b"):
            return intent("diagnostics.ews_capabilities", "EWS capability request", 0.94)
        if has(r"\b(?:show|open|view) (?:the )?logs?\b"):
            return intent("diagnostics.logs", "Log-view request", 0.94)
        if has(r"\bexport (?:sanitized )?diagnostics\b"):
            return intent("diagnostics.export_sanitized_diagnostics", "Diagnostic export", 0.93)

        # Settings queries open the real category surface instead of pretending to mutate a
        # metadata value that the canonical command does not accept as a typed parameter.
        if has(r"\bnotifications? settings\b"):
            return intent("settings.open_notification_settings", "Notification settings", 0.95)
        if has(r"\bstorage settings\b") or has(r"\b(?:change|set).*(?:quota|mailbox limit)\b"):
            return intent("settings.open_storage_settings", "Storage settings", 0.94)
        if has(r"\b(?:advanced settings|expert mode)\b"):
            return intent("settings.open_advanced_settings", "Advanced settings", 0.94)
        if has(r"\breset appearance settings\b"):
            return intent("appearance.reset_appearance", "Appearance reset", 0.93)
        if has(
            r"\b(?:change|set|enable|disable|turn).*(?:download folder|remote images?|autosave|"
            r"fuzzy search|attachment indexing)\b"
        ):
            return intent("settings.search", "Setting lookup", 0.91)
        if text in {"settings", "preferences"} or has(r"\b(?:open|show) settings\b"):
            return intent("settings.open", "Settings navigation", 0.95)

        # Layout and appearance are separated so "preview" is not confused with theme preview.
        if has(r"\b(?:preview|email|reader) (?:pane )?(?:even )?(?:wider|bigger|larger)\b"):
            return intent("layout.preview_wider", "Preview-width adjustment", 0.95)
        if has(r"\b(?:preview|email|reader) (?:pane )?(?:narrower|smaller)\b"):
            return intent("layout.preview_narrower", "Preview-width adjustment", 0.95)
        if has(r"\b(?:list|message list) wider\b"):
            return intent("layout.make_message_list_wider", "Message-list width adjustment", 0.94)
        if has(r"\bsidebar (?:smaller|narrower)\b"):
            return intent("layout.make_sidebar_narrower", "Sidebar width adjustment", 0.94)
        if has(r"\b(?:hide|toggle) sidebar\b"):
            return intent("layout.toggle_sidebar", "Sidebar visibility", 0.94)
        if has(r"\b(?:hide|collapse) preview\b"):
            return intent("layout.collapse_preview", "Preview visibility", 0.94)
        if has(r"\b(?:open|show|expand) preview\b"):
            return intent("layout.expand_preview", "Preview visibility", 0.94)
        if has(r"\breading (?:mode|layout)\b"):
            return intent("layout.switch_to_reading_layout", "Reading layout", 0.94)
        if has(r"\blist only\b"):
            return intent("layout.switch_to_list_only_layout", "List-only layout", 0.94)
        if has(r"\breset layout\b"):
            return intent("layout.reset", "Layout reset", 0.95)
        if has(r"\b(?:open .* separately|float this|detach reader|floating reader)\b"):
            return intent("layout.open_floating_reader", "Detached-reader request", 0.94)

        if (
            has(r"\b(?:dark|light) mode\b")
            or has(r"\b(?:switch|use|apply).*(?:purple|german|guc|theme)\b|\bguc theme\b")
        ) and "logo" not in tokens:
            return intent("appearance.apply_theme", "Theme application", 0.93)
        if has(r"\b(?:show|find|search).*(?:oled|blue|themes?)\b"):
            return intent("appearance.search_themes", "Theme search", 0.93)
        if has(r"\bpreview (?!pane|email|message|cleanup)(?:the )?[\w -]+(?: theme)?\b"):
            return intent("appearance.preview_theme", "Theme preview", 0.91)
        if has(r"\bnext theme\b"):
            return intent("appearance.next_theme", "Next-theme navigation", 0.95)
        if has(r"\bprevious theme\b"):
            return intent("appearance.previous_theme", "Previous-theme navigation", 0.95)
        if has(r"\bfavorite (?:this )?theme\b"):
            return intent("appearance.favorite_theme", "Favorite-theme request", 0.94)
        if has(r"\b(?:make|create) (?:a )?custom theme\b"):
            return intent("appearance.create_custom_theme", "Custom-theme creation", 0.94)
        if has(r"\bhide (?:the )?(?:guc |ascii )?(?:banner|logo)\b"):
            return intent("appearance.hide_ascii_banner", "Branding visibility", 0.94)
        if has(r"\b(?:use|show) (?:the )?guc logo\b"):
            return intent("appearance.show_ascii_banner", "GUC branding request", 0.94)
        if has(r"\bchange (?:the )?(?:ascii )?(?:logo|banner)\b"):
            return intent("appearance.change_ascii_banner", "Branding selection", 0.94)

        # Integrations are recognized before generic notification/send words.
        if "discord" in tokens and tokens & {"test", "check"}:
            return intent("integrations.discord_test", "Discord test", 0.95)
        if has(r"\b(?:add|setup|configure|notify|send).*(?:discord)\b"):
            return intent("integrations.add_discord_webhook", "Discord integration setup", 0.92)
        if has(r"\bdisable discord notifications\b"):
            return intent("integrations.pause_webhook", "Discord integration pause", 0.92)
        if has(r"\btest (?:generic )?webhook\b"):
            return intent("integrations.webhook_test", "Webhook test", 0.95)
        if has(r"\bretry failed webhooks?\b"):
            return intent("integrations.retry_failed_webhook", "Webhook retry", 0.93)
        if has(r"\b(?:show|view).*(?:webhook failures|failed webhook|webhook history)\b"):
            return intent("integrations.show_webhook_delivery_history", "Webhook history", 0.94)
        if has(r"\badd (?:a )?webhook\b"):
            return intent("integrations.add_webhook", "Webhook setup", 0.92)
        if has(r"\b(?:show my webhooks|open integrations|webhook settings)\b"):
            return intent("integrations.open", "Integration navigation", 0.94)

        if has(r"\btest notifications?\b"):
            return intent("notifications.test", "Notification test", 0.95)
        if has(r"\bnotification history\b"):
            return intent("notifications.history", "Notification history", 0.95)
        if has(r"\b(?:dont|do not) show subject.*notifications?\b"):
            return intent("notifications.hide_notification_previews", "Notification privacy", 0.94)
        if has(r"\bmute notifications?\b"):
            return intent("notifications.mute_notifications", "Notification mute", 0.94)
        if has(r"\bquiet hours?\b"):
            return intent("notifications.enable_quiet_hours", "Quiet-hours configuration", 0.94)
        if has(r"\bturn notifications? on\b"):
            return intent(
                "notifications.enable_desktop_notifications", "Enable notifications", 0.94
            )
        if has(r"\bturn notifications? off\b"):
            return intent(
                "notifications.disable_desktop_notifications", "Disable notifications", 0.94
            )
        if text == "desktop notifications":
            return intent("notifications.settings", "Notification settings", 0.9)
        if has(r"^notifications?\s+.+") and not tokens & {
            "history",
            "settings",
            "test",
        }:
            return intent("monitoring.start", "Sender-filtered notification request", 0.9)

        if has(r"\b(?:stop monitoring|stop watching|pause watcher)\b"):
            return intent("monitoring.stop", "Stop monitoring", 0.95)
        if has(r"\b(?:watch status|monitoring status|last sync|last check)\b"):
            return intent("monitoring.status", "Monitoring status", 0.95)
        if has(r"\b(?:refresh monitoring|force sync)\b"):
            return intent("monitoring.force_synchronization", "Monitoring refresh", 0.93)
        if has(r"\b(?:watch|monitor|keep checking|notify me (?:of|only|when|about))\b"):
            if "important" in tokens:
                return intent(
                    "monitoring.monitor_important_messages", "Important-message monitoring", 0.92
                )
            if tokens & {"attachment", "attachments", "pdf", "pdfs"}:
                return intent(
                    "monitoring.monitor_messages_with_attachments",
                    "Attachment-message monitoring",
                    0.92,
                )
            if has(r"\b(?:from|when)\s+[\w@.+ -]+(?:emails?|messages?)?\b"):
                return intent("monitoring.start", "Sender-filtered monitoring", 0.92)
            if has(r"\b(?:inbox|mail)\b"):
                return intent("monitoring.start", "Mailbox monitoring", 0.93)
            return intent("monitoring.start", "Monitoring request", 0.9)

        # Storage and cleanup analysis remain read-only unless a clearly named local plan action
        # is selected.  "Clean" never maps directly to deletion.
        if has(r"\b(?:storage|space).*(?:sent|sent items)\b"):
            return intent("storage.show_storage_by_folder", "Folder storage analysis", 0.95)
        if has(r"\b(?:quota|how full is my mailbox|space remaining|space do i have left)\b"):
            return intent("storage.open", "Mailbox storage request", 0.94)
        if has(r"\b(?:storage by folder|biggest folders|largest folders)\b"):
            return intent("storage.show_storage_by_folder", "Storage-by-folder analysis", 0.94)
        if has(
            r"\b(?:most frequent sender|who emails me most|which sender takes most space|"
            r"largest senders)\b"
        ):
            return intent("storage.show_storage_by_sender", "Sender storage ranking", 0.92)
        if has(r"\bmost common attachment types?\b"):
            return intent(
                "storage.show_storage_by_attachment_type", "Attachment-type storage ranking", 0.94
            )
        if has(r"\b(?:largest|biggest|top \d+).*(?:attachments?|attached files?)\b"):
            return intent("storage.largest_attachments", "Largest-attachment ranking", 0.95)
        if has(r"\b(?:largest|biggest|top \d+).*(?:emails?|messages?)\b"):
            return intent("storage.largest_messages", "Largest-message ranking", 0.95)
        if has(r"\b(?:whats|what is) (?:using|taking up) (?:my )?(?:storage|space)\b"):
            return intent("storage.open", "Storage analysis", 0.93)
        if has(r"\b(?:why is my mailbox full|how much could i free)\b"):
            return intent("storage.analyze_reclaimable_space", "Reclaimable-space analysis", 0.93)
        if text in {"storage", "mailbox size", "space remaining", "quota"}:
            return intent("storage.open", "Storage navigation", 0.95)

        if has(r"\bfind (?:old|stale) newsletters?\b"):
            return intent("cleanup.find_stale_newsletters", "Newsletter cleanup analysis", 0.94)
        if has(r"\bfind duplicate (?:mail|emails?|messages?)\b"):
            return intent("cleanup.find_duplicate_messages", "Duplicate-message analysis", 0.94)
        if has(r"\bfind duplicate attachments?\b"):
            return intent(
                "cleanup.find_duplicate_attachments", "Duplicate-attachment analysis", 0.94
            )
        if has(r"\bfind (?:huge|large) messages?\b"):
            return intent("cleanup.find_large_messages", "Large-message analysis", 0.94)
        if has(r"\bfind junk taking space\b"):
            return intent("cleanup.analyze", "Junk cleanup analysis", 0.92)
        if has(r"\b(?:make|create) (?:a )?cleanup plan\b"):
            return intent("cleanup.create_cleanup_plan", "Cleanup-plan creation", 0.91)
        if has(r"\bpreview cleanup\b"):
            return intent("cleanup.preview_cleanup_plan", "Cleanup-plan preview", 0.94)
        if has(
            r"\b(?:clean|cleanup|clean up|free|reduce).*(?:inbox|mailbox|mail|space|mb)\b"
        ) or has(r"\b(?:things|stuff).*(?:safe to remove|can delete)\b"):
            return intent("cleanup.analyze", "Safe cleanup analysis", 0.92)

        if has(r"\bdelete old backups?\b"):
            return intent("backup.prune_old_backups", "Old-backup pruning", 0.91)
        if has(r"\b(?:how big|estimate).*(?:backup)\b"):
            return intent("backup.estimate_backup_size", "Backup-size estimate", 0.94)
        if has(r"\bverify (?:the )?(?:latest|last)?\s*backup\b"):
            return intent("backup.verify", "Backup verification", 0.95)
        if has(r"\bresume backup\b"):
            return intent("backup.resume_backup", "Backup resume", 0.94)
        if has(r"\b(?:open|browse) (?:my |last |latest )?backup\b"):
            return intent("backup.open_backup", "Backup browsing", 0.94)
        if has(r"\b(?:make|create) (?:a )?restore plan\b"):
            return intent("backup.restore_plan", "Restore-plan request", 0.94)
        if has(r"\bincremental backup\b"):
            return intent("backup.create_incremental_backup", "Incremental-backup request", 0.93)
        if has(r"\bfull backup\b"):
            return intent("backup.create_full_backup", "Full-backup request", 0.93)
        if has(r"\bback ?up (?:these )?search results?\b"):
            return intent("backup.back_up_search_results", "Search-result backup", 0.92)
        if has(r"\bback ?up (?:these|selected)\b"):
            return intent("backup.back_up_selected_messages", "Selected-message backup", 0.92)
        if has(r"\bback ?up (?:this|current|the)?\s*folder\b"):
            return intent("backup.back_up_current_folder", "Folder backup", 0.92)
        if has(r"\bback ?up|\bbackup\b"):
            if text == "backup" or has(r"\bshow backups?\b"):
                return intent("backup.open", "Backup navigation", 0.94)
            return intent("backup.create", "Mailbox-backup request", 0.9)

        # Task/contact/calendar vocabulary is strong enough to outrank generic mail words.
        if has(r"\b(?:finish|complete|mark).*\btask\b"):
            return intent("tasks.complete", "Task completion", 0.94)
        if has(r"\breopen (?:this )?task\b"):
            return intent("tasks.reopen_task", "Task reopening", 0.94)
        if has(r"\boverdue tasks?\b"):
            return intent("tasks.show_overdue_tasks", "Overdue-task view", 0.95)
        if has(r"\btasks? (?:today|due today)\b"):
            return intent("tasks.show_tasks_due_today", "Today's tasks", 0.95)
        if has(r"\bmove due date\b"):
            return intent("tasks.change_due_date", "Task due-date change", 0.92)
        if has(r"\b(?:new task|task due|remind me|make a task|task from this email)\b"):
            return intent("tasks.create", "Task creation", 0.92)
        if text in {"task", "tasks", "open tasks"}:
            return intent("tasks.open", "Task navigation", 0.95)

        if has(r"\bfind duplicate contacts?\b"):
            return intent("contacts.find_duplicate_contacts", "Duplicate-contact search", 0.94)
        if has(r"\bmerge (?:these )?contacts?\b"):
            return intent("contacts.merge_contacts", "Contact merge", 0.91)
        if has(r"\brecent recipients?\b"):
            return intent("contacts.show_recent_recipients", "Recent recipients", 0.95)
        if has(r"\b(?:people|contacts?) i email most\b"):
            return intent("contacts.show_frequent_correspondents", "Frequent correspondents", 0.94)
        if has(r"\b(?:show )?correspondence with\b"):
            return intent("contacts.show_correspondence_history", "Correspondence history", 0.93)
        if has(r"\b(?:open|show) this senders? contact\b"):
            return intent("contacts.open_sender_contact", "Selected-sender contact", 0.95)
        if has(r"\badd this sender to contacts\b"):
            return intent("contacts.add_sender_to_contacts", "Add sender to contacts", 0.94)
        if has(r"\b(?:email|write to|compose to) this contact\b"):
            return intent("contacts.compose_to_contact", "Compose to selected contact", 0.94)
        if has(r"\b(?:meeting|meet|schedule).*(?:with )?this contact\b"):
            return intent(
                "contacts.schedule_meeting_with_contact",
                "Schedule with selected contact",
                0.93,
            )
        if has(r"\b(?:save|add).*(?:as )?(?:a )?contact\b"):
            return intent("contacts.create", "Contact creation", 0.92)
        if has(r"\bedit scheduled draft\b"):
            return intent("drafts.edit_draft", "Scheduled-draft editing", 0.93)
        if has(r"\bedit [\w@.+ -]+(?: contact)?$") and not tokens & {
            "draft",
            "email",
            "meeting",
            "scheduled",
        }:
            return intent("contacts.edit_contact", "Contact editing", 0.89)
        if has(r"\b(?:contact|phone number|email address)\b") and not has(
            r"\b(?:cancel|new|schedule|send|compose|write)\b"
        ):
            return intent("contacts.search", "Contact lookup", 0.91)
        if has(r"^(?:what is )?[\w-]+s email$") and not has(
            r"\b(?:cancel|new|schedule|send|compose|write)\b"
        ):
            return intent("contacts.search", "Contact lookup", 0.91)
        if has(r"\b(?:find|look up|search) [\w@.+-]+$") and not tokens & {
            "mail",
            "message",
            "messages",
        }:
            return intent("contacts.search", "Person lookup", 0.86)

        if has(r"\b(?:cancel this meeting|cancel meeting)\b"):
            return intent("calendar.cancel_meeting", "Meeting cancellation", 0.92)
        if has(r"\baccept (?:this )?invitation\b"):
            return intent("calendar.accept_invitation", "Invitation response", 0.94)
        if has(r"\bdecline (?:this )?(?:invitation|meeting)\b"):
            return intent("calendar.decline_invitation", "Invitation response", 0.94)
        if text == "tentative" or has(r"\btentatively accept\b"):
            return intent("calendar.tentatively_accept", "Tentative invitation response", 0.94)
        if has(r"\b(?:when is|find free time|free time with).*(?:free|with|[\w@.+-])\b"):
            return intent("calendar.find_free_time", "Free-time search", 0.92)
        if has(r"\bconflicts?\b"):
            return intent("calendar.show_conflicts", "Calendar-conflict request", 0.94)
        if has(r"\bsearch calendar\b") or has(r"\bshow meetings? with\b"):
            return intent("calendar.search", "Calendar search", 0.93)
        if has(r"\badd .* to this meeting\b"):
            return intent("calendar.add_attendee", "Meeting-attendee addition", 0.92)
        if has(r"\bmove this meeting\b"):
            return intent("calendar.edit_event", "Meeting-time edit", 0.91)
        if has(
            r"\b(?:create event|make a meeting|new meeting|meeting .* tomorrow|"
            r"schedule (?!this|email|mail|draft).+ monday)\b"
        ):
            return intent("calendar.create", "Calendar-event creation", 0.9)
        if has(r"\bgo to [a-z]+ \d{1,2}\b"):
            return intent("calendar.go_to_date", "Calendar date navigation", 0.93)
        if has(r"\b(?:calendar|appointments?|meetings?)\b"):
            if "tomorrow" in tokens:
                return intent("calendar.tomorrow", "Tomorrow calendar view", 0.95)
            if has(r"\bnext week\b"):
                return intent("calendar.open_next_week", "Next-week calendar view", 0.95)
            if has(r"\bthis week\b"):
                return intent("calendar.open_this_week", "This-week calendar view", 0.95)
            if "today" in tokens:
                return intent("calendar.open_today", "Today's calendar view", 0.95)
            return intent("calendar.open", "Calendar navigation", 0.94)
        if has(r"^(?:show )?today$") or has(r"\bwhat do i have today\b"):
            return intent("calendar.open_today", "Today's calendar view", 0.91)

        # Scheduling must precede ordinary send language.  Future time extraction supplies an
        # absolute timezone-aware preview in the entity stage.
        if has(r"\bpause recurring (?:email|message)\b"):
            return intent("scheduled.pause_recurring_message", "Recurring-message pause", 0.92)
        if has(r"\bsend scheduled (?:message|email) now\b"):
            return intent("scheduled.send_scheduled_message_now", "Immediate scheduled send", 0.92)
        if has(r"\breschedule (?:email|message)\b"):
            return intent("scheduled.reschedule", "Scheduled-message reschedule", 0.92)
        if has(r"\bcancel .*(?:scheduled|tomorrows? email|email to)\b"):
            return intent("scheduled.cancel", "Scheduled-message cancellation", 0.9)
        if has(r"\b(?:next scheduled|scheduled messages?|what .* scheduled|what im sending)\b"):
            if has(r"\bnext scheduled\b"):
                return intent(
                    "scheduled.show_next_scheduled_message", "Next scheduled message", 0.94
                )
            return intent("scheduled.open", "Scheduled-message view", 0.94)
        if has(r"\bscheduler status\b"):
            return intent("scheduled.open_scheduler_status", "Scheduler-status request", 0.94)
        if has(
            r"\b(?:schedule|send|delay).*(?:tomorrow|tonight|monday|tuesday|wednesday|"
            r"thursday|friday|saturday|sunday|next week|next weekday|in \d+|by an? hour)\b"
        ):
            return intent("scheduled.schedule_current", "Future send schedule", 0.93)

        if has(r"\bedit scheduled draft\b"):
            return intent("drafts.edit_draft", "Scheduled-draft editing", 0.93)

        if has(r"\bdelete this draft\b"):
            return intent("drafts.delete_draft", "Draft deletion", 0.92)
        if has(r"\bsend this draft\b"):
            return intent("drafts.send_draft", "Draft send", 0.92)
        if has(r"\bduplicate this draft\b"):
            return intent("drafts.duplicate_draft", "Draft duplication", 0.94)
        if has(r"\b(?:recover my last draft|recover.*draft|autosaved draft)\b"):
            return intent("draft.recover", "Draft recovery", 0.94)
        if has(r"\b(?:find old drafts|show drafts? from|search drafts?)\b"):
            return intent("drafts.search_drafts", "Draft search", 0.93)
        if has(r"\b(?:open my drafts|show unfinished emails|open drafts)\b"):
            return intent("mail.open_drafts", "Draft-folder navigation", 0.95)
        if has(r"\b(?:save this as draft|draft this)\b"):
            return intent("draft.save", "Draft save", 0.94)

        # Compose opens a populated editor; wording such as "send an email to" does not transmit.
        if has(r"\b(?:email|write|new mail|new email|compose|send an email)\s+(?:to )?[\w@.+-]"):
            return intent("compose.new", "Compose request", 0.92)
        if has(r"\bnew email with this file attached\b"):
            return intent("compose.new", "Compose-with-attachment request", 0.92)

        # Scoped message actions.  Broad deletion remains cleanup analysis.
        if has(r"\breply all\b") or has(r"\banswer everyone\b"):
            return intent("mail.reply_all", "Reply-all action", 0.95)
        if text == "reply" or has(r"\b(?:reply|answer|respond) (?:to )?this\b"):
            return intent("mail.reply", "Reply action", 0.94)
        if has(r"\b(?:forward this|send this on)\b"):
            return intent("mail.forward", "Forward action", 0.92)
        if has(r"\bmark .*(?:unread|not read)\b") or has(r"\bmake this unread\b"):
            return intent("mail.mark_unread", "Unread-state action", 0.95)
        if has(r"\bmark .*(?:read)\b"):
            return intent("mail.mark_read", "Read-state action", 0.95)
        if has(r"\bunflag this\b"):
            return intent("mail.unflag", "Flag removal", 0.95)
        if has(r"\bflag this\b"):
            return intent("mail.flag", "Flag action", 0.95)
        if has(r"\b(?:move|put|file) (?:this|these).*(?:trash|deleted items)\b"):
            return intent("mail.delete", "Move to Deleted Items", 0.92)
        if has(r"\b(?:move|put|file) (?:this|these).*(?:to|in|under)\b"):
            return intent("mail.move", "Message move", 0.92)
        if has(r"\bcopy (?:this|these).*(?:to|in)\b"):
            return intent("mail.copy_message", "Message copy", 0.92)
        if has(r"\b(?:restore this|put this back)\b"):
            return intent("mail.restore", "Message restore", 0.92)
        if has(r"\bdelete (?:this|selected)\b"):
            return intent("mail.delete", "Scoped message deletion", 0.92)
        if has(r"\bdelete .*(?:mail|email|messages?)\b"):
            return intent(
                "cleanup.analyze",
                "Dangerous language converted to bounded cleanup analysis",
                0.82,
            )
        if has(
            r"\b(?:open|read|show) (?:this|selected|full) (?:email|mail|message)?\b"
        ) or text in {
            "open this",
            "read this",
            "open message",
            "show full message",
        }:
            return intent("mail.open_message", "Message-reader action", 0.94)

        if has(r"\bsave all attachments? from this message\b"):
            return intent("attachments.download_all_attachments", "Attachment download", 0.92)
        if has(r"\bdownload pdfs? from\b"):
            return intent("attachments.search", "Bounded attachment search before download", 0.9)
        if has(r"\b(?:find|search).*(?:attachment|attachments).*\b") or has(
            r"\battachments? (?:containing|bigger|larger|over)\b"
        ):
            return intent("attachments.search", "Attachment search", 0.92)
        if has(r"\b(?:anything from|conversation with|our emails with)\b"):
            return intent("search.mail", "Correspondent mail search", 0.91)
        if has(r"\b(?:show|find) more from this sender\b"):
            return intent("search.search_sender", "Selected-sender mail search", 0.95)
        if has(r"\bfind (?:emails?|mail|messages?) related to this\b"):
            return intent("search.similar_message_search", "Related-message search", 0.94)
        if has(
            r"\b(?:find|search).*(?:in subject|bodies? for|anywhere)\b|"
            r"\bsubject (?:has|contains?)\b"
        ):
            return intent("search.mail", "Fielded mail search", 0.91)
        if has(r"\b(?:people waiting on me|waiting for (?:me|them))\b"):
            return intent("search.mail", "Conversation-response search", 0.92)

        # Search intent is deliberately broad and order-independent, but requires recognizable
        # mail/filter vocabulary so unrelated prose continues to the safe global-search fallback.
        mail_words = {"email", "emails", "mail", "message", "messages", "msgs"}
        filter_words = {
            "after",
            "attachment",
            "attachments",
            "before",
            "between",
            "except",
            "flagged",
            "from",
            "important",
            "largest",
            "larger",
            "bigger",
            "than",
            "kb",
            "mb",
            "gb",
            "newest",
            "oldest",
            "pdf",
            "pdfs",
            "read",
            "recipient",
            "recipients",
            "subject",
            "to",
            "unread",
            "without",
        }
        if (
            tokens & mail_words
            or (
                tokens & {"pdf", "pdfs", "attachments", "unread", "flagged"}
                and tokens & filter_words
            )
            or has(r"\b(?:conversation|threads?).*(?:reply|response|waiting|last message)\b")
        ):
            return intent("search.mail", "Natural-language mail search", 0.91)

        # Common folder aliases work even without an explicit navigation verb.
        folder_commands = {
            "archive": "mail.open_archive",
            "bin": "mail.open_deleted",
            "deleted": "mail.open_deleted",
            "deleted items": "mail.open_deleted",
            "deleted mail": "mail.open_deleted",
            "drafts": "mail.open_drafts",
            "inbox": "mail.open_inbox",
            "junk": "mail.open_junk",
            "outgoing history": "mail.open_sent",
            "sent": "mail.open_sent",
            "sent items": "mail.open_sent",
            "sent mail": "mail.open_sent",
            "trash": "mail.open_deleted",
        }
        if text in folder_commands:
            return intent(folder_commands[text], "Folder alias", 0.96)
        if text.endswith(" folder"):
            return intent("folders.open", "Folder-name request", 0.88)
        return None

    @staticmethod
    def _split_allowlisted_sequence(
        text: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
        normalized = normalize_text(text).normalized
        separator = r"(?:;|\band\s+then\b|\bthen\b|\band\b)"
        patterns: tuple[tuple[str, str, str], ...] = (
            (
                rf"^(.+?)\s+{separator}\s+(mark (?:them|those|the results) read)$",
                "search.mail",
                "mail.mark_read",
            ),
            (
                rf"^(.+?)\s+{separator}\s+(mark (?:them|those|the results) unread)$",
                "search.mail",
                "mail.mark_unread",
            ),
            (
                rf"^(.+?)\s+{separator}\s+(back (?:them|those|the results) up)$",
                "search.mail",
                "backup.back_up_search_results",
            ),
            (
                rf"^(.+?)\s+{separator}\s+((?:save|download) (?:them|those|the results)"
                r"(?: to [\w .\\/-]+)?)$",
                "attachments.search",
                "attachments.download_all_attachments",
            ),
        )
        handoff_names = {
            "mail.mark_read": "message_results",
            "mail.mark_unread": "message_results",
            "backup.back_up_search_results": "search_results",
            "attachments.download_all_attachments": "attachment_results",
        }
        for pattern, first_command, second_command in patterns:
            match = re.fullmatch(pattern, normalized)
            if match is None:
                continue
            first, second = match.group(1).strip(), match.group(2).strip()
            if not re.search(
                r"\b(?:find|search|show|mail|emails?|messages?|pdfs?|attachments?)\b", first
            ):
                return None
            return (
                (first, second),
                (first_command, second_command),
                (handoff_names[second_command],),
            )
        return None

    @staticmethod
    def _choose_exact(exact_ids: Sequence[str], candidates: Sequence[CommandCandidate]) -> str:
        if len(exact_ids) == 1:
            return exact_ids[0]
        ranking = {candidate.command_id: index for index, candidate in enumerate(candidates)}
        return min(exact_ids, key=lambda command_id: ranking.get(command_id, 1_000_000))

    def _build_result(
        self,
        normalized: NormalizedInput,
        command_id: str,
        candidates: Sequence[CommandCandidate],
        *,
        confidence: float,
        reason: str,
        context: Mapping[str, str] | None,
        folder_candidates: Sequence[str],
        person_candidates: Sequence[str],
        exact: bool,
    ) -> NaturalLanguageResult:
        command = self.registry.get(command_id)
        entities = self._entities(
            normalized.original,
            command,
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
        )
        if (entities.person and entities.person.ambiguous) or (
            entities.folder and entities.folder.ambiguous
        ):
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=tuple(candidates[:6]),
                entities=entities,
                explanation="A person or folder name has multiple plausible matches.",
            )
        slots = self._slots(command, normalized.original, entities, context=context)
        preview = self._preview_lines(
            command,
            slots,
            entities,
            confidence=confidence,
            available_context=tuple(key for key, value in (context or {}).items() if value),
        )
        invocation = CommandInvocation.from_mapping(
            command.command_id,
            slots,
            original_text=normalized.original,
            confidence=confidence,
            explanation=reason,
        )
        errors = self.registry.validate_invocation(invocation)
        if errors:
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.INCOMPLETE,
                invocation=None,
                candidates=tuple(candidates[:6]),
                entities=entities,
                explanation="; ".join(errors),
                preview=preview,
            )
        return NaturalLanguageResult(
            original=normalized.original,
            normalized=normalized.normalized,
            kind=InterpretationKind.COMMAND,
            invocation=invocation,
            candidates=tuple(candidates[:6]),
            entities=entities,
            explanation=(
                f"{command.name}. {reason}."
                + (
                    " Preview and confirmation are required."
                    if command.confirmation_required
                    else ""
                )
                + (" Parameters were resolved from context." if context and not exact else "")
            ),
            preview=preview,
        )

    def _safe_dangerous_result(
        self,
        normalized: NormalizedInput,
        candidates: Sequence[CommandCandidate],
        *,
        context: Mapping[str, str] | None,
        folder_candidates: Sequence[str],
        person_candidates: Sequence[str],
    ) -> NaturalLanguageResult:
        if re.search(r"\bsend\b", normalized.normalized):
            try:
                scheduled_time = extract_future_datetime(
                    normalized.original,
                    timezone=self.timezone,
                    now=self.now,
                )
            except ValueError:
                scheduled_time = None
            if scheduled_time is not None and "send all" not in normalized.normalized:
                return self._build_result(
                    normalized,
                    "scheduled.schedule_current",
                    candidates,
                    confidence=0.88,
                    reason="Explicit future send time",
                    context=context,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                    exact=False,
                )
        deletion_words = {"bulk", "delete", "empty", "erase", "purge", "remove", "wipe"}
        if not any(
            re.search(rf"\b{re.escape(word)}\b", normalized.normalized) for word in deletion_words
        ):
            return NaturalLanguageResult(
                original=normalized.original,
                normalized=normalized.normalized,
                kind=InterpretationKind.AMBIGUOUS,
                invocation=None,
                candidates=tuple(candidates[:6]),
                entities=self._entities(
                    normalized.original,
                    None,
                    folder_candidates=folder_candidates,
                    person_candidates=person_candidates,
                ),
                explanation=(
                    "A safety-sensitive action was not explicit enough; choose an exact command."
                ),
            )
        command = self.registry.get("cleanup.analyze")
        entities = self._entities(
            normalized.original,
            command,
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
        )
        invocation = CommandInvocation.from_mapping(
            command.command_id,
            self._slots(command, normalized.original, entities, context=context),
            original_text=normalized.original,
            confidence=0.72,
            explanation="Dangerous language was converted to safe cleanup analysis.",
        )
        safe_candidate = CommandCandidate(command.command_id, 0.72, "Safe analysis fallback")
        preview = self._preview_lines(
            command,
            invocation.as_dict(),
            entities,
            confidence=invocation.confidence,
            available_context=tuple(key for key, value in (context or {}).items() if value),
        )
        return NaturalLanguageResult(
            original=normalized.original,
            normalized=normalized.normalized,
            kind=InterpretationKind.COMMAND,
            invocation=invocation,
            candidates=(safe_candidate, *tuple(candidates[:5])),
            entities=entities,
            explanation="No deletion was selected. Review a cleanup analysis before any mutation.",
            preview=preview,
        )

    def _fallback(
        self,
        normalized: NormalizedInput,
        candidates: Sequence[CommandCandidate],
        *,
        folder_candidates: Sequence[str],
        person_candidates: Sequence[str],
    ) -> NaturalLanguageResult:
        help_question = normalized.tokens[:1] in {("how",), ("why",), ("what",), ("where",)}
        command_id = "help.search" if help_question else "search.mail"
        invocation = CommandInvocation.from_mapping(
            command_id,
            {"query": normalized.original},
            original_text=normalized.original,
            confidence=0.5,
            explanation="No sufficiently confident command matched.",
        )
        entities = self._entities(
            normalized.original,
            None,
            folder_candidates=folder_candidates,
            person_candidates=person_candidates,
        )
        return NaturalLanguageResult(
            original=normalized.original,
            normalized=normalized.normalized,
            kind=(
                InterpretationKind.COMMAND if help_question else InterpretationKind.SEARCH_FALLBACK
            ),
            invocation=invocation,
            candidates=tuple(candidates[:6]),
            entities=entities,
            explanation=(
                "This will search Help."
                if help_question
                else "This will run as a mail search and can never mutate data."
            ),
            preview=(
                f"Action: {'Search Help' if help_question else 'Search All Mail'}",
                f"Query: {normalized.original}",
                "Mutation: never",
            ),
        )

    def _entities(
        self,
        text: str,
        command: CommandSpec | None,
        *,
        folder_candidates: Sequence[str],
        person_candidates: Sequence[str],
    ) -> ParsedEntities:
        date_range = extract_date_range(text, timezone=self.timezone, now=self.now)
        when = None
        uncertainties: list[str] = []
        if AMBIGUOUS_NUMERIC_DATE_RE.search(text):
            uncertainties.append("Ambiguous numeric date; use YYYY-MM-DD.")
        if command and any(
            parameter.type is ParameterType.DATETIME for parameter in command.parameters
        ):
            try:
                when = extract_future_datetime(text, timezone=self.timezone, now=self.now)
            except ValueError as exc:
                uncertainties.append(str(exc))
        attachment_type = _attachment_type(normalize_text(text).tokens)
        normalized = normalize_text(text).normalized
        exclusions = extract_exclusions(text)
        excluded_fields = {exclusion.field for exclusion in exclusions}
        excluded_attachment_types = {
            exclusion.value for exclusion in exclusions if exclusion.field == "attachment_type"
        }
        if attachment_type in excluded_attachment_types:
            attachment_type = None
        person = resolve_person(text, candidates=person_candidates)
        folder = resolve_folder(text, candidates=folder_candidates)
        if attachment_type == "archive" and not re.search(
            r"\b(?:attachments?|files?|zip|compressed)\b", normalized
        ):
            attachment_type = None
        if (
            folder is not None
            and person is not None
            and normalize_text(person.display).normalized.startswith(("in ", "folder "))
        ):
            person = None
        # Context nouns and folder destinations are not people.  Keep them unresolved until a
        # real selected object is supplied by the caller; never manufacture an ``@current``
        # person merely to make a contextual command look complete.
        if re.search(
            r"\b(?:this|selected|current) (?:contacts?|senders?|messages?|emails?)\b|"
            r"\brelated to this\b|\b(?:move|copy|file|put) (?:this|these)\b",
            normalized,
        ):
            person = None
        if "sender" in excluded_fields and not re.search(
            r"\b(?:to|with|cc|recipient)\s+[\w@.+-]", normalized
        ):
            person = None
        if "folder" in excluded_fields and not re.search(
            r"\b(?:in|folder)\s+(?!deleted\b)[\w -]+", normalized
        ):
            folder = None
        return ParsedEntities(
            person=person,
            folder=folder,
            date_range=date_range,
            when=when,
            size=extract_size_constraint(text),
            attachment_type=attachment_type,
            read_state=(
                "unread"
                if "unread" in normalized
                or "not read" in normalized
                or re.search(r"\b(?:havent|have not|hasnt|has not) read\b", normalized)
                else "read"
                if re.search(r"\bread\b", normalized)
                else None
            ),
            flagged=True if any(word in normalized for word in ("flagged", "starred")) else None,
            important=True
            if any(word in normalized for word in ("important", "high priority"))
            else None,
            has_attachments=(
                False
                if re.search(r"\b(?:without|no) attachments?\b", normalized)
                else True
                if attachment_type or re.search(r"\b(?:with|has) attachments?\b", normalized)
                else None
            ),
            exclusions=exclusions,
            ranking=extract_ranking(text),
            thread_response=extract_thread_response_state(text),
            uncertainties=tuple(uncertainties),
        )

    @staticmethod
    def _slots(
        command: CommandSpec,
        text: str,
        entities: ParsedEntities,
        *,
        context: Mapping[str, str] | None,
    ) -> dict[str, object]:
        allowed = {parameter.name for parameter in command.parameters}
        slots: dict[str, object] = {}
        contextual_backup_prompt = bool(
            command.command_id == "search.search_backups"
            and (context or {}).get("current_backup")
            and normalize_text(text).normalized == "search"
        )
        if "query" in allowed and not contextual_backup_prompt:
            slots["query"] = text
        field_search = re.match(
            r"^(?:search|find)\s+(subject|body)\s+(?:for\s+)?(.+)$", text.strip(), re.I
        )
        folder_search = re.match(
            r"^(?:search|find)\s+(?:in\s+)?"
            r"(?:deleted items|trash|inbox|drafts|sent items|junk)\s+for\s+(.+)$",
            text.strip(),
            re.I,
        )
        if command.command_id == "search.mail" and field_search:
            slots.pop("query", None)
            slots[field_search[1].lower()] = field_search[2].strip()
        elif command.command_id == "search.mail" and folder_search:
            slots["query"] = folder_search[1].strip()
        if command.command_id == "search.search_all_data" and normalize_text(text).normalized in {
            "open local search",
            "local search",
        }:
            slots.pop("query", None)
        if entities.person is not None and not (field_search or folder_search):
            if "person" in allowed:
                slots["person"] = entities.person
            normalized = normalize_text(text).normalized
            if "sender" in allowed and re.search(r"\b(?:from|by|got from)\b", normalized):
                slots["sender"] = entities.person
            if "recipient" in allowed and (
                re.search(
                    r"\b(?:to|cc|i sent|between me and|is ccd|is in (?:the )?recipients)\b",
                    normalized,
                )
                or (
                    re.search(r"\bwith\b", normalized)
                    and "sender" not in slots
                    and not re.search(
                        r"\bwith (?:attachments?|pdfs?|images?|word documents?|excel files?)\b",
                        normalized,
                    )
                )
                or ("sender" not in slots and command.command_id == "compose.new")
            ):
                slots["recipient"] = entities.person
            if (
                "sender" in allowed
                and "sender" not in slots
                and "recipient" not in slots
                and command.command_id in {"attachments.search", "search.mail"}
            ):
                slots["sender"] = entities.person
            if "sender" in allowed and command.command_id == "monitoring.start":
                slots["sender"] = entities.person
        if entities.folder is not None and "folder" in allowed:
            slots["folder"] = entities.folder
        folder_presets = {
            "mail.open_archive": "archive",
            "mail.open_deleted": "deleted",
            "mail.open_drafts": "drafts",
            "mail.open_inbox": "inbox",
            "mail.open_junk": "junk",
            "mail.open_sent": "sent",
        }
        if "folder" in allowed and command.command_id in folder_presets:
            name = folder_presets[command.command_id]
            slots["folder"] = FolderEntity(name.title(), name, source="command_preset")
        if entities.date_range is not None and "date_range" in allowed:
            slots["date_range"] = entities.date_range
        if entities.when is not None and "when" in allowed:
            slots["when"] = entities.when
        if entities.size is not None and "size" in allowed:
            slots["size"] = entities.size
        for key in ("attachment_type", "read_state", "flagged", "important", "has_attachments"):
            value = getattr(entities, key)
            if key in allowed and value is not None:
                slots[key] = value
        if entities.exclusions and "exclusions" in allowed:
            slots["exclusions"] = entities.exclusions
        if entities.ranking is not None and "ranking" in allowed:
            slots["ranking"] = entities.ranking
        if entities.thread_response is not None and "thread_response" in allowed:
            slots["thread_response"] = entities.thread_response
        if command.command_id == "mail.open_unread" and "read_state" in allowed:
            slots["read_state"] = "unread"
        if command.command_id == "mail.open_flagged" and "flagged" in allowed:
            slots["flagged"] = True
        if "theme" in allowed:
            theme = re.sub(
                r"\b(?:apply|change|colors?|mode|switch|theme|to|turn|on|use)\b",
                " ",
                normalize_text(text).normalized,
            )
            slots["theme"] = " ".join(theme.split()) or "default"
        selected = None
        if context:
            for key in command.context_requirements:
                if context.get(key):
                    selected = context[key]
                    break
            # Contact-context commands do not all declare a registry context requirement because
            # they also accept a typed person entered directly.  Resolve pronouns only when the
            # application supplies an actual selected contact; absence remains visibly gated.
            selected_contact = context.get("selected_contact")
            if (
                selected_contact
                and "person" in allowed
                and entities.person is None
                and (
                    re.search(r"\bthis contact\b", normalize_text(text).normalized)
                    or command.command_id
                    in {"compose.compose_to_contact", "contacts.compose_to_contact"}
                )
            ):
                slots["person"] = PersonEntity(
                    display=str(selected_contact),
                    email=(str(selected_contact) if "@" in str(selected_contact) else None),
                    source="context",
                    confidence=1.0,
                )
        if selected is not None:
            if "reference" in allowed:
                slots["reference"] = selected
            if "selection" in allowed:
                slots["selection"] = (selected,)
        return slots

    @staticmethod
    def _preview_lines(
        command: CommandSpec,
        slots: Mapping[str, object],
        entities: ParsedEntities,
        *,
        confidence: float,
        available_context: Sequence[str] = (),
    ) -> tuple[str, ...]:
        lines = [f"Action: {command.name}", f"Confidence: {confidence:.0%}"]
        person_slot = slots.get("person")
        sender_slot = slots.get("sender")
        recipient_slot = slots.get("recipient")
        person = person_slot if isinstance(person_slot, PersonEntity) else None
        sender = sender_slot if isinstance(sender_slot, PersonEntity) else None
        recipient = recipient_slot if isinstance(recipient_slot, PersonEntity) else None
        if sender is not None:
            address = f" <{sender.email}>" if sender.email else ""
            lines.append(f"Sender: {sender.display}{address}")
        elif recipient is not None:
            address = f" <{recipient.email}>" if recipient.email else ""
            lines.append(f"Recipient: {recipient.display}{address}")
        elif person is None:
            person = entities.person
        if person is not None:
            address = f" <{person.email}>" if person.email else ""
            lines.append(f"Person: {person.display}{address}")
        if entities.folder is not None:
            lines.append(f"Folder: {entities.folder.display}")
        date_range = slots.get("date_range")
        if isinstance(date_range, DateRange):
            start = date_range.start
            end = date_range.end
            if start is not None and end is not None:
                inclusive_end = end - timedelta(microseconds=1)
                if start.year == inclusive_end.year and start.month == inclusive_end.month:
                    lines.append(
                        f"Date: {start.strftime('%B')} {start.day}\u2013{inclusive_end.day}, "
                        f"{start.year}"
                    )
                else:
                    lines.append(
                        f"Date: {start.strftime('%B')} {start.day}, {start.year} \u2013 "
                        f"{inclusive_end.strftime('%B')} {inclusive_end.day}, "
                        f"{inclusive_end.year}"
                    )
            if start is not None:
                lines.append(f"From: {start.isoformat()}")
            if end is not None:
                lines.append(f"To (exclusive): {end.isoformat()}")
            lines.append(f"Time zone: {date_range.timezone}")
        when = slots.get("when")
        if isinstance(when, ResolvedDateTime):
            lines.extend(
                (
                    f"Date: {when.local.strftime('%B')} {when.local.day}, {when.local.year}",
                    f"Time: {when.local:%H:%M}",
                    f"Date/time: {when.local.isoformat()}",
                    f"Time zone: {when.timezone}",
                )
            )
            if command.command_id.startswith("scheduled."):
                lines.append("Action: Send")
                lines.append("Scheduling mechanism: persistent local scheduler")
        if entities.size is not None:
            if entities.size.minimum_bytes is not None:
                lines.append(f"Minimum size: {entities.size.minimum_bytes} bytes")
            if entities.size.maximum_bytes is not None:
                lines.append(f"Maximum size: {entities.size.maximum_bytes} bytes")
        if entities.read_state is not None:
            lines.append(f"Read state: {entities.read_state.title()}")
        if entities.attachment_type is not None:
            lines.append(f"Attachment type: {entities.attachment_type}")
        for exclusion in entities.exclusions:
            lines.append(f"Exclude {exclusion.field}: {exclusion.value}")
        if entities.ranking is not None:
            limit = f", top {entities.ranking.limit}" if entities.ranking.limit else ""
            lines.append(f"Order: {entities.ranking.field} {entities.ranking.direction}{limit}")
        if entities.thread_response is not None:
            lines.append(f"Response state: {entities.thread_response.state}")
        if command.command_id == "search.mail":
            folder = slots.get("folder")
            if isinstance(folder, FolderEntity):
                lines.append(f"Scope: {folder.display}")
            elif isinstance(folder, str) and folder.strip():
                lines.append(f"Scope: {folder.strip()}")
            else:
                lines.append("Scope: All Mail")
        selection = slots.get("selection")
        if isinstance(selection, (tuple, list)):
            lines.append(f"Selection: {len(selection)} item(s)")
        if command.confirmation_required or confidence < 0.82:
            lines.append("Confirmation: required before execution")
        implicit_context = {
            "contacts.compose_to_contact": ("selected_contact",),
            "contacts.schedule_meeting_with_contact": ("selected_contact",),
            "search.similar_message_search": ("selected_message",),
        }.get(command.command_id, ())
        context_requirements = tuple(
            dict.fromkeys((*command.context_requirements, *implicit_context))
        )
        missing_context = tuple(key for key in context_requirements if key not in available_context)
        if missing_context and not any(
            key in slots for key in ("person", "reference", "selection")
        ):
            lines.append("Context required: " + ", ".join(missing_context))
        elif context_requirements:
            resolved = tuple(key for key in context_requirements if key in available_context)
            if resolved:
                lines.append("Context: " + ", ".join(resolved))
        return tuple(lines)

    @staticmethod
    def _has_dangerous_language(text: str) -> bool:
        return any(re.search(rf"\b{re.escape(word)}\b", text) for word in DANGEROUS_WORDS)

    @staticmethod
    def _incomplete_candidates(text: str) -> tuple[str, ...]:
        patterns = {
            r"^(?:emails?|mail|messages?)\s+from$": (
                "search.mail",
                "attachments.search",
            ),
            r"^(?:schedule|send\s+later)$": (
                "scheduled.schedule_current",
                "scheduled.open",
            ),
            r"^largest$": (
                "storage.largest_messages",
                "storage.largest_attachments",
            ),
            r"^(?:move|copy)\s+(?:this|these)\s+to$": ("mail.move",),
            r"^(?:move|copy)\s+(?:this|these)$": ("mail.move",),
            r"^(?:email|compose\s+to)$": ("compose.new",),
        }
        for pattern, command_ids in patterns.items():
            if re.fullmatch(pattern, text):
                return command_ids
        return ()

    @staticmethod
    def _is_combined_mail_search(normalized: NormalizedInput) -> bool:
        tokens = set(normalized.tokens)
        mail_words = {"email", "emails", "mail", "message", "messages"}
        filter_words = {
            "after",
            "attachment",
            "attachments",
            "before",
            "between",
            "flagged",
            "from",
            "important",
            "last",
            "larger",
            "pdf",
            "pdfs",
            "to",
            "unread",
            "with",
            "without",
        }
        return bool(tokens & mail_words) and len(tokens & filter_words) >= 2


def resolve_person(text: str, *, candidates: Sequence[str] = ()) -> PersonEntity | None:
    email_match = EMAIL_RE.search(text)
    if email_match:
        return PersonEntity(email_match.group(1), email_match.group(1), source="email")
    normalized = normalize_text(text).normalized
    possessive_match = re.search(
        r"\b([\w@.+-]+(?:\s+[\w@.+-]+){0,3})['\N{RIGHT SINGLE QUOTATION MARK}]s\s+"
        r"(?:email|phone number|contact)\b",
        text,
        re.IGNORECASE,
    )
    # Negative sender clauses belong to ``exclusions`` and must never become a positive sender.
    positive_text = re.sub(r"\b(?:not from|except|excluding)\s+[\w@.+ -]+", " ", normalized)
    # Prefer structural forms where the person's position is unambiguous.  These cover contact,
    # calendar, monitoring, recipient, and fielded-search wording without treating an attachment
    # type, a subject phrase, or a response-state phrase as a person.
    structural_patterns = (
        r"\bbetween me and (.+)",
        r"\bwhere (.+?) is (?:cc(?:d)?|in (?:the )?recipients?)\b",
        r"\b(?:show|find) (.+?) (?:emails?|mail|messages?|contact)\b",
        r"\b(?:show me )?(?:stuff|things|messages?|emails?)\s+"
        r"((?!(?:i|we|they)\b).+?)\s+sent\b",
        r"\b(?:when is|find free time with|free time with) (.+?)(?: free)?$",
        r"\b(?:show )?(?:correspondence|conversation|meetings?|our emails) with (.+)",
        r"\b(?:meeting|schedule) (.+?)(?=\s+(?:today|tomorrow|next|monday|tuesday|"
        r"wednesday|thursday|friday|saturday|sunday|\d)\b|$)",
        r"\b(?:save|edit) (.+?)(?: as (?:a )?contact)?$",
        r"\bcontacts?\s+(.+)$",
        r"\bemail (?!this contact\b)(.+?)(?=\s+(?:with subject|about|cc)\b|$)",
        r"\bwrite (.+?) (?:an )?email\b",
        r"\b(?:new mail|new email|compose(?: a mail)?|send an email) to "
        r"(.+?)(?=\s+(?:with subject|about|cc)\b|$)",
        r"\b(?:what is |show )?(.+?) (?:email|phone number|contact)$",
        r"\bnotify me when (.+?) emails?\b",
        r"\bnotify me only for (.+)$",
        r"\b(?:watch|monitor) (?!inbox\b|mail\b|messages?\b|important\b)(.+)$",
        r"\bi sent (?!the last message\b|with no response\b)(.+)",
    )
    extracted = possessive_match.group(1) if possessive_match else ""
    extracted = re.sub(r"^(?:show|what is)\s+", "", extracted, flags=re.IGNORECASE)
    if not extracted:
        for pattern in structural_patterns:
            structural_match = re.search(pattern, positive_text)
            if structural_match is not None:
                extracted = structural_match.group(1)
                break
    if not extracted and extract_thread_response_state(positive_text) is None:
        relation_match = re.search(r"\b(?:sent by|got from|by|from|to|cc)\s+(.+)", positive_text)
        extracted = relation_match.group(1) if relation_match else ""
    extracted = re.split(
        r"\b(?:after|before|between|since|around|last|this|next|today|tomorrow|yesterday|"
        r"with|without|except|excluding|in|unread|read|flagged|important|larger|bigger|"
        r"over|under|smaller|less than|more than|below|above|at least|up to|"
        r"subject|body|attachment|attachments|pdf|pdfs|calendar|newsletter|"
        r"reply|response|older|newer|please|now|emails?|mail|messages?|contact|folder|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        extracted,
        maxsplit=1,
    )[0].strip()
    extracted = re.sub(r"^to\s+", "", extracted).strip()
    if not extracted and "student affairs" in positive_text:
        extracted = "student affairs"
    extracted = re.sub(r"\b(?:and|or)$", "", extracted).strip()
    content_or_aggregate_language = re.search(
        r"\b(?:about|mentioning|containing|exact phrase|subject|bodies?|attachments?|"
        r"word documents?|excel files?|images?|replied|response|most messages|email most|"
        r"emails me most|waiting|owe a reply|last message|nobody replied|"
        r"sent with no response)\b",
        normalized,
    )
    if (
        not extracted
        and content_or_aggregate_language is None
        and set(normalized.split())
        & {
            "email",
            "emails",
            "mail",
            "message",
            "messages",
            "pdf",
            "pdfs",
            "unread",
        }
    ):
        stop_words = {
            "anything",
            "email",
            "emails",
            "find",
            "from",
            "havent",
            "i",
            "mail",
            "me",
            "message",
            "messages",
            "not",
            "pdf",
            "pdfs",
            "read",
            "search",
            "sent",
            "show",
            "stuff",
            "that",
            "this",
            "unread",
            "with",
            "except",
            "newsletters",
            "oldest",
            "newest",
            "largest",
            "larger",
            "bigger",
            "smaller",
            "over",
            "under",
            "below",
            "above",
            "less",
            "more",
            "at",
            "least",
            "up",
            "between",
            "and",
            "than",
            "kb",
            "mb",
            "gb",
            "for",
            "my",
            "reply",
            "response",
            "waiting",
            "owe",
        }
        # Only words left after explicit search filters can be an implied person.  Without
        # removing these spans, e.g. "mail before June not in Deleted" manufactured the
        # sender "Before June In Deleted" and silently narrowed the search.
        person_text = normalized
        for exclusion in extract_exclusions(normalized):
            person_text = person_text.replace(exclusion.expression, " ", 1)
        date_filter = extract_date_range(person_text, timezone="UTC")
        if date_filter is not None:
            expression = re.escape(date_filter.expression)
            if date_filter.expression in {"week", "weekend", "month", "year", "semester"}:
                expression = rf"(?:last|this|next)\s+{expression}"
            person_text = re.sub(rf"\b{expression}\b", " ", person_text, count=1)
        remainder = [
            token for token in SIZE_RE.sub(" ", person_text).split() if token not in stop_words
        ]
        if remainder and all(
            token.isdigit() or token.replace(".", "", 1).isdigit() for token in remainder
        ):
            remainder = []
        if 0 < len(remainder) <= 4:
            extracted = " ".join(remainder)
    non_people = {
        "archive",
        "attachment",
        "attachments",
        "deleted",
        "drafts",
        "files",
        "images",
        "inbox",
        "items",
        "junk",
        "pdf",
        "pdfs",
        "sent",
        "trash",
        "newsletters",
        "oldest",
        "newest",
        "largest",
        "student",
        "contact",
        "contacts",
        "document",
        "documents",
        "excel",
        "exact",
        "most",
        "nobody",
        "phrase",
        "replied",
        "response",
        "word",
    }
    # "Student Affairs" is a valid organization despite "student" being a generic mail token.
    if (
        extracted.split()[:1]
        and (extracted.split()[0] in non_people or extracted.split()[0] in MONTHS)
        and not extracted.startswith("student affairs")
    ):
        extracted = ""
    if not extracted and candidates:
        candidate_phrase = re.sub(r"^(?:show|find|search|open)(?:\s+me)?\s+", "", normalized)
        # Local candidates may resolve a bare name, but must not manufacture a
        # sender from an entire search when its structural filters named nobody.
        if content_or_aggregate_language is not None or re.search(
            r"\b(?:emails?|mail|messages?|unread|read|flagged|important)\b", candidate_phrase
        ):
            return None
        extracted = candidate_phrase
    if not extracted:
        return None
    if not candidates:
        return PersonEntity(extracted.title())
    ranked = sorted(
        (
            (fuzz.WRatio(extracted, normalize_text(candidate).normalized), candidate)
            for candidate in candidates
        ),
        key=lambda item: (-item[0], item[1].casefold()),
    )
    if not ranked or ranked[0][0] < 65:
        return PersonEntity(extracted.title(), confidence=0.6)
    alternatives = tuple(candidate for score, candidate in ranked[1:] if ranked[0][0] - score <= 2)
    return PersonEntity(
        ranked[0][1],
        source="local_candidates",
        confidence=ranked[0][0] / 100.0,
        alternatives=alternatives,
    )


def _explicit_folder_navigation_reference(text: str) -> str | None:
    if len(text) > 1_024:
        return None
    match = re.fullmatch(
        r"(?i:open(?:\s+folder)?|(?:go\s+to|show)\s+folder)\s+"
        r"(?:id:)?(?P<reference>fld_[a-z2-7]{16})",
        text.strip(),
    )
    return match.group("reference") if match is not None else None


def resolve_folder(text: str, *, candidates: Sequence[str] = ()) -> FolderEntity | None:
    explicit_reference = _explicit_folder_navigation_reference(text)
    if explicit_reference is not None:
        return FolderEntity(
            explicit_reference, explicit_reference, source="local_reference", confidence=1.0
        )
    candidates = tuple(
        {candidate.casefold(): candidate for candidate in reversed(candidates)}.values()
    )
    normalized = normalize_text(text).normalized
    negative_folder = re.search(r"\b(?:not in|except|excluding)\s+([\w -]+)", normalized)
    alias_matches: list[tuple[str, str]] = []
    for alias, canonical in FOLDER_ALIASES.items():
        if not re.search(rf"\b{re.escape(alias)}\b", normalized):
            continue
        if negative_folder and re.search(rf"\b{re.escape(alias)}\b", negative_folder.group(1)):
            continue
        # "sent by" and response-state wording use *sent* as a verb, not a folder.  Archive is
        # also an attachment format, so only accept its folder meaning with a folder/navigation
        # cue (or when the whole input is the alias).
        if canonical == "sent" and re.search(
            r"\b(?:sent by|they sent|sent (?:with no response|the last message))\b|"
            r"\b(?!(?:i|we)\s)\w+\s+sent\b",
            normalized,
        ):
            continue
        if canonical == "archive" and not (
            normalized == alias
            or re.search(
                rf"\b(?:in|to|under|folder|open|show|storage)\s+(?:the )?{re.escape(alias)}\b",
                normalized,
            )
        ):
            continue
        alias_matches.append((alias, canonical))
    if alias_matches:
        alias, canonical = max(alias_matches, key=lambda item: len(item[0]))
        if candidates:
            explicit_paths = sorted(
                (
                    candidate
                    for candidate in candidates
                    if "/" in candidate and normalize_text(candidate).normalized in normalized
                ),
                key=lambda candidate: (-len(candidate), candidate.casefold()),
            )
            if explicit_paths:
                return FolderEntity(
                    explicit_paths[0],
                    explicit_paths[0],
                    source="folder_candidates",
                    confidence=1.0,
                    alternatives=tuple(explicit_paths[1:]),
                )
            exact_candidates = sorted(
                (
                    candidate
                    for candidate in candidates
                    if normalize_text(candidate).normalized == alias
                    or normalize_text(candidate.rsplit("/", 1)[-1]).normalized == alias
                ),
                key=str.casefold,
            )
            if exact_candidates:
                return FolderEntity(
                    exact_candidates[0],
                    exact_candidates[0],
                    source="folder_candidates",
                    confidence=1.0,
                    alternatives=tuple(exact_candidates[1:]),
                )
        return FolderEntity(alias.title(), canonical)
    move_target = bool(re.search(r"\b(?:move|put|file|copy)\b", normalized))
    folder_cues = r"folder(?: named| called)?|under|in" + (r"|to" if move_target else "")
    folder_phrase_match = re.search(rf"\b(?:{folder_cues})\s+([\w -]+)", normalized)
    if folder_phrase_match and folder_phrase_match.group(1).split()[0] in MONTHS:
        folder_phrase_match = None
    if folder_phrase_match and re.match(
        r"^(?:the )?(?:subject|body|recipients?|to|cc|bcc)\b",
        folder_phrase_match.group(1),
    ):
        folder_phrase_match = None
    folder_phrase = folder_phrase_match.group(1).strip() if folder_phrase_match else normalized
    folder_phrase = re.split(
        r"\b(?:after|before|between|since|last|this|next|today|with|without|except|not)\b",
        folder_phrase,
        maxsplit=1,
    )[0].strip()
    folder_phrase = re.sub(r"\s+folder$", "", folder_phrase).strip()
    if not candidates:
        match = folder_phrase_match
        if match:
            display = folder_phrase
            return FolderEntity(display.title(), display, source="text", confidence=0.75)
        return None
    ranked = sorted(
        (
            (
                max(
                    fuzz.WRatio(folder_phrase, normalize_text(candidate).normalized),
                    fuzz.partial_ratio(folder_phrase, normalize_text(candidate).normalized),
                ),
                candidate,
            )
            for candidate in candidates
        ),
        key=lambda item: (-item[0], item[1].casefold()),
    )
    if not ranked or ranked[0][0] < 70:
        return None
    alternatives = tuple(candidate for score, candidate in ranked[1:] if ranked[0][0] - score <= 2)
    return FolderEntity(
        ranked[0][1],
        ranked[0][1],
        source="folder_candidates",
        confidence=ranked[0][0] / 100.0,
        alternatives=alternatives,
    )


def extract_exclusions(text: str) -> tuple[Exclusion, ...]:
    """Extract explicit negative clauses without converting them into positive slots."""

    normalized = normalize_text(text).normalized
    exclusions: list[Exclusion] = []

    def add(field: str, value: str, expression: str) -> None:
        value = value.strip(" .,-")
        if value and (field, value.casefold()) not in {
            (item.field, item.value.casefold()) for item in exclusions
        }:
            exclusions.append(Exclusion(field, value, expression.strip()))

    not_from = re.search(
        r"\bnot from\s+(.+?)(?=\s+(?:after|before|between|since|with|without|in)\b|$)",
        normalized,
    )
    if not_from:
        add("sender", not_from.group(1), not_from.group(0))
    not_in = re.search(
        r"\bnot in\s+(.+?)(?=\s+(?:after|before|between|since|with|without)\b|$)",
        normalized,
    )
    if not_in:
        add("folder", not_in.group(1), not_in.group(0))
    for match in re.finditer(
        r"\b(?:except|excluding|but not)\s+(.+?)(?=\s+(?:after|before|between|since|in)\b|$)",
        normalized,
    ):
        value = match.group(1).strip()
        if re.search(r"\b(?:image|images|photo|photos)\b", value):
            add("attachment_type", "image", match.group(0))
        elif re.search(r"\bcalendar invitations?\b", value):
            add("item_type", "calendar_invitation", match.group(0))
        elif re.search(r"\bnewsletters?\b", value):
            add("text", "newsletter", match.group(0))
        else:
            add("text", value, match.group(0))
    without = re.search(r"\bwithout attachments?\b", normalized)
    if without:
        add("has_attachments", "true", without.group(0))
    return tuple(exclusions)


def extract_ranking(text: str) -> Ranking | None:
    """Extract stable ranking semantics independently from intent phrase matching."""

    normalized = normalize_text(text).normalized
    number = re.search(r"\b(?:top\s+)?(\d{1,4})\b", normalized)
    limit = min(int(number.group(1)), 1_000) if number else None
    if re.search(r"\b(?:largest|biggest)\b", normalized):
        if limit is None and re.search(r"\b(?:email|message|attachment)\b(?!s)", normalized):
            limit = 1
        return Ranking("size", "descending", limit, "largest")
    if re.search(r"\boldest\b", normalized):
        return Ranking("received_at", "ascending", limit, "oldest")
    if re.search(r"\bnewest\b", normalized):
        return Ranking("received_at", "descending", limit, "newest")
    if re.search(
        r"\b(?:most frequent sender|who emails me most|sender takes most space)\b", normalized
    ):
        return Ranking("sender_frequency", "descending", limit, "sender frequency")
    if re.search(r"\bmost common attachment types?\b", normalized):
        return Ranking("attachment_type_frequency", "descending", limit, "attachment types")
    if re.search(r"\bthreads? with most messages\b", normalized):
        return Ranking("thread_message_count", "descending", limit, "thread size")
    return None


def extract_thread_response_state(text: str) -> ThreadResponseState | None:
    """Recognize response ownership without claiming that Exchange exposes it server-side."""

    normalized = normalize_text(text).normalized
    if re.search(
        r"\b(?:waiting for my reply|havent replied|owe (?:a )?reply|people waiting on me|"
        r"they sent the last message)\b",
        normalized,
    ):
        return ThreadResponseState("waiting_on_me", "response ownership")
    if re.search(
        r"\b(?:waiting for them|sent with no response|nobody replied|i sent the last message)\b",
        normalized,
    ):
        return ThreadResponseState("waiting_on_them", "response ownership")
    return None


def extract_size_constraint(text: str) -> SizeConstraint | None:
    range_match = re.search(
        r"\bbetween\s+(\d+(?:\.\d+)?)\s*(bytes?|kb|kib|mb|mib|gb|gib)?\s+"
        r"and\s+(\d+(?:\.\d+)?)\s*(bytes?|kb|kib|mb|mib|gb|gib)\b",
        text,
        re.IGNORECASE,
    )
    if range_match:
        first_unit = range_match.group(2) or range_match.group(4)
        first = _size_bytes(float(range_match.group(1)), first_unit)
        range_second = _size_bytes(float(range_match.group(3)), range_match.group(4))
        lower, upper = sorted((first, range_second))
        return SizeConstraint(lower, upper, range_match.group(0))
    match = SIZE_RE.search(text)
    if not match:
        return None
    first = _size_bytes(float(match.group(2)), match.group(3))
    second = (
        _size_bytes(float(match.group(4)), match.group(5))
        if match.group(4) and match.group(5)
        else None
    )
    prefix = text[: match.start()].casefold()
    if match.group(1) or second is not None:
        lower, upper = sorted((first, second or first))
        return SizeConstraint(lower, upper, match.group(0))
    if re.search(r"(?:under|less than|smaller than|below|up to)\s*$", prefix):
        return SizeConstraint(None, first, match.group(0))
    if re.search(r"(?:over|more than|larger than|bigger than|above|at least)\s*$", prefix):
        return SizeConstraint(first, None, match.group(0))
    return SizeConstraint(first, first, match.group(0))


def extract_date_range(
    text: str,
    *,
    timezone: str,
    now: datetime | None = None,
) -> DateRange | None:
    zone = ZoneInfo(timezone)
    current = now or datetime.now(zone)
    current = current.replace(tzinfo=zone) if current.tzinfo is None else current.astimezone(zone)
    normalized = normalize_text(text).normalized

    def at_start(target: date) -> datetime:
        return datetime.combine(target, time.min, zone)

    def day_range(target: date, expression: str) -> DateRange:
        start = at_start(target)
        return DateRange(start, start + timedelta(days=1), timezone, expression)

    def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
        start = datetime(year, month, 1, tzinfo=zone)
        end = (
            datetime(year + 1, 1, 1, tzinfo=zone)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=zone)
        )
        return start, end

    def named_date(value: str) -> tuple[date, bool] | None:
        match = re.fullmatch(
            r"(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?(?:\s+(20\d{2}))?",
            value.strip(),
        )
        if match is None:
            return None
        month = MONTHS[match.group(1)]
        day = int(match.group(2) or 1)
        year = int(match.group(3) or current.year)
        if day > monthrange(year, month)[1]:
            return None
        return date(year, month, day), match.group(2) is not None

    # Time-of-day search windows are explicit local wall-clock ranges.  They are kept distinct
    # from scheduling instants and retain the configured time zone in every preview.
    time_windows = {
        "this morning": (0, 12, 0),
        "this afternoon": (12, 17, 0),
        "this evening": (17, 22, 0),
        "tonight": (18, 24, 0),
        "last night": (18, 6, -1),
    }
    for expression, (start_hour, end_hour, day_offset) in time_windows.items():
        if expression not in normalized:
            continue
        start_date = current.date() + timedelta(days=day_offset)
        start = datetime.combine(start_date, time(start_hour), zone)
        end_date = (
            start_date + timedelta(days=1)
            if end_hour >= 24 or end_hour <= start_hour
            else start_date
        )
        end = datetime.combine(end_date, time(end_hour % 24), zone)
        return DateRange(start, end, timezone, expression)

    if "yesterday" in normalized:
        return day_range(current.date() - timedelta(days=1), "yesterday")
    if "tomorrow" in normalized:
        return day_range(current.date() + timedelta(days=1), "tomorrow")
    if re.search(r"\btoday\b", normalized):
        return day_range(current.date(), "today")
    weekday_match = re.search(
        r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"(?:\s+(morning|afternoon|evening|night))?\b",
        normalized,
    )
    if weekday_match:
        weekday = WEEKDAYS[weekday_match.group(2)]
        day_offset = (weekday - current.weekday()) % 7
        if day_offset == 0 and weekday_match.group(1):
            day_offset = 7
        target = current.date() + timedelta(days=day_offset)
        period = weekday_match.group(3)
        if period:
            hours = {
                "morning": (6, 12),
                "afternoon": (12, 17),
                "evening": (17, 22),
                "night": (18, 24),
            }[period]
            start = datetime.combine(target, time(hours[0]), zone)
            end = datetime.combine(
                target + timedelta(days=1 if hours[1] >= 24 else 0),
                time(hours[1] % 24),
                zone,
            )
            return DateRange(start, end, timezone, weekday_match.group(0))
        return day_range(target, weekday_match.group(0))
    relative = re.search(r"\b(?:past|last)\s+(\d+)\s+(days?|weeks?|months?|years?)\b", normalized)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        days = amount * (
            365
            if unit.startswith("year")
            else 30
            if unit.startswith("month")
            else 7
            if unit.startswith("week")
            else 1
        )
        end = datetime.combine(current.date() + timedelta(days=1), time.min, zone)
        return DateRange(end - timedelta(days=days), end, timezone, relative.group(0))
    age = re.search(r"\b(older|newer) than\s+(\d+)\s+(days?|weeks?)\b", normalized)
    if age:
        amount = int(age.group(2)) * (7 if age.group(3).startswith("week") else 1)
        age_boundary = at_start(current.date() - timedelta(days=amount))
        return (
            DateRange(None, age_boundary, timezone, age.group(0))
            if age.group(1) == "older"
            else DateRange(age_boundary, None, timezone, age.group(0))
        )

    if any(value in normalized for value in ("last weekend", "this weekend", "next weekend")):
        monday = current.date() - timedelta(days=current.weekday())
        offset = -7 if "last weekend" in normalized else 7 if "next weekend" in normalized else 0
        start = at_start(monday + timedelta(days=5 + offset))
        return DateRange(start, start + timedelta(days=2), timezone, "weekend")
    if "last week" in normalized or "this week" in normalized or "next week" in normalized:
        week_start = current - timedelta(days=current.weekday())
        week_start = datetime.combine(week_start.date(), time.min, zone)
        offset = -7 if "last week" in normalized else 7 if "next week" in normalized else 0
        start = week_start + timedelta(days=offset)
        return DateRange(start, start + timedelta(days=7), timezone, "week")
    if any(value in normalized for value in ("last month", "this month", "next month")):
        month_offset = -1 if "last month" in normalized else 1 if "next month" in normalized else 0
        year = current.year
        month = current.month + month_offset
        if month == 0:
            year -= 1
            month = 12
        elif month == 13:
            year += 1
            month = 1
        start, end = month_bounds(year, month)
        return DateRange(start, end, timezone, "month")

    if any(value in normalized for value in ("last year", "this year", "next year")):
        year = current.year + (
            -1 if "last year" in normalized else 1 if "next year" in normalized else 0
        )
        return DateRange(
            datetime(year, 1, 1, tzinfo=zone),
            datetime(year + 1, 1, 1, tzinfo=zone),
            timezone,
            "year",
        )
    if "this semester" in normalized:
        start_month = 1 if current.month <= 6 else 7
        start = datetime(current.year, start_month, 1, tzinfo=zone)
        end_month = 7 if start_month == 1 else 1
        end_year = current.year if start_month == 1 else current.year + 1
        return DateRange(start, datetime(end_year, end_month, 1, tzinfo=zone), timezone, "semester")

    between = re.search(
        r"\bbetween\s+((?:[a-z]+)(?:\s+\d{1,2})?(?:\s+20\d{2})?)\s+and\s+"
        r"((?:[a-z]+)(?:\s+\d{1,2})?(?:\s+20\d{2})?)\b",
        normalized,
    )
    if between:
        first = named_date(between.group(1))
        second = named_date(between.group(2))
        if first is not None and second is not None:
            start_date, _first_has_day = first
            end_date, second_has_day = second
            if end_date < start_date:
                end_date = end_date.replace(year=end_date.year + 1)
            end = (
                at_start(end_date + timedelta(days=1))
                if second_has_day
                else month_bounds(end_date.year, end_date.month)[1]
            )
            return DateRange(at_start(start_date), end, timezone, between.group(0))

    date_boundary = re.search(
        r"\b(before|after|since|around|on)\s+"
        r"((?:january|february|march|april|may|june|july|august|september|october|"
        r"november|december)(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:\s+20\d{2})?|20\d{2})\b",
        normalized,
    )
    if date_boundary:
        operation, raw_target = date_boundary.groups()
        if re.fullmatch(r"20\d{2}", raw_target):
            year = int(raw_target)
            target = date(year, 1, 1)
            has_day = False
            target_end = date(year + 1, 1, 1)
        else:
            parsed_target = named_date(raw_target)
            if parsed_target is None:
                return None
            target, has_day = parsed_target
            target_end = (
                target + timedelta(days=1)
                if has_day
                else month_bounds(target.year, target.month)[1].date()
            )
        if operation == "before":
            return DateRange(None, at_start(target), timezone, date_boundary.group(0))
        if operation in {"after", "since"}:
            start_date = target if operation == "since" or has_day else target_end
            if operation == "after" and has_day:
                start_date = target + timedelta(days=1)
            return DateRange(at_start(start_date), None, timezone, date_boundary.group(0))
        if operation == "around":
            return DateRange(
                at_start(target - timedelta(days=1)),
                at_start(target + timedelta(days=2)),
                timezone,
                date_boundary.group(0),
            )
        return DateRange(at_start(target), at_start(target_end), timezone, date_boundary.group(0))

    match = ISO_DATE_RE.search(text)
    if match:
        try:
            start = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=zone
            )
        except ValueError:
            return None
        return DateRange(start, start + timedelta(days=1), timezone, match.group(0))
    return None


def extract_future_datetime(
    text: str,
    *,
    timezone: str,
    now: datetime | None = None,
) -> ResolvedDateTime | None:
    normalized = normalize_text(text).normalized
    if AMBIGUOUS_NUMERIC_DATE_RE.search(text):
        raise ValueError("Slash-separated dates are ambiguous; use YYYY-MM-DD.")
    for word, number in NUMBER_WORDS.items():
        normalized = re.sub(rf"\b{word}\b", str(number), normalized)
    normalized = re.sub(r"\b(?:an|a)\s+(hour|minute|day|week)\b", r"1 \1", normalized)
    normalized = re.sub(r"\bdelay(?: this)? by\b", "in", normalized)
    relative = re.search(
        r"\b(?:in|after|by)\s+\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)\b", normalized
    )
    expression = relative.group(0).replace("by ", "in ", 1) if relative else None
    zone = ZoneInfo(timezone)
    current = now or datetime.now(zone)
    current = current.replace(tzinfo=zone) if current.tzinfo is None else current.astimezone(zone)
    if expression is None and "after lunch" in normalized:
        target = datetime.combine(current.date(), time(13), zone)
        if target <= current:
            target = datetime.combine(current.date() + timedelta(days=1), time(13), zone)
        expression = target.replace(tzinfo=None).isoformat(sep=" ")
    if expression is None and "end of day" in normalized:
        target = datetime.combine(current.date(), time(17), zone)
        if target <= current:
            target = datetime.combine(current.date() + timedelta(days=1), time(17), zone)
        expression = target.replace(tzinfo=None).isoformat(sep=" ")
    if expression is None and "next weekday" in normalized:
        match = re.search(
            r"\bnext weekday(?:\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?",
            normalized,
        )
        clock = match.group(1) if match and match.group(1) else "9 AM"
        if re.fullmatch(r"\d{1,2}", clock):
            clock += ":00"
        target_date = current.date() + timedelta(days=1)
        while target_date.weekday() >= 5:
            target_date += timedelta(days=1)
        expression = f"{target_date.isoformat()} {clock}"
    if expression is None and "tonight" in normalized:
        target = datetime.combine(current.date(), time(20), zone)
        if target <= current:
            target = datetime.combine(current.date() + timedelta(days=1), time(20), zone)
        expression = target.replace(tzinfo=None).isoformat(sep=" ")
    if expression is None:
        day = re.search(
            r"\b(?:(?:next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
            r"(?:\s+(?:morning|afternoon|evening|night|(?:at\s+)?\d{1,2}"
            r"(?::\d{2})?\s*(?:am|pm)?(?:\s+to\s+\d{1,2}(?::\d{2})?"
            r"\s*(?:am|pm)?)?))?",
            normalized,
        )
        tomorrow = re.search(
            r"\b(?:today|tomorrow)(?:\s+(?:morning|afternoon|evening|night|(?:at\s+)?"
            r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+to\s+\d{1,2}(?::\d{2})?"
            r"\s*(?:am|pm)?)?))?",
            normalized,
        )
        named_day = tomorrow or day
        expression = named_day.group(0) if named_day is not None else None
    if expression is None and "next week" in normalized:
        expression = "next Monday 9 AM"
    if expression is None:
        named = re.search(
            r"\b(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\s+(\d{1,2})(?:st|nd|rd|th)?"
            r"(?:\s+(20\d{2}))?(?:\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?",
            normalized,
        )
        if named:
            month = MONTHS[named.group(1)]
            day_number = int(named.group(2))
            year = int(named.group(3) or current.year)
            if day_number <= monthrange(year, month)[1]:
                clock = named.group(4) or "9 AM"
                expression = f"{year:04d}-{month:02d}-{day_number:02d} {clock}"
    if expression is None:
        return None
    expression = re.sub(r"\bmorning\b", "8 AM", expression)
    expression = re.sub(r"\bafternoon\b", "2 PM", expression)
    expression = re.sub(r"\bevening\b", "6 PM", expression)
    expression = re.sub(r"\bnight\b", "8 PM", expression)
    expression = re.sub(
        r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+to\s+"
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
        r"\1 at \2",
        expression,
    )
    expression = re.sub(
        r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        r"\1 at \2",
        expression,
    )
    expression = re.sub(r"\bat\s+(\d{1,2})\b(?!\s*(?::|am|pm))", r"at \1:00", expression)
    try:
        return parse_datetime_expression(
            expression,
            timezone=timezone,
            now=now,
            future_required=True,
        )
    except Exception as exc:
        raise ValueError(f"Could not resolve future time: {expression!r}.") from exc


def _attachment_type(tokens: Iterable[str]) -> str | None:
    token_set = set(tokens)
    for kind, aliases in ATTACHMENT_TYPES.items():
        if token_set & aliases:
            return kind
    return None


def _size_bytes(value: float, unit: str) -> int:
    normalized = unit.casefold().rstrip("s")
    multiplier = {
        "byte": 1,
        "kb": 1_000,
        "kib": 1_024,
        "mb": 1_000_000,
        "mib": 1_048_576,
        "gb": 1_000_000_000,
        "gib": 1_073_741_824,
    }[normalized]
    return int(value * multiplier)
