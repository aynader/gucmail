"""Quiet hours, digests, routing matrices, and named monitoring profiles."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gucmail.models import Importance
from gucmail.notification_events import (
    EventDisclosurePolicy,
    NewMailEvent,
    PrivacyMode,
    event_payload,
)
from gucmail.notification_rules import (
    Condition,
    MatchState,
    NotificationRule,
    RuleDecision,
    RuleEvaluationContext,
    condition_from_data,
    condition_to_data,
    rule_from_data,
    rule_to_data,
)

_MAX_PROFILE_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_PROFILES = 100
_SAFE_MINIMUM_POLL_SECONDS = 60.0


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown time zone: {name}") from exc


def _valid_local_instant(day: date, local_time: time, zone: ZoneInfo) -> datetime:
    """Resolve ambiguous/nonexistent wall times deterministically.

    Ambiguous fall-back times use the later occurrence so queued notifications are not
    released early.  Nonexistent spring-forward times normalize to the first valid instant
    after the requested wall time.
    """

    naive = datetime.combine(day, local_time)
    candidates: list[tuple[datetime, datetime]] = []
    for fold in (0, 1):
        attached = naive.replace(tzinfo=zone, fold=fold)
        utc_value = attached.astimezone(UTC)
        normalized = utc_value.astimezone(zone)
        candidates.append((utc_value, normalized))
    exact = [
        (utc_value, normalized)
        for utc_value, normalized in candidates
        if normalized.replace(tzinfo=None) == naive
    ]
    if exact:
        return max(utc_value for utc_value, _normalized in exact)
    after = [
        (utc_value, normalized)
        for utc_value, normalized in candidates
        if normalized.replace(tzinfo=None) > naive
    ]
    if after:
        return min(after, key=lambda item: item[1].replace(tzinfo=None))[0]
    return max(utc_value for utc_value, _normalized in candidates)


class QuietHoursBehavior(StrEnum):
    SUPPRESS = "suppress"
    QUEUE = "queue"
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class QuietHoursWindow:
    window_id: str
    start: time
    end: time
    timezone: str
    weekdays: frozenset[int] = field(default_factory=lambda: frozenset(range(7)))
    behavior: QuietHoursBehavior = QuietHoursBehavior.SUPPRESS
    bypass_importance: frozenset[Importance] = field(default_factory=frozenset)
    bypass_senders: frozenset[str] = field(default_factory=frozenset)
    bypass_sender_domains: frozenset[str] = field(default_factory=frozenset)
    bypass_rule_ids: frozenset[str] = field(default_factory=frozenset)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.window_id.strip() or len(self.window_id) > 200:
            raise ValueError("quiet-hours window_id must be between 1 and 200 characters")
        if self.start == self.end:
            raise ValueError("quiet-hours start and end cannot be equal")
        _zone(self.timezone)
        if not self.weekdays or any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("quiet-hours weekdays must be between 0 and 6")
        senders = frozenset(value.casefold().strip() for value in self.bypass_senders)
        domains = frozenset(
            value.casefold().strip().lstrip("@.") for value in self.bypass_sender_domains
        )
        rules = frozenset(value.casefold().strip() for value in self.bypass_rule_ids)
        if any(not value for value in (*senders, *domains, *rules)):
            raise ValueError("quiet-hours bypass values cannot be empty")
        object.__setattr__(self, "bypass_senders", senders)
        object.__setattr__(self, "bypass_sender_domains", domains)
        object.__setattr__(self, "bypass_rule_ids", rules)

    def active_start_day(self, instant: datetime) -> date | None:
        if not self.enabled:
            return None
        _require_aware(instant, "instant")
        local = instant.astimezone(_zone(self.timezone))
        current = local.timetz().replace(tzinfo=None)
        if self.start < self.end:
            if self.start <= current < self.end and local.weekday() in self.weekdays:
                return local.date()
            return None
        if current >= self.start and local.weekday() in self.weekdays:
            return local.date()
        prior = local.date() - timedelta(days=1)
        if current < self.end and prior.weekday() in self.weekdays:
            return prior
        return None

    def release_at(self, instant: datetime) -> datetime | None:
        start_day = self.active_start_day(instant)
        if start_day is None:
            return None
        end_day = start_day if self.start < self.end else start_day + timedelta(days=1)
        return _valid_local_instant(end_day, self.end, _zone(self.timezone))

    def bypass_reason(
        self,
        event: NewMailEvent,
        matched_rule_ids: Sequence[str],
    ) -> str | None:
        if event.importance is not None and event.importance in self.bypass_importance:
            return f"importance:{event.importance.value}"
        if event.sender is not None:
            address = event.sender.email.casefold()
            if address in self.bypass_senders:
                return "selected_sender"
            if "@" in address:
                domain = address.rsplit("@", 1)[1].rstrip(".")
                if any(
                    domain == allowed or domain.endswith(f".{allowed}")
                    for allowed in self.bypass_sender_domains
                ):
                    return "selected_sender_domain"
        normalized_rules = {value.casefold() for value in matched_rule_ids}
        if normalized_rules & self.bypass_rule_ids:
            return "selected_rule"
        return None


@dataclass(frozen=True, slots=True)
class QuietHoursDecision:
    active: bool
    behavior: QuietHoursBehavior | None = None
    window_id: str | None = None
    release_at: datetime | None = None
    bypassed: bool = False
    bypass_reason: str | None = None


@dataclass(frozen=True, slots=True)
class QuietHoursSchedule:
    windows: tuple[QuietHoursWindow, ...] = ()

    def __post_init__(self) -> None:
        identifiers = [window.window_id.casefold() for window in self.windows]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("quiet-hours window identifiers must be unique")

    def evaluate(
        self,
        event: NewMailEvent,
        *,
        matched_rule_ids: Sequence[str] = (),
        at: datetime | None = None,
    ) -> QuietHoursDecision:
        instant = at or event.detected_at
        _require_aware(instant, "at")
        for window in self.windows:
            release_at = window.release_at(instant)
            if release_at is None:
                continue
            reason = window.bypass_reason(event, matched_rule_ids)
            if reason is not None:
                return QuietHoursDecision(
                    True,
                    window.behavior,
                    window.window_id,
                    release_at,
                    True,
                    reason,
                )
            return QuietHoursDecision(
                True,
                window.behavior,
                window.window_id,
                release_at,
            )
        return QuietHoursDecision(False)


class DigestCadence(StrEnum):
    EVERY_30_MINUTES = "every_30_minutes"
    HOURLY = "hourly"
    MORNING = "morning"
    EVENING = "evening"
    DAILY = "daily"
    CUSTOM_INTERVAL = "custom_interval"
    CUSTOM_DAILY = "custom_daily"


@dataclass(frozen=True, slots=True)
class DigestWindow:
    window_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start, "digest window start")
        _require_aware(self.end, "digest window end")
        if self.end <= self.start:
            raise ValueError("digest window end must follow its start")


@dataclass(frozen=True, slots=True)
class DigestSchedule:
    schedule_id: str
    name: str
    cadence: DigestCadence
    timezone: str
    delivery_time: time | None = None
    interval_minutes: int | None = None
    maximum_events: int = 500
    maximum_window: timedelta = timedelta(days=2)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.schedule_id.strip() or len(self.schedule_id) > 200:
            raise ValueError("digest schedule_id must be between 1 and 200 characters")
        if not self.name.strip() or len(self.name) > 300:
            raise ValueError("digest schedule name must be between 1 and 300 characters")
        _zone(self.timezone)
        interval = self.effective_interval_minutes
        if interval is not None and not 5 <= interval <= 1_440:
            raise ValueError("digest intervals must be between 5 and 1440 minutes")
        if self.is_daily and self.effective_delivery_time is None:
            raise ValueError("daily digests require a delivery time")
        if not 1 <= self.maximum_events <= 10_000:
            raise ValueError("digest maximum_events must be between 1 and 10000")
        if not timedelta(minutes=5) <= self.maximum_window <= timedelta(days=7):
            raise ValueError("digest maximum_window must be between 5 minutes and 7 days")

    @property
    def effective_interval_minutes(self) -> int | None:
        if self.cadence is DigestCadence.EVERY_30_MINUTES:
            return 30
        if self.cadence is DigestCadence.HOURLY:
            return 60
        if self.cadence is DigestCadence.CUSTOM_INTERVAL:
            return self.interval_minutes
        return None

    @property
    def is_daily(self) -> bool:
        return self.effective_interval_minutes is None

    @property
    def effective_delivery_time(self) -> time | None:
        if self.delivery_time is not None:
            return self.delivery_time
        if self.cadence is DigestCadence.MORNING:
            return time(8, 0)
        if self.cadence is DigestCadence.EVENING:
            return time(18, 0)
        if self.cadence is DigestCadence.DAILY:
            return time(9, 0)
        return None

    def window_for(self, instant: datetime) -> DigestWindow:
        _require_aware(instant, "digest instant")
        current = instant.astimezone(UTC)
        interval = self.effective_interval_minutes
        if interval is not None:
            interval_seconds = interval * 60
            timestamp = current.timestamp()
            start_timestamp = math.floor(timestamp / interval_seconds) * interval_seconds
            start = datetime.fromtimestamp(start_timestamp, tz=UTC)
            end = start + timedelta(seconds=interval_seconds)
        else:
            zone = _zone(self.timezone)
            local = current.astimezone(zone)
            boundary_time = cast(time, self.effective_delivery_time)
            today_boundary = _valid_local_instant(local.date(), boundary_time, zone)
            if current >= today_boundary:
                start = today_boundary
                end = _valid_local_instant(local.date() + timedelta(days=1), boundary_time, zone)
            else:
                start = _valid_local_instant(local.date() - timedelta(days=1), boundary_time, zone)
                end = today_boundary
        if end - start > self.maximum_window:
            raise ValueError("computed digest window exceeds maximum_window")
        material = (
            f"{self.schedule_id}\x00{start.astimezone(UTC).isoformat()}\x00"
            f"{end.astimezone(UTC).isoformat()}"
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return DigestWindow(f"digest-{digest}", start, end)


class DigestAddState(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CAPACITY_REACHED = "capacity_reached"


@dataclass(frozen=True, slots=True)
class DigestAddResult:
    state: DigestAddState
    window_id: str
    event_count: int


@dataclass(frozen=True, slots=True)
class DigestSummary:
    total_messages: int
    sender_groups: tuple[tuple[str, int], ...]
    attachment_messages: int
    unread_messages: int
    important_messages: int


@dataclass(frozen=True, slots=True)
class DigestBatch:
    profile_id: str
    schedule_id: str
    window: DigestWindow
    events: tuple[NewMailEvent, ...]

    @property
    def batch_id(self) -> str:
        return f"{self.profile_id}:{self.schedule_id}:{self.window.window_id}"

    def summary(
        self,
        policy: EventDisclosurePolicy | None = None,
        *,
        maximum_sender_groups: int = 10,
    ) -> DigestSummary:
        if not 0 <= maximum_sender_groups <= 100:
            raise ValueError("maximum_sender_groups must be between 0 and 100")
        disclosure = policy or EventDisclosurePolicy()
        senders: dict[str, int] = {}
        for event in self.events:
            payload = event_payload(event, disclosure)
            sender = payload.get("sender")
            if isinstance(sender, Mapping):
                name = sender.get("name") or sender.get("address")
                if isinstance(name, str) and name:
                    senders[name] = senders.get(name, 0) + 1
        groups = tuple(
            sorted(senders.items(), key=lambda item: (-item[1], item[0].casefold()))[
                :maximum_sender_groups
            ]
        )
        return DigestSummary(
            len(self.events),
            groups,
            sum(event.has_attachments is True for event in self.events),
            sum(event.is_read is False for event in self.events),
            sum(event.importance is Importance.HIGH for event in self.events),
        )


class DigestRepository(Protocol):
    """Persistence boundary for digest constituents and delivery acknowledgements."""

    def add(
        self,
        profile_id: str,
        schedule: DigestSchedule,
        window: DigestWindow,
        event: NewMailEvent,
    ) -> DigestAddResult: ...

    def due(self, profile_id: str, schedule_id: str, at: datetime) -> tuple[DigestBatch, ...]: ...

    def acknowledge(self, batch_id: str) -> bool: ...


class InMemoryDigestRepository:
    """Atomic in-memory reference implementation with constituent deduplication."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._batches: dict[str, tuple[DigestWindow, dict[str, NewMailEvent]]] = {}
        self._delivered: set[tuple[str, str, str]] = set()

    @staticmethod
    def _batch_key(profile_id: str, schedule_id: str, window_id: str) -> str:
        return f"{profile_id}:{schedule_id}:{window_id}"

    def add(
        self,
        profile_id: str,
        schedule: DigestSchedule,
        window: DigestWindow,
        event: NewMailEvent,
    ) -> DigestAddResult:
        if not profile_id.strip():
            raise ValueError("profile_id is required")
        delivery_key = (profile_id.casefold(), schedule.schedule_id.casefold(), event.event_id)
        batch_key = self._batch_key(profile_id, schedule.schedule_id, window.window_id)
        with self._lock:
            if delivery_key in self._delivered:
                return DigestAddResult(DigestAddState.DUPLICATE, window.window_id, 0)
            for key, (_existing_window, events) in self._batches.items():
                same_schedule = key.startswith(f"{profile_id}:{schedule.schedule_id}:")
                if same_schedule and event.event_id in events:
                    return DigestAddResult(
                        DigestAddState.DUPLICATE,
                        key.rsplit(":", 1)[-1],
                        len(events),
                    )
            stored_window, events = self._batches.setdefault(batch_key, (window, {}))
            if stored_window != window:
                raise ValueError("digest window identifier collision")
            if len(events) >= schedule.maximum_events:
                return DigestAddResult(
                    DigestAddState.CAPACITY_REACHED, window.window_id, len(events)
                )
            events[event.event_id] = event
            return DigestAddResult(DigestAddState.ACCEPTED, window.window_id, len(events))

    def due(self, profile_id: str, schedule_id: str, at: datetime) -> tuple[DigestBatch, ...]:
        _require_aware(at, "digest due time")
        instant = at.astimezone(UTC)
        prefix = f"{profile_id}:{schedule_id}:"
        with self._lock:
            batches = [
                DigestBatch(
                    profile_id,
                    schedule_id,
                    window,
                    tuple(
                        sorted(
                            events.values(),
                            key=lambda item: (
                                item.detected_at.astimezone(UTC),
                                item.event_id,
                            ),
                        )
                    ),
                )
                for key, (window, events) in self._batches.items()
                if key.startswith(prefix) and window.end.astimezone(UTC) <= instant and events
            ]
        return tuple(sorted(batches, key=lambda batch: (batch.window.end, batch.batch_id)))

    def acknowledge(self, batch_id: str) -> bool:
        with self._lock:
            stored = self._batches.pop(batch_id, None)
            if stored is None:
                return False
            _window, events = stored
            parts = batch_id.split(":", 2)
            if len(parts) != 3:
                raise ValueError("invalid digest batch identifier")
            profile_id, schedule_id, _window_id = parts
            self._delivered.update(
                (profile_id.casefold(), schedule_id.casefold(), event_id) for event_id in events
            )
            return True


class DigestBatcher:
    def __init__(
        self,
        repository: DigestRepository,
        *,
        profile_id: str,
        schedule: DigestSchedule,
    ) -> None:
        if not profile_id.strip():
            raise ValueError("profile_id is required")
        self._repository = repository
        self._profile_id = profile_id
        self._schedule = schedule

    def queue(self, event: NewMailEvent) -> DigestAddResult:
        if not self._schedule.enabled:
            raise ValueError("cannot queue into a disabled digest schedule")
        window = self._schedule.window_for(event.detected_at)
        return self._repository.add(self._profile_id, self._schedule, window, event)

    def due(self, at: datetime) -> tuple[DigestBatch, ...]:
        return self._repository.due(self._profile_id, self._schedule.schedule_id, at)

    def acknowledge(self, batch_id: str) -> bool:
        return self._repository.acknowledge(batch_id)


class RouteMode(StrEnum):
    OFF = "off"
    IMMEDIATE = "immediate"
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    provider_id: str
    enabled: bool = False
    privacy: EventDisclosurePolicy = field(default_factory=EventDisclosurePolicy)
    filter_condition: Condition | None = None
    digest_schedule_id: str | None = None
    maximum_notifications_per_burst: int = 5

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or len(self.provider_id) > 200:
            raise ValueError("provider_id must be between 1 and 200 characters")
        if not 1 <= self.maximum_notifications_per_burst <= 100:
            raise ValueError("maximum_notifications_per_burst must be between 1 and 100")
        if self.digest_schedule_id is not None and not self.digest_schedule_id.strip():
            raise ValueError("digest_schedule_id cannot be empty")


@dataclass(frozen=True, slots=True)
class RoutingCell:
    rule_id: str
    provider_id: str
    mode: RouteMode
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.provider_id.strip():
            raise ValueError("routing cells require rule_id and provider_id")


@dataclass(frozen=True, slots=True)
class RoutingMatrix:
    provider_ids: tuple[str, ...] = ()
    cells: tuple[RoutingCell, ...] = ()

    def __post_init__(self) -> None:
        normalized = [value.casefold() for value in self.provider_ids]
        if len(normalized) != len(set(normalized)):
            raise ValueError("routing-matrix provider identifiers must be unique")
        keys = [(cell.rule_id.casefold(), cell.provider_id.casefold()) for cell in self.cells]
        if len(keys) != len(set(keys)):
            raise ValueError("routing matrix contains duplicate cells")
        providers = set(normalized)
        unknown = sorted(
            {
                cell.provider_id
                for cell in self.cells
                if cell.provider_id.casefold() not in providers
            }
        )
        if unknown:
            raise ValueError(f"routing cells reference unknown providers: {', '.join(unknown)}")

    def mode_for(self, rule_id: str, provider_id: str) -> RouteMode:
        for cell in self.cells:
            if (
                cell.rule_id.casefold() == rule_id.casefold()
                and cell.provider_id.casefold() == provider_id.casefold()
                and cell.enabled
            ):
                return cell.mode
        return RouteMode.OFF

    def resolve(self, matched_rule_ids: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        matched = {value.casefold() for value in matched_rule_ids}
        immediate: list[str] = []
        digest: list[str] = []
        for provider in self.provider_ids:
            modes = {
                cell.mode
                for cell in self.cells
                if cell.enabled
                and cell.rule_id.casefold() in matched
                and cell.provider_id.casefold() == provider.casefold()
            }
            if RouteMode.IMMEDIATE in modes:
                immediate.append(provider)
            elif RouteMode.DIGEST in modes:
                digest.append(provider)
        return tuple(immediate), tuple(digest)

    def with_cell(
        self,
        rule_id: str,
        provider_id: str,
        mode: RouteMode,
        *,
        enabled: bool = True,
    ) -> RoutingMatrix:
        if provider_id.casefold() not in {value.casefold() for value in self.provider_ids}:
            raise KeyError(provider_id)
        key = (rule_id.casefold(), provider_id.casefold())
        cells = [
            cell
            for cell in self.cells
            if (cell.rule_id.casefold(), cell.provider_id.casefold()) != key
        ]
        cells.append(RoutingCell(rule_id, provider_id, mode, enabled))
        return RoutingMatrix(self.provider_ids, tuple(cells))


@dataclass(frozen=True, slots=True)
class MonitoringProfile:
    profile_id: str
    name: str
    folders: tuple[str, ...]
    rules: tuple[NotificationRule, ...] = ()
    providers: tuple[ProviderPolicy, ...] = ()
    quiet_hours: QuietHoursSchedule = field(default_factory=QuietHoursSchedule)
    digest_schedules: tuple[DigestSchedule, ...] = ()
    routing_matrix: RoutingMatrix = field(default_factory=RoutingMatrix)
    poll_interval_seconds: float = _SAFE_MINIMUM_POLL_SECONDS
    enabled: bool = False
    notify_existing: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or len(self.profile_id) > 200:
            raise ValueError("profile_id must be between 1 and 200 characters")
        if not self.name.strip() or len(self.name) > 300:
            raise ValueError("profile name must be between 1 and 300 characters")
        folders = tuple(dict.fromkeys(folder.strip() for folder in self.folders if folder.strip()))
        if not folders:
            raise ValueError("monitoring profiles require at least one folder")
        object.__setattr__(self, "folders", folders)
        if self.poll_interval_seconds < _SAFE_MINIMUM_POLL_SECONDS:
            raise ValueError(
                f"profile polling interval must be at least {_SAFE_MINIMUM_POLL_SECONDS:g} seconds"
            )
        for label, identifiers in (
            ("rules", [rule.rule_id.casefold() for rule in self.rules]),
            ("providers", [provider.provider_id.casefold() for provider in self.providers]),
            (
                "digest schedules",
                [schedule.schedule_id.casefold() for schedule in self.digest_schedules],
            ),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"profile {label} must have unique identifiers")
        providers = {provider.provider_id.casefold() for provider in self.providers}
        matrix_providers = {provider.casefold() for provider in self.routing_matrix.provider_ids}
        if matrix_providers - providers:
            raise ValueError("routing matrix references providers outside the profile")
        schedules = {schedule.schedule_id.casefold() for schedule in self.digest_schedules}
        for provider in self.providers:
            if (
                provider.digest_schedule_id is not None
                and provider.digest_schedule_id.casefold() not in schedules
            ):
                raise ValueError(
                    f"provider {provider.provider_id} references an unknown digest schedule"
                )

    @classmethod
    def safe_default(cls) -> MonitoringProfile:
        """Return a disabled, private profile that makes no external disclosures."""

        return cls("default", "Everything", ("Inbox",))


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    immediate_providers: tuple[str, ...]
    digest_providers: tuple[str, ...]
    queued_providers: tuple[str, ...]
    suppressed: bool
    release_at: datetime | None
    quiet_hours: QuietHoursDecision
    provider_payloads: Mapping[str, Mapping[str, Any]]
    skipped_providers: Mapping[str, str]


class NotificationRouter:
    """Resolve rule actions, matrix cells, provider filters, quiet hours, and privacy."""

    def __init__(self, *, context: RuleEvaluationContext | None = None) -> None:
        self._context = context or RuleEvaluationContext()

    def plan(
        self,
        event: NewMailEvent,
        decision: RuleDecision,
        profile: MonitoringProfile,
        *,
        at: datetime | None = None,
    ) -> RoutingPlan:
        quiet = profile.quiet_hours.evaluate(
            event,
            matched_rule_ids=decision.matched_rule_ids,
            at=at,
        )
        if decision.suppressed:
            return RoutingPlan((), (), (), True, None, quiet, {}, {})
        matrix_immediate, matrix_digest = profile.routing_matrix.resolve(decision.matched_rule_ids)
        if not profile.rules and not profile.routing_matrix.cells:
            # A profile with no rules and no routing means "tell me about new mail":
            # every enabled provider is notified immediately. Without this default an
            # attached desktop provider silently received nothing.
            matrix_immediate = tuple(
                provider.provider_id for provider in profile.providers if provider.enabled
            )
        immediate = list(dict.fromkeys((*decision.immediate_providers, *matrix_immediate)))
        digest = list(dict.fromkeys((*decision.digest_providers, *matrix_digest)))
        providers = {provider.provider_id.casefold(): provider for provider in profile.providers}
        skipped: dict[str, str] = {}

        def accepted(values: list[str]) -> list[str]:
            result: list[str] = []
            for provider_id in values:
                provider = providers.get(provider_id.casefold())
                if provider is None:
                    skipped[provider_id] = "provider is not configured"
                    continue
                if not provider.enabled:
                    skipped[provider_id] = "provider is disabled"
                    continue
                if provider.filter_condition is not None:
                    trace = provider.filter_condition.evaluate(event, self._context)
                    if trace.outcome is not MatchState.MATCH:
                        skipped[provider_id] = f"provider filter: {trace.outcome.value}"
                        continue
                result.append(provider.provider_id)
            return result

        immediate = accepted(immediate)
        digest = accepted(digest)
        queued: list[str] = []
        suppressed = False
        release_at: datetime | None = None
        if quiet.active and not quiet.bypassed:
            if quiet.behavior is QuietHoursBehavior.SUPPRESS:
                immediate.clear()
                digest.clear()
                suppressed = True
            elif quiet.behavior is QuietHoursBehavior.QUEUE:
                queued = immediate
                immediate = []
                release_at = quiet.release_at
            elif quiet.behavior is QuietHoursBehavior.DIGEST:
                digest = list(dict.fromkeys((*digest, *immediate)))
                immediate = []
        exposed_ids = tuple(dict.fromkeys((*immediate, *digest, *queued)))
        payloads: dict[str, Mapping[str, Any]] = {}
        for provider_id in exposed_ids:
            provider = providers[provider_id.casefold()]
            payloads[provider.provider_id] = event_payload(event, provider.privacy)
        return RoutingPlan(
            tuple(immediate),
            tuple(digest),
            tuple(queued),
            suppressed,
            release_at,
            quiet,
            payloads,
            dict(sorted(skipped.items())),
        )


class MonitoringProfileRepository(Protocol):
    """Storage boundary for named profiles and the active-profile pointer."""

    def list_profiles(self) -> tuple[MonitoringProfile, ...]: ...

    def get_profile(self, profile_id: str) -> MonitoringProfile | None: ...

    def save_profile(self, profile: MonitoringProfile) -> None: ...

    def delete_profile(self, profile_id: str) -> bool: ...

    def activate(self, profile_id: str) -> MonitoringProfile: ...

    def active_profile(self) -> MonitoringProfile | None: ...


class InMemoryMonitoringProfileRepository:
    def __init__(self, profiles: Sequence[MonitoringProfile] = ()) -> None:
        self._lock = RLock()
        self._profiles: dict[str, MonitoringProfile] = {}
        self._active: str | None = None
        for profile in profiles:
            self.save_profile(profile)

    def list_profiles(self) -> tuple[MonitoringProfile, ...]:
        with self._lock:
            return tuple(sorted(self._profiles.values(), key=lambda item: item.name.casefold()))

    def get_profile(self, profile_id: str) -> MonitoringProfile | None:
        with self._lock:
            return self._profiles.get(profile_id.casefold())

    def save_profile(self, profile: MonitoringProfile) -> None:
        with self._lock:
            self._profiles[profile.profile_id.casefold()] = profile

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            key = profile_id.casefold()
            removed = self._profiles.pop(key, None) is not None
            if self._active == key:
                self._active = None
            return removed

    def activate(self, profile_id: str) -> MonitoringProfile:
        with self._lock:
            profile = self.get_profile(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            self._active = profile.profile_id.casefold()
            return profile

    def active_profile(self) -> MonitoringProfile | None:
        with self._lock:
            return self._profiles.get(self._active) if self._active is not None else None


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


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of text")
    return tuple(cast(list[str], value))


def _privacy_to_data(policy: EventDisclosurePolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode.value,
        "field_allowlist": (
            sorted(policy.field_allowlist) if policy.field_allowlist is not None else None
        ),
        "redact_addresses": policy.redact_addresses,
        "include_attachment_names": policy.include_attachment_names,
        "include_server_identifiers": policy.include_server_identifiers,
    }


def _privacy_from_data(value: object) -> EventDisclosurePolicy:
    data = _mapping(value, "privacy")
    _strict_keys(
        data,
        required=frozenset(
            {
                "mode",
                "field_allowlist",
                "redact_addresses",
                "include_attachment_names",
                "include_server_identifiers",
            }
        ),
    )
    allowlist = data["field_allowlist"]
    fields = None if allowlist is None else frozenset(_text_tuple(allowlist, "field_allowlist"))
    return EventDisclosurePolicy(
        PrivacyMode(_text(data["mode"], "privacy.mode")),
        fields,
        _boolean(data["redact_addresses"], "privacy.redact_addresses"),
        _boolean(data["include_attachment_names"], "privacy.include_attachment_names"),
        _boolean(data["include_server_identifiers"], "privacy.include_server_identifiers"),
    )


def _quiet_window_to_data(window: QuietHoursWindow) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "timezone": window.timezone,
        "weekdays": sorted(window.weekdays),
        "behavior": window.behavior.value,
        "bypass_importance": sorted(value.value for value in window.bypass_importance),
        "bypass_senders": sorted(window.bypass_senders),
        "bypass_sender_domains": sorted(window.bypass_sender_domains),
        "bypass_rule_ids": sorted(window.bypass_rule_ids),
        "enabled": window.enabled,
    }


def _quiet_window_from_data(value: object) -> QuietHoursWindow:
    data = _mapping(value, "quiet-hours window")
    required = frozenset(
        {
            "window_id",
            "start",
            "end",
            "timezone",
            "weekdays",
            "behavior",
            "bypass_importance",
            "bypass_senders",
            "bypass_sender_domains",
            "bypass_rule_ids",
            "enabled",
        }
    )
    _strict_keys(data, required=required)
    weekdays_value = data["weekdays"]
    if not isinstance(weekdays_value, list):
        raise ValueError("quiet-hours weekdays must be a list")
    try:
        start = time.fromisoformat(_text(data["start"], "quiet-hours start"))
        end = time.fromisoformat(_text(data["end"], "quiet-hours end"))
    except ValueError as exc:
        raise ValueError("quiet-hours times must be ISO local times") from exc
    return QuietHoursWindow(
        _text(data["window_id"], "quiet-hours window_id"),
        start,
        end,
        _text(data["timezone"], "quiet-hours timezone"),
        frozenset(_integer(day, "quiet-hours weekday") for day in weekdays_value),
        QuietHoursBehavior(_text(data["behavior"], "quiet-hours behavior")),
        frozenset(
            Importance(item) for item in _text_tuple(data["bypass_importance"], "bypass_importance")
        ),
        frozenset(_text_tuple(data["bypass_senders"], "bypass_senders")),
        frozenset(_text_tuple(data["bypass_sender_domains"], "bypass_sender_domains")),
        frozenset(_text_tuple(data["bypass_rule_ids"], "bypass_rule_ids")),
        _boolean(data["enabled"], "quiet-hours enabled"),
    )


def _digest_to_data(schedule: DigestSchedule) -> dict[str, Any]:
    return {
        "schedule_id": schedule.schedule_id,
        "name": schedule.name,
        "cadence": schedule.cadence.value,
        "timezone": schedule.timezone,
        "delivery_time": schedule.delivery_time.isoformat() if schedule.delivery_time else None,
        "interval_minutes": schedule.interval_minutes,
        "maximum_events": schedule.maximum_events,
        "maximum_window_seconds": int(schedule.maximum_window.total_seconds()),
        "enabled": schedule.enabled,
    }


def _digest_from_data(value: object) -> DigestSchedule:
    data = _mapping(value, "digest schedule")
    _strict_keys(
        data,
        required=frozenset(
            {
                "schedule_id",
                "name",
                "cadence",
                "timezone",
                "delivery_time",
                "interval_minutes",
                "maximum_events",
                "maximum_window_seconds",
                "enabled",
            }
        ),
    )
    delivery = data["delivery_time"]
    if delivery is not None and not isinstance(delivery, str):
        raise ValueError("digest delivery_time must be text or null")
    interval_value = data["interval_minutes"]
    interval = None if interval_value is None else _integer(interval_value, "interval_minutes")
    try:
        delivery_time = time.fromisoformat(delivery) if isinstance(delivery, str) else None
    except ValueError as exc:
        raise ValueError("digest delivery_time must be an ISO local time") from exc
    return DigestSchedule(
        _text(data["schedule_id"], "digest schedule_id"),
        _text(data["name"], "digest name"),
        DigestCadence(_text(data["cadence"], "digest cadence")),
        _text(data["timezone"], "digest timezone"),
        delivery_time,
        interval,
        _integer(data["maximum_events"], "maximum_events"),
        timedelta(seconds=_integer(data["maximum_window_seconds"], "maximum_window_seconds")),
        _boolean(data["enabled"], "digest enabled"),
    )


def profile_to_data(profile: MonitoringProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "folders": list(profile.folders),
        "poll_interval_seconds": profile.poll_interval_seconds,
        "enabled": profile.enabled,
        "notify_existing": profile.notify_existing,
        "rules": [rule_to_data(rule) for rule in profile.rules],
        "providers": [
            {
                "provider_id": provider.provider_id,
                "enabled": provider.enabled,
                "privacy": _privacy_to_data(provider.privacy),
                "filter_condition": (
                    condition_to_data(provider.filter_condition)
                    if provider.filter_condition is not None
                    else None
                ),
                "digest_schedule_id": provider.digest_schedule_id,
                "maximum_notifications_per_burst": provider.maximum_notifications_per_burst,
            }
            for provider in profile.providers
        ],
        "quiet_hours": [_quiet_window_to_data(window) for window in profile.quiet_hours.windows],
        "digest_schedules": [_digest_to_data(schedule) for schedule in profile.digest_schedules],
        "routing_matrix": {
            "provider_ids": list(profile.routing_matrix.provider_ids),
            "cells": [
                {
                    "rule_id": cell.rule_id,
                    "provider_id": cell.provider_id,
                    "mode": cell.mode.value,
                    "enabled": cell.enabled,
                }
                for cell in profile.routing_matrix.cells
            ],
        },
    }


def profile_from_data(value: object) -> MonitoringProfile:
    data = _mapping(value, "profile")
    _strict_keys(
        data,
        required=frozenset(
            {
                "profile_id",
                "name",
                "folders",
                "poll_interval_seconds",
                "enabled",
                "notify_existing",
                "rules",
                "providers",
                "quiet_hours",
                "digest_schedules",
                "routing_matrix",
            }
        ),
    )
    raw_rules = data["rules"]
    raw_providers = data["providers"]
    raw_quiet = data["quiet_hours"]
    raw_digests = data["digest_schedules"]
    profile_lists = (raw_rules, raw_providers, raw_quiet, raw_digests)
    if not all(isinstance(value, list) for value in profile_lists):
        raise ValueError("profile rules, providers, quiet_hours, and digests must be lists")
    providers: list[ProviderPolicy] = []
    for raw_provider in cast(list[object], raw_providers):
        provider = _mapping(raw_provider, "provider")
        _strict_keys(
            provider,
            required=frozenset(
                {
                    "provider_id",
                    "enabled",
                    "privacy",
                    "filter_condition",
                    "digest_schedule_id",
                    "maximum_notifications_per_burst",
                }
            ),
        )
        digest_id = provider["digest_schedule_id"]
        if digest_id is not None and not isinstance(digest_id, str):
            raise ValueError("provider digest_schedule_id must be text or null")
        filter_value = provider["filter_condition"]
        providers.append(
            ProviderPolicy(
                _text(provider["provider_id"], "provider_id"),
                _boolean(provider["enabled"], "provider enabled"),
                _privacy_from_data(provider["privacy"]),
                condition_from_data(filter_value) if filter_value is not None else None,
                digest_id,
                _integer(
                    provider["maximum_notifications_per_burst"],
                    "maximum_notifications_per_burst",
                ),
            )
        )
    matrix_data = _mapping(data["routing_matrix"], "routing matrix")
    _strict_keys(matrix_data, required=frozenset({"provider_ids", "cells"}))
    raw_cells = matrix_data["cells"]
    if not isinstance(raw_cells, list):
        raise ValueError("routing matrix cells must be a list")
    cells: list[RoutingCell] = []
    for raw_cell in raw_cells:
        cell = _mapping(raw_cell, "routing cell")
        _strict_keys(
            cell,
            required=frozenset({"rule_id", "provider_id", "mode", "enabled"}),
        )
        cells.append(
            RoutingCell(
                _text(cell["rule_id"], "routing rule_id"),
                _text(cell["provider_id"], "routing provider_id"),
                RouteMode(_text(cell["mode"], "routing mode")),
                _boolean(cell["enabled"], "routing enabled"),
            )
        )
    return MonitoringProfile(
        _text(data["profile_id"], "profile_id"),
        _text(data["name"], "profile name"),
        _text_tuple(data["folders"], "profile folders"),
        tuple(rule_from_data(item) for item in cast(list[object], raw_rules)),
        tuple(providers),
        QuietHoursSchedule(
            tuple(_quiet_window_from_data(item) for item in cast(list[object], raw_quiet))
        ),
        tuple(_digest_from_data(item) for item in cast(list[object], raw_digests)),
        RoutingMatrix(
            _text_tuple(matrix_data["provider_ids"], "routing provider_ids"), tuple(cells)
        ),
        _number(data["poll_interval_seconds"], "poll_interval_seconds"),
        _boolean(data["enabled"], "profile enabled"),
        _boolean(data["notify_existing"], "notify_existing"),
    )


def export_profiles(profiles: Sequence[MonitoringProfile]) -> str:
    identifiers = [profile.profile_id.casefold() for profile in profiles]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("cannot export duplicate profile identifiers")
    value = {
        "schema": "gucmail.monitoring-profiles",
        "version": 1,
        "profiles": [profile_to_data(profile) for profile in profiles],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def import_profiles(document: str | bytes) -> tuple[MonitoringProfile, ...]:
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > _MAX_PROFILE_DOCUMENT_BYTES:
        raise ValueError("monitoring-profile document is too large")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("monitoring-profile document is not valid UTF-8 JSON") from exc
    data = _mapping(decoded, "profile document")
    _strict_keys(data, required=frozenset({"schema", "version", "profiles"}))
    if data["schema"] != "gucmail.monitoring-profiles" or data["version"] != 1:
        raise ValueError("unsupported monitoring-profile schema or version")
    raw_profiles = data["profiles"]
    if not isinstance(raw_profiles, list):
        raise ValueError("profile document profiles must be a list")
    if len(raw_profiles) > _MAX_PROFILES:
        raise ValueError("monitoring-profile document contains too many profiles")
    profiles = tuple(profile_from_data(item) for item in raw_profiles)
    identifiers = [profile.profile_id.casefold() for profile in profiles]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("monitoring-profile document contains duplicate identifiers")
    return profiles


__all__ = [
    "DigestAddResult",
    "DigestAddState",
    "DigestBatch",
    "DigestBatcher",
    "DigestCadence",
    "DigestRepository",
    "DigestSchedule",
    "DigestSummary",
    "DigestWindow",
    "InMemoryDigestRepository",
    "InMemoryMonitoringProfileRepository",
    "MonitoringProfile",
    "MonitoringProfileRepository",
    "NotificationRouter",
    "ProviderPolicy",
    "QuietHoursBehavior",
    "QuietHoursDecision",
    "QuietHoursSchedule",
    "QuietHoursWindow",
    "RouteMode",
    "RoutingCell",
    "RoutingMatrix",
    "RoutingPlan",
    "export_profiles",
    "import_profiles",
    "profile_from_data",
    "profile_to_data",
]
