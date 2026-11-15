"""CLI-agnostic lifecycle and health primitives for the mailbox watcher.

This module deliberately has no dependency on the TUI or the Exchange adapter.
It owns only process coordination: one watcher per account/profile scope, durable
status and heartbeat records, cooperative stop/pause controls, and secret-free
health output.  The command layer supplies the actual monitoring callback.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from gucmail.constants import SAFE_MINIMUM_POLL_INTERVAL_SECONDS
from gucmail.errors import ConfigurationError, ConflictError, LocalStateError

_STATUS_SCHEMA_VERSION = 1
_LOCK_SCHEMA_VERSION = 1
_CONTROL_SCHEMA_VERSION = 1
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
PidChecker = Callable[[int], bool]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Watcher timestamps must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _validate_single_line(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ConfigurationError(f"{field_name} may not be empty.")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ConfigurationError(f"{field_name} may not contain control lines.")
    return normalized


def watcher_scope_id(account_id: str, profile: str) -> str:
    """Return a stable, privacy-preserving process scope identifier."""

    account = _validate_single_line(account_id, field_name="account_id").casefold()
    selected_profile = _validate_single_line(profile, field_name="profile").casefold()
    digest = hashlib.sha256(f"v1\0{account}\0{selected_profile}".encode()).hexdigest()
    return digest[:24]


def _is_password_argument(arguments: Sequence[str]) -> bool:
    for index, argument in enumerate(arguments):
        normalized = argument.casefold().strip()
        if normalized in {"--password", "-p"}:
            return True
        if normalized.startswith(("--password=", "password=", "gucmail_password=")):
            return True
        if index and arguments[index - 1].casefold().strip() in {"--password", "-p"}:
            return True
    return False


@dataclass(frozen=True, slots=True)
class WatcherProcessSpec:
    """Safe executable and process identity for a long-running watcher."""

    executable: Path
    arguments: tuple[str, ...]
    account_id: str
    profile: str = "default"
    working_directory: Path | None = None
    poll_interval_seconds: int = 300
    writable_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        executable_text = str(self.executable)
        _validate_single_line(executable_text, field_name="executable")
        if not self.executable.is_absolute():
            raise ConfigurationError("The watcher executable must be an absolute path.")
        _validate_single_line(self.account_id, field_name="account_id")
        _validate_single_line(self.profile, field_name="profile")
        if self.working_directory is not None and not self.working_directory.is_absolute():
            raise ConfigurationError("The watcher working directory must be an absolute path.")
        for path in self.writable_paths:
            _validate_single_line(str(path), field_name="writable path")
            if not path.is_absolute():
                raise ConfigurationError("Watcher writable paths must be absolute.")
        for argument in self.arguments:
            _validate_single_line(argument, field_name="watcher argument", allow_empty=True)
        if _is_password_argument(self.arguments):
            raise ConfigurationError("Passwords may not be supplied in watcher arguments.")
        if self.poll_interval_seconds < SAFE_MINIMUM_POLL_INTERVAL_SECONDS:
            raise ConfigurationError(
                "The watcher poll interval must be at least "
                f"{SAFE_MINIMUM_POLL_INTERVAL_SECONDS} seconds."
            )

    @property
    def scope_id(self) -> str:
        return watcher_scope_id(self.account_id, self.profile)

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


def start_watcher_process(spec: WatcherProcessSpec) -> int:
    """Start one detached watcher process without invoking a command shell."""

    if not spec.executable.exists() or not spec.executable.is_file():
        raise ConfigurationError("The watcher executable does not exist or is not a file.")
    options: dict[str, Any] = {
        "cwd": spec.working_directory,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": False,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(spec.command, **options)  # noqa: S603
    return int(process.pid)


class WatcherRunState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WatcherStatus:
    scope_id: str
    profile: str
    state: WatcherRunState
    pid: int
    instance_id: str
    started_at: datetime
    heartbeat_at: datetime
    connection_status: str = "starting"
    strategy: str = "unknown"
    folders: tuple[str, ...] = ()
    last_successful_check: datetime | None = None
    last_event_at: datetime | None = None
    next_poll_at: datetime | None = None
    backoff_seconds: float = 0.0
    messages_detected: int = 0
    notifications_delivered: int = 0
    notification_failures: int = 0
    current_error: str | None = None
    stopped_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.pid < 1:
            raise ValueError("pid must be positive")
        if self.scope_id != self.scope_id.casefold() or not self.scope_id.isalnum():
            raise ValueError("scope_id is invalid")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds may not be negative")
        for value in (
            self.started_at,
            self.heartbeat_at,
            self.last_successful_check,
            self.last_event_at,
            self.next_poll_at,
            self.stopped_at,
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError("Watcher timestamps must be timezone-aware.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _STATUS_SCHEMA_VERSION,
            "scope_id": self.scope_id,
            "profile": self.profile,
            "state": self.state.value,
            "pid": self.pid,
            "instance_id": self.instance_id,
            "started_at": _isoformat(self.started_at),
            "heartbeat_at": _isoformat(self.heartbeat_at),
            "connection_status": self.connection_status,
            "strategy": self.strategy,
            "folders": list(self.folders),
            "last_successful_check": _isoformat(self.last_successful_check),
            "last_event_at": _isoformat(self.last_event_at),
            "next_poll_at": _isoformat(self.next_poll_at),
            "backoff_seconds": self.backoff_seconds,
            "messages_detected": self.messages_detected,
            "notifications_delivered": self.notifications_delivered,
            "notification_failures": self.notification_failures,
            "current_error": self.current_error,
            "stopped_at": _isoformat(self.stopped_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WatcherStatus:
        if data.get("schema_version") != _STATUS_SCHEMA_VERSION:
            raise ValueError("unsupported watcher status schema")
        folders = data.get("folders", [])
        if not isinstance(folders, list) or not all(isinstance(item, str) for item in folders):
            raise ValueError("folders must be a string list")
        error = data.get("current_error")
        if error is not None and not isinstance(error, str):
            raise ValueError("current_error must be a string")
        return cls(
            scope_id=str(data["scope_id"]),
            profile=str(data["profile"]),
            state=WatcherRunState(str(data["state"])),
            pid=int(str(data["pid"])),
            instance_id=str(data["instance_id"]),
            started_at=_required_timestamp(data.get("started_at"), "started_at"),
            heartbeat_at=_required_timestamp(data.get("heartbeat_at"), "heartbeat_at"),
            connection_status=str(data.get("connection_status", "unknown")),
            strategy=str(data.get("strategy", "unknown")),
            folders=tuple(folders),
            last_successful_check=_parse_timestamp(data.get("last_successful_check")),
            last_event_at=_parse_timestamp(data.get("last_event_at")),
            next_poll_at=_parse_timestamp(data.get("next_poll_at")),
            backoff_seconds=float(str(data.get("backoff_seconds", 0.0))),
            messages_detected=int(str(data.get("messages_detected", 0))),
            notifications_delivered=int(str(data.get("notifications_delivered", 0))),
            notification_failures=int(str(data.get("notification_failures", 0))),
            current_error=error,
            stopped_at=_parse_timestamp(data.get("stopped_at")),
        )


def _required_timestamp(value: object, name: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"{name} is required")
    return parsed


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path, _PRIVATE_DIRECTORY_MODE)


def _atomic_write_json(path: Path, data: Mapping[str, object]) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with suppress(OSError):
            os.chmod(temporary, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with suppress(OSError):
            os.chmod(path, _PRIVATE_FILE_MODE)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalStateError(f"Could not read watcher state at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LocalStateError(f"Watcher state at {path} is not a JSON object.")
    return value


class WatcherStatusStore:
    """Persist one private status/control record per account/profile scope."""

    def __init__(self, directory: Path, scope_id: str) -> None:
        if not directory.is_absolute():
            raise ConfigurationError("Watcher state directory must be an absolute path.")
        if not scope_id.isalnum() or scope_id != scope_id.casefold():
            raise ConfigurationError("Watcher scope identifier is invalid.")
        self.directory = directory
        self.scope_id = scope_id
        self.status_path = directory / f"watcher-{scope_id}.status.json"
        self.lock_path = directory / f"watcher-{scope_id}.lock"
        self.control_path = directory / f"watcher-{scope_id}.control.json"

    def write(self, status: WatcherStatus) -> None:
        if status.scope_id != self.scope_id:
            raise LocalStateError("Refusing to write status for a different watcher scope.")
        _atomic_write_json(self.status_path, status.as_dict())

    def read(self) -> WatcherStatus | None:
        if not self.status_path.exists():
            return None
        try:
            return WatcherStatus.from_dict(_read_json(self.status_path))
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError(f"Watcher status is invalid: {exc}") from exc

    def request(self, action: str, *, requested_at: datetime | None = None) -> Path:
        normalized = action.casefold().strip()
        if normalized not in {"stop", "pause", "resume", "sync"}:
            raise ConfigurationError("Watcher control action must be stop, pause, resume, or sync.")
        _atomic_write_json(
            self.control_path,
            {
                "schema_version": _CONTROL_SCHEMA_VERSION,
                "scope_id": self.scope_id,
                "action": normalized,
                "requested_at": _isoformat(requested_at or _utc_now()),
                "request_id": uuid.uuid4().hex,
            },
        )
        return self.control_path

    def read_request(self) -> str | None:
        if not self.control_path.exists():
            return None
        data = _read_json(self.control_path)
        if data.get("schema_version") != _CONTROL_SCHEMA_VERSION:
            raise LocalStateError("Watcher control request has an unsupported schema.")
        if data.get("scope_id") != self.scope_id:
            raise LocalStateError("Watcher control request belongs to a different scope.")
        action = data.get("action")
        if action not in {"stop", "pause", "resume", "sync"}:
            raise LocalStateError("Watcher control request has an invalid action.")
        return str(action)

    def acknowledge_request(self) -> None:
        self.control_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class WatcherLockOwner:
    scope_id: str
    instance_id: str
    pid: int
    acquired_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": _LOCK_SCHEMA_VERSION,
            "scope_id": self.scope_id,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "acquired_at": _isoformat(self.acquired_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WatcherLockOwner:
        if data.get("schema_version") != _LOCK_SCHEMA_VERSION:
            raise ValueError("unsupported lock schema")
        return cls(
            scope_id=str(data["scope_id"]),
            instance_id=str(data["instance_id"]),
            pid=int(str(data["pid"])),
            acquired_at=_required_timestamp(data.get("acquired_at"), "acquired_at"),
        )


def process_is_alive(pid: int) -> bool:
    """Conservatively determine whether a process identifier is still active."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    """Query a Windows process handle without sending it a signal.

    Python's Windows implementation treats most values passed to ``os.kill`` as
    ``TerminateProcess`` requests, including zero on supported Python releases.
    A read-only process handle is therefore required for safe liveness checks.
    Access-denied and other indeterminate results fail closed as "alive" so they
    can never authorize a duplicate watcher.
    """

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            # ERROR_INVALID_PARAMETER is how OpenProcess reports a missing PID.
            return error != 87
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    except (AttributeError, ImportError, OSError):
        return True


def _pid_reused_since(pid: int, acquired_at: datetime) -> bool:
    """True when a live PID belongs to a process that started after the lock was taken.

    Windows recycles process IDs quickly. A crashed watcher's lock could otherwise name
    an unrelated new process and block every future watcher. Indeterminate results
    return False, so this can only ever release a lock, never authorize a duplicate.
    """

    if os.name != "nt" or pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (
            ctypes.POINTER(wintypes.FILETIME),
        ) * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return False
        finally:
            kernel32.CloseHandle(handle)
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        started = datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
        reference = acquired_at if acquired_at.tzinfo else acquired_at.replace(tzinfo=UTC)
        # A small allowance covers clock granularity for the genuine owner.
        return started > reference + timedelta(seconds=5)
    except (AttributeError, ImportError, OSError, OverflowError, ValueError):
        return False


class WatcherAlreadyRunningError(ConflictError):
    def __init__(self, owner: WatcherLockOwner | None) -> None:
        if owner is None:
            message = "Watcher startup is already in progress for this account/profile."
        else:
            message = f"Watcher is already running for this account/profile (PID {owner.pid})."
        super().__init__(message)
        self.owner = owner


class SingleInstanceLock:
    """Atomic portable lock file with PID-based stale-owner recovery."""

    def __init__(
        self,
        path: Path,
        scope_id: str,
        *,
        pid: int | None = None,
        instance_id: str | None = None,
        pid_checker: PidChecker = process_is_alive,
        clock: Clock = _utc_now,
        incomplete_record_grace_seconds: float = 5.0,
    ) -> None:
        self.path = path
        self.scope_id = scope_id
        self.pid = pid or os.getpid()
        self.instance_id = instance_id or uuid.uuid4().hex
        self.pid_checker = pid_checker
        self.clock = clock
        self.incomplete_record_grace_seconds = incomplete_record_grace_seconds
        self.owner: WatcherLockOwner | None = None

    def _read_owner(self) -> WatcherLockOwner | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return WatcherLockOwner.from_dict(data)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _record_is_too_new_to_recover(self) -> bool:
        try:
            age = max(0.0, self.clock().timestamp() - self.path.stat().st_mtime)
        except OSError:
            return True
        return age < self.incomplete_record_grace_seconds

    def _quarantine_stale(self) -> None:
        quarantine = self.path.with_name(f".{self.path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.replace(self.path, quarantine)
        except FileNotFoundError:
            return
        quarantine.unlink(missing_ok=True)

    def acquire(self) -> WatcherLockOwner:
        if self.owner is not None:
            return self.owner
        _ensure_private_directory(self.path.parent)
        for _attempt in range(4):
            owner = WatcherLockOwner(
                scope_id=self.scope_id,
                instance_id=self.instance_id,
                pid=self.pid,
                acquired_at=self.clock(),
            )
            payload = json.dumps(owner.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    _PRIVATE_FILE_MODE,
                )
            except FileExistsError:
                existing = self._read_owner()
                if existing is None:
                    if self._record_is_too_new_to_recover():
                        raise WatcherAlreadyRunningError(None) from None
                elif existing.scope_id != self.scope_id or (
                    self.pid_checker(existing.pid)
                    and not (
                        self.pid_checker is process_is_alive
                        and _pid_reused_since(existing.pid, existing.acquired_at)
                    )
                ):
                    raise WatcherAlreadyRunningError(existing) from None
                self._quarantine_stale()
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                with suppress(OSError):
                    os.chmod(self.path, _PRIVATE_FILE_MODE)
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            self.owner = owner
            return owner
        raise ConflictError("Could not acquire watcher lock because its owner changed repeatedly.")

    def release(self) -> None:
        if self.owner is None:
            return
        current = self._read_owner()
        if current is not None and current.instance_id == self.instance_id:
            self.path.unlink(missing_ok=True)
        self.owner = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


class ShutdownController:
    """Cooperative stop/pause state with reversible signal handlers."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._reason: str | None = None

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def request_stop(self, reason: str = "requested") -> None:
        self._reason = _validate_single_line(reason, field_name="shutdown reason")
        self._stop.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def wait(self, timeout: float) -> bool:
        return self._stop.wait(max(0.0, timeout))

    @contextmanager
    def install_signal_handlers(self) -> Iterator[ShutdownController]:
        if threading.current_thread() is not threading.main_thread():
            yield self
            return
        previous: dict[signal.Signals, Any] = {}

        def handle(signum: int, _frame: object) -> None:
            try:
                name = signal.Signals(signum).name
            except ValueError:
                name = str(signum)
            self.request_stop(f"signal {name}")

        watched = [signal.SIGINT]
        if hasattr(signal, "SIGTERM"):
            watched.append(signal.SIGTERM)
        try:
            for item in watched:
                previous[item] = signal.getsignal(item)
                signal.signal(item, handle)
            yield self
        finally:
            for item, handler in previous.items():
                signal.signal(item, handler)


@dataclass(frozen=True, slots=True)
class HeadlessCycleResult:
    connection_status: str = "connected"
    strategy: str = "unknown"
    folders: tuple[str, ...] = ()
    successful: bool = True
    events_detected: int = 0
    notifications_delivered: int = 0
    notification_failures: int = 0
    next_poll_at: datetime | None = None
    backoff_seconds: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class HeadlessWatcherRuntime:
    """Own watcher lifecycle state while a caller performs monitoring cycles."""

    spec: WatcherProcessSpec
    store: WatcherStatusStore
    controller: ShutdownController = field(default_factory=ShutdownController)
    clock: Clock = _utc_now
    pid: int = field(default_factory=os.getpid)
    pid_checker: PidChecker = process_is_alive
    _lock: SingleInstanceLock | None = field(default=None, init=False, repr=False)
    _status: WatcherStatus | None = field(default=None, init=False, repr=False)
    _sync_requested: bool = field(default=False, init=False, repr=False)

    @property
    def status(self) -> WatcherStatus:
        if self._status is None:
            raise LocalStateError("Watcher runtime has not been started.")
        return self._status

    def start(self) -> WatcherStatus:
        if self._lock is not None:
            return self.status
        lock = SingleInstanceLock(
            self.store.lock_path,
            self.spec.scope_id,
            pid=self.pid,
            pid_checker=self.pid_checker,
            clock=self.clock,
        )
        owner = lock.acquire()
        now = self.clock()
        self._lock = lock
        self._status = WatcherStatus(
            scope_id=self.spec.scope_id,
            profile=self.spec.profile,
            state=WatcherRunState.STARTING,
            pid=self.pid,
            instance_id=owner.instance_id,
            started_at=now,
            heartbeat_at=now,
        )
        self.store.write(self._status)
        return self._status

    def _replace_status(self, **changes: object) -> WatcherStatus:
        current = self.status
        values = asdict(current)
        values.update(changes)
        updated = WatcherStatus(**values)
        self._status = updated
        self.store.write(updated)
        return updated

    def heartbeat(self, result: HeadlessCycleResult | None = None) -> WatcherStatus:
        now = self.clock()
        current = self.status
        changes: dict[str, object] = {"heartbeat_at": now}
        if result is None:
            changes["state"] = WatcherRunState.PAUSED if self.controller.paused else current.state
        else:
            changes.update(
                state=(
                    WatcherRunState.RUNNING if result.successful else WatcherRunState.RECONNECTING
                ),
                connection_status=result.connection_status,
                strategy=result.strategy,
                folders=result.folders,
                last_successful_check=(now if result.successful else current.last_successful_check),
                last_event_at=(now if result.events_detected else current.last_event_at),
                next_poll_at=result.next_poll_at,
                backoff_seconds=result.backoff_seconds,
                messages_detected=current.messages_detected + result.events_detected,
                notifications_delivered=(
                    current.notifications_delivered + result.notifications_delivered
                ),
                notification_failures=(
                    current.notification_failures + result.notification_failures
                ),
                current_error=result.error,
            )
        return self._replace_status(**changes)

    def consume_control_request(self) -> str | None:
        action = self.store.read_request()
        if action is None:
            return None
        if action == "stop":
            self.controller.request_stop("remote stop request")
            self._replace_status(state=WatcherRunState.STOPPING, heartbeat_at=self.clock())
        elif action == "pause":
            self.controller.pause()
            self._replace_status(state=WatcherRunState.PAUSED, heartbeat_at=self.clock())
        elif action == "resume":
            self.controller.resume()
            self._replace_status(state=WatcherRunState.RUNNING, heartbeat_at=self.clock())
        else:
            # A sync request wakes the existing single-instance process and asks the
            # cycle loop to run immediately.  It does not create a second Exchange
            # session and it deliberately does not resume a paused watcher.
            self._sync_requested = True
            self._replace_status(next_poll_at=self.clock(), heartbeat_at=self.clock())
        self.store.acknowledge_request()
        return action

    def consume_sync_request(self) -> bool:
        requested = self._sync_requested
        self._sync_requested = False
        return requested

    def stop(self, *, error: str | None = None) -> WatcherStatus:
        if self._status is None:
            raise LocalStateError("Watcher runtime has not been started.")
        now = self.clock()
        retained_error = error if error is not None else self._status.current_error
        final = self._replace_status(
            state=WatcherRunState.FAILED if error else WatcherRunState.STOPPED,
            connection_status="disconnected",
            heartbeat_at=now,
            stopped_at=now,
            current_error=retained_error,
            next_poll_at=None,
            backoff_seconds=0.0,
        )
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        return final

    @contextmanager
    def running(self) -> Iterator[HeadlessWatcherRuntime]:
        self.start()
        try:
            with self.controller.install_signal_handlers():
                yield self
        except BaseException as exc:
            self.stop(error=f"{type(exc).__name__}: watcher terminated unexpectedly")
            raise
        else:
            self.stop()


class HealthLevel(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class WatcherHealth:
    status: HealthLevel
    watcher: str
    process_alive: bool
    exchange: str
    strategy: str
    profile: str | None
    pid: int | None
    uptime_seconds: int | None
    heartbeat_seconds_ago: int | None
    last_sync_seconds_ago: int | None
    notification_failures: int
    current_error: str | None
    checks: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "watcher": self.watcher,
            "process_alive": self.process_alive,
            "exchange": self.exchange,
            "strategy": self.strategy,
            "profile": self.profile,
            "pid": self.pid,
            "uptime_seconds": self.uptime_seconds,
            "heartbeat_seconds_ago": self.heartbeat_seconds_ago,
            "last_sync_seconds_ago": self.last_sync_seconds_ago,
            "notification_failures": self.notification_failures,
            "current_error": self.current_error,
            "checks": list(self.checks),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _seconds_since(now: datetime, value: datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def evaluate_watcher_health(
    store: WatcherStatusStore,
    *,
    now: datetime | None = None,
    heartbeat_timeout_seconds: int = 180,
    pid_checker: PidChecker = process_is_alive,
    configuration_errors: Sequence[str] = (),
) -> WatcherHealth:
    """Build mailbox-content-free JSON health data for local status commands."""

    current_time = (now or _utc_now()).astimezone(UTC)
    checks: list[dict[str, object]] = []
    try:
        status = store.read()
    except LocalStateError as exc:
        checks.append({"name": "status_file", "ok": False, "detail": str(exc)})
        return WatcherHealth(
            status=HealthLevel.UNHEALTHY,
            watcher="unknown",
            process_alive=False,
            exchange="unknown",
            strategy="unknown",
            profile=None,
            pid=None,
            uptime_seconds=None,
            heartbeat_seconds_ago=None,
            last_sync_seconds_ago=None,
            notification_failures=0,
            current_error="Watcher status is unreadable.",
            checks=tuple(checks),
        )
    if status is None:
        checks.append({"name": "watcher_process", "ok": False, "detail": "not started"})
        return WatcherHealth(
            status=HealthLevel.DEGRADED,
            watcher="stopped",
            process_alive=False,
            exchange="disconnected",
            strategy="unknown",
            profile=None,
            pid=None,
            uptime_seconds=None,
            heartbeat_seconds_ago=None,
            last_sync_seconds_ago=None,
            notification_failures=0,
            current_error=None,
            checks=tuple(checks),
        )

    alive = pid_checker(status.pid)
    heartbeat_age = _seconds_since(current_time, status.heartbeat_at)
    stale = heartbeat_age is None or heartbeat_age > heartbeat_timeout_seconds
    stopped = status.state in {WatcherRunState.STOPPED, WatcherRunState.FAILED}
    checks.append({"name": "watcher_process", "ok": alive or stopped, "detail": status.state.value})
    checks.append(
        {
            "name": "heartbeat",
            "ok": not stale or stopped,
            "detail": heartbeat_age,
        }
    )
    for error in configuration_errors:
        checks.append({"name": "configuration", "ok": False, "detail": str(error)})

    process_failed = not stopped and (not alive or stale)
    if configuration_errors or process_failed or status.state is WatcherRunState.FAILED:
        level = HealthLevel.UNHEALTHY
    elif stopped or status.current_error or status.notification_failures:
        level = HealthLevel.DEGRADED
    else:
        level = HealthLevel.HEALTHY
    watcher_state = (
        "stopped" if stopped else ("stale" if stale or not alive else status.state.value)
    )
    return WatcherHealth(
        status=level,
        watcher=watcher_state,
        process_alive=alive and not stopped,
        exchange=status.connection_status,
        strategy=status.strategy,
        profile=status.profile,
        pid=status.pid,
        uptime_seconds=_seconds_since(current_time, status.started_at),
        heartbeat_seconds_ago=heartbeat_age,
        last_sync_seconds_ago=_seconds_since(current_time, status.last_successful_check),
        notification_failures=status.notification_failures,
        current_error=status.current_error,
        checks=tuple(checks),
    )


@dataclass(frozen=True, slots=True)
class HeadlessValidationResult:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def require_valid(self) -> HeadlessValidationResult:
        if not self.valid:
            raise ConfigurationError("Headless configuration is invalid: " + "; ".join(self.errors))
        return self


def validate_headless_configuration(
    *,
    account_id: str | None,
    profile: str,
    poll_interval_seconds: int,
    executable: Path,
    state_directory: Path,
    log_directory: Path,
    credentials_available: bool,
    provider_names: Sequence[str] = (),
    expert_allow_fast_polling: bool = False,
) -> HeadlessValidationResult:
    """Validate deployability without opening credentials or touching the filesystem."""

    errors: list[str] = []
    warnings: list[str] = []
    if not account_id:
        errors.append("No account is configured.")
    else:
        try:
            _validate_single_line(account_id, field_name="account_id")
        except ConfigurationError as exc:
            errors.append(str(exc))
    try:
        _validate_single_line(profile, field_name="profile")
    except ConfigurationError as exc:
        errors.append(str(exc))
    if poll_interval_seconds < SAFE_MINIMUM_POLL_INTERVAL_SECONDS and not expert_allow_fast_polling:
        errors.append(
            f"Polling faster than {SAFE_MINIMUM_POLL_INTERVAL_SECONDS} seconds is disabled."
        )
    if not executable.is_absolute():
        errors.append("Watcher executable path is not absolute.")
    elif not executable.is_file():
        errors.append("Watcher executable does not exist or is not a file.")
    if not state_directory.is_absolute():
        errors.append("Watcher state directory path is not absolute.")
    if not log_directory.is_absolute():
        errors.append("Watcher log directory path is not absolute.")
    if not credentials_available:
        errors.append("No non-interactive credential source is available.")
    if not provider_names:
        warnings.append("No notification provider is enabled; events will only update local state.")
    for provider in provider_names:
        try:
            _validate_single_line(provider, field_name="provider name")
        except ConfigurationError as exc:
            errors.append(str(exc))
    return HeadlessValidationResult(not errors, tuple(errors), tuple(warnings))


def run_headless_cycles(
    runtime: HeadlessWatcherRuntime,
    cycle: Callable[[], HeadlessCycleResult],
    *,
    max_cycles: int | None = None,
    monotonic: MonotonicClock = time.monotonic,
    tick: Callable[[], None] | None = None,
    tick_seconds: float = 30.0,
) -> int:
    """Run cooperative cycles; return the number attempted.

    The monitoring service decides the delay between mail checks; this loop handles
    pause/stop requests and keeps the process record current. ``tick`` (sending due
    scheduled mail) runs after each check and every ``tick_seconds`` while waiting, so
    a long mail interval doesn't delay scheduled messages.
    """

    attempted = 0
    with runtime.running():
        while not runtime.controller.stop_requested:
            runtime.consume_control_request()
            if runtime.controller.stop_requested:
                break
            if runtime.controller.paused:
                runtime.heartbeat()
                runtime.controller.wait(1.0)
                continue
            runtime.consume_sync_request()
            started = monotonic()
            result = cycle()
            runtime.heartbeat(result)
            attempted += 1
            if tick is not None:
                tick()
            if max_cycles is not None and attempted >= max_cycles:
                break
            elapsed = max(0.0, monotonic() - started)
            delay = max(0.0, result.backoff_seconds or runtime.spec.poll_interval_seconds)
            remaining = max(0.0, delay - elapsed)
            heartbeat_due = monotonic() + 30.0
            tick_due = monotonic() + tick_seconds
            while remaining > 0 and not runtime.controller.stop_requested:
                before_wait = monotonic()
                runtime.controller.wait(min(1.0, remaining))
                after_wait = monotonic()
                remaining = max(0.0, remaining - max(0.0, after_wait - before_wait))
                action = runtime.consume_control_request()
                if action == "sync" and not runtime.controller.paused:
                    runtime.consume_sync_request()
                    break
                if after_wait >= heartbeat_due and not runtime.controller.stop_requested:
                    runtime.heartbeat()
                    heartbeat_due = after_wait + 30.0
                if tick is not None and after_wait >= tick_due and not runtime.controller.paused:
                    tick()
                    tick_due = after_wait + tick_seconds
    return attempted
