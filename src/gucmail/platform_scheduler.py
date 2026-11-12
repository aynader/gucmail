"""Generate and explicitly install operating-system scheduler integrations.

Generation is pure and is the default.  Applying or removing a plan requires
both ``dry_run=False`` and ``confirmed=True``; callers can inject a command
runner for tests.  Commands are always argument vectors and never use a shell.
"""

from __future__ import annotations

import html
import os
import plistlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from gucmail.config import AppPaths
from gucmail.errors import ConfigurationError, ConfirmationRequiredError, LocalStateError
from gucmail.windows_identity import (
    current_windows_user_sid,
    validate_windows_sid,
    windowless_python,
)


class SchedulerPlatform(StrEnum):
    WINDOWS = "windows"
    SYSTEMD = "systemd"
    LAUNCHD = "launchd"


@dataclass(frozen=True, slots=True)
class SchedulerArtifact:
    path: Path
    content: str
    mode: int = 0o600


@dataclass(frozen=True, slots=True)
class SchedulerPlan:
    platform: SchedulerPlatform
    label: str
    interval_seconds: int
    arguments: tuple[str, ...]
    artifacts: tuple[SchedulerArtifact, ...]
    install_commands: tuple[tuple[str, ...], ...]
    uninstall_commands: tuple[tuple[str, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "label": self.label,
            "interval_seconds": self.interval_seconds,
            "arguments": list(self.arguments),
            "artifacts": [
                {"path": str(item.path), "content": item.content, "mode": oct(item.mode)}
                for item in self.artifacts
            ],
            "install_commands": [list(item) for item in self.install_commands],
            "uninstall_commands": [list(item) for item in self.uninstall_commands],
            "dry_run": True,
        }


@dataclass(frozen=True, slots=True)
class SchedulerApplyResult:
    plan: SchedulerPlan
    dry_run: bool
    written: tuple[Path, ...] = ()
    commands_run: tuple[tuple[str, ...], ...] = ()


CommandRunner = Callable[..., object]


def detect_scheduler_platform(platform_name: str | None = None) -> SchedulerPlatform:
    raw = (platform_name or sys.platform).casefold()
    if raw.startswith("win") or raw == "windows":
        return SchedulerPlatform.WINDOWS
    if raw in {"darwin", "mac", "macos", "launchd"}:
        return SchedulerPlatform.LAUNCHD
    if raw.startswith("linux") or raw == "systemd":
        return SchedulerPlatform.SYSTEMD
    raise ConfigurationError(f"No scheduler integration is available for platform: {raw}")


def _validate_arguments(arguments: Iterable[str]) -> tuple[str, ...]:
    result = tuple(str(item) for item in arguments)
    if not result or not result[0]:
        raise ConfigurationError("A scheduler executable is required.")
    if any(not item or "\x00" in item or "\n" in item or "\r" in item for item in result):
        raise ConfigurationError("Scheduler arguments may not be empty or contain control lines.")
    return result


def default_scheduler_arguments(python_executable: str | Path | None = None) -> tuple[str, ...]:
    executable = str(python_executable or sys.executable)
    return (executable, "-m", "gucmail", "--quiet", "scheduler", "run-once")


def bound_scheduler_arguments(paths: AppPaths) -> tuple[str, ...]:
    """Preserve the selected queue after login without embedding any credentials."""
    return (
        sys.executable,
        "-m",
        "gucmail.scheduler_entry",
        "--config",
        str(paths.config_file.resolve()),
        "--data-dir",
        str(paths.data_dir.resolve()),
        "--cache-dir",
        str(paths.cache_dir.resolve()),
        "--log-dir",
        str(paths.log_dir.resolve()),
    )


def _systemd_quote(value: str) -> str:
    # systemd performs its own percent expansion even inside quotes.
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _windows_task_xml(arguments: Sequence[str], *, interval_seconds: int, user_sid: str) -> str:
    executable, *rest = arguments
    command_line = subprocess.list2cmdline(rest)
    minutes = max(1, (interval_seconds + 59) // 60)
    command = html.escape(windowless_python(executable), quote=False)
    argument_text = html.escape(command_line, quote=False)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Run due GUC Mail local scheduled messages.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT{minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_sid}</UserId>
      <LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{command}</Command><Arguments>{argument_text}</Arguments></Exec>
  </Actions>
</Task>
"""


def _systemd_units(arguments: Sequence[str], *, interval_seconds: int) -> tuple[str, str]:
    command = " ".join(_systemd_quote(item) for item in arguments)
    service = f"""[Unit]
Description=Send due GUC Mail scheduled messages
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={command}
TimeoutStartSec=15min
Nice=10
"""
    timer = f"""[Unit]
Description=Check for due GUC Mail messages

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval_seconds}s
Persistent=true
AccuracySec=15s
Unit=gucmail-scheduler.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def _launchd_plist(arguments: Sequence[str], *, label: str, interval_seconds: int) -> str:
    document = {
        "Label": label,
        "ProgramArguments": list(arguments),
        "StartInterval": interval_seconds,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def generate_scheduler_plan(
    *,
    platform_name: str | None = None,
    arguments: Iterable[str] | None = None,
    interval_seconds: int = 300,
    output_directory: Path | None = None,
    label: str = "org.gucmail.scheduler",
    windows_user_sid: str | None = None,
) -> SchedulerPlan:
    """Return scheduler files and commands without touching the filesystem."""

    if interval_seconds < 60:
        raise ConfigurationError("Scheduler interval must be at least 60 seconds.")
    if not label or any(character in label for character in "\r\n\x00"):
        raise ConfigurationError("Scheduler label is invalid.")
    platform = detect_scheduler_platform(platform_name)
    argv = _validate_arguments(arguments or default_scheduler_arguments())
    artifacts: tuple[SchedulerArtifact, ...]
    install_commands: tuple[tuple[str, ...], ...]
    uninstall_commands: tuple[tuple[str, ...], ...]

    if platform is SchedulerPlatform.WINDOWS:
        user_sid = validate_windows_sid(
            current_windows_user_sid() if windows_user_sid is None else windows_user_sid
        )
        local_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        directory = output_directory or (local_data / "GUC Mail CLI" / "scheduler")
        path = directory / "gucmail-scheduler.xml"
        artifacts = (
            SchedulerArtifact(
                path=path,
                content=_windows_task_xml(
                    argv, interval_seconds=interval_seconds, user_sid=user_sid
                ),
            ),
        )
        install_commands = (
            (
                "schtasks.exe",
                "/Create",
                "/TN",
                "GUC Mail Scheduler",
                "/XML",
                str(path),
                "/F",
            ),
        )
        uninstall_commands = (("schtasks.exe", "/Delete", "/TN", "GUC Mail Scheduler", "/F"),)
    elif platform is SchedulerPlatform.SYSTEMD:
        directory = output_directory or (Path.home() / ".config" / "systemd" / "user")
        service, timer = _systemd_units(argv, interval_seconds=interval_seconds)
        artifacts = (
            SchedulerArtifact(directory / "gucmail-scheduler.service", service),
            SchedulerArtifact(directory / "gucmail-scheduler.timer", timer),
        )
        install_commands = (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "gucmail-scheduler.timer"),
        )
        uninstall_commands = (
            ("systemctl", "--user", "disable", "--now", "gucmail-scheduler.timer"),
            ("systemctl", "--user", "daemon-reload"),
        )
    else:
        directory = output_directory or (Path.home() / "Library" / "LaunchAgents")
        path = directory / f"{label}.plist"
        artifacts = (
            SchedulerArtifact(
                path,
                _launchd_plist(argv, label=label, interval_seconds=interval_seconds),
            ),
        )
        domain_target = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/$UID"
        install_commands = (
            ("launchctl", "bootstrap", domain_target, str(path)),
            ("launchctl", "enable", f"{domain_target}/{label}"),
        )
        uninstall_commands = (("launchctl", "bootout", domain_target, str(path)),)
    return SchedulerPlan(
        platform=platform,
        label=label,
        interval_seconds=interval_seconds,
        arguments=argv,
        artifacts=artifacts,
        install_commands=install_commands,
        uninstall_commands=uninstall_commands,
    )


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with suppress(OSError):
            os.chmod(temporary, mode)
        encoding = (
            "utf-16" if content.startswith('<?xml version="1.0" encoding="UTF-16"?>') else "utf-8"
        )
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with suppress(OSError):
            os.chmod(path, mode)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _run_commands(
    commands: Iterable[tuple[str, ...]],
    *,
    runner: CommandRunner,
) -> tuple[tuple[str, ...], ...]:
    completed: list[tuple[str, ...]] = []
    for command in commands:
        try:
            runner(command, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            rendered = " ".join(command)
            raise LocalStateError(f"Scheduler command failed ({rendered}): {exc}") from exc
        completed.append(command)
    return tuple(completed)


def install_scheduler(
    plan: SchedulerPlan | None = None,
    *,
    confirmed: bool = False,
    dry_run: bool = True,
    runner: CommandRunner = subprocess.run,
) -> SchedulerApplyResult:
    """Apply a generated plan only after two explicit opt-ins."""

    selected = plan or generate_scheduler_plan()
    if dry_run:
        return SchedulerApplyResult(selected, dry_run=True)
    if not confirmed:
        raise ConfirmationRequiredError(
            "Installing an OS scheduler requires explicit confirmation."
        )
    written: list[Path] = []
    try:
        for artifact in selected.artifacts:
            _atomic_write(artifact.path, artifact.content, artifact.mode)
            written.append(artifact.path)
        commands = _run_commands(selected.install_commands, runner=runner)
    except Exception:
        # Leave generated files available for diagnosis, but never run more
        # installation commands after the first failure.
        raise
    return SchedulerApplyResult(
        selected,
        dry_run=False,
        written=tuple(written),
        commands_run=commands,
    )


def uninstall_scheduler(
    plan: SchedulerPlan | None = None,
    *,
    confirmed: bool = False,
    dry_run: bool = True,
    runner: CommandRunner = subprocess.run,
) -> SchedulerApplyResult:
    selected = plan or generate_scheduler_plan()
    if dry_run:
        return SchedulerApplyResult(selected, dry_run=True)
    if not confirmed:
        raise ConfirmationRequiredError("Removing an OS scheduler requires explicit confirmation.")
    commands = _run_commands(selected.uninstall_commands, runner=runner)
    removed: list[Path] = []
    for artifact in selected.artifacts:
        try:
            if artifact.path.exists():
                artifact.path.unlink()
                removed.append(artifact.path)
        except OSError as exc:
            raise LocalStateError(
                f"Scheduler was disabled, but its file could not be removed: {artifact.path}"
            ) from exc
    return SchedulerApplyResult(
        selected,
        dry_run=False,
        written=tuple(removed),
        commands_run=commands,
    )


# Concise aliases for CLI composition.
generate_plan = generate_scheduler_plan
install = install_scheduler
uninstall = uninstall_scheduler
