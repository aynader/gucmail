"""Pure startup/service-manager plans for the headless mailbox watcher.

Generating or inspecting a plan never invokes Task Scheduler or systemd.  All
external actions are represented as argument vectors so a CLI can show them,
ask for confirmation, and execute them with ``shell=False`` later.  This keeps
normal tests and dry runs free of operating-system mutations.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any

from defusedxml.ElementTree import fromstring as safe_fromstring

from gucmail.errors import ConfigurationError
from gucmail.headless import WatcherProcessSpec
from gucmail.windows_identity import (
    current_windows_user_sid,
    validate_windows_sid,
    windowless_python,
)


class StartupPlatform(StrEnum):
    WINDOWS_TASK = "windows-task-scheduler"
    SYSTEMD_USER = "systemd-user"
    SYSTEMD_SYSTEM = "systemd-system"


class StartupOperation(StrEnum):
    ENABLE = "enable"
    DISABLE = "disable"
    REPAIR = "repair"
    UNINSTALL = "uninstall"
    NOOP = "noop"


class StartupIssue(StrEnum):
    INSPECTION_FAILED = "inspection-failed"
    NOT_INSTALLED = "not-installed"
    DISABLED = "disabled"
    DEFINITION_DRIFT = "definition-drift"
    BROKEN_EXECUTABLE = "broken-executable"
    DUPLICATE_ENTRIES = "duplicate-entries"
    UNREADABLE_DEFINITION = "unreadable-definition"


@dataclass(frozen=True, slots=True)
class StartupArtifact:
    path: Path
    content: str
    mode: int = 0o600

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def as_dict(self, *, include_content: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "path": str(self.path),
            "mode": oct(self.mode),
            "sha256": self.sha256,
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True, slots=True)
class StartupCommand:
    """One future process invocation represented as inert data."""

    argv: tuple[str, ...]
    purpose: str
    ignore_not_found: bool = False

    def __post_init__(self) -> None:
        if not self.argv:
            raise ConfigurationError("Startup command may not be empty.")
        for value in self.argv:
            if not value or any(character in value for character in ("\x00", "\r", "\n")):
                raise ConfigurationError("Startup command arguments may not contain control lines.")

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "purpose": self.purpose,
            "ignore_not_found": self.ignore_not_found,
            "shell": False,
        }


@dataclass(frozen=True, slots=True)
class StartupPlan:
    platform: StartupPlatform
    scope_id: str
    service_name: str
    process: WatcherProcessSpec
    startup_delay_seconds: int
    restart_delay_seconds: int
    restart_attempts: int
    artifacts: tuple[StartupArtifact, ...]
    create_commands: tuple[StartupCommand, ...]
    enable_commands: tuple[StartupCommand, ...]
    disable_commands: tuple[StartupCommand, ...]
    inspect_commands: tuple[StartupCommand, ...]
    uninstall_commands: tuple[StartupCommand, ...]

    @property
    def definition_sha256(self) -> str:
        digest = hashlib.sha256()
        for artifact in self.artifacts:
            digest.update(str(artifact.path).encode("utf-8"))
            digest.update(b"\0")
            digest.update(artifact.content.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def as_dict(self, *, include_content: bool = True) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "scope_id": self.scope_id,
            "service_name": self.service_name,
            "startup_delay_seconds": self.startup_delay_seconds,
            "restart_delay_seconds": self.restart_delay_seconds,
            "restart_attempts": self.restart_attempts,
            "process": {
                "executable": str(self.process.executable),
                "arguments": list(self.process.arguments),
                "working_directory": (
                    str(self.process.working_directory)
                    if self.process.working_directory is not None
                    else None
                ),
                "profile": self.process.profile,
                "scope_id": self.process.scope_id,
                "poll_interval_seconds": self.process.poll_interval_seconds,
            },
            "definition_sha256": self.definition_sha256,
            "artifacts": [item.as_dict(include_content=include_content) for item in self.artifacts],
            "create_commands": [item.as_dict() for item in self.create_commands],
            "enable_commands": [item.as_dict() for item in self.enable_commands],
            "disable_commands": [item.as_dict() for item in self.disable_commands],
            "inspect_commands": [item.as_dict() for item in self.inspect_commands],
            "uninstall_commands": [item.as_dict() for item in self.uninstall_commands],
        }


@dataclass(frozen=True, slots=True)
class InstalledStartupSnapshot:
    """Sanitized observations supplied by a platform-specific command adapter."""

    installed: bool
    enabled: bool = False
    definitions: tuple[str, ...] = ()
    executable_exists: bool | None = None
    detail: str | None = None
    state_known: bool = True


@dataclass(frozen=True, slots=True)
class StartupInspection:
    platform: StartupPlatform
    service_name: str
    installed: bool
    enabled: bool
    healthy: bool
    issues: tuple[StartupIssue, ...]
    expected_sha256: str
    installed_sha256: tuple[str, ...]
    detail: str | None = None
    state_known: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "service_name": self.service_name,
            "installed": self.installed,
            "enabled": self.enabled,
            "healthy": self.healthy,
            "issues": [item.value for item in self.issues],
            "expected_sha256": self.expected_sha256,
            "installed_sha256": list(self.installed_sha256),
            "detail": self.detail,
            "state_known": self.state_known,
        }


@dataclass(frozen=True, slots=True)
class StartupOperationPlan:
    operation: StartupOperation
    platform: StartupPlatform
    service_name: str
    requires_confirmation: bool
    can_apply: bool
    reason: str
    artifacts_to_write: tuple[StartupArtifact, ...] = ()
    artifact_paths_to_remove: tuple[Path, ...] = ()
    commands: tuple[StartupCommand, ...] = ()
    commands_after_removal: tuple[StartupCommand, ...] = ()

    @property
    def no_changes(self) -> bool:
        return (
            not self.artifacts_to_write
            and not self.artifact_paths_to_remove
            and not self.commands
            and not self.commands_after_removal
        )

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "platform": self.platform.value,
            "service_name": self.service_name,
            "requires_confirmation": self.requires_confirmation,
            "can_apply": self.can_apply,
            "reason": self.reason,
            "no_changes": self.no_changes,
            "artifacts_to_write": [
                item.as_dict(include_content=include_content) for item in self.artifacts_to_write
            ],
            "artifact_paths_to_remove": [str(item) for item in self.artifact_paths_to_remove],
            "commands": [item.as_dict() for item in self.commands],
            "commands_after_removal": [item.as_dict() for item in self.commands_after_removal],
        }


def detect_startup_platform(
    platform_name: str | None = None,
    *,
    system_service: bool = False,
) -> StartupPlatform:
    normalized = (platform_name or sys.platform).casefold()
    if normalized.startswith("win") or normalized in {"windows", "windows-task-scheduler"}:
        return StartupPlatform.WINDOWS_TASK
    if normalized.startswith("linux") or normalized.startswith("systemd"):
        return StartupPlatform.SYSTEMD_SYSTEM if system_service else StartupPlatform.SYSTEMD_USER
    raise ConfigurationError(
        f"Watcher startup integration is unavailable for platform: {normalized}"
    )


def default_watcher_arguments(profile: str = "default") -> tuple[str, ...]:
    if not profile or any(character in profile for character in ("\x00", "\r", "\n")):
        raise ConfigurationError("Monitoring profile name is invalid.")
    return (
        "--non-interactive",
        "headless",
        "run",
        "--profile",
        profile,
        "--forever",
    )


def build_watcher_process_spec(
    *,
    account_id: str,
    profile: str = "default",
    executable: Path | None = None,
    arguments: Sequence[str] | None = None,
    working_directory: Path | None = None,
    poll_interval_seconds: int = 300,
) -> WatcherProcessSpec:
    selected_executable = (executable or Path(sys.executable)).absolute()
    if arguments is not None:
        selected_arguments = tuple(arguments)
    elif selected_executable.stem.casefold().startswith(("python", "pypy")):
        selected_arguments = ("-m", "gucmail", *default_watcher_arguments(profile))
    else:
        selected_arguments = default_watcher_arguments(profile)
    return WatcherProcessSpec(
        executable=selected_executable,
        arguments=selected_arguments,
        account_id=account_id,
        profile=profile,
        working_directory=working_directory,
        poll_interval_seconds=poll_interval_seconds,
    )


def _duration(seconds: int) -> str:
    if seconds < 0:
        raise ConfigurationError("Startup and restart delays may not be negative.")
    return f"PT{seconds}S"


def _windows_path(path: Path) -> str:
    # ``Path`` on Windows already produces the right form.  PureWindowsPath also
    # normalizes deliberately supplied slash-separated Windows paths in tests.
    return str(PureWindowsPath(str(path)))


def _windows_task_xml(
    process: WatcherProcessSpec,
    *,
    startup_delay_seconds: int,
    restart_delay_seconds: int,
    restart_attempts: int,
    user_sid: str,
) -> str:
    arguments = subprocess.list2cmdline(list(process.arguments))
    executable = html.escape(windowless_python(_windows_path(process.executable)), quote=True)
    argument_text = html.escape(arguments, quote=True)
    working_directory = process.working_directory or process.executable.parent
    working_text = html.escape(_windows_path(working_directory), quote=True)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Run the GUC Mail mailbox watcher for this Windows user.</Description>
    <URI>\\GUC Mail CLI\\Watcher</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <UserId>{user_sid}</UserId>
      <Enabled>true</Enabled>
      <Delay>{_duration(startup_delay_seconds)}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>{_duration(restart_delay_seconds)}</Interval>
      <Count>{restart_attempts}</Count>
    </RestartOnFailure>
    <Hidden>true</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{executable}</Command>
      <Arguments>{argument_text}</Arguments>
      <WorkingDirectory>{working_text}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _systemd_quote(value: str) -> str:
    """Quote one systemd.exec argument without involving a shell."""

    # systemd performs percent specifier and dollar environment expansion even
    # without a shell.  Doubling retains literal user/path content.
    escaped = value.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_service(
    process: WatcherProcessSpec,
    *,
    platform: StartupPlatform,
    restart_delay_seconds: int,
    service_user: str | None,
) -> str:
    command = " ".join(_systemd_quote(value) for value in process.command)
    working_directory = process.working_directory or process.executable.parent
    user_line = ""
    if platform is StartupPlatform.SYSTEMD_SYSTEM:
        if not service_user:
            raise ConfigurationError("A systemd system service requires an explicit service user.")
        if not service_user.replace("-", "").replace("_", "").isalnum():
            raise ConfigurationError("The systemd service user is invalid.")
        user_line = f"User={service_user}\n"
    writable = "".join(
        f"ReadWritePaths={_systemd_quote(str(path))}\n" for path in process.writable_paths
    )
    return f"""[Unit]
Description=GUC Mail mailbox watcher ({process.scope_id[:8]})
Documentation=man:gucmail(1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}ExecStart={command}
WorkingDirectory={_systemd_quote(str(working_directory))}
Restart=on-failure
RestartSec={restart_delay_seconds}s
TimeoutStopSec=30s
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
{writable}UMask=0077

[Install]
WantedBy={"multi-user.target" if platform is StartupPlatform.SYSTEMD_SYSTEM else "default.target"}
"""


def _validate_plan_values(
    *,
    startup_delay_seconds: int,
    restart_delay_seconds: int,
    restart_attempts: int,
) -> None:
    if not 0 <= startup_delay_seconds <= 3600:
        raise ConfigurationError("Startup delay must be between 0 and 3600 seconds.")
    if not 1 <= restart_delay_seconds <= 3600:
        raise ConfigurationError("Restart delay must be between 1 and 3600 seconds.")
    if not 1 <= restart_attempts <= 999:
        raise ConfigurationError("Restart attempts must be between 1 and 999.")


def generate_startup_plan(
    process: WatcherProcessSpec,
    *,
    platform_name: str | None = None,
    system_service: bool = False,
    output_directory: Path | None = None,
    startup_delay_seconds: int = 30,
    restart_delay_seconds: int = 60,
    restart_attempts: int = 3,
    service_user: str | None = None,
    windows_user_sid: str | None = None,
) -> StartupPlan:
    """Generate deterministic Task Scheduler or systemd definitions and argv."""

    _validate_plan_values(
        startup_delay_seconds=startup_delay_seconds,
        restart_delay_seconds=restart_delay_seconds,
        restart_attempts=restart_attempts,
    )
    platform = detect_startup_platform(platform_name, system_service=system_service)
    scope_tag = process.scope_id[:8]
    artifacts: tuple[StartupArtifact, ...]
    create_commands: tuple[StartupCommand, ...]
    enable_commands: tuple[StartupCommand, ...]
    disable_commands: tuple[StartupCommand, ...]
    inspect_commands: tuple[StartupCommand, ...]
    uninstall_commands: tuple[StartupCommand, ...]

    if platform is StartupPlatform.WINDOWS_TASK:
        user_sid = validate_windows_sid(
            current_windows_user_sid() if windows_user_sid is None else windows_user_sid
        )
        base = (
            output_directory
            or Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "GUC Mail CLI"
            / "watcher"
        )
        service_name = f"GUC Mail Watcher ({scope_tag})"
        definition = StartupArtifact(
            base / f"gucmail-watcher-{scope_tag}.xml",
            _windows_task_xml(
                process,
                startup_delay_seconds=startup_delay_seconds,
                restart_delay_seconds=restart_delay_seconds,
                restart_attempts=restart_attempts,
                user_sid=user_sid,
            ),
        )
        artifacts = (definition,)
        create_commands = (
            StartupCommand(
                (
                    "schtasks.exe",
                    "/Create",
                    "/TN",
                    service_name,
                    "/XML",
                    str(definition.path),
                    "/F",
                ),
                "create or replace the confirmed watcher task",
            ),
        )
        enable_commands = (
            StartupCommand(
                ("schtasks.exe", "/Change", "/TN", service_name, "/ENABLE"),
                "enable the watcher task",
            ),
            # Start monitoring now instead of at the next sign-in. The task's
            # IgnoreNew policy makes this a no-op when a watcher is already running.
            StartupCommand(
                ("schtasks.exe", "/Run", "/TN", service_name),
                "start the watcher now",
            ),
        )
        disable_commands = (
            StartupCommand(
                ("schtasks.exe", "/Change", "/TN", service_name, "/DISABLE"),
                "disable the watcher task",
                ignore_not_found=True,
            ),
        )
        inspect_commands = (
            StartupCommand(
                ("schtasks.exe", "/Query", "/TN", service_name, "/XML"),
                "inspect the watcher task",
                ignore_not_found=True,
            ),
        )
        uninstall_commands = (
            StartupCommand(
                ("schtasks.exe", "/Delete", "/TN", service_name, "/F"),
                "remove the watcher task",
                ignore_not_found=True,
            ),
        )
    else:
        user_mode = platform is StartupPlatform.SYSTEMD_USER
        if output_directory is not None:
            base = output_directory
        elif user_mode:
            base = Path.home() / ".config" / "systemd" / "user"
        else:
            base = Path("/etc/systemd/system")
        service_name = f"gucmail-watcher-{scope_tag}.service"
        definition = StartupArtifact(
            base / service_name,
            _systemd_service(
                process,
                platform=platform,
                restart_delay_seconds=restart_delay_seconds,
                service_user=service_user,
            ),
            mode=0o644,
        )
        artifacts = (definition,)
        prefix = ("systemctl", "--user") if user_mode else ("systemctl",)
        create_commands = (
            StartupCommand((*prefix, "daemon-reload"), "reload systemd definitions"),
        )
        enable_commands = (
            StartupCommand(
                (*prefix, "enable", "--now", service_name),
                "enable the watcher service",
            ),
        )
        disable_commands = (
            StartupCommand(
                (*prefix, "disable", "--now", service_name),
                "disable the watcher service",
                ignore_not_found=True,
            ),
        )
        inspect_commands = (
            StartupCommand(
                (*prefix, "show", service_name, "--no-pager"),
                "inspect the watcher service",
                ignore_not_found=True,
            ),
        )
        uninstall_commands = (
            StartupCommand(
                (*prefix, "disable", "--now", service_name),
                "disable the watcher service",
                ignore_not_found=True,
            ),
            StartupCommand((*prefix, "daemon-reload"), "reload systemd after removal"),
        )
    return StartupPlan(
        platform=platform,
        scope_id=process.scope_id,
        service_name=service_name,
        process=process,
        startup_delay_seconds=startup_delay_seconds,
        restart_delay_seconds=restart_delay_seconds,
        restart_attempts=restart_attempts,
        artifacts=artifacts,
        create_commands=create_commands,
        enable_commands=enable_commands,
        disable_commands=disable_commands,
        inspect_commands=inspect_commands,
        uninstall_commands=uninstall_commands,
    )


def _definition_hash(content: str, platform: StartupPlatform) -> str:
    if platform is StartupPlatform.WINDOWS_TASK and len(content) <= 2_000_000:
        # Unreadable XML remains a definition mismatch.
        with contextlib.suppress(ET.ParseError, ValueError):
            content = json.dumps(_windows_task_semantics(content), sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Task Scheduler schema defaults: an installed task omits elements at these values.
_TASK_SETTING_DEFAULTS = {
    "MultipleInstancesPolicy": "IgnoreNew",
    "DisallowStartIfOnBatteries": "true",
    "StopIfGoingOnBatteries": "true",
    "AllowHardTerminate": "true",
    "StartWhenAvailable": "false",
    "RunOnlyIfNetworkAvailable": "false",
    "WakeToRun": "false",
    "ExecutionTimeLimit": "PT72H",
    "Priority": "7",
    "Hidden": "false",
    "Enabled": "true",
}
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def _duration_seconds(value: str) -> str:
    match = _ISO_DURATION_RE.match(value.strip())
    if not match:
        return value.strip()
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    return str(parts["d"] * 86_400 + parts["h"] * 3_600 + parts["m"] * 60 + parts["s"])


def _task_user(value: str) -> str:
    """Compare task identities as SIDs; Windows shows a logon trigger's user by name."""

    if not value or value.upper().startswith("S-1-"):
        return value.upper()
    current = "\\".join((os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")))
    if value.casefold() == current.casefold():
        with contextlib.suppress(Exception):
            return current_windows_user_sid().upper()
    return value.casefold()


def _windows_task_semantics(content: str) -> dict[str, Any]:
    """What a task does, independent of how Task Scheduler re-serializes it.

    On import Windows reorders elements, drops default-valued ones, rewrites
    ``PT60S`` as ``PT1M``, shows the trigger user as ``DOMAIN\\user`` instead of the
    SID, and replaces the URI with the task name. None of that changes the action,
    trigger, principal or settings, so it must not be reported as drift.
    """

    if content.lstrip().startswith("<?xml"):
        content = content.split("?>", 1)[1]
    root = safe_fromstring(content.strip())

    def local(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def child(parent: ET.Element | None, name: str) -> ET.Element | None:
        if parent is None:
            return None
        return next((item for item in parent if local(item) == name), None)

    def text(parent: ET.Element | None, name: str, default: str = "") -> str:
        found = child(parent, name)
        return (found.text or "").strip() if found is not None else default

    settings = child(root, "Settings")
    normalized_settings = {
        name: text(settings, name, default) for name, default in _TASK_SETTING_DEFAULTS.items()
    }
    normalized_settings["ExecutionTimeLimit"] = _duration_seconds(
        normalized_settings["ExecutionTimeLimit"]
    )
    restart = child(settings, "RestartOnFailure")
    principal = child(child(root, "Principals"), "Principal")
    triggers = child(root, "Triggers")
    actions = child(root, "Actions")
    return {
        "actions": [
            [text(item, "Command"), text(item, "Arguments"), text(item, "WorkingDirectory")]
            for item in (actions if actions is not None else ())
            if local(item) == "Exec"
        ],
        "triggers": [
            [
                local(item),
                text(item, "Enabled", "true"),
                _duration_seconds(text(item, "Delay")),
                _task_user(text(item, "UserId")),
            ]
            for item in (triggers if triggers is not None else ())
        ],
        "principal": [
            _task_user(text(principal, "UserId")),
            text(principal, "LogonType"),
            text(principal, "RunLevel", "LeastPrivilege"),
        ],
        "settings": normalized_settings,
        "restart": [
            text(restart, "Count"),
            _duration_seconds(text(restart, "Interval")),
        ]
        if restart is not None
        else None,
    }


def inspect_startup(plan: StartupPlan, snapshot: InstalledStartupSnapshot) -> StartupInspection:
    """Compare sanitized installed state with a generated definition."""

    issues: list[StartupIssue] = []
    installed_hashes = tuple(
        _definition_hash(value, plan.platform) for value in snapshot.definitions
    )
    expected_hashes = tuple(
        _definition_hash(item.content, plan.platform) for item in plan.artifacts
    )
    if not snapshot.state_known:
        issues.append(StartupIssue.INSPECTION_FAILED)
    elif not snapshot.installed:
        issues.append(StartupIssue.NOT_INSTALLED)
    else:
        if not snapshot.enabled:
            issues.append(StartupIssue.DISABLED)
        if len(snapshot.definitions) > len(plan.artifacts):
            issues.append(StartupIssue.DUPLICATE_ENTRIES)
        if not snapshot.definitions:
            issues.append(StartupIssue.UNREADABLE_DEFINITION)
        elif installed_hashes[: len(expected_hashes)] != expected_hashes:
            issues.append(StartupIssue.DEFINITION_DRIFT)
    executable_exists = (
        snapshot.executable_exists
        if snapshot.executable_exists is not None
        else plan.process.executable.is_file()
    )
    if not executable_exists:
        issues.append(StartupIssue.BROKEN_EXECUTABLE)
    return StartupInspection(
        platform=plan.platform,
        service_name=plan.service_name,
        installed=snapshot.installed,
        enabled=snapshot.enabled,
        healthy=not issues,
        issues=tuple(dict.fromkeys(issues)),
        expected_sha256=plan.definition_sha256,
        installed_sha256=installed_hashes,
        detail=snapshot.detail,
        state_known=snapshot.state_known,
    )


def _unknown_startup_operation(
    plan: StartupPlan, inspection: StartupInspection, operation: StartupOperation
) -> StartupOperationPlan | None:
    if inspection.state_known:
        return None
    return StartupOperationPlan(
        operation,
        plan.platform,
        plan.service_name,
        requires_confirmation=False,
        can_apply=False,
        reason=(
            "Startup state could not be inspected. No change attempted. "
            "Refresh startup status and resolve the reported error before changing startup. "
            + (inspection.detail or "")
        ).strip(),
    )


def plan_enable_startup(
    plan: StartupPlan,
    inspection: StartupInspection,
) -> StartupOperationPlan:
    blocked = _unknown_startup_operation(plan, inspection, StartupOperation.ENABLE)
    if blocked is not None:
        return blocked
    if inspection.healthy:
        return StartupOperationPlan(
            StartupOperation.NOOP,
            plan.platform,
            plan.service_name,
            requires_confirmation=False,
            can_apply=True,
            reason="Startup monitoring is already installed, enabled, and current.",
        )
    if StartupIssue.BROKEN_EXECUTABLE in inspection.issues:
        return StartupOperationPlan(
            StartupOperation.ENABLE,
            plan.platform,
            plan.service_name,
            requires_confirmation=True,
            can_apply=False,
            reason=(
                "The configured watcher executable does not exist; reinstall or select it first."
            ),
        )
    installed_and_current = inspection.installed and not any(
        item in inspection.issues
        for item in (
            StartupIssue.DEFINITION_DRIFT,
            StartupIssue.DUPLICATE_ENTRIES,
            StartupIssue.UNREADABLE_DEFINITION,
        )
    )
    if installed_and_current:
        commands = plan.enable_commands
        artifacts: tuple[StartupArtifact, ...] = ()
    else:
        commands = (*plan.create_commands, *plan.enable_commands)
        artifacts = plan.artifacts
    return StartupOperationPlan(
        StartupOperation.ENABLE,
        plan.platform,
        plan.service_name,
        requires_confirmation=True,
        can_apply=True,
        reason="Install or enable automatic watcher startup.",
        artifacts_to_write=artifacts,
        commands=commands,
    )


def plan_repair_startup(
    plan: StartupPlan,
    inspection: StartupInspection,
) -> StartupOperationPlan:
    blocked = _unknown_startup_operation(plan, inspection, StartupOperation.REPAIR)
    if blocked is not None:
        return blocked
    if inspection.healthy:
        return StartupOperationPlan(
            StartupOperation.NOOP,
            plan.platform,
            plan.service_name,
            requires_confirmation=False,
            can_apply=True,
            reason="The startup integration does not need repair.",
        )
    if StartupIssue.BROKEN_EXECUTABLE in inspection.issues:
        return StartupOperationPlan(
            StartupOperation.REPAIR,
            plan.platform,
            plan.service_name,
            requires_confirmation=True,
            can_apply=False,
            reason="Repair is blocked until the watcher executable path is corrected.",
        )
    return StartupOperationPlan(
        StartupOperation.REPAIR,
        plan.platform,
        plan.service_name,
        requires_confirmation=True,
        can_apply=True,
        reason="Replace the confirmed managed definition and re-enable it.",
        artifacts_to_write=plan.artifacts,
        commands=(*plan.create_commands, *plan.enable_commands),
    )


def plan_disable_startup(
    plan: StartupPlan,
    inspection: StartupInspection,
) -> StartupOperationPlan:
    blocked = _unknown_startup_operation(plan, inspection, StartupOperation.DISABLE)
    if blocked is not None:
        return blocked
    if not inspection.installed or not inspection.enabled:
        return StartupOperationPlan(
            StartupOperation.NOOP,
            plan.platform,
            plan.service_name,
            requires_confirmation=False,
            can_apply=True,
            reason="Automatic watcher startup is already disabled.",
        )
    return StartupOperationPlan(
        StartupOperation.DISABLE,
        plan.platform,
        plan.service_name,
        requires_confirmation=True,
        can_apply=True,
        reason="Disable automatic watcher startup without removing its definition.",
        commands=plan.disable_commands,
    )


def plan_uninstall_startup(
    plan: StartupPlan,
    inspection: StartupInspection,
) -> StartupOperationPlan:
    blocked = _unknown_startup_operation(plan, inspection, StartupOperation.UNINSTALL)
    if blocked is not None:
        return blocked
    if not inspection.installed and not any(item.path.exists() for item in plan.artifacts):
        return StartupOperationPlan(
            StartupOperation.NOOP,
            plan.platform,
            plan.service_name,
            requires_confirmation=False,
            can_apply=True,
            reason="Automatic watcher startup is not installed.",
        )
    return StartupOperationPlan(
        StartupOperation.UNINSTALL,
        plan.platform,
        plan.service_name,
        requires_confirmation=True,
        can_apply=True,
        reason="Disable and remove the managed watcher startup definition.",
        artifact_paths_to_remove=tuple(item.path for item in plan.artifacts),
        commands=(plan.uninstall_commands[:1] if inspection.installed else ()),
        commands_after_removal=(
            plan.uninstall_commands[1:] if plan.platform is not StartupPlatform.WINDOWS_TASK else ()
        ),
    )


def startup_manifest(plan: StartupPlan) -> str:
    """Return stable JSON suitable for diagnostics or an offline setup wizard."""

    return json.dumps(plan.as_dict(include_content=True), sort_keys=True, indent=2) + "\n"


def startup_plan_contains_secrets(plan: StartupPlan) -> bool:
    """Fail-closed check used before displaying or applying generated plans."""

    forbidden = ("--password", "password=", "authorization:", "gucmail_password")
    rendered = json.dumps(plan.as_dict(include_content=True), sort_keys=True).casefold()
    return any(marker in rendered for marker in forbidden)


def sanitized_environment(
    source: Mapping[str, str],
    *,
    allow_names: Sequence[str] = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "GUCMAIL_CONFIG",
        "GUCMAIL_CONFIG_DIR",
        "GUCMAIL_DATA_DIR",
        "GUCMAIL_CACHE_DIR",
        "GUCMAIL_LOG_DIR",
    ),
) -> dict[str, str]:
    """Return a minimal environment without credential-bearing variables."""

    allowed = {item.casefold() for item in allow_names}
    result: dict[str, str] = {}
    for key, value in source.items():
        normalized = key.casefold()
        if normalized not in allowed:
            continue
        if any(marker in normalized for marker in ("password", "secret", "token", "credential")):
            continue
        if any(character in value for character in ("\x00", "\r", "\n")):
            continue
        result[key] = value
    return result


def executable_problem(process: WatcherProcessSpec) -> str | None:
    """Explain a broken watcher executable without trying to execute it."""

    path = process.executable
    if not path.exists():
        return "Watcher executable does not exist."
    if not path.is_file():
        return "Watcher executable path is not a file."
    if os.name != "nt" and not os.access(path, os.X_OK):
        return "Watcher executable is not executable by the current user."
    return None


def assert_safe_startup_plan(plan: StartupPlan) -> StartupPlan:
    """Validate a plan immediately before an integration layer applies it."""

    if startup_plan_contains_secrets(plan):
        raise ConfigurationError("Startup plan appears to contain credential material.")
    for command in (
        *plan.create_commands,
        *plan.enable_commands,
        *plan.disable_commands,
        *plan.inspect_commands,
        *plan.uninstall_commands,
    ):
        if command.argv[0].casefold() in {"cmd", "cmd.exe", "sh", "bash", "zsh"}:
            raise ConfigurationError("Startup plans may not invoke a command shell.")
    return plan
