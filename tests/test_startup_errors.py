import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gucmail.errors import ConfigurationError, ConflictError
from gucmail.services import startup_runtime as runtime
from gucmail.startup import (
    InstalledStartupSnapshot,
    build_watcher_process_spec,
    generate_startup_plan,
    inspect_startup,
    plan_disable_startup,
    plan_enable_startup,
    plan_repair_startup,
    plan_uninstall_startup,
)


def make_plan(tmp_path: Path, platform_name: str = "windows"):
    spec = build_watcher_process_spec(
        account_id="synthetic@example.test",
        executable=Path(sys.executable).resolve(),
        working_directory=tmp_path.resolve(),
    )
    return generate_startup_plan(
        spec,
        platform_name=platform_name,
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=tmp_path / "startup",
    )


@pytest.mark.parametrize(
    "planner",
    [plan_enable_startup, plan_repair_startup, plan_disable_startup, plan_uninstall_startup],
)
def test_unknown_inspection_blocks_every_startup_planner(tmp_path, monkeypatch, planner):
    plan = make_plan(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: Access is denied."
        ),
    )
    observation = runtime.inspect_startup_snapshot(plan)
    inspection = inspect_startup(plan, observation)
    operation = planner(plan, inspection)
    assert not operation.can_apply
    assert operation.no_changes
    assert "inspection-failed" in {issue.value for issue in inspection.issues}
    assert "not-installed" not in {issue.value for issue in inspection.issues}


@pytest.mark.parametrize("planner", [plan_disable_startup, plan_uninstall_startup])
@pytest.mark.parametrize(
    "message", ["ERROR: Access is denied.", "ERROR: Task Scheduler service is unavailable."]
)
def test_idempotent_flag_does_not_hide_scheduler_failure(tmp_path, monkeypatch, planner, message):
    plan = make_plan(tmp_path)
    runtime.write_startup_artifact(plan.artifacts[0].path, plan.artifacts[0].content, 0o600)
    original = plan.artifacts[0].path.read_bytes()
    operation = planner(
        plan,
        inspect_startup(
            plan, InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True)
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=message),
    )
    with pytest.raises(ConfigurationError, match="returned 1"):
        runtime.apply_startup_operation(plan, operation)
    assert plan.artifacts[0].path.read_bytes() == original


@pytest.mark.parametrize(
    "error",
    [
        OSError("synthetic executable unavailable"),
        subprocess.TimeoutExpired("synthetic scheduler", 20),
    ],
)
def test_inspection_transport_failure_is_unknown(tmp_path, monkeypatch, error):
    plan = make_plan(tmp_path)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(runtime.subprocess, "run", fail)
    observation = runtime.inspect_startup_snapshot(plan)
    assert observation.state_known is False
    assert not plan_enable_startup(plan, inspect_startup(plan, observation)).can_apply


def test_running_disabled_systemd_unit_is_not_enabled_at_startup(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "linux")
    plan.artifacts[0].path.parent.mkdir()
    plan.artifacts[0].path.write_text(plan.artifacts[0].content, encoding="utf-8")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nUnitFileState=disabled\nActiveState=active\n",
            stderr="",
        ),
    )
    observation = runtime.inspect_startup_snapshot(plan)
    assert observation.installed
    assert observation.enabled is False


def test_systemd_not_found_property_means_absent(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "linux")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n", stderr=""
        ),
    )
    observation = runtime.inspect_startup_snapshot(plan)
    assert observation.installed is False
    assert observation.state_known is True


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: The system cannot find the file specified.",
        "ERROR: The system cannot find the path specified.",
    ],
)
def test_recognized_windows_absence_is_known(tmp_path, monkeypatch, message):
    plan = make_plan(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=message),
    )
    observation = runtime.inspect_startup_snapshot(plan)
    assert not observation.installed
    assert observation.state_known
    assert plan_enable_startup(plan, inspect_startup(plan, observation)).can_apply


@pytest.mark.parametrize(
    "code,stdout,stderr",
    [
        (2, "", "ERROR: The system cannot find the file specified."),
        (1, "", "ERROR: Zugriff verweigert."),
        (1, "", "ERROR: The system cannot find the file specified.\nAccess is denied."),
        (0, "", ""),
        (0, "<broken", ""),
        (0, "<Other><Enabled>true</Enabled></Other>", ""),
        (0, "x" * 2_000_001, ""),
        (
            0,
            '<!DOCTYPE Task [<!ENTITY x SYSTEM "file:///unused">]>'
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>',
            "",
        ),
    ],
    ids=[
        "wrong-exit-code",
        "localized-unknown",
        "mixed-error",
        "empty",
        "malformed",
        "wrong-root",
        "oversized",
        "dtd",
    ],
)
def test_ambiguous_or_unreadable_windows_result_is_unknown(
    tmp_path, monkeypatch, code, stdout, stderr
):
    plan = make_plan(tmp_path)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr),
    )
    observation = runtime.inspect_startup_snapshot(plan)
    assert not observation.state_known
    assert not plan_uninstall_startup(plan, inspect_startup(plan, observation)).can_apply


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: The system cannot find the file specified.",
        "ERROR: The system cannot find the path specified.",
    ],
)
def test_disappeared_windows_task_allows_confirmed_local_cleanup(tmp_path, monkeypatch, message):
    plan = make_plan(tmp_path)
    runtime.write_startup_artifact(plan.artifacts[0].path, plan.artifacts[0].content, 0o600)
    operation = plan_uninstall_startup(
        plan,
        inspect_startup(
            plan, InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True)
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=message),
    )
    result = runtime.apply_startup_operation(plan, operation)
    assert result["applied"]
    assert not plan.artifacts[0].path.exists()


@pytest.mark.parametrize("installed", [False, True])
def test_systemd_reload_runs_after_definition_removal(tmp_path, monkeypatch, installed):
    plan = make_plan(tmp_path, "linux")
    runtime.write_startup_artifact(plan.artifacts[0].path, plan.artifacts[0].content, 0o600)
    inspection = inspect_startup(
        plan,
        InstalledStartupSnapshot(
            installed, installed, (plan.artifacts[0].content,) if installed else (), True
        ),
    )
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        assert kwargs["shell"] is False
        if "daemon-reload" in argv:
            assert not plan.artifacts[0].path.exists()
        else:
            assert plan.artifacts[0].path.exists()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    result = runtime.apply_startup_operation(plan, plan_uninstall_startup(plan, inspection))
    assert result["applied"]
    assert len(commands) == (2 if installed else 1)


def test_systemd_removal_failure_stops_reload_and_preserves_definition(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "linux")
    runtime.write_startup_artifact(plan.artifacts[0].path, plan.artifacts[0].content, 0o600)
    original = plan.artifacts[0].path.read_bytes()
    inspection = inspect_startup(
        plan, InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True)
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="Access denied")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(ConfigurationError, match="has not been removed"):
        runtime.apply_startup_operation(plan, plan_uninstall_startup(plan, inspection))
    assert len(calls) == 1
    assert plan.artifacts[0].path.read_bytes() == original


def test_reload_failure_reports_partial_local_removal(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "linux")
    runtime.write_startup_artifact(plan.artifacts[0].path, plan.artifacts[0].content, 0o600)
    inspection = inspect_startup(
        plan, InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True)
    )

    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=1 if "daemon-reload" in argv else 0,
            stdout="",
            stderr="Reload denied" if "daemon-reload" in argv else "",
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(ConfigurationError, match="removed before this follow-up") as failure:
        runtime.apply_startup_operation(plan, plan_uninstall_startup(plan, inspection))
    assert failure.value.context.mutation_uncertain
    assert failure.value.context.retry_safe is False
    assert not plan.artifacts[0].path.exists()


def test_definition_unlink_failure_keeps_file_and_stops_follow_up(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, "linux")
    artifact = plan.artifacts[0]
    runtime.write_startup_artifact(artifact.path, artifact.content, 0o600)
    original = artifact.path.read_bytes()
    inspection = inspect_startup(
        plan, InstalledStartupSnapshot(True, True, (artifact.content,), True)
    )
    unlink = Path.unlink

    def deny(path, *args, **kwargs):
        if path == artifact.path:
            raise PermissionError("synthetic owned definition denied")
        return unlink(path, *args, **kwargs)

    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(Path, "unlink", deny)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(ConfigurationError, match="could not be removed") as failure:
        runtime.apply_startup_operation(plan, plan_uninstall_startup(plan, inspection))
    assert len(commands) == 1
    assert failure.value.context.mutation_uncertain
    assert artifact.path.read_bytes() == original


@pytest.mark.parametrize("change", ["denied", "new-task", "missing-executable"])
def test_shared_apply_rechecks_reviewed_state_without_mutating(tmp_path, monkeypatch, change):
    plan = make_plan(tmp_path)
    original = InstalledStartupSnapshot(False, executable_exists=True)
    inspection = inspect_startup(plan, original)
    preview = runtime.StartupPreview(plan, inspection, plan_enable_startup(plan, inspection))
    changed = {
        "denied": replace(original, state_known=False, detail="Access denied"),
        "new-task": InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True),
        "missing-executable": replace(original, executable_exists=False),
    }[change]
    monkeypatch.setattr(runtime, "inspect_startup_snapshot", lambda _: changed)
    monkeypatch.setattr(
        runtime, "apply_startup_operation", lambda *args: pytest.fail("Applied stale startup plan")
    )
    with pytest.raises(ConfigurationError if change == "denied" else ConflictError):
        runtime.apply_startup_preview(preview, confirmed=True)
    assert not plan.artifacts[0].path.exists()


@pytest.mark.parametrize("operation", ["enable", "repair", "disable", "uninstall"])
def test_cli_unknown_state_has_reviewable_preview_and_refuses_apply(
    tmp_path, monkeypatch, operation
):
    from tests.test_cli import _environment
    from typer.testing import CliRunner

    import gucmail.cli as cli

    plan = make_plan(tmp_path)
    monkeypatch.setattr(cli.background, "_startup_plan", lambda *args, **kwargs: plan)
    unknown = InstalledStartupSnapshot(False, detail="Access denied", state_known=False)
    monkeypatch.setattr(cli.background, "_inspect_startup_snapshot", lambda _: unknown)
    monkeypatch.setattr(
        runtime, "apply_startup_operation", lambda *args: pytest.fail("Unknown startup mutated")
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--json", "startup", operation], env=_environment(tmp_path))
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["inspection"]["state_known"] is False
    assert data["operation"]["can_apply"] is False
    result = runner.invoke(
        cli.app, ["--json", "startup", operation, "--apply"], env=_environment(tmp_path)
    )
    assert result.exit_code == 3, result.output
    assert "No change attempted" in result.output
    assert not plan.artifacts[0].path.exists()


def test_cli_confirmation_rechecks_same_reviewed_state(tmp_path, monkeypatch):
    from tests.test_cli import _environment
    from typer.testing import CliRunner

    import gucmail.cli as cli

    plan = make_plan(tmp_path)
    state = InstalledStartupSnapshot(False, executable_exists=True)
    monkeypatch.setattr(cli.background, "_startup_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(cli.background, "_inspect_startup_snapshot", lambda _: state)
    monkeypatch.setattr(runtime, "inspect_startup_snapshot", lambda _: state)

    def confirm(*args):
        nonlocal state
        state = replace(state, state_known=False, detail="Access denied after preview")

    monkeypatch.setattr(cli.core, "confirm", confirm)
    monkeypatch.setattr(
        runtime,
        "apply_startup_operation",
        lambda *args: pytest.fail("Applied without state recheck"),
    )
    result = CliRunner().invoke(
        cli.app, ["--json", "startup", "enable", "--apply"], env=_environment(tmp_path)
    )
    assert result.exit_code == 3, result.output
    assert "after confirmation" in result.output
    assert not plan.artifacts[0].path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_startup_unknown_refresh_and_cancel(tmp_path, monkeypatch, size):
    from tests.test_settings_secondpass import _context
    from textual.widgets import Button, Input, Static

    from gucmail.tui.app import GUCmailApp, PaletteScreen
    from gucmail.tui.area_common import ConfirmationScreen, ViewState
    from gucmail.tui.settings_screen import SettingsScreen
    from gucmail.tui.startup_screen import StartupScreen

    context = _context(tmp_path)
    # The scheduler subprocess trap must not intercept Python's lazy Windows
    # platform.node() implementation when the root Settings service starts.
    monkeypatch.setattr("platform.node", lambda: "synthetic-startup-host")
    monkeypatch.setattr(
        context.credential_resolver,
        "resolve",
        lambda *args, **kwargs: pytest.fail("Startup setup resolved live credentials"),
    )
    monkeypatch.setattr(
        runtime,
        "generate_startup_plan",
        lambda process, **kwargs: generate_startup_plan(
            process,
            **kwargs,
            platform_name="windows",
            windows_user_sid="S-1-5-21-100-200-300-1001",
            output_directory=tmp_path / "startup",
        ),
    )
    denied = True
    queries = []

    def run(argv, **kwargs):
        assert "/Query" in argv
        assert kwargs["shell"] is False
        queries.append(argv)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ERROR: Access is denied."
            if denied
            else "ERROR: The system cannot find the file specified.",
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(
        runtime, "apply_startup_operation", lambda *args: pytest.fail("Cancelled setup applied")
    )

    async def wait(predicate):
        for _ in range(120):
            await pilot.pause(0.025)
            if predicate():
                return
        assert predicate()

    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("ctrl+k")
        await wait(lambda: isinstance(app.screen, PaletteScreen))
        app.screen.query_one("#palette-input", Input).value = "Open Settings"
        await pilot.pause()
        await pilot.press("enter")
        await wait(
            lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog is not None
        )
        settings = app.screen
        settings.query_one("#settings-search", Input).value = "monitoring.start_at_login"
        await wait(lambda: settings._selected_setting_id == "monitoring.start_at_login")
        settings.action_edit_setting()
        settings.query_one("#settings-managed", Button).focus()
        await pilot.press("enter")
        await wait(
            lambda: (
                isinstance(app.screen, StartupScreen)
                and app.screen.current is not None
                and not app.screen._operation_active
            )
        )
        startup = app.screen
        assert startup.view_state is ViewState.ERROR
        assert startup.current.inspection.state_known is False
        assert "Installed: Unknown" in str(startup.query_one("#startup-status", Static).render())
        assert "Access is denied" in str(startup.query_one("#startup-status", Static).render())
        for identifier in ("enable", "repair", "disable", "uninstall"):
            assert startup.query_one("#startup-" + identifier, Button).disabled
        assert context.state.get_ui_preference("preferences", scope="startup") is None
        denied = False
        await pilot.press("f5")
        await wait(lambda: startup.current.inspection.state_known and not startup._operation_active)
        assert startup.view_state is ViewState.READY
        for identifier in ("enable", "repair", "disable", "uninstall"):
            assert not startup.query_one("#startup-" + identifier, Button).disabled
        startup.query_one("#startup-enable", Button).focus()
        await pilot.press("enter")
        await wait(lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("escape")
        assert app.screen is startup
        assert context.state.get_ui_preference("preferences", scope="startup") is None
        assert not (tmp_path / "startup").exists()
        await pilot.press("escape")
        assert app.screen is settings
    assert queries and all("/Query" in command for command in queries)
