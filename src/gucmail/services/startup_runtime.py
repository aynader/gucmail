"""Shared Task Scheduler/systemd execution boundary for CLI and TUI.

Planning is pure. Callers must review and confirm the concrete operation before
invoking apply_startup_operation. No credentials enter generated commands.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree as ET

from gucmail.headless import WatcherProcessSpec

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext

from gucmail.errors import ConfigurationError, ConflictError, ErrorContext
from gucmail.security import redact_text, sanitize_terminal
from gucmail.startup import (
    InstalledStartupSnapshot,
    StartupCommand,
    StartupInspection,
    StartupOperationPlan,
    StartupPlan,
    assert_safe_startup_plan,
    generate_startup_plan,
    inspect_startup,
    plan_disable_startup,
    plan_enable_startup,
    plan_repair_startup,
    plan_uninstall_startup,
    sanitized_environment,
)


def _windows_task_absent(returncode: int, output: str) -> bool:
    """Recognize only the two previously observed missing-task responses.

    An unrecognized/localized error remains unknown rather than granting a
    mutation or deletion permission. Exit code 1 alone also covers access denial.
    """
    return (
        returncode == 1
        and re.fullmatch(
            r"(?:ERROR:\s*)?The system cannot find the (?:file|path) specified\.?",
            output.strip(),
            re.IGNORECASE,
        )
        is not None
    )


def _unknown_snapshot(
    plan: StartupPlan, detail: object, *, installed: bool = False
) -> InstalledStartupSnapshot:
    return InstalledStartupSnapshot(
        installed=installed,
        executable_exists=plan.process.executable.is_file(),
        detail=sanitize_terminal(redact_text(detail), max_length=300),
        state_known=False,
    )


def inspect_startup_snapshot(plan: StartupPlan) -> InstalledStartupSnapshot:
    """Inspect one managed entry using argv only; never invoke a command shell."""

    command = plan.inspect_commands[0]
    try:
        completed = subprocess.run(  # noqa: S603 - validated argv, no shell
            list(command.argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=sanitized_environment(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unknown_snapshot(plan, exc)
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    installed = completed.returncode == 0
    windows = plan.platform.value == "windows-task-scheduler"
    combined_output = "\n".join(value for value in (output, error) if value)
    missing_entry = windows and _windows_task_absent(completed.returncode, combined_output)
    if not installed and not missing_entry:
        return _unknown_snapshot(
            plan, combined_output or f"Startup inspection returned {completed.returncode}."
        )
    definitions: tuple[str, ...] = ()
    enabled = False
    if installed:
        if len(output) > 2_000_000:
            return _unknown_snapshot(plan, "Startup inspection returned an oversized definition.")
        if windows:
            definitions = (output,) if output else ()
            try:
                document = ET.fromstring(
                    re.sub(r"<\?xml[^?]*\?>", "", output, count=1).encode("utf-8"),
                    parser=ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False),
                )
                if (
                    document.tag != "{http://schemas.microsoft.com/windows/2004/02/mit/task}Task"
                    or document.getroottree().docinfo.doctype
                ):
                    return _unknown_snapshot(plan, "Startup task definition is invalid.")
                enabled_node = document.find("{*}Settings/{*}Enabled")
                # Task Scheduler defaults task Enabled to true. Trigger Enabled
                # is a separate setting and must never override task disablement.
                enabled_text = "true" if enabled_node is None else (enabled_node.text or "").strip()
                if enabled_text not in {"true", "false", "1", "0"}:
                    return _unknown_snapshot(plan, "Startup task Enabled value is invalid.")
                enabled = enabled_text in {"true", "1"}
            except (ET.XMLSyntaxError, ValueError):
                return _unknown_snapshot(plan, "Startup task definition could not be parsed.")
        else:
            properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            load_state = properties.get("LoadState")
            if load_state == "not-found":
                installed = False
                missing_entry = True
            elif load_state not in {"loaded", "masked", "error", "bad-setting", "stub", "merged"}:
                return _unknown_snapshot(plan, "Startup service load state could not be read.")
            else:
                # Running and configured to start at login are different states.
                if "UnitFileState" not in properties:
                    return _unknown_snapshot(plan, "Startup service enablement could not be read.")
                enabled = properties.get("UnitFileState") in {"enabled", "enabled-runtime"}
                try:
                    definitions = tuple(
                        artifact.path.read_text(encoding="utf-8")
                        for artifact in plan.artifacts
                        if artifact.path.is_file()
                    )
                except (OSError, UnicodeError) as exc:
                    return _unknown_snapshot(plan, exc, installed=True)
    detail_value = (
        "No startup task is installed. Choose Enable to configure one."
        if missing_entry and windows
        else "No startup service is installed. Choose Enable to configure one."
        if missing_entry
        else error or ("installed" if installed else "not installed")
    )
    return InstalledStartupSnapshot(
        installed=installed,
        enabled=enabled,
        definitions=definitions,
        executable_exists=plan.process.executable.is_file(),
        detail=sanitize_terminal(redact_text(detail_value), max_length=300),
    )


def write_startup_artifact(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        # schtasks consumes UTF-16 XML; the BOM and declaration must agree.
        encoding = (
            "utf-16" if content.startswith('<?xml version="1.0" encoding="UTF-16"?>') else "utf-8"
        )
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def apply_startup_operation(
    plan: StartupPlan,
    operation: StartupOperationPlan,
) -> dict[str, object]:
    if not operation.can_apply:
        raise ConfigurationError(operation.reason)
    if operation.no_changes:
        return {"operation": operation, "applied": False, "results": ()}
    assert_safe_startup_plan(plan)
    allowed_paths = {artifact.path.resolve() for artifact in plan.artifacts}
    if any(
        artifact.path.resolve() not in allowed_paths for artifact in operation.artifacts_to_write
    ) or any(path.resolve() not in allowed_paths for path in operation.artifact_paths_to_remove):
        raise ConfigurationError("Startup operation contains an unplanned artifact path.")
    for artifact in operation.artifacts_to_write:
        write_startup_artifact(artifact.path, artifact.content, artifact.mode)
    results: list[dict[str, object]] = []
    removed: list[str] = []
    environment = sanitized_environment(os.environ)

    def run_command(command: StartupCommand) -> None:
        try:
            completed = subprocess.run(  # noqa: S603 - validated argv, no shell
                list(command.argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError(
                f"Startup integration command failed: {sanitize_terminal(redact_text(exc))}. "
                "Refresh startup status before retrying; an earlier step may have succeeded.",
                context=ErrorContext(
                    mutation_uncertain=bool(results or removed)
                    or isinstance(exc, subprocess.TimeoutExpired),
                    retry_safe=False,
                    suggestion="Refresh startup status before retrying.",
                ),
            ) from exc
        detail = sanitize_terminal(
            redact_text(completed.stderr or completed.stdout), max_length=500
        )
        results.append(
            {
                "program": Path(command.argv[0]).name,
                "purpose": command.purpose,
                "exit_code": completed.returncode,
                "detail": detail,
            }
        )
        ignored_missing = (
            command.ignore_not_found
            and plan.platform.value == "windows-task-scheduler"
            and _windows_task_absent(
                completed.returncode,
                "\n".join(
                    value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
                ),
            )
        )
        if completed.returncode and not ignored_missing:
            local_status = (
                "The local definition was removed before this follow-up command failed."
                if removed
                else "The local definition has not been removed."
            )
            raise ConfigurationError(
                f"Startup integration command returned {completed.returncode}: {detail}. "
                f"{local_status} Refresh startup status before retrying.",
                context=ErrorContext(
                    mutation_uncertain=len(results) > 1 or bool(removed),
                    retry_safe=False,
                    suggestion="Refresh startup status before retrying.",
                ),
            )

    for command in operation.commands:
        run_command(command)
    for path in operation.artifact_paths_to_remove:
        try:
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            raise ConfigurationError(
                "Startup definition could not be removed. Refresh startup status before retrying; "
                "the scheduler command may already have succeeded. "
                + sanitize_terminal(redact_text(exc), max_length=300),
                context=ErrorContext(
                    mutation_uncertain=bool(results or removed),
                    retry_safe=False,
                    suggestion="Refresh startup status and check definition-file permissions.",
                ),
            ) from exc
    for command in operation.commands_after_removal:
        run_command(command)
    return {
        "operation": operation.operation.value,
        "applied": True,
        "service_name": operation.service_name,
        "results": tuple(results),
        "artifacts_removed": tuple(removed),
    }


def context_watcher_process(context: AppContext, *, profile: str = "default") -> WatcherProcessSpec:
    import sys

    from gucmail.services.monitoring_preferences import configured_watcher

    if not context.config.email:
        raise ConfigurationError("Configure the account before setting up automatic monitoring.")
    configured = configured_watcher(context, profile)
    paths = context.paths
    arguments = (
        "-m",
        "gucmail.watcher_entry",
        "--config",
        str(paths.config_file.resolve()),
        "--data-dir",
        str(paths.data_dir.resolve()),
        "--cache-dir",
        str(paths.cache_dir.resolve()),
        "--log-dir",
        str(paths.log_dir.resolve()),
        "--profile",
        profile,
        "--forever",
    )
    return WatcherProcessSpec(
        executable=Path(sys.executable).resolve(),
        arguments=arguments,
        account_id=context.config.email,
        profile=profile,
        working_directory=paths.config_dir.resolve(),
        poll_interval_seconds=int(
            configured.poll_interval_seconds if configured else context.config.poll_interval_seconds
        ),
        writable_paths=tuple(
            dict.fromkeys(
                p.resolve()
                for p in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir)
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class StartupPreview:
    plan: StartupPlan
    inspection: StartupInspection
    operation: StartupOperationPlan | None = None


def apply_startup_preview(preview: StartupPreview, *, confirmed: bool) -> dict[str, object]:
    """Recheck the same reviewed state for both CLI and TUI before changing it."""
    if not confirmed or preview.operation is None:
        raise ConfigurationError("Review and confirm a startup operation first.")
    if not preview.operation.can_apply:
        raise ConfigurationError(preview.operation.reason)
    current = inspect_startup(preview.plan, inspect_startup_snapshot(preview.plan))
    if not current.state_known:
        raise ConfigurationError(
            "Startup state could not be inspected after confirmation. No change attempted. "
            "Refresh startup status and resolve the reported error. " + (current.detail or "")
        )
    if (current.installed, current.enabled, current.installed_sha256, current.issues) != (
        preview.inspection.installed,
        preview.inspection.enabled,
        preview.inspection.installed_sha256,
        preview.inspection.issues,
    ):
        raise ConflictError("Startup state changed after preview. Refresh and review a new plan.")
    return apply_startup_operation(preview.plan, preview.operation)


class StartupService:
    """Read real integration state and recheck it after user confirmation."""

    def __init__(self, context: AppContext) -> None:
        self.context = context

    def preview(
        self,
        *,
        profile: str = "default",
        startup_delay: int = 30,
        restart_delay: int = 60,
        restart_attempts: int = 3,
        operation: str | None = None,
    ) -> StartupPreview:
        process = context_watcher_process(self.context, profile=profile)
        plan = assert_safe_startup_plan(
            generate_startup_plan(
                process,
                startup_delay_seconds=startup_delay,
                restart_delay_seconds=restart_delay,
                restart_attempts=restart_attempts,
            )
        )
        inspection = inspect_startup(plan, inspect_startup_snapshot(plan))
        mutation = None if operation is None else self._planners()[operation](plan, inspection)
        return StartupPreview(plan, inspection, mutation)

    @staticmethod
    def _planners() -> dict[str, Callable[[StartupPlan, StartupInspection], StartupOperationPlan]]:
        return {
            "enable": plan_enable_startup,
            "repair": plan_repair_startup,
            "disable": plan_disable_startup,
            "uninstall": plan_uninstall_startup,
        }

    def apply(self, preview: StartupPreview, *, confirmed: bool) -> dict[str, object]:
        return apply_startup_preview(preview, confirmed=confirmed)
