"""Pure retry, circuit-breaker, dead-letter, and provider-isolation logic."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum

from gucmail.models import NotificationEvent
from gucmail.security import sanitize_terminal
from gucmail.services.notifications import (
    DeliveryResult,
    DeliveryState,
    NotificationProvider,
)


class RetryDisposition(StrEnum):
    COMPLETE = "complete"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 4
    maximum_age: timedelta = timedelta(hours=24)
    base_delay: timedelta = timedelta(seconds=5)
    maximum_delay: timedelta = timedelta(minutes=15)
    jitter_ratio: float = 0.2
    retryable_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_attempts <= 20:
            raise ValueError("maximum_attempts must be between 1 and 20.")
        if self.maximum_age <= timedelta(0) or self.maximum_age > timedelta(days=30):
            raise ValueError("maximum_age must be positive and no more than 30 days.")
        if self.base_delay <= timedelta(0) or self.maximum_delay < self.base_delay:
            raise ValueError("Retry delays must be positive and ordered.")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1.")


@dataclass(frozen=True, slots=True)
class RetryContext:
    attempt_number: int
    first_attempt_at: datetime
    now: datetime
    status_code: int | None = None
    retry_after: str | float | int | None = None
    retryable_failure: bool = True

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive.")
        if self.first_attempt_at.tzinfo is None or self.now.tzinfo is None:
            raise ValueError("Retry timestamps must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    disposition: RetryDisposition
    delay: timedelta | None = None
    next_attempt_at: datetime | None = None
    reason: str | None = None


def parse_retry_after(value: str | float | int | None, now: datetime) -> timedelta | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip()) if isinstance(value, str) else float(value)
        if not math.isfinite(seconds):
            return None
        return timedelta(seconds=max(0.0, seconds))
    except (OverflowError, ValueError):
        if not isinstance(value, str):
            return None
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(parsed - now, timedelta(0))
        except (TypeError, ValueError, OverflowError):
            return None


def schedule_retry(
    policy: RetryPolicy,
    context: RetryContext,
    *,
    random_fraction: float = 0.5,
) -> RetrySchedule:
    """Plan one bounded retry without sleeping or mutating persistence."""

    if not 0 <= random_fraction <= 1:
        raise ValueError("random_fraction must be between 0 and 1.")
    if not context.retryable_failure or (
        context.status_code is not None and context.status_code not in policy.retryable_statuses
    ):
        return RetrySchedule(RetryDisposition.DEAD_LETTER, reason="Failure is not retryable.")
    if context.attempt_number >= policy.maximum_attempts:
        return RetrySchedule(RetryDisposition.DEAD_LETTER, reason="Maximum attempts reached.")
    deadline = context.first_attempt_at + policy.maximum_age
    if context.now >= deadline:
        return RetrySchedule(RetryDisposition.DEAD_LETTER, reason="Maximum retry age reached.")
    server_delay = parse_retry_after(context.retry_after, context.now)
    if server_delay is None:
        exponent = max(0, context.attempt_number - 1)
        raw_seconds = min(
            policy.base_delay.total_seconds() * (2**exponent),
            policy.maximum_delay.total_seconds(),
        )
        spread = raw_seconds * policy.jitter_ratio
        seconds = raw_seconds - spread + ((spread * 2) * random_fraction)
        delay = timedelta(seconds=seconds)
    else:
        # ``Retry-After`` is a server minimum.  ``maximum_delay`` bounds our own
        # exponential policy, but must never shorten an explicit provider request.
        # The maximum-age check below still prevents an unbounded retry lifetime.
        delay = server_delay
    next_attempt = context.now + delay
    if next_attempt > deadline:
        return RetrySchedule(
            RetryDisposition.DEAD_LETTER,
            reason="Next attempt would exceed maximum retry age.",
        )
    return RetrySchedule(RetryDisposition.RETRY, delay, next_attempt, "Retry scheduled.")


class CircuitPhase(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 3
    recovery_timeout: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not 1 <= self.failure_threshold <= 100:
            raise ValueError("failure_threshold must be between 1 and 100.")
        if self.recovery_timeout <= timedelta(0) or self.recovery_timeout > timedelta(days=1):
            raise ValueError("recovery_timeout must be positive and no more than one day.")


@dataclass(frozen=True, slots=True)
class CircuitState:
    phase: CircuitPhase = CircuitPhase.CLOSED
    consecutive_failures: int = 0
    opened_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    retry_at: datetime | None = None


def circuit_decision(
    state: CircuitState,
    policy: CircuitBreakerPolicy,
    now: datetime,
) -> CircuitDecision:
    if now.tzinfo is None:
        raise ValueError("Circuit timestamps must be timezone-aware.")
    if state.phase is CircuitPhase.OPEN:
        if state.opened_at is None:
            raise ValueError("An open circuit must have an opened_at timestamp.")
        retry_at = state.opened_at + policy.recovery_timeout
        if now >= retry_at:
            return CircuitDecision(True, replace(state, phase=CircuitPhase.HALF_OPEN), retry_at)
        return CircuitDecision(False, state, retry_at)
    return CircuitDecision(True, state)


def record_circuit_result(
    state: CircuitState,
    policy: CircuitBreakerPolicy,
    now: datetime,
    *,
    succeeded: bool,
) -> CircuitState:
    if succeeded:
        return CircuitState()
    failures = state.consecutive_failures + 1
    if state.phase is CircuitPhase.HALF_OPEN or failures >= policy.failure_threshold:
        return CircuitState(CircuitPhase.OPEN, failures, now)
    return CircuitState(CircuitPhase.CLOSED, failures)


@dataclass(frozen=True, slots=True)
class DeadLetter:
    event_id: str
    provider: str
    failed_at: datetime
    attempts: int
    reason: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderDispatchOutcome:
    binding_id: str
    result: DeliveryResult
    circuit_state: CircuitState
    retry: RetrySchedule
    dead_letter: DeadLetter | None = None


class ProviderIsolationDispatcher:
    """Invoke all providers independently and return persistence-ready decisions.

    This coordinator intentionally does not sleep, retry, or write delivery state.  A
    watcher can atomically persist the returned schedule and remain responsive.
    """

    def __init__(
        self,
        providers: Sequence[NotificationProvider],
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_policy: CircuitBreakerPolicy | None = None,
    ) -> None:
        self.providers = tuple(providers)
        self.retry_policy = retry_policy or RetryPolicy()
        self.circuit_policy = circuit_policy or CircuitBreakerPolicy()

    @staticmethod
    def _provider_name(provider: NotificationProvider, index: int) -> str:
        try:
            name = sanitize_terminal(provider.name, max_length=200)
        except Exception:
            return f"invalid-provider-{index + 1}"
        return name or f"provider-{index + 1}"

    @classmethod
    def _binding_ids(cls, providers: Sequence[NotificationProvider]) -> tuple[str, ...]:
        counts: dict[str, int] = {}
        result: list[str] = []
        for index, provider in enumerate(providers):
            name = cls._provider_name(provider, index)
            counts[name] = counts.get(name, 0) + 1
            result.append(name if counts[name] == 1 else f"{name}#{counts[name]}")
        return tuple(result)

    def dispatch(
        self,
        event: NotificationEvent,
        *,
        now: datetime,
        attempt_numbers: Mapping[str, int] | None = None,
        first_attempts: Mapping[str, datetime] | None = None,
        circuit_states: Mapping[str, CircuitState] | None = None,
        random_fraction: float = 0.5,
    ) -> tuple[ProviderDispatchOutcome, ...]:
        attempts = attempt_numbers or {}
        starts = first_attempts or {}
        circuits = circuit_states or {}
        outcomes: list[ProviderDispatchOutcome] = []
        for binding_id, provider in zip(
            self._binding_ids(self.providers), self.providers, strict=True
        ):
            attempt = max(1, attempts.get(binding_id, 1))
            first_attempt = starts.get(binding_id, now)
            prior_circuit = circuits.get(binding_id, CircuitState())
            decision = circuit_decision(prior_circuit, self.circuit_policy, now)
            if not decision.allowed:
                delay = (decision.retry_at - now) if decision.retry_at is not None else None
                retry = RetrySchedule(
                    RetryDisposition.RETRY,
                    delay,
                    decision.retry_at,
                    "Provider circuit is open.",
                )
                outcomes.append(
                    ProviderDispatchOutcome(
                        binding_id,
                        DeliveryResult(
                            event.event_id,
                            binding_id,
                            DeliveryState.FAILED,
                            "Provider circuit is open; delivery was not attempted.",
                            attempts=0,
                        ),
                        decision.state,
                        retry,
                    )
                )
                continue
            try:
                result = provider.notify(event)
                if result.event_id != event.event_id or result.provider != binding_id:
                    result = replace(result, event_id=event.event_id, provider=binding_id)
            except Exception as exc:  # Provider isolation is the purpose of this boundary.
                result = DeliveryResult(
                    event.event_id,
                    binding_id,
                    DeliveryState.FAILED,
                    f"Provider failed ({type(exc).__name__}).",
                    attempts=attempt,
                )
            succeeded = result.succeeded
            updated_circuit = record_circuit_result(
                decision.state,
                self.circuit_policy,
                now,
                succeeded=succeeded,
            )
            if succeeded:
                retry = RetrySchedule(RetryDisposition.COMPLETE, reason="Delivery completed.")
                dead_letter = None
            else:
                retry = schedule_retry(
                    self.retry_policy,
                    RetryContext(
                        attempt,
                        first_attempt,
                        now,
                        status_code=result.status_code,
                        retry_after=result.retry_after,
                    ),
                    random_fraction=random_fraction,
                )
                dead_letter = (
                    DeadLetter(
                        event.event_id,
                        binding_id,
                        now,
                        attempt,
                        retry.reason or "Delivery failed.",
                        result.status_code,
                    )
                    if retry.disposition is RetryDisposition.DEAD_LETTER
                    else None
                )
            outcomes.append(
                ProviderDispatchOutcome(
                    binding_id,
                    result,
                    updated_circuit,
                    retry,
                    dead_letter,
                )
            )
        return tuple(outcomes)


__all__ = [
    "CircuitBreakerPolicy",
    "CircuitDecision",
    "CircuitPhase",
    "CircuitState",
    "DeadLetter",
    "ProviderDispatchOutcome",
    "ProviderIsolationDispatcher",
    "RetryContext",
    "RetryDisposition",
    "RetryPolicy",
    "RetrySchedule",
    "circuit_decision",
    "parse_retry_after",
    "record_circuit_result",
    "schedule_retry",
]
