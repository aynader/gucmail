"""Deterministic notification-rule AST, evaluation, simulation, and interchange.

Rules operate on :mod:`gucmail.notification_events` values and never invoke providers.  A
tri-state evaluator preserves the difference between "did not match" and "the watcher did
not fetch enough information", which is essential for safe body and attachment matching.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import time
from enum import StrEnum
from pathlib import PurePath
from threading import RLock
from typing import Any, Protocol, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gucmail.notification_events import (
    EventDisclosurePolicy,
    NewMailEvent,
    event_payload,
)

_MAX_PATTERN_LENGTH = 512
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_RULES = 1_000
_MAX_NESTING = 32
_MAX_GROUP_CHILDREN = 100


class MatchState(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvaluationTrace:
    """Explain one AST node without copying message content into diagnostics."""

    node: str
    outcome: MatchState
    detail: str
    children: tuple[EvaluationTrace, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class RuleEvaluationContext:
    """Evaluation features that are intentionally explicit and fail closed by default."""

    body_matching_enabled: bool = False
    default_timezone: str = "UTC"
    internal_domains: frozenset[str] = field(default_factory=lambda: frozenset({"guc.edu.eg"}))

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown time zone: {self.default_timezone}") from exc
        normalized = frozenset(value.casefold().lstrip("@.") for value in self.internal_domains)
        if any(not value for value in normalized):
            raise ValueError("internal_domains cannot contain empty values")
        object.__setattr__(self, "internal_domains", normalized)


class Condition(Protocol):
    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace: ...


class TextField(StrEnum):
    FOLDER = "folder"
    SENDER = "sender"
    SENDER_DOMAIN = "sender_domain"
    RECIPIENT = "recipient"
    CC = "cc"
    SUBJECT = "subject"
    BODY_PREVIEW = "body_preview"
    ATTACHMENT_FILENAME = "attachment_filename"
    ATTACHMENT_EXTENSION = "attachment_extension"


class TextOperator(StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    GLOB = "glob"
    REGEX = "regex"


def _address_values(addresses: Sequence[object]) -> tuple[str, ...]:
    values: list[str] = []
    for item in addresses:
        email = getattr(item, "email", None)
        name = getattr(item, "name", None)
        if isinstance(email, str):
            values.append(email)
        if isinstance(name, str) and name:
            values.append(name)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class TextCondition:
    field: TextField
    operator: TextOperator
    values: tuple[str, ...]
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.values or any(not value for value in self.values):
            raise ValueError("text conditions require one or more non-empty values")
        if any(len(value) > _MAX_PATTERN_LENGTH for value in self.values):
            raise ValueError(f"text patterns cannot exceed {_MAX_PATTERN_LENGTH} characters")
        if self.operator is TextOperator.REGEX:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            for value in self.values:
                try:
                    re.compile(value, flags=flags)
                except re.error as exc:
                    raise ValueError(f"invalid regular expression: {exc}") from exc

    def _candidates(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> tuple[str, ...] | None:
        if self.field is TextField.FOLDER:
            return (event.folder,) if event.folder is not None else None
        if self.field is TextField.SENDER:
            return _address_values((event.sender,)) if event.sender is not None else None
        if self.field is TextField.SENDER_DOMAIN:
            if event.sender is None or "@" not in event.sender.email:
                return None
            return (event.sender.email.rsplit("@", 1)[1].casefold(),)
        if self.field is TextField.RECIPIENT:
            return _address_values(event.recipients) if event.recipients is not None else None
        if self.field is TextField.CC:
            return _address_values(event.cc) if event.cc is not None else None
        if self.field is TextField.SUBJECT:
            return (event.subject,) if event.subject is not None else None
        if self.field is TextField.BODY_PREVIEW:
            if not context.body_matching_enabled:
                return None
            return (event.body_preview,) if event.body_preview is not None else None
        if self.field is TextField.ATTACHMENT_FILENAME:
            return event.attachment_filenames
        if self.field is TextField.ATTACHMENT_EXTENSION:
            if event.attachment_filenames is None:
                return None
            return tuple(PurePath(name).suffix.casefold() for name in event.attachment_filenames)
        raise AssertionError(f"unsupported text field: {self.field}")

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        candidates = self._candidates(event, context)
        label = f"text:{self.field.value}:{self.operator.value}"
        if candidates is None:
            reason = "field unavailable"
            if self.field is TextField.BODY_PREVIEW and not context.body_matching_enabled:
                reason = "body matching is disabled"
            return EvaluationTrace(label, MatchState.UNKNOWN, reason)

        def normalize(value: str) -> str:
            return value if self.case_sensitive else value.casefold()

        normalized_candidates = tuple(normalize(value) for value in candidates)
        normalized_values = tuple(normalize(value) for value in self.values)
        matched: bool
        if self.operator is TextOperator.EXACT:
            matched = any(
                candidate == expected
                for candidate in normalized_candidates
                for expected in normalized_values
            )
        elif self.operator is TextOperator.CONTAINS:
            matched = any(
                expected in candidate
                for candidate in normalized_candidates
                for expected in normalized_values
            )
        elif self.operator is TextOperator.NOT_CONTAINS:
            matched = all(
                expected not in candidate
                for candidate in normalized_candidates
                for expected in normalized_values
            )
        elif self.operator is TextOperator.GLOB:
            matched = any(
                fnmatch.fnmatchcase(candidate, expected)
                for candidate in normalized_candidates
                for expected in normalized_values
            )
        else:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            matched = any(
                re.search(pattern, candidate, flags=flags) is not None
                for candidate in candidates
                for pattern in self.values
            )
        state = MatchState.MATCH if matched else MatchState.NO_MATCH
        return EvaluationTrace(label, state, f"evaluated {len(candidates)} value(s)")


class BooleanField(StrEnum):
    IS_READ = "is_read"
    HAS_ATTACHMENTS = "has_attachments"
    SENDER_IN_CONTACTS = "sender_in_contacts"
    SENDER_EXTERNAL = "sender_external"


@dataclass(frozen=True, slots=True)
class BooleanCondition:
    field: BooleanField
    expected: bool

    def _value(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> bool | None:
        if self.field is BooleanField.IS_READ:
            return event.is_read
        if self.field is BooleanField.HAS_ATTACHMENTS:
            return event.has_attachments
        if self.field is BooleanField.SENDER_IN_CONTACTS:
            return event.sender_in_contacts
        if event.sender is None or "@" not in event.sender.email:
            return None
        domain = event.sender.email.rsplit("@", 1)[1].casefold().rstrip(".")
        internal = any(
            domain == expected or domain.endswith(f".{expected}")
            for expected in context.internal_domains
        )
        return not internal

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        label = f"boolean:{self.field.value}"
        actual = self._value(event, context)
        if actual is None:
            return EvaluationTrace(label, MatchState.UNKNOWN, "field unavailable")
        matched = actual is self.expected
        return EvaluationTrace(
            label,
            MatchState.MATCH if matched else MatchState.NO_MATCH,
            f"actual={str(actual).lower()}, expected={str(self.expected).lower()}",
        )


class ChoiceField(StrEnum):
    IMPORTANCE = "importance"
    CATEGORY = "category"


@dataclass(frozen=True, slots=True)
class ChoiceCondition:
    field: ChoiceField
    values: tuple[str, ...]
    negate: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(value.casefold().strip() for value in self.values))
        if not normalized or any(not value for value in normalized):
            raise ValueError("choice conditions require non-empty values")
        object.__setattr__(self, "values", normalized)

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        del context
        label = f"choice:{self.field.value}"
        candidates: tuple[str, ...]
        if self.field is ChoiceField.IMPORTANCE:
            if event.importance is None:
                return EvaluationTrace(label, MatchState.UNKNOWN, "field unavailable")
            candidates = (event.importance.value.casefold(),)
        else:
            if event.categories is None:
                return EvaluationTrace(label, MatchState.UNKNOWN, "field unavailable")
            candidates = tuple(value.casefold() for value in event.categories)
        matched = any(value in self.values for value in candidates)
        if self.negate:
            matched = not matched
        return EvaluationTrace(
            label,
            MatchState.MATCH if matched else MatchState.NO_MATCH,
            f"evaluated {len(candidates)} value(s)",
        )


@dataclass(frozen=True, slots=True)
class SizeCondition:
    minimum_bytes: int | None = None
    maximum_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_bytes is None and self.maximum_bytes is None:
            raise ValueError("a size condition needs a minimum or maximum")
        if self.minimum_bytes is not None and self.minimum_bytes < 0:
            raise ValueError("minimum_bytes cannot be negative")
        if self.maximum_bytes is not None and self.maximum_bytes < 0:
            raise ValueError("maximum_bytes cannot be negative")
        if (
            self.minimum_bytes is not None
            and self.maximum_bytes is not None
            and self.minimum_bytes > self.maximum_bytes
        ):
            raise ValueError("minimum_bytes cannot exceed maximum_bytes")

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        del context
        if event.size_bytes is None:
            return EvaluationTrace("size", MatchState.UNKNOWN, "field unavailable")
        matched = (self.minimum_bytes is None or event.size_bytes >= self.minimum_bytes) and (
            self.maximum_bytes is None or event.size_bytes <= self.maximum_bytes
        )
        return EvaluationTrace(
            "size",
            MatchState.MATCH if matched else MatchState.NO_MATCH,
            "evaluated message size",
        )


def _time_in_window(value: time, start: time, end: time) -> bool:
    if start < end:
        return start <= value < end
    return value >= start or value < end


@dataclass(frozen=True, slots=True)
class TimeWindowCondition:
    start: time
    end: time
    timezone: str | None = None

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("time window start and end cannot be equal")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown time zone: {self.timezone}") from exc

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        zone_name = self.timezone or context.default_timezone
        local = event.occurred_at.astimezone(ZoneInfo(zone_name))
        current = local.timetz().replace(tzinfo=None)
        matched = _time_in_window(current, self.start, self.end)
        return EvaluationTrace(
            "time_window",
            MatchState.MATCH if matched else MatchState.NO_MATCH,
            f"evaluated in {zone_name}",
        )


@dataclass(frozen=True, slots=True)
class WeekdayCondition:
    weekdays: frozenset[int]
    timezone: str | None = None

    def __post_init__(self) -> None:
        if not self.weekdays or any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("weekdays must contain integers from 0 (Monday) to 6 (Sunday)")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown time zone: {self.timezone}") from exc

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        zone_name = self.timezone or context.default_timezone
        weekday = event.occurred_at.astimezone(ZoneInfo(zone_name)).weekday()
        matched = weekday in self.weekdays
        return EvaluationTrace(
            "weekday",
            MatchState.MATCH if matched else MatchState.NO_MATCH,
            f"weekday={weekday} in {zone_name}",
        )


@dataclass(frozen=True, slots=True)
class AllCondition:
    children: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ValueError("AND groups cannot be empty")
        if len(self.children) > _MAX_GROUP_CHILDREN:
            raise ValueError("AND group contains too many children")

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        traces = tuple(child.evaluate(event, context) for child in self.children)
        if any(trace.outcome is MatchState.NO_MATCH for trace in traces):
            outcome = MatchState.NO_MATCH
        elif all(trace.outcome is MatchState.MATCH for trace in traces):
            outcome = MatchState.MATCH
        else:
            outcome = MatchState.UNKNOWN
        return EvaluationTrace("and", outcome, f"evaluated {len(traces)} child nodes", traces)


@dataclass(frozen=True, slots=True)
class AnyCondition:
    children: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ValueError("OR groups cannot be empty")
        if len(self.children) > _MAX_GROUP_CHILDREN:
            raise ValueError("OR group contains too many children")

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        traces = tuple(child.evaluate(event, context) for child in self.children)
        if any(trace.outcome is MatchState.MATCH for trace in traces):
            outcome = MatchState.MATCH
        elif all(trace.outcome is MatchState.NO_MATCH for trace in traces):
            outcome = MatchState.NO_MATCH
        else:
            outcome = MatchState.UNKNOWN
        return EvaluationTrace("or", outcome, f"evaluated {len(traces)} child nodes", traces)


@dataclass(frozen=True, slots=True)
class NotCondition:
    child: Condition

    def evaluate(
        self,
        event: NewMailEvent,
        context: RuleEvaluationContext,
    ) -> EvaluationTrace:
        trace = self.child.evaluate(event, context)
        outcome = {
            MatchState.MATCH: MatchState.NO_MATCH,
            MatchState.NO_MATCH: MatchState.MATCH,
            MatchState.UNKNOWN: MatchState.UNKNOWN,
        }[trace.outcome]
        return EvaluationTrace("not", outcome, "logical negation", (trace,))


ConditionType: TypeAlias = (
    TextCondition
    | BooleanCondition
    | ChoiceCondition
    | SizeCondition
    | TimeWindowCondition
    | WeekdayCondition
    | AllCondition
    | AnyCondition
    | NotCondition
)


class RuleActionKind(StrEnum):
    ROUTE_IMMEDIATE = "route_immediate"
    ROUTE_DIGEST = "route_digest"
    SUPPRESS = "suppress"
    QUEUE_UNTIL_ALLOWED = "queue_until_allowed"
    PLAY_SOUND = "play_sound"
    SET_URGENCY = "set_urgency"


@dataclass(frozen=True, slots=True)
class RuleAction:
    kind: RuleActionKind
    provider_id: str | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        route_actions = {
            RuleActionKind.ROUTE_IMMEDIATE,
            RuleActionKind.ROUTE_DIGEST,
        }
        if self.kind in route_actions and not (self.provider_id and self.provider_id.strip()):
            raise ValueError(f"{self.kind.value} requires provider_id")
        if self.kind not in route_actions and self.provider_id is not None:
            raise ValueError(f"{self.kind.value} does not accept provider_id")
        if self.value is not None and len(self.value) > 500:
            raise ValueError("rule action values cannot exceed 500 characters")


@dataclass(frozen=True, slots=True)
class NotificationRule:
    rule_id: str
    name: str
    condition: Condition
    actions: tuple[RuleAction, ...]
    enabled: bool = False
    stop_processing: bool = False
    priority: int = 0
    order: int = 0
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or len(self.rule_id) > 200:
            raise ValueError("rule_id must be between 1 and 200 characters")
        if not self.name.strip() or len(self.name) > 300:
            raise ValueError("rule name must be between 1 and 300 characters")
        if not self.actions:
            raise ValueError("notification rules require at least one action")
        if not -1_000 <= self.priority <= 1_000:
            raise ValueError("rule priority must be between -1000 and 1000")
        if self.order < 0:
            raise ValueError("rule order cannot be negative")
        if self.description is not None and len(self.description) > 2_000:
            raise ValueError("rule description cannot exceed 2000 characters")


@dataclass(frozen=True, slots=True)
class RuleTrace:
    rule_id: str
    name: str
    enabled: bool
    matched: bool
    evaluation: EvaluationTrace | None
    detail: str


@dataclass(frozen=True, slots=True)
class RuleDecision:
    matched_rule_ids: tuple[str, ...]
    immediate_providers: tuple[str, ...]
    digest_providers: tuple[str, ...]
    actions: tuple[RuleAction, ...]
    suppressed: bool
    stopped_by_rule_id: str | None
    traces: tuple[RuleTrace, ...]


class NotificationRuleEngine:
    """Evaluate enabled rules by descending priority, then explicit order."""

    def __init__(
        self,
        rules: Sequence[NotificationRule],
        *,
        context: RuleEvaluationContext | None = None,
    ) -> None:
        identifiers = [rule.rule_id.casefold() for rule in rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("rule identifiers must be unique")
        self._rules = tuple(
            sorted(rules, key=lambda rule: (-rule.priority, rule.order, rule.rule_id.casefold()))
        )
        self._context = context or RuleEvaluationContext()

    @property
    def rules(self) -> tuple[NotificationRule, ...]:
        return self._rules

    def evaluate(self, event: NewMailEvent) -> RuleDecision:
        matched_ids: list[str] = []
        immediate: list[str] = []
        digest: list[str] = []
        actions: list[RuleAction] = []
        traces: list[RuleTrace] = []
        suppressed = False
        stopped_by: str | None = None
        for rule in self._rules:
            if not rule.enabled:
                traces.append(
                    RuleTrace(rule.rule_id, rule.name, False, False, None, "rule disabled")
                )
                continue
            evaluation = rule.condition.evaluate(event, self._context)
            matched = evaluation.outcome is MatchState.MATCH
            traces.append(
                RuleTrace(
                    rule.rule_id,
                    rule.name,
                    True,
                    matched,
                    evaluation,
                    "matched" if matched else evaluation.outcome.value,
                )
            )
            if not matched:
                continue
            matched_ids.append(rule.rule_id)
            for action in rule.actions:
                if action not in actions:
                    actions.append(action)
                if action.kind is RuleActionKind.ROUTE_IMMEDIATE:
                    provider = cast(str, action.provider_id)
                    if provider not in immediate:
                        immediate.append(provider)
                elif action.kind is RuleActionKind.ROUTE_DIGEST:
                    provider = cast(str, action.provider_id)
                    if provider not in digest:
                        digest.append(provider)
                elif action.kind is RuleActionKind.SUPPRESS:
                    suppressed = True
            if rule.stop_processing:
                stopped_by = rule.rule_id
                break
        if suppressed:
            immediate.clear()
            digest.clear()
        return RuleDecision(
            tuple(matched_ids),
            tuple(immediate),
            tuple(digest),
            tuple(actions),
            suppressed,
            stopped_by,
            tuple(traces),
        )


@dataclass(frozen=True, slots=True)
class SimulationExample:
    event_id: str
    matched_rule_ids: tuple[str, ...]
    immediate_providers: tuple[str, ...]
    digest_providers: tuple[str, ...]
    exposed_fields: Mapping[str, tuple[str, ...]]
    traces: tuple[RuleTrace, ...]
    queued_providers: tuple[str, ...] = ()
    suppressed: bool = False
    skipped_providers: Mapping[str, str] = field(default_factory=dict)
    message_reference: str | None = None
    subject: str | None = None
    folder: str | None = None


class SimulationRoutingPlan(Protocol):
    """Read-only routing boundary, avoiding a dependency on profile policy types."""

    @property
    def immediate_providers(self) -> tuple[str, ...]: ...

    @property
    def digest_providers(self) -> tuple[str, ...]: ...

    @property
    def queued_providers(self) -> tuple[str, ...]: ...

    @property
    def suppressed(self) -> bool: ...

    @property
    def provider_payloads(self) -> Mapping[str, Mapping[str, Any]]: ...

    @property
    def skipped_providers(self) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class RuleSimulationResult:
    total_events: int
    matching_events: int
    notification_events: int
    suppressed_events: int
    provider_counts: Mapping[str, int]
    provider_exposed_fields: Mapping[str, tuple[str, ...]]
    examples: tuple[SimulationExample, ...]


class NotificationRuleSimulator:
    """Dry-run an engine; providers are never called from this class."""

    def __init__(
        self,
        engine: NotificationRuleEngine,
        *,
        provider_policies: Mapping[str, EventDisclosurePolicy] | None = None,
        max_examples: int = 10,
        route: Callable[[NewMailEvent, RuleDecision], SimulationRoutingPlan] | None = None,
    ) -> None:
        if not 0 <= max_examples <= 100:
            raise ValueError("max_examples must be between 0 and 100")
        self._engine = engine
        self._policies = dict(provider_policies or {})
        self._max_examples = max_examples
        self._route = route

    def simulate(self, events: Sequence[NewMailEvent]) -> RuleSimulationResult:
        matching = 0
        notification_events = 0
        suppressed = 0
        provider_counts: dict[str, int] = {}
        provider_fields: dict[str, set[str]] = {}
        examples: list[SimulationExample] = []
        for event in events:
            decision = self._engine.evaluate(event)
            if decision.matched_rule_ids:
                matching += 1
            routing = self._route(event, decision) if self._route is not None else None
            is_suppressed = routing.suppressed if routing is not None else decision.suppressed
            immediate = (
                routing.immediate_providers if routing is not None else decision.immediate_providers
            )
            digest = routing.digest_providers if routing is not None else decision.digest_providers
            queued = routing.queued_providers if routing is not None else ()
            if is_suppressed:
                suppressed += 1
                immediate = digest = queued = ()
            providers = tuple(dict.fromkeys((*immediate, *digest, *queued)))
            if providers:
                notification_events += 1
            exposed: dict[str, tuple[str, ...]] = {}
            for provider in providers:
                if routing is not None:
                    fields = tuple(sorted(routing.provider_payloads[provider]))
                else:
                    policy = self._policies.get(provider, EventDisclosurePolicy())
                    fields = tuple(sorted(event_payload(event, policy)))
                exposed[provider] = fields
                provider_fields.setdefault(provider, set()).update(fields)
                provider_counts[provider] = provider_counts.get(provider, 0) + 1
            if decision.matched_rule_ids and len(examples) < self._max_examples:
                examples.append(
                    SimulationExample(
                        event.event_id,
                        decision.matched_rule_ids,
                        immediate,
                        digest,
                        exposed,
                        decision.traces,
                        queued,
                        is_suppressed,
                        routing.skipped_providers if routing is not None else {},
                        event.message_reference,
                        event.subject,
                        event.folder,
                    )
                )
        return RuleSimulationResult(
            len(events),
            matching,
            notification_events,
            suppressed,
            dict(sorted(provider_counts.items())),
            {
                provider: tuple(sorted(fields))
                for provider, fields in sorted(provider_fields.items())
            },
            tuple(examples),
        )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _strict_keys(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of text values")
    return tuple(cast(list[str], value))


def condition_to_data(condition: Condition) -> dict[str, Any]:
    if isinstance(condition, TextCondition):
        return {
            "type": "text",
            "field": condition.field.value,
            "operator": condition.operator.value,
            "values": list(condition.values),
            "case_sensitive": condition.case_sensitive,
        }
    if isinstance(condition, BooleanCondition):
        return {
            "type": "boolean",
            "field": condition.field.value,
            "expected": condition.expected,
        }
    if isinstance(condition, ChoiceCondition):
        return {
            "type": "choice",
            "field": condition.field.value,
            "values": list(condition.values),
            "negate": condition.negate,
        }
    if isinstance(condition, SizeCondition):
        return {
            "type": "size",
            "minimum_bytes": condition.minimum_bytes,
            "maximum_bytes": condition.maximum_bytes,
        }
    if isinstance(condition, TimeWindowCondition):
        return {
            "type": "time",
            "start": condition.start.isoformat(),
            "end": condition.end.isoformat(),
            "timezone": condition.timezone,
        }
    if isinstance(condition, WeekdayCondition):
        return {
            "type": "weekday",
            "weekdays": sorted(condition.weekdays),
            "timezone": condition.timezone,
        }
    if isinstance(condition, AllCondition):
        return {"type": "and", "children": [condition_to_data(item) for item in condition.children]}
    if isinstance(condition, AnyCondition):
        return {"type": "or", "children": [condition_to_data(item) for item in condition.children]}
    if isinstance(condition, NotCondition):
        return {"type": "not", "child": condition_to_data(condition.child)}
    raise TypeError(f"unsupported condition type: {type(condition).__name__}")


def condition_from_data(value: object, *, _depth: int = 0) -> ConditionType:
    if _depth > _MAX_NESTING:
        raise ValueError("condition nesting is too deep")
    data = _mapping(value, "condition")
    kind = _string(data.get("type"), "condition.type")
    if kind == "text":
        _strict_keys(
            data,
            required=frozenset({"type", "field", "operator", "values"}),
            optional=frozenset({"case_sensitive"}),
        )
        case_sensitive = data.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise ValueError("condition.case_sensitive must be true or false")
        return TextCondition(
            TextField(_string(data["field"], "condition.field")),
            TextOperator(_string(data["operator"], "condition.operator")),
            _string_tuple(data["values"], "condition.values"),
            case_sensitive,
        )
    if kind == "boolean":
        _strict_keys(data, required=frozenset({"type", "field", "expected"}))
        expected = data["expected"]
        if not isinstance(expected, bool):
            raise ValueError("condition.expected must be true or false")
        return BooleanCondition(BooleanField(_string(data["field"], "condition.field")), expected)
    if kind == "choice":
        _strict_keys(
            data,
            required=frozenset({"type", "field", "values"}),
            optional=frozenset({"negate"}),
        )
        negate = data.get("negate", False)
        if not isinstance(negate, bool):
            raise ValueError("condition.negate must be true or false")
        return ChoiceCondition(
            ChoiceField(_string(data["field"], "condition.field")),
            _string_tuple(data["values"], "condition.values"),
            negate,
        )
    if kind == "size":
        _strict_keys(
            data,
            required=frozenset({"type"}),
            optional=frozenset({"minimum_bytes", "maximum_bytes"}),
        )
        return SizeCondition(
            _optional_integer(data.get("minimum_bytes"), "condition.minimum_bytes"),
            _optional_integer(data.get("maximum_bytes"), "condition.maximum_bytes"),
        )
    if kind == "time":
        _strict_keys(
            data,
            required=frozenset({"type", "start", "end"}),
            optional=frozenset({"timezone"}),
        )
        timezone = data.get("timezone")
        if timezone is not None and not isinstance(timezone, str):
            raise ValueError("condition.timezone must be text or null")
        try:
            start = time.fromisoformat(_string(data["start"], "condition.start"))
            end = time.fromisoformat(_string(data["end"], "condition.end"))
        except ValueError as exc:
            raise ValueError("condition times must be ISO local times") from exc
        return TimeWindowCondition(start, end, timezone)
    if kind == "weekday":
        _strict_keys(
            data,
            required=frozenset({"type", "weekdays"}),
            optional=frozenset({"timezone"}),
        )
        raw_days = data["weekdays"]
        if not isinstance(raw_days, list):
            raise ValueError("condition.weekdays must be a list")
        days = frozenset(_integer(day, "condition.weekday") for day in raw_days)
        timezone = data.get("timezone")
        if timezone is not None and not isinstance(timezone, str):
            raise ValueError("condition.timezone must be text or null")
        return WeekdayCondition(days, timezone)
    if kind in {"and", "or"}:
        _strict_keys(data, required=frozenset({"type", "children"}))
        raw_children = data["children"]
        if not isinstance(raw_children, list):
            raise ValueError("condition.children must be a list")
        if len(raw_children) > _MAX_GROUP_CHILDREN:
            raise ValueError("condition group contains too many children")
        children = tuple(condition_from_data(child, _depth=_depth + 1) for child in raw_children)
        return AllCondition(children) if kind == "and" else AnyCondition(children)
    if kind == "not":
        _strict_keys(data, required=frozenset({"type", "child"}))
        return NotCondition(condition_from_data(data["child"], _depth=_depth + 1))
    raise ValueError(f"unknown condition type: {kind}")


def action_to_data(action: RuleAction) -> dict[str, Any]:
    return {
        "kind": action.kind.value,
        "provider_id": action.provider_id,
        "value": action.value,
    }


def action_from_data(value: object) -> RuleAction:
    data = _mapping(value, "action")
    _strict_keys(
        data,
        required=frozenset({"kind"}),
        optional=frozenset({"provider_id", "value"}),
    )
    provider = data.get("provider_id")
    action_value = data.get("value")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("action.provider_id must be text or null")
    if action_value is not None and not isinstance(action_value, str):
        raise ValueError("action.value must be text or null")
    return RuleAction(
        RuleActionKind(_string(data["kind"], "action.kind")),
        provider,
        action_value,
    )


def rule_to_data(rule: NotificationRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "name": rule.name,
        "description": rule.description,
        "enabled": rule.enabled,
        "stop_processing": rule.stop_processing,
        "priority": rule.priority,
        "order": rule.order,
        "condition": condition_to_data(rule.condition),
        "actions": [action_to_data(action) for action in rule.actions],
    }


def rule_from_data(value: object) -> NotificationRule:
    data = _mapping(value, "rule")
    _strict_keys(
        data,
        required=frozenset(
            {
                "rule_id",
                "name",
                "enabled",
                "stop_processing",
                "priority",
                "order",
                "condition",
                "actions",
            }
        ),
        optional=frozenset({"description"}),
    )
    enabled = data["enabled"]
    stop_processing = data["stop_processing"]
    if not isinstance(enabled, bool) or not isinstance(stop_processing, bool):
        raise ValueError("rule enabled and stop_processing fields must be boolean")
    raw_actions = data["actions"]
    if not isinstance(raw_actions, list):
        raise ValueError("rule.actions must be a list")
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("rule.description must be text or null")
    return NotificationRule(
        _string(data["rule_id"], "rule.rule_id"),
        _string(data["name"], "rule.name"),
        condition_from_data(data["condition"]),
        tuple(action_from_data(action) for action in raw_actions),
        enabled,
        stop_processing,
        _integer(data["priority"], "rule.priority"),
        _integer(data["order"], "rule.order"),
        description,
    )


def export_rules(rules: Sequence[NotificationRule]) -> str:
    identifiers = [rule.rule_id.casefold() for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("cannot export duplicate rule identifiers")
    document = {
        "schema": "gucmail.notification-rules",
        "version": 1,
        "rules": [rule_to_data(rule) for rule in rules],
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def import_rules(document: str | bytes) -> tuple[NotificationRule, ...]:
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ValueError("notification-rule document is too large")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("notification-rule document is not valid UTF-8 JSON") from exc
    data = _mapping(decoded, "document")
    _strict_keys(data, required=frozenset({"schema", "version", "rules"}))
    if data["schema"] != "gucmail.notification-rules" or data["version"] != 1:
        raise ValueError("unsupported notification-rule document schema or version")
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise ValueError("document.rules must be a list")
    if len(raw_rules) > _MAX_RULES:
        raise ValueError("notification-rule document contains too many rules")
    rules = tuple(rule_from_data(value) for value in raw_rules)
    identifiers = [rule.rule_id.casefold() for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("notification-rule document contains duplicate identifiers")
    return rules


class NotificationRuleRepository(Protocol):
    """Persistence boundary implemented by StateStore integration."""

    def list_rules(self) -> tuple[NotificationRule, ...]: ...

    def get_rule(self, rule_id: str) -> NotificationRule | None: ...

    def save_rule(self, rule: NotificationRule) -> None: ...

    def delete_rule(self, rule_id: str) -> bool: ...

    def set_enabled(self, rule_id: str, *, enabled: bool) -> NotificationRule: ...

    def duplicate_rule(self, rule_id: str, *, new_id: str, new_name: str) -> NotificationRule: ...

    def reorder(self, rule_ids: Sequence[str]) -> tuple[NotificationRule, ...]: ...


class InMemoryNotificationRuleRepository:
    """Thread-safe reference repository used by offline flows and tests."""

    def __init__(self, rules: Sequence[NotificationRule] = ()) -> None:
        self._lock = RLock()
        self._rules: dict[str, NotificationRule] = {}
        for rule in rules:
            self.save_rule(rule)

    def list_rules(self) -> tuple[NotificationRule, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._rules.values(),
                    key=lambda rule: (-rule.priority, rule.order, rule.rule_id.casefold()),
                )
            )

    def get_rule(self, rule_id: str) -> NotificationRule | None:
        with self._lock:
            return self._rules.get(rule_id.casefold())

    def save_rule(self, rule: NotificationRule) -> None:
        with self._lock:
            self._rules[rule.rule_id.casefold()] = rule

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            return self._rules.pop(rule_id.casefold(), None) is not None

    def _required(self, rule_id: str) -> NotificationRule:
        rule = self.get_rule(rule_id)
        if rule is None:
            raise KeyError(rule_id)
        return rule

    def set_enabled(self, rule_id: str, *, enabled: bool) -> NotificationRule:
        with self._lock:
            updated = replace(self._required(rule_id), enabled=enabled)
            self.save_rule(updated)
            return updated

    def duplicate_rule(self, rule_id: str, *, new_id: str, new_name: str) -> NotificationRule:
        with self._lock:
            if new_id.casefold() in self._rules:
                raise ValueError(f"notification rule already exists: {new_id}")
            duplicate = replace(
                self._required(rule_id),
                rule_id=new_id,
                name=new_name,
                enabled=False,
                order=len(self._rules),
            )
            self.save_rule(duplicate)
            return duplicate

    def reorder(self, rule_ids: Sequence[str]) -> tuple[NotificationRule, ...]:
        with self._lock:
            normalized = [value.casefold() for value in rule_ids]
            if len(normalized) != len(set(normalized)):
                raise ValueError("reorder list contains duplicate rule identifiers")
            if set(normalized) != set(self._rules):
                raise ValueError("reorder list must contain every rule exactly once")
            self._rules = {
                identifier: replace(self._rules[identifier], order=order)
                for order, identifier in enumerate(normalized)
            }
            return self.list_rules()


__all__ = [
    "AllCondition",
    "AnyCondition",
    "BooleanCondition",
    "BooleanField",
    "ChoiceCondition",
    "ChoiceField",
    "Condition",
    "ConditionType",
    "EvaluationTrace",
    "InMemoryNotificationRuleRepository",
    "MatchState",
    "NotCondition",
    "NotificationRule",
    "NotificationRuleEngine",
    "NotificationRuleRepository",
    "NotificationRuleSimulator",
    "RuleAction",
    "RuleActionKind",
    "RuleDecision",
    "RuleEvaluationContext",
    "RuleSimulationResult",
    "SimulationExample",
    "SizeCondition",
    "TextCondition",
    "TextField",
    "TextOperator",
    "TimeWindowCondition",
    "WeekdayCondition",
    "action_from_data",
    "action_to_data",
    "condition_from_data",
    "condition_to_data",
    "export_rules",
    "import_rules",
    "rule_from_data",
    "rule_to_data",
]
