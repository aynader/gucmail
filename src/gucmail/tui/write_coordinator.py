"""Keep unfinished changes owned by the open app, rather than one screen.

A Textual worker can be cancelled while its executor thread keeps running.
Only that thread's completion releases a started lease. An unstarted lease may
be cancelled atomically, so removing a screen cannot strand or later run it.
This is an in-process UI guard; durable service claims still own send recovery.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from threading import Event, Lock
from typing import Any, Concatenate, ParamSpec, TypeVar
from weakref import ref

from textual.app import App
from textual.message import Message
from textual.worker import get_current_worker

PENDING_CHANGE_MESSAGE = (
    "A change is still running in this app. No new action was started. "
    "Wait for it to finish, then refresh before another change."
)
CHANGE_IN_PROGRESS_MESSAGE = "Change in progress…"


class WriteLease:
    """One reservation, with an atomic boundary before its thread begins."""

    def __init__(self, coordinator: WriteCoordinator) -> None:
        self.finished = Event()
        self._coordinator = coordinator
        self._started = False

    def start(self) -> bool:
        with self._coordinator._lock:
            if self._started or self.finished.is_set() or self._coordinator._pending is not self:
                return False
            self._started = True
            return True

    def cancel_if_unstarted(self) -> bool:
        with self._coordinator._lock:
            if self._started or self.finished.is_set():
                return False
            changed = self._finish_locked()
        if changed:
            self._coordinator._notify()
        return True

    def finish(self) -> None:
        with self._coordinator._lock:
            changed = self._finish_locked()
        if changed:
            self._coordinator._notify()

    def _finish_locked(self) -> bool:
        self.finished.set()
        if self._coordinator._pending is self:
            self._coordinator._pending = None
            return True
        return False


class WriteCoordinator:
    """Reject overlapping UI changes without waiting, queuing, or retrying."""

    def __init__(self, *, on_change: Callable[[], None] | None = None) -> None:
        self._lock = Lock()
        self._pending: WriteLease | None = None
        self._on_change = on_change

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                # UI teardown/notification failure must not strand a lease or
                # prevent the protected operation from owning its real effect.
                logging.getLogger(__name__).debug("Write status notification unavailable")

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def reserve(self) -> WriteLease | None:
        with self._lock:
            if self._pending is not None:
                return None
            lease = WriteLease(self)
            self._pending = lease
        self._notify()
        return lease


_APP_COORDINATOR_LOCK = Lock()


def app_write_coordinator(app: App[Any]) -> WriteCoordinator:
    # Plain Textual apps used by reusable operational screens need this too.
    # Synchronize creation because root worker methods may be the first caller.
    with _APP_COORDINATOR_LOCK:
        coordinator = getattr(app, "_gucmail_write_coordinator", None)
        if not isinstance(coordinator, WriteCoordinator):
            owner = ref(app)

            def changed() -> None:
                current = owner()
                if current is not None:
                    current.post_message(WriteStateChanged())

            coordinator = WriteCoordinator(on_change=changed)
            app._gucmail_write_coordinator = coordinator  # type: ignore[attr-defined]
        return coordinator


class WriteBlocked(Message):
    """Safe visible rejection, with no mailbox identifiers or user content."""

    def __init__(self, hint: str = "") -> None:
        super().__init__()
        self.detail = PENDING_CHANGE_MESSAGE + (" " + hint if hint else "")


class WriteStateChanged(Message):
    """Recheck the actual lease; a delayed UI message must not revive old busy state."""


_App = TypeVar("_App", bound=App[Any])
_Args = ParamSpec("_Args")


def coordinated_write(
    *, blocked_hint: str = ""
) -> Callable[[Callable[Concatenate[_App, _Args], None]], Callable[Concatenate[_App, _Args], None]]:
    """Wrap a root worker's complete change, releasing in the real thread.

    Apply inside ``@work(thread=True)``. Cancellation before executor entry
    skips the callable; cancellation after entry does not release its lease.
    """

    def decorate(
        operation: Callable[Concatenate[_App, _Args], None],
    ) -> Callable[Concatenate[_App, _Args], None]:
        @wraps(operation)
        def run(app: _App, /, *args: _Args.args, **kwargs: _Args.kwargs) -> None:
            worker = get_current_worker()
            if worker.is_cancelled:
                return
            lease = app_write_coordinator(app).reserve()
            if lease is None:
                app.post_message(WriteBlocked(blocked_hint))
                return
            try:
                if lease.start() and not worker.is_cancelled:
                    operation(app, *args, **kwargs)
            finally:
                lease.finish()

        return run

    return decorate
