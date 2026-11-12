"""Small, provider-neutral primitives shared by mailbox services."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from gucmail.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError, ConfirmationRequiredError
from gucmail.models import to_primitive

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A validated, deliberately bounded offset page request."""

    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ConfigurationError("Page limit must be an integer.")
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise ConfigurationError(f"Page limit must be between 1 and {MAX_PAGE_SIZE}.")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int):
            raise ConfigurationError("Page offset must be an integer.")
        if self.offset < 0:
            raise ConfigurationError("Page offset may not be negative.")


@dataclass(frozen=True, slots=True)
class ServicePage(Generic[T]):
    """A presentation-neutral page. ``has_more`` is conservative at exact boundaries."""

    items: tuple[T, ...]
    offset: int
    limit: int
    has_more: bool
    next_offset: int | None


def bounded_page(items: tuple[T, ...] | list[T], request: PageRequest) -> ServicePage[T]:
    """Defensively cap a gateway response even if a buggy gateway over-delivers."""

    bounded = tuple(items[: request.limit])
    has_more = len(items) > request.limit or len(bounded) == request.limit
    return ServicePage(
        items=bounded,
        offset=request.offset,
        limit=request.limit,
        has_more=has_more,
        next_offset=request.offset + len(bounded) if has_more else None,
    )


class MutationRisk(StrEnum):
    ROUTINE = "routine"
    HIGH_IMPACT = "high_impact"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class MutationPlan:
    """An immutable preview that can be safely confirmed and then executed."""

    operation: str
    targets: tuple[str, ...]
    summary: str
    risk: MutationRisk = MutationRisk.ROUTINE
    arguments: tuple[tuple[str, Any], ...] = ()
    requires_confirmation: bool = False
    confirmation_scope: str = ""

    @property
    def parameters(self) -> Mapping[str, Any]:
        return dict(self.arguments)


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    """A target's effect, kept separate from the failure of the overall batch."""

    reference: str
    status: str
    result_reference: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class MutationExecution:
    plan: MutationPlan
    dry_run: bool
    executed: bool
    results: tuple[Any, ...] = field(default_factory=tuple)
    operation_id: str | None = None
    outcomes: tuple[MutationOutcome, ...] = ()


def make_mutation_plan(
    *,
    operation: str,
    targets: tuple[str, ...],
    summary: str,
    risk: MutationRisk = MutationRisk.ROUTINE,
    arguments: Mapping[str, Any] | None = None,
    requires_confirmation: bool = False,
) -> MutationPlan:
    if not operation or any(character.isspace() for character in operation):
        raise ConfigurationError("Mutation operation must be a non-empty machine name.")
    normalized_arguments = tuple(sorted((arguments or {}).items()))
    payload = {
        "operation": operation,
        "targets": targets,
        "risk": risk.value,
        "arguments": to_primitive(dict(normalized_arguments)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return MutationPlan(
        operation=operation,
        targets=targets,
        summary=summary,
        risk=risk,
        arguments=normalized_arguments,
        requires_confirmation=requires_confirmation,
        confirmation_scope=f"{operation}:{digest}",
    )


def require_confirmation(plan: MutationPlan, supplied_scope: str | None) -> None:
    if not plan.requires_confirmation:
        return
    if supplied_scope is None or not hmac.compare_digest(supplied_scope, plan.confirmation_scope):
        raise ConfirmationRequiredError(
            "This mailbox change requires confirmation for the exact previewed scope."
        )


def validate_reference(reference: str, *, label: str = "reference") -> str:
    value = reference.strip()
    if not value:
        raise ConfigurationError(f"{label.capitalize()} may not be empty.")
    if len(value) > 4096:
        raise ConfigurationError(f"{label.capitalize()} is unexpectedly long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{label.capitalize()} contains control characters.")
    return value


def normalize_targets(references: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not references:
        raise ConfigurationError("At least one message reference is required.")
    if len(references) > MAX_PAGE_SIZE:
        raise ConfigurationError(f"A single mutation may target at most {MAX_PAGE_SIZE} messages.")
    unique: list[str] = []
    seen: set[str] = set()
    for raw in references:
        reference = validate_reference(raw, label="message reference")
        if reference not in seen:
            seen.add(reference)
            unique.append(reference)
    return tuple(unique)
