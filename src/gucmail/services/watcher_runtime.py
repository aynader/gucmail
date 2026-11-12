"""Runtime coordination primitives for mailbox watchers."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4


class WatcherAlreadyRunningError(RuntimeError):
    """Raised when the same account/profile already owns a watcher lease."""


@dataclass(frozen=True, slots=True)
class WatcherLease:
    scope: str
    lease_id: str
    process_id: int
    acquired_at: datetime


class WatcherLeaseManager(Protocol):
    def acquire(self, scope: str) -> AbstractContextManager[WatcherLease]: ...


class InProcessWatcherLeaseManager:
    """Prevent duplicate watchers inside one process.

    The interface is deliberately compatible with a future database lease or Windows
    named-mutex implementation.  It already prevents duplicate embedded/TUI watcher
    instances and makes ownership explicit instead of relying on provider deduplication.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, WatcherLease] = {}

    @contextmanager
    def acquire(self, scope: str) -> Iterator[WatcherLease]:
        clean_scope = scope.strip()
        if not clean_scope:
            raise ValueError("watcher lease scope may not be empty")
        with self._lock:
            existing = self._active.get(clean_scope)
            if existing is not None:
                raise WatcherAlreadyRunningError(
                    "GUC Mail monitoring is already running for this account and profile."
                )
            lease = WatcherLease(
                scope=clean_scope,
                lease_id=uuid4().hex,
                process_id=os.getpid(),
                acquired_at=datetime.now(UTC),
            )
            self._active[clean_scope] = lease
        try:
            yield lease
        finally:
            with self._lock:
                if self._active.get(clean_scope) == lease:
                    del self._active[clean_scope]

    def status(self, scope: str) -> WatcherLease | None:
        with self._lock:
            return self._active.get(scope)


@dataclass(slots=True)
class SleepResumeDetector:
    """Detect a large monotonic gap without creating catch-up polling bursts."""

    threshold_seconds: float
    _last_observed: float | None = None

    def __post_init__(self) -> None:
        if self.threshold_seconds <= 0:
            raise ValueError("sleep/resume threshold must be positive")

    def observe(self, monotonic_now: float) -> float | None:
        previous = self._last_observed
        self._last_observed = monotonic_now
        if previous is None:
            return None
        gap = max(monotonic_now - previous, 0.0)
        return gap if gap > self.threshold_seconds else None


DEFAULT_WATCHER_LEASE_MANAGER = InProcessWatcherLeaseManager()


__all__ = [
    "DEFAULT_WATCHER_LEASE_MANAGER",
    "InProcessWatcherLeaseManager",
    "SleepResumeDetector",
    "WatcherAlreadyRunningError",
    "WatcherLease",
    "WatcherLeaseManager",
]
