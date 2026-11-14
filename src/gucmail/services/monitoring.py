"""Conservative mailbox monitoring with durable checkpoints and deduplication.

The monitor owns no credentials and never constructs a gateway.  A caller injects one
authenticated gateway instance, which is retained for the lifetime of foreground watch
mode.  Incremental polling is preferred; a bounded recent-message snapshot is the safe
fallback when the server exposes no usable synchronization token.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import socket
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event
from typing import Any, Protocol, cast
from uuid import uuid4

from gucmail.constants import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    SAFE_MINIMUM_POLL_INTERVAL_SECONDS,
)
from gucmail.errors import GUCmailError, ThrottledError, UnsupportedFeatureError
from gucmail.models import Importance, MessageSummary, NotificationEvent
from gucmail.security import redact_text
from gucmail.services.monitoring_outbox import EventOutbox, StateStoreEventOutbox
from gucmail.services.notifications import (
    DeliveryResult,
    DeliveryState,
)
from gucmail.services.watcher_runtime import (
    DEFAULT_WATCHER_LEASE_MANAGER,
    SleepResumeDetector,
    WatcherAlreadyRunningError,
    WatcherLeaseManager,
)
from gucmail.state import StateStore

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]
RandomSource = Callable[[], float]


class MonitorStrategy(StrEnum):
    STREAMING = "ews_streaming"
    PULL = "ews_pull"
    INCREMENTAL = "incremental_poll"
    SYNC = "incremental_sync"
    POLLING = "bounded_poll"
    # Backwards-compatible presentation name. Both names honestly describe a
    # bounded newest-page query rather than an EWS notification subscription.
    SNAPSHOT = "bounded_poll"


class PollNewMessagesGateway(Protocol):
    """Preferred provider-neutral gateway surface."""

    def poll_new_messages(
        self,
        folder: str,
        since: str | None,
        limit: int,
    ) -> object: ...


class ListMessagesGateway(Protocol):
    """Fallback surface implemented by the ordinary message gateway/service."""

    def list_messages(
        self,
        folder: str,
        *,
        limit: int,
        offset: int,
        order_by: str,
    ) -> object: ...


class NotificationEventDispatcher(Protocol):
    """Delivery boundary implemented by direct and profile-aware dispatchers."""

    def dispatch(self, event: NotificationEvent) -> tuple[DeliveryResult, ...]: ...


@dataclass(frozen=True, slots=True)
class MonitorChange:
    """Optional richer change record returned by incremental adapters."""

    kind: str
    message: MessageSummary | None = None
    reference: str | None = None
    occurred_at: datetime | None = None
    folder: str | None = None
    event_id: str | None = None
    open_hint: str | None = None


@dataclass(frozen=True, slots=True)
class PollBatch:
    items: tuple[MessageSummary | MonitorChange | NotificationEvent, ...]
    checkpoint: str | None
    more_available: bool = False
    baseline: bool = False


@dataclass(frozen=True, slots=True)
class FolderPollResult:
    folder: str
    strategy: MonitorStrategy
    checkpoint: str | None
    events: tuple[NotificationEvent, ...] = ()
    deliveries: tuple[DeliveryResult, ...] = ()
    duplicates: int = 0
    baseline_items: int = 0
    error: str | None = None
    retry_after: float | None = None
    throttled: bool = False
    more_available: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether mailbox detection/checkpointing succeeded.

        Provider delivery health is intentionally separate. A failed webhook must
        not turn a successful Exchange poll into a rapid mailbox retry loop.
        """

        return self.error is None

    @property
    def delivery_succeeded(self) -> bool:
        return all(item.succeeded for item in self.deliveries)

    @property
    def fully_succeeded(self) -> bool:
        return self.succeeded and self.delivery_succeeded


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    started_at: datetime
    completed_at: datetime
    folders: tuple[FolderPollResult, ...]
    resumed_after_seconds: float | None = None

    @property
    def events(self) -> tuple[NotificationEvent, ...]:
        return tuple(event for folder in self.folders for event in folder.events)

    @property
    def deliveries(self) -> tuple[DeliveryResult, ...]:
        return tuple(delivery for folder in self.folders for delivery in folder.deliveries)

    @property
    def succeeded(self) -> bool:
        return all(folder.succeeded for folder in self.folders)

    @property
    def delivery_succeeded(self) -> bool:
        return all(folder.delivery_succeeded for folder in self.folders)

    @property
    def fully_succeeded(self) -> bool:
        return self.succeeded and self.delivery_succeeded

    @property
    def retry_after(self) -> float | None:
        requested = [
            folder.retry_after for folder in self.folders if folder.retry_after is not None
        ]
        return max(requested) if requested else None


@dataclass(frozen=True, slots=True)
class MonitoringStatus:
    enabled: bool
    strategy: MonitorStrategy
    poll_interval_seconds: float
    folders: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)


class MailboxMonitor:
    """One-shot and foreground monitor over one already-authenticated gateway."""

    _LIST_CURSOR_PREFIX = "gucmail-monitor-list-v1:"
    _POLL_LOOKBACK = timedelta(minutes=5)

    def __init__(
        self,
        gateway: object,
        state: StateStore,
        *,
        account_id: str,
        dispatcher: NotificationEventDispatcher | None = None,
        folders: Sequence[str] = ("inbox",),
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        limit: int = 50,
        allow_unsafe_poll_interval: bool = False,
        notify_existing: bool = False,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 120.0,
        maximum_drain_batches: int = 16,
        outbox: EventOutbox | None = None,
        lease_manager: WatcherLeaseManager = DEFAULT_WATCHER_LEASE_MANAGER,
        profile_id: str = "default",
        strategy_preference: str | None = None,
        durable_lease: bool = True,
        resume_threshold_seconds: float | None = None,
        clock: Clock | None = None,
        monotonic: MonotonicClock = time.monotonic,
        sleep: Sleeper = time.sleep,
        random_source: RandomSource = random.random,
    ) -> None:
        if (
            poll_interval_seconds < SAFE_MINIMUM_POLL_INTERVAL_SECONDS
            and not allow_unsafe_poll_interval
        ):
            raise ValueError(
                f"poll interval must be at least {SAFE_MINIMUM_POLL_INTERVAL_SECONDS} seconds"
            )
        if not 1 <= limit <= 500:
            raise ValueError("monitoring limit must be between 1 and 500")
        normalized_folders_list: list[str] = []
        seen_folders: set[str] = set()
        for folder in folders:
            clean_folder = folder.strip()
            key = clean_folder.casefold()
            if clean_folder and key not in seen_folders:
                normalized_folders_list.append(clean_folder)
                seen_folders.add(key)
        normalized_folders = tuple(normalized_folders_list)
        if not normalized_folders:
            raise ValueError("at least one monitoring folder is required")
        if not 0 < retry_base_seconds <= retry_max_seconds <= 3600:
            raise ValueError("monitoring retry delays are invalid")
        if not 1 <= maximum_drain_batches <= 100:
            raise ValueError("monitoring drain batches must be between 1 and 100")
        if resume_threshold_seconds is not None and resume_threshold_seconds <= 0:
            raise ValueError("monitoring resume threshold must be positive")
        normalized_profile = profile_id.casefold().strip()
        if not normalized_profile or any(
            character in profile_id for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("monitoring profile ID is invalid")
        if strategy_preference in {"ews_streaming", "ews_pull"}:
            raise UnsupportedFeatureError(
                "This watcher has no active streaming/pull transport. "
                "Choose automatic, incremental, or bounded polling."
            )
        if strategy_preference not in {
            None,
            "incremental_poll",
            "incremental_sync",
            "bounded_poll",
        }:
            raise ValueError("Unknown monitoring strategy preference")
        self._strategy_preference = strategy_preference
        self._gateway = gateway
        self._state = state
        self._account_id = account_id
        self._account_identity = account_id.casefold().strip()
        if not self._account_identity:
            raise ValueError("monitoring account ID may not be empty")
        self._dispatcher = dispatcher
        self._folders = normalized_folders
        self._interval = float(poll_interval_seconds)
        self._limit = limit
        self._notify_existing = notify_existing
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._maximum_drain_batches = maximum_drain_batches
        self._outbox = outbox if outbox is not None else StateStoreEventOutbox(state)
        self._uses_atomic_state_outbox = (
            isinstance(self._outbox, StateStoreEventOutbox) and self._outbox.state_store is state
        )
        self._delivery_worker_id = f"monitor-{os.getpid()}-{uuid4().hex}"
        self._lease_manager = lease_manager
        self._profile_id = normalized_profile
        self._durable_lease_enabled = durable_lease
        self._durable_lease_ttl = min(
            86_400,
            max(120, math.ceil(self._interval) + 120),
        )
        self._resume_threshold = float(
            resume_threshold_seconds
            if resume_threshold_seconds is not None
            else max(self._interval * 2, 120.0)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._sleep = sleep
        self._random = random_source

    @property
    def gateway(self) -> object:
        """The same instance is returned for every cycle (and is never reauthenticated here)."""

        return self._gateway

    @property
    def folders(self) -> tuple[str, ...]:
        return self._folders

    def _scope(self, folder: str) -> str:
        digest = hashlib.sha256(self._account_identity.encode("utf-8")).hexdigest()[:16]
        return f"monitor:{digest}:{folder.casefold()}" + (
            f":{self._strategy_preference}" if self._strategy_preference else ""
        )

    def _lease_scope(self) -> str:
        # Detection checkpoints and the provider-neutral outbox are account-wide,
        # not profile-scoped. Until one process can route all profiles atomically,
        # fail closed instead of allowing profiles to race and suppress each other.
        identity = f"v1\x00{self._account_identity}\x00account-watcher"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    @contextmanager
    def _durable_lease(self) -> Iterator[str | None]:
        """Claim schema-v5 ownership for cross-process watcher exclusion.

        The lightweight in-process lease still protects embedded callers.  This
        second lease is persisted so a foreground CLI, one-shot scheduler, and
        headless watcher cannot silently poll the same account/profile together.
        """

        if not self._durable_lease_enabled:
            yield None
            return
        lease_id = uuid4().hex
        scope = self._lease_scope()
        acquired = self._state.claim_watcher_lease(
            scope=scope,
            lease_id=lease_id,
            owner_id=self._delivery_worker_id,
            process_id=os.getpid(),
            profile_id=self._profile_id,
            ttl_seconds=self._durable_lease_ttl,
            now=self._clock(),
            host_name=socket.gethostname(),
        )
        if acquired is None:
            raise WatcherAlreadyRunningError(
                "gucmail is already watching this mailbox."
            )
        try:
            yield lease_id
        finally:
            self._state.release_watcher_lease(
                scope,
                lease_id,
                state="stopped",
                at=self._clock(),
            )

    def _heartbeat_durable_lease(
        self,
        lease_id: str | None,
        result: MonitorRunResult,
        *,
        next_poll_at: datetime | None = None,
        backoff_until: datetime | None = None,
    ) -> None:
        if lease_id is None:
            return
        strategies = ",".join(dict.fromkeys(folder.strategy.value for folder in result.folders))
        delivered = sum(delivery.state is DeliveryState.DELIVERED for delivery in result.deliveries)
        failed = sum(delivery.state is DeliveryState.FAILED for delivery in result.deliveries)
        last_event = max(
            (event.occurred_at for event in result.events),
            default=None,
        )
        errors = tuple(folder.error for folder in result.folders if folder.error)
        renewed = self._state.heartbeat_watcher(
            self._lease_scope(),
            lease_id,
            ttl_seconds=self._durable_lease_ttl,
            now=self._clock(),
            state="running" if result.succeeded else "reconnecting",
            strategy=strategies or None,
            connection_state="connected" if result.succeeded else "reconnecting",
            last_success_at=result.completed_at if result.succeeded else None,
            last_event_at=last_event,
            next_poll_at=next_poll_at,
            backoff_until=backoff_until,
            detected_delta=len(result.events),
            delivered_delta=delivered,
            failed_delta=failed,
            error_summary="; ".join(errors) if errors else None,
        )
        if renewed is None:
            raise WatcherAlreadyRunningError(
                "The durable watcher lease expired or was replaced; monitoring stopped safely."
            )

    def _renew_durable_lease_before_poll(self, lease_id: str | None) -> None:
        """Fence every Exchange cycle before touching the shared mailbox session."""

        if lease_id is None:
            return
        renewed = self._state.claim_watcher_lease(
            scope=self._lease_scope(),
            lease_id=lease_id,
            owner_id=self._delivery_worker_id,
            process_id=os.getpid(),
            profile_id=self._profile_id,
            ttl_seconds=self._durable_lease_ttl,
            now=self._clock(),
            host_name=socket.gethostname(),
        )
        if renewed is None:
            raise WatcherAlreadyRunningError(
                "The durable watcher lease is now owned by another process; "
                "monitoring stopped before polling."
            )

    def _checkpoint(self, folder: str) -> str | None:
        return self._state.monitor_checkpoint(self._scope(folder))

    def _event_seen(self, event_id: str) -> bool:
        self._state.initialize()
        with self._state.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM seen_items WHERE event_key=?",
                (event_id,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _snapshot_checkpoint(
        items: Sequence[MessageSummary | MonitorChange | NotificationEvent],
        previous: str | None,
    ) -> str | None:
        times: list[datetime] = []
        for item in items:
            candidate: datetime | None = None
            if isinstance(item, MessageSummary) and item.received_at is not None:
                candidate = item.received_at
            elif isinstance(item, MonitorChange) and item.occurred_at is not None:  # noqa: SIM114
                candidate = item.occurred_at
            elif isinstance(item, NotificationEvent):
                candidate = item.occurred_at
            if candidate is not None:
                times.append(candidate)
        if not times:
            return previous
        return max(times).astimezone(UTC).isoformat()

    @staticmethod
    def _summary_time(item: MessageSummary) -> datetime | None:
        value = item.received_at or item.sent_at
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _list_cursor(
        self,
        checkpoint: str | None,
    ) -> tuple[datetime | None, int, datetime | None]:
        if checkpoint is None:
            return None, 0, None
        if checkpoint.startswith(self._LIST_CURSOR_PREFIX):
            try:
                payload = json.loads(checkpoint[len(self._LIST_CURSOR_PREFIX) :])
                threshold = datetime.fromisoformat(str(payload["threshold"]))
                newest = datetime.fromisoformat(str(payload["newest"]))
                offset = int(payload["offset"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("Monitoring list continuation is invalid.") from exc
            if offset < 1:
                raise RuntimeError("Monitoring list continuation is invalid.")
            return (
                self._summary_datetime(threshold),
                offset,
                self._summary_datetime(newest),
            )
        try:
            threshold = datetime.fromisoformat(checkpoint.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("Monitoring list checkpoint is invalid.") from exc
        normalized = self._summary_datetime(threshold)
        return normalized, 0, normalized

    @staticmethod
    def _summary_datetime(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("Monitoring list checkpoint must include a UTC offset.")
        return value.astimezone(UTC)

    def _list_continuation(
        self,
        *,
        threshold: datetime,
        offset: int,
        newest: datetime,
    ) -> str:
        return self._LIST_CURSOR_PREFIX + json.dumps(
            {
                "newest": newest.isoformat(),
                "offset": offset,
                "threshold": threshold.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _normalize_items(
        value: object,
    ) -> tuple[MessageSummary | MonitorChange | NotificationEvent, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes, bytearray)):
            raise TypeError("monitor gateway returned text instead of mailbox items")
        if isinstance(value, Sequence):
            raw_items = tuple(value)
        else:
            try:
                raw_items = tuple(cast(Any, value))
            except TypeError as exc:
                raise TypeError("monitor gateway returned a non-iterable item collection") from exc
        accepted = (MessageSummary, MonitorChange, NotificationEvent)
        if any(not isinstance(item, accepted) for item in raw_items):
            raise TypeError("monitor gateway returned an unsupported change type")
        return cast(tuple[MessageSummary | MonitorChange | NotificationEvent, ...], raw_items)

    def _normalize_batch(self, result: object, previous: str | None) -> PollBatch:
        if isinstance(result, PollBatch):
            return result
        if isinstance(result, Mapping):
            items = result.get("items", result.get("messages", result.get("events", ())))
            checkpoint = result.get("checkpoint", result.get("sync_state", previous))
            if "more_available" in result:
                more = bool(result["more_available"])
            elif "includes_last" in result:
                # EWS names this flag from the opposite perspective: True means
                # the final item was included and therefore no continuation exists.
                more = not bool(result["includes_last"])
            else:
                more = False
            return PollBatch(
                self._normalize_items(items),
                str(checkpoint) if checkpoint is not None else None,
                more,
            )
        if isinstance(result, tuple) and len(result) in {2, 3}:
            items = self._normalize_items(result[0])
            checkpoint = str(result[1]) if result[1] is not None else None
            more = bool(result[2]) if len(result) == 3 else False
            return PollBatch(items, checkpoint, more)
        items_attr = getattr(result, "items", None)
        if items_attr is not None:
            checkpoint_attr = getattr(result, "checkpoint", getattr(result, "sync_state", previous))
            if hasattr(result, "more_available"):
                more = bool(cast(Any, result).more_available)
            elif hasattr(result, "includes_last"):
                more = not bool(cast(Any, result).includes_last)
            else:
                more = bool(getattr(result, "has_more", False))
            return PollBatch(
                self._normalize_items(items_attr),
                str(checkpoint_attr) if checkpoint_attr is not None else None,
                more,
            )
        return PollBatch(self._normalize_items(result), previous)

    def _declared_strategy(self, fallback: MonitorStrategy) -> MonitorStrategy:
        raw = getattr(self._gateway, "monitor_strategy", None)
        if raw is None:
            return fallback
        try:
            return raw if isinstance(raw, MonitorStrategy) else MonitorStrategy(str(raw))
        except ValueError:
            return fallback

    def _fetch(
        self, folder: str, checkpoint: str | None, *, boundary_reference: str | None = None
    ) -> tuple[PollBatch, MonitorStrategy]:
        incremental = getattr(self._gateway, "poll_new_messages", None)
        if callable(incremental) and self._strategy_preference not in {
            "bounded_poll",
            "incremental_sync",
        }:
            result = incremental(folder, checkpoint, self._limit)
            return (
                self._normalize_batch(result, checkpoint),
                self._declared_strategy(MonitorStrategy.INCREMENTAL),
            )

        sync = getattr(self._gateway, "sync_folder_items", None)
        if callable(sync) and self._strategy_preference != "bounded_poll":
            result = sync(folder=folder, sync_state=checkpoint, max_changes=self._limit)
            return self._normalize_batch(result, checkpoint), MonitorStrategy.SYNC

        if self._strategy_preference in {"incremental_poll", "incremental_sync"}:
            raise UnsupportedFeatureError(
                "The gateway has no usable requested incremental method. "
                "Choose automatic or bounded polling."
            )
        listing = getattr(self._gateway, "list_messages", None)
        if not callable(listing):
            raise TypeError(
                "gateway must provide poll_new_messages, sync_folder_items, or list_messages"
            )
        threshold, offset, newest = self._list_cursor(checkpoint)
        # Re-read the previous page's final item with each continuation. Offset
        # pagination is not a snapshot: a move, deletion, or insertion ahead of
        # the cursor can shift unread items across the boundary. A changed anchor
        # makes the entire cycle fail before the checkpoint or outbox is committed.
        # This detects boundary movement; it cannot prove snapshot consistency.
        if offset and not boundary_reference:
            # Older persisted continuations did not contain an anchor. Replay
            # from their original watermark instead of trusting an unsafe offset.
            offset = 0
            newest = threshold
        request_limit = max(self._limit, 2) if offset else self._limit
        result = listing(
            folder,
            limit=request_limit,
            offset=offset - 1 if offset else 0,
            order_by="-datetime_received",
        )
        items_value = getattr(result, "items", result)
        items = self._normalize_items(items_value)
        summaries = tuple(item for item in items if isinstance(item, MessageSummary))
        if len(summaries) != len(items):
            raise TypeError("list_messages monitoring fallback returned non-message changes")
        if offset:
            if not summaries or summaries[0].reference != boundary_reference:
                raise GUCmailError(
                    "The monitored folder changed during pagination; no checkpoint was advanced. "
                    "The next polling cycle can retry safely."
                )
            summaries = summaries[1:]
        filtered = tuple(
            item
            for item in summaries
            if threshold is None
            or self._summary_time(item) is None
            or cast(datetime, self._summary_time(item)) >= threshold
        )
        times = tuple(
            value for item in summaries if (value := self._summary_time(item)) is not None
        )
        if times:
            newest = max((*times, *(value for value in (newest, threshold) if value is not None)))
        last_time = self._summary_time(summaries[-1]) if summaries else None
        more_available = bool(
            threshold is not None
            and len(items) == request_limit
            and (last_time is None or last_time >= threshold)
        )
        next_checkpoint: str | None
        if more_available:
            assert threshold is not None
            assert newest is not None
            next_checkpoint = self._list_continuation(
                threshold=threshold,
                offset=offset + len(summaries),
                newest=newest,
            )
        elif newest is None:
            next_checkpoint = checkpoint
        elif threshold is None:
            next_checkpoint = newest.isoformat()
        else:
            next_checkpoint = max(threshold, newest - self._POLL_LOOKBACK).isoformat()
        return (
            PollBatch(filtered, next_checkpoint, more_available),
            MonitorStrategy.POLLING,
        )

    def _fetch_all(
        self,
        folder: str,
        checkpoint: str | None,
    ) -> tuple[PollBatch, MonitorStrategy]:
        """Drain a bounded continuation chain without committing a partial cursor.

        If the configured request budget cannot drain the chain, the original
        checkpoint remains untouched and the cycle fails closed. This can delay a
        very large backlog, but it cannot silently advance past unseen events.
        """

        combined: list[MessageSummary | MonitorChange | NotificationEvent] = []
        current = checkpoint
        boundary_reference: str | None = None
        strategy = MonitorStrategy.INCREMENTAL
        for batch_number in range(1, self._maximum_drain_batches + 1):
            batch, strategy = self._fetch(folder, current, boundary_reference=boundary_reference)
            if strategy is MonitorStrategy.SYNC:
                # Native EWS pages describe every change, so each page can be
                # atomically committed with its outbox. Resume the next page at
                # the normal interval, including during a large quiet baseline.
                return batch, strategy
            combined.extend(batch.items)
            if not batch.more_available:
                return PollBatch(tuple(combined), batch.checkpoint, False), strategy
            if batch.checkpoint is None or batch.checkpoint == current:
                raise RuntimeError("Monitoring continuation did not make progress.")
            if batch.checkpoint.startswith(self._LIST_CURSOR_PREFIX):
                last = batch.items[-1] if batch.items else None
                if not isinstance(last, MessageSummary) or not last.reference:
                    raise GUCmailError(
                        "Monitoring could not verify a list page boundary; "
                        "no checkpoint was advanced."
                    )
                boundary_reference = last.reference
            current = batch.checkpoint
            if batch_number == self._maximum_drain_batches:
                raise RuntimeError(
                    "Monitoring backlog exceeded the bounded per-cycle request budget."
                )
        raise RuntimeError("Monitoring continuation ended unexpectedly.")  # pragma: no cover

    def _stable_event_id(
        self,
        *,
        kind: str,
        folder: str,
        reference: str | None,
        occurred_at: datetime,
    ) -> str:
        # A stable item reference remains the same when a message is moved, so folder is
        # intentionally excluded in that case.  Folder and time are the fallback identity
        # only for reference-less delete/change records.
        parts = [self._account_identity, kind]
        if reference:
            parts.append(reference)
        else:
            parts.extend((folder.casefold(), occurred_at.astimezone(UTC).isoformat()))
        identity = "\x00".join(parts)
        return "mail-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def _to_event(
        self,
        item: MessageSummary | MonitorChange | NotificationEvent,
        folder: str,
    ) -> NotificationEvent:
        if isinstance(item, NotificationEvent):
            return item if item.folder is not None else replace(item, folder=folder)
        if isinstance(item, MonitorChange):
            message = item.message
            occurred_at = item.occurred_at or (
                message.received_at if message and message.received_at else self._clock()
            )
            reference = item.reference or (message.reference if message else None)
            event_folder = item.folder or (message.folder_path if message else None) or folder
            event_id = item.event_id or self._stable_event_id(
                kind=item.kind,
                folder=event_folder,
                reference=reference,
                occurred_at=occurred_at,
            )
            return NotificationEvent(
                event_id=event_id,
                event_type=item.kind,
                occurred_at=occurred_at,
                account_id=self._account_id,
                folder=event_folder,
                message_reference=reference,
                sender=message.sender if message else None,
                # "" (not None) marks a real message whose subject is empty.
                subject=(message.subject or "") if message else None,
                importance=message.importance if message else Importance.NORMAL,
                has_attachments=message.has_attachments if message else False,
                preview=message.preview if message else None,
                open_hint=item.open_hint
                or (
                    f"gucmail messages show {reference}"
                    if reference
                    and "deleted" not in item.kind
                    and not item.kind.startswith("calendar_")
                    else None
                ),
            )
        occurred_at = item.received_at or item.sent_at or self._clock()
        event_folder = item.folder_path or folder
        return NotificationEvent(
            event_id=self._stable_event_id(
                kind="new_message",
                folder=event_folder,
                reference=item.reference,
                occurred_at=occurred_at,
            ),
            event_type="new_message",
            occurred_at=occurred_at,
            account_id=self._account_id,
            folder=event_folder,
            message_reference=item.reference,
            sender=item.sender,
            subject=item.subject or "",
            importance=item.importance,
            has_attachments=item.has_attachments,
            preview=item.preview,
            open_hint=f"gucmail messages show {item.reference}",
        )

    @staticmethod
    def _event_sort_key(event: NotificationEvent) -> tuple[datetime, str]:
        value = event.occurred_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC), event.event_id

    @staticmethod
    def _safe_retry_after(value: object) -> float | None:
        """Normalize an untrusted server retry hint to a finite safe duration."""

        if value is None:
            return None
        if isinstance(value, timedelta):
            seconds = value.total_seconds()
        else:
            try:
                seconds = float(cast(Any, value))
            except (TypeError, ValueError, OverflowError):
                return None
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        # A pathological response must not overflow Event.wait or park a service
        # forever. One day still honors throttling conservatively without a loop.
        return min(seconds, 86_400.0)

    def poll_folder(self, folder: str) -> FolderPollResult:
        checkpoint = self._checkpoint(folder)
        strategy = (
            MonitorStrategy(self._strategy_preference)
            if self._strategy_preference
            else self._declared_strategy(MonitorStrategy.INCREMENTAL)
        )
        try:
            batch, strategy = self._fetch_all(folder, checkpoint)
            events = tuple(
                sorted(
                    (self._to_event(item, folder) for item in batch.items), key=self._event_sort_key
                )
            )
            initial_baseline = (checkpoint is None or batch.baseline) and not self._notify_existing
            duplicates = 0
            baseline = 0
            detected: list[NotificationEvent] = []
            deliveries: list[DeliveryResult] = []

            if self._uses_atomic_state_outbox:
                committed = self._state.commit_monitor_batch(
                    self._scope(folder),
                    checkpoint,
                    batch.checkpoint,
                    events,
                    detected_at=self._clock(),
                    enqueue=not initial_baseline and self._dispatcher is not None,
                )
                if not committed.committed:
                    raise RuntimeError(
                        "Monitoring checkpoint changed while this batch was being committed."
                    )
                inserted = frozenset(committed.inserted_event_ids)
                duplicates = committed.duplicate_count
                if initial_baseline:
                    baseline = len(inserted)
                else:
                    detected.extend(event for event in events if event.event_id in inserted)
            else:
                # Custom/in-memory outboxes remain useful for isolated tests and
                # embedders. They cannot share the SQLite transaction, so retain the
                # conservative enqueue-before-checkpoint ordering.
                for event in events:
                    if self._event_seen(event.event_id):
                        duplicates += 1
                        continue
                    if initial_baseline:
                        self._state.remember_event(event.event_id, event.message_reference)
                        baseline += 1
                        continue
                    if self._dispatcher is not None:
                        self._outbox.enqueue(event, folder=folder, detected_at=self._clock())
                    self._state.remember_event(event.event_id, event.message_reference)
                    detected.append(event)
                self._state.checkpoint(
                    self._scope(folder),
                    sync_state=batch.checkpoint,
                    succeeded=True,
                )

            return FolderPollResult(
                folder=folder,
                strategy=strategy,
                checkpoint=batch.checkpoint,
                events=tuple(detected),
                deliveries=tuple(deliveries),
                duplicates=duplicates,
                baseline_items=baseline,
                error=None,
                more_available=batch.more_available,
            )
        except Exception as exc:
            safe_error = f"Monitoring failed ({type(exc).__name__})."
            if isinstance(exc, GUCmailError):
                safe_error = exc.message
            throttled = isinstance(exc, ThrottledError)
            retry_after = (
                self._safe_retry_after(exc.retry_after) if isinstance(exc, ThrottledError) else None
            )
            self._state.checkpoint(
                self._scope(folder),
                sync_state=None,
                succeeded=False,
                error=redact_text(safe_error),
            )
            return FolderPollResult(
                folder=folder,
                strategy=strategy,
                checkpoint=checkpoint,
                error=safe_error,
                retry_after=retry_after,
                throttled=throttled,
            )

    def _drain_outbox(self) -> tuple[DeliveryResult, ...]:
        """Deliver due provider work independently of Exchange polling success.

        Claims are account-wide so changing the monitored folder set cannot strand
        a retry. Provider health is returned separately and never changes mailbox
        checkpoint or polling cadence.
        """

        if self._dispatcher is None:
            # A structured-output-only monitor must not consume another watcher's
            # queued deliveries merely because it shares this account database.
            return ()
        deliveries: list[DeliveryResult] = []
        claimed = False
        # A new token for every claim prevents an expired claim from being
        # mistaken for a later claim made by the same monitor instance.
        claim_owner = f"{self._delivery_worker_id}-{uuid4().hex}"
        try:
            claim_due = getattr(self._outbox, "claim_due", None)
            if callable(claim_due):
                due_entries = claim_due(
                    folder=None,
                    now=self._clock(),
                    limit=max(self._limit * self._maximum_drain_batches, 1),
                    lease_owner=claim_owner,
                    lease_for=timedelta(minutes=15),
                )
                claimed = True
            else:
                due_entries = self._outbox.due(
                    folder=None,
                    now=self._clock(),
                    limit=max(self._limit * self._maximum_drain_batches, 1),
                )
        except Exception as exc:
            return (
                DeliveryResult(
                    "outbox:account",
                    "notification-outbox",
                    DeliveryState.FAILED,
                    f"Notification outbox unavailable ({type(exc).__name__}).",
                    attempts=0,
                ),
            )
        for entry in due_entries:
            try:
                event_deliveries = (
                    self._dispatcher.dispatch(entry.event) if self._dispatcher is not None else ()
                )
            except Exception as exc:  # Defensive isolation around dispatcher plugins/state.
                event_deliveries = (
                    DeliveryResult(
                        entry.event.event_id,
                        "notification-dispatcher",
                        DeliveryState.FAILED,
                        f"Notification dispatch failed ({type(exc).__name__}).",
                    ),
                )
            deliveries.extend(event_deliveries)
            try:
                self._outbox.record(
                    entry.event.event_id,
                    event_deliveries,
                    now=self._clock(),
                    lease_owner=claim_owner if claimed else None,
                )
            except Exception as exc:
                deliveries.append(
                    DeliveryResult(
                        entry.event.event_id,
                        "notification-outbox",
                        DeliveryState.FAILED,
                        f"Notification outcome was not recorded ({type(exc).__name__}).",
                        attempts=0,
                    )
                )
        if self._dispatcher is not None:
            drain_due = getattr(self._dispatcher, "drain_due", None)
            if callable(drain_due):
                try:
                    deliveries.extend(drain_due())
                except Exception as exc:
                    deliveries.append(
                        DeliveryResult(
                            "routing-queue:account",
                            "notification-routing-queue",
                            DeliveryState.FAILED,
                            f"Deferred notification routing failed ({type(exc).__name__}).",
                            attempts=0,
                        )
                    )
        return tuple(deliveries)

    def _run_cycle(self, *, resumed_after_seconds: float | None = None) -> MonitorRunResult:
        started = self._clock()
        results: list[FolderPollResult] = []
        for folder in self._folders:
            result = self.poll_folder(folder)
            results.append(result)
            # A server throttling signal applies to the account/session, not merely
            # to the folder that happened to encounter it. Do not issue more EWS
            # requests in this cycle.
            if result.throttled:
                break
        deliveries = self._drain_outbox()
        if deliveries:
            results[0] = replace(results[0], deliveries=deliveries)
        return MonitorRunResult(
            started,
            self._clock(),
            tuple(results),
            resumed_after_seconds=resumed_after_seconds,
        )

    def run_once(self) -> MonitorRunResult:
        """Poll every configured folder exactly once; suitable for OS schedulers."""

        with (
            self._lease_manager.acquire(self._lease_scope()),
            self._durable_lease() as lease_id,
        ):
            result = self._run_cycle()
            self._heartbeat_durable_lease(lease_id, result)
            return result

    # CLI-friendly spelling.
    one_shot = run_once

    def reset_checkpoints(self) -> None:
        """Reset only the selected local detection cursors under the watcher lease."""
        with self._lease_manager.acquire(self._lease_scope()), self._durable_lease():
            for folder in self._folders:
                self._state.reset_checkpoint(self._scope(folder))

    def _backoff(self, failure_count: int) -> float:
        ceiling = min(self._retry_base * (2 ** max(failure_count - 1, 0)), self._retry_max)
        jitter = max(0.0, min(float(self._random()), 1.0))
        return float(ceiling * (0.5 + (jitter * 0.5)))

    def iter_foreground(
        self,
        *,
        stop_event: Event | None = None,
        max_cycles: int | None = None,
    ) -> Iterator[MonitorRunResult]:
        """Yield bounded cycles until stopped, sleeping once between cycles.

        A long suspend naturally results in one immediate cycle after resume; no burst of
        catch-up requests is attempted.
        """

        if max_cycles is not None and max_cycles < 1:
            return
        failures = 0
        cycles = 0
        detector = SleepResumeDetector(self._resume_threshold)
        with (
            self._lease_manager.acquire(self._lease_scope()),
            self._durable_lease() as lease_id,
        ):
            while stop_event is None or not stop_event.is_set():
                self._renew_durable_lease_before_poll(lease_id)
                cycle_started = self._monotonic()
                resumed_after = detector.observe(cycle_started)
                if resumed_after is not None:
                    resume_hook = getattr(self._gateway, "handle_system_resume", None)
                    if callable(resume_hook):
                        # A recovery hint must never replace the normal safe poll,
                        # which remains capable of surfacing the actual connection error.
                        with contextlib.suppress(Exception):
                            resume_hook(resumed_after)
                result = self._run_cycle(resumed_after_seconds=resumed_after)
                cycles += 1
                delay: float
                if result.succeeded:
                    failures = 0
                    delay = max(self._interval - (self._monotonic() - cycle_started), 0.0)
                else:
                    failures += 1
                    requested_delay = result.retry_after
                    if requested_delay is None:
                        delay = max(
                            self._backoff(failures),
                            float(SAFE_MINIMUM_POLL_INTERVAL_SECONDS),
                        )
                    else:
                        # A server backoff is a minimum, not a value to cap below
                        # what Exchange requested.
                        delay = max(
                            float(requested_delay),
                            float(SAFE_MINIMUM_POLL_INTERVAL_SECONDS),
                        )
                final_cycle = max_cycles is not None and cycles >= max_cycles
                next_poll_at = None if final_cycle else self._clock() + timedelta(seconds=delay)
                self._heartbeat_durable_lease(
                    lease_id,
                    result,
                    next_poll_at=next_poll_at,
                    backoff_until=(
                        next_poll_at if not result.succeeded and not final_cycle else None
                    ),
                )
                yield result
                if final_cycle:
                    return
                if stop_event is not None:
                    if stop_event.wait(delay):
                        return
                else:
                    self._sleep(delay)

    def run_foreground(
        self,
        *,
        stop_event: Event | None = None,
        max_cycles: int | None = None,
        on_result: Callable[[MonitorRunResult], None] | None = None,
    ) -> MonitorRunResult | None:
        """Run foreground watch mode and return the final completed cycle."""

        latest: MonitorRunResult | None = None
        for latest in self.iter_foreground(stop_event=stop_event, max_cycles=max_cycles):
            if on_result is not None:
                on_result(latest)
        return latest

    def status(self) -> MonitoringStatus:
        strategy = (
            self._declared_strategy(MonitorStrategy.INCREMENTAL)
            if callable(getattr(self._gateway, "poll_new_messages", None))
            else (
                MonitorStrategy.SYNC
                if callable(getattr(self._gateway, "sync_folder_items", None))
                else MonitorStrategy.POLLING
            )
        )
        if self._strategy_preference == "bounded_poll":
            strategy = MonitorStrategy.POLLING
        elif self._strategy_preference == "incremental_sync":
            strategy = MonitorStrategy.SYNC
        self._state.initialize()
        folder_status: dict[str, Mapping[str, str | None]] = {}
        with self._state.connection() as connection:
            for folder in self._folders:
                row = connection.execute(
                    """
                    SELECT sync_state, last_success_at, last_error, updated_at
                    FROM sync_checkpoints WHERE scope=?
                    """,
                    (self._scope(folder),),
                ).fetchone()
                folder_status[folder] = (
                    dict(row)
                    if row
                    else {
                        "sync_state": None,
                        "last_success_at": None,
                        "last_error": None,
                        "updated_at": None,
                    }
                )
        return MonitoringStatus(True, strategy, self._interval, folder_status)


# A shorter name for presentation layers.
MonitoringService = MailboxMonitor


__all__ = [
    "FolderPollResult",
    "ListMessagesGateway",
    "MailboxMonitor",
    "MonitorChange",
    "MonitorRunResult",
    "MonitorStrategy",
    "MonitoringService",
    "MonitoringStatus",
    "PollBatch",
    "PollNewMessagesGateway",
]
