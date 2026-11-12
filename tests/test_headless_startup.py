from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import gucmail.headless as headless_module
from gucmail.errors import ConfigurationError
from gucmail.headless import (
    HeadlessCycleResult,
    HeadlessWatcherRuntime,
    HealthLevel,
    ShutdownController,
    SingleInstanceLock,
    WatcherAlreadyRunningError,
    WatcherProcessSpec,
    WatcherRunState,
    WatcherStatusStore,
    evaluate_watcher_health,
    run_headless_cycles,
    start_watcher_process,
    validate_headless_configuration,
    watcher_scope_id,
)
from gucmail.startup import (
    InstalledStartupSnapshot,
    StartupIssue,
    StartupOperation,
    StartupPlatform,
    assert_safe_startup_plan,
    build_watcher_process_spec,
    executable_problem,
    generate_startup_plan,
    inspect_startup,
    plan_disable_startup,
    plan_enable_startup,
    plan_repair_startup,
    plan_uninstall_startup,
    sanitized_environment,
    startup_manifest,
    startup_plan_contains_secrets,
)

NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_spec(tmp_path: Path, **changes: object) -> WatcherProcessSpec:
    values: dict[str, object] = {
        "executable": Path(sys.executable).resolve(),
        "arguments": ("watch", "--headless", "--profile", "Everything"),
        "account_id": "synthetic.user@example.test",
        "profile": "Everything",
        "working_directory": tmp_path.resolve(),
        "poll_interval_seconds": 60,
    }
    values.update(changes)
    return WatcherProcessSpec(**values)  # type: ignore[arg-type]


def test_watcher_scope_is_stable_private_and_profile_specific() -> None:
    first = watcher_scope_id("User@Example.Test", "Everything")
    second = watcher_scope_id("user@example.test", "everything")

    assert first == second
    assert len(first) == 24
    assert "user" not in first
    assert watcher_scope_id("user@example.test", "Important") != first


def test_windows_liveness_probe_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def forbidden_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows process probes must never call os.kill")

    def windows_probe(pid: int) -> bool:
        calls.append(pid)
        return False

    monkeypatch.setattr(headless_module.os, "name", "nt")
    monkeypatch.setattr(headless_module.os, "kill", forbidden_kill)
    monkeypatch.setattr(
        headless_module,
        "_windows_process_is_alive",
        windows_probe,
    )

    assert not headless_module.process_is_alive(987654321)
    assert calls == [987654321]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"executable": Path("gucmail.exe")}, "absolute"),
        ({"arguments": ("watch", "--password", "do-not-store")}, "Passwords"),
        ({"arguments": ("watch", "--password=do-not-store")}, "Passwords"),
        ({"poll_interval_seconds": 5}, "at least"),
        ({"profile": "bad\nprofile"}, "control"),
    ],
)
def test_process_spec_rejects_unsafe_values(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        make_spec(tmp_path, **changes)


def test_single_instance_rejects_duplicate_live_owner(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    first = SingleInstanceLock(path, "abc123", pid=111, pid_checker=lambda _pid: True)
    second = SingleInstanceLock(path, "abc123", pid=222, pid_checker=lambda _pid: True)

    owner = first.acquire()
    with pytest.raises(WatcherAlreadyRunningError) as caught:
        second.acquire()

    assert caught.value.owner == owner
    assert caught.value.owner is not None
    assert caught.value.owner.pid == 111
    first.release()
    assert not path.exists()


def test_single_instance_recovers_stale_pid_and_preserves_new_owner(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    crashed = SingleInstanceLock(path, "abc123", pid=111, pid_checker=lambda _pid: False)
    crashed.acquire()

    replacement = SingleInstanceLock(path, "abc123", pid=222, pid_checker=lambda _pid: False)
    owner = replacement.acquire()

    assert owner.pid == 222
    assert json.loads(path.read_text(encoding="utf-8"))["instance_id"] == owner.instance_id
    crashed.release()
    assert path.exists(), "an obsolete owner must not remove its replacement's lock"
    replacement.release()
    assert not path.exists()


def test_recent_incomplete_lock_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text("", encoding="utf-8")
    lock = SingleInstanceLock(path, "abc123", pid_checker=lambda _pid: False)

    with pytest.raises(WatcherAlreadyRunningError, match="startup is already in progress"):
        lock.acquire()


def test_old_corrupt_lock_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text("not json", encoding="utf-8")
    old = NOW.timestamp() - 60
    os.utime(path, (old, old))
    clock = MutableClock()
    lock = SingleInstanceLock(
        path,
        "abc123",
        pid=222,
        pid_checker=lambda _pid: False,
        clock=clock,
    )

    assert lock.acquire().pid == 222
    lock.release()


def test_runtime_persists_private_heartbeat_and_clean_stop(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    clock = MutableClock()
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(
        spec,
        store,
        clock=clock,
        pid=321,
        pid_checker=lambda pid: pid == 321,
    )

    started = runtime.start()
    clock.value += timedelta(seconds=10)
    running = runtime.heartbeat(
        HeadlessCycleResult(
            strategy="SyncFolderItems",
            folders=("inbox",),
            events_detected=2,
            notifications_delivered=1,
            next_poll_at=clock.value + timedelta(seconds=60),
        )
    )
    health = evaluate_watcher_health(
        store,
        now=clock.value,
        pid_checker=lambda pid: pid == 321,
    )

    assert started.state is WatcherRunState.STARTING
    assert running.messages_detected == 2
    assert health.status is HealthLevel.HEALTHY
    assert health.as_dict()["exchange"] == "connected"
    assert "synthetic.user" not in health.to_json()
    if os.name != "nt":
        assert stat.S_IMODE(store.status_path.stat().st_mode) == 0o600

    clock.value += timedelta(seconds=1)
    stopped = runtime.stop()
    assert stopped.state is WatcherRunState.STOPPED
    assert not store.lock_path.exists()
    stopped_health = evaluate_watcher_health(
        store,
        now=clock.value,
        pid_checker=lambda _pid: True,
    )
    assert stopped_health.status is HealthLevel.DEGRADED
    assert not stopped_health.process_alive


def test_health_reports_stale_dead_process_without_mailbox_content(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    clock = MutableClock()
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(
        spec,
        store,
        clock=clock,
        pid=444,
        pid_checker=lambda _pid: True,
    )
    runtime.start()
    runtime.heartbeat(HeadlessCycleResult(strategy="bounded-polling"))

    clock.value += timedelta(minutes=10)
    health = evaluate_watcher_health(
        store,
        now=clock.value,
        heartbeat_timeout_seconds=180,
        pid_checker=lambda _pid: False,
    )

    assert health.status is HealthLevel.UNHEALTHY
    assert health.watcher == "stale"
    assert health.heartbeat_seconds_ago == 600
    assert set(health.as_dict()) == {
        "status",
        "watcher",
        "process_alive",
        "exchange",
        "strategy",
        "profile",
        "pid",
        "uptime_seconds",
        "heartbeat_seconds_ago",
        "last_sync_seconds_ago",
        "notification_failures",
        "current_error",
        "checks",
    }
    runtime.stop()


def test_control_requests_are_scoped_acknowledged_and_cooperative(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(spec, store)
    runtime.start()

    store.request("pause", requested_at=NOW)
    assert runtime.consume_control_request() == "pause"
    assert runtime.controller.paused
    assert runtime.status.state is WatcherRunState.PAUSED
    assert not store.control_path.exists()

    store.request("resume", requested_at=NOW)
    assert runtime.consume_control_request() == "resume"
    assert not runtime.controller.paused

    store.request("sync", requested_at=NOW)
    assert runtime.consume_control_request() == "sync"
    assert runtime.consume_sync_request()
    assert not runtime.consume_sync_request()

    store.request("stop", requested_at=NOW)
    assert runtime.consume_control_request() == "stop"
    assert runtime.controller.stop_requested
    assert runtime.status.state is WatcherRunState.STOPPING
    runtime.stop()


def test_runtime_failure_records_sanitized_failure_and_releases_lock(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(spec, store)

    with pytest.raises(RuntimeError, match="synthetic failure"), runtime.running():
        raise RuntimeError("synthetic failure")

    status = store.read()
    assert status is not None
    assert status.state is WatcherRunState.FAILED
    assert status.current_error == "RuntimeError: watcher terminated unexpectedly"
    assert not store.lock_path.exists()


def test_run_headless_cycles_is_tui_independent_and_bounded(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(spec, store)
    calls = 0

    def cycle() -> HeadlessCycleResult:
        nonlocal calls
        calls += 1
        return HeadlessCycleResult(strategy="synthetic", events_detected=1)

    assert run_headless_cycles(runtime, cycle, max_cycles=1) == 1
    assert calls == 1
    stored = store.read()
    assert stored is not None
    assert stored.state is WatcherRunState.STOPPED
    assert stored.messages_detected == 1


def test_failed_one_shot_retains_last_error_for_status_inspection(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    store = WatcherStatusStore(tmp_path.resolve() / "runtime", spec.scope_id)
    runtime = HeadlessWatcherRuntime(spec, store)

    run_headless_cycles(
        runtime,
        lambda: HeadlessCycleResult(
            successful=False,
            connection_status="offline",
            error="Synthetic network failure",
        ),
        max_cycles=1,
    )

    stored = store.read()
    assert stored is not None
    assert stored.state is WatcherRunState.STOPPED
    assert stored.current_error == "Synthetic network failure"


def test_shutdown_controller_wait_pause_and_resume() -> None:
    controller = ShutdownController()
    controller.pause()
    assert controller.paused
    controller.resume()
    assert not controller.paused
    controller.request_stop("test complete")
    assert controller.stop_requested
    assert controller.reason == "test complete"
    assert controller.wait(0)


def test_headless_configuration_requires_safe_noninteractive_inputs(tmp_path: Path) -> None:
    valid = validate_headless_configuration(
        account_id="synthetic@example.test",
        profile="Everything",
        poll_interval_seconds=60,
        executable=Path(sys.executable).resolve(),
        state_directory=(tmp_path / "state").resolve(),
        log_directory=(tmp_path / "logs").resolve(),
        credentials_available=True,
        provider_names=("console",),
    )
    invalid = validate_headless_configuration(
        account_id=None,
        profile="Everything",
        poll_interval_seconds=5,
        executable=(tmp_path / "missing.exe").resolve(),
        state_directory=Path("relative"),
        log_directory=Path("relative"),
        credentials_available=False,
    )

    assert valid.valid
    assert not valid.errors
    assert not invalid.valid
    assert len(invalid.errors) >= 6
    with pytest.raises(ConfigurationError, match="Headless configuration is invalid"):
        invalid.require_valid()


def test_windows_task_plan_is_deterministic_private_and_restartable(tmp_path: Path) -> None:
    executable = (tmp_path / "Program Files" / "GUC Mail" / "gucmail.exe").resolve()
    spec = make_spec(
        tmp_path,
        executable=executable,
        arguments=("watch", "--headless", "--profile", "Everything"),
    )

    first = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
        startup_delay_seconds=45,
        restart_delay_seconds=120,
        restart_attempts=5,
    )
    second = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
        startup_delay_seconds=45,
        restart_delay_seconds=120,
        restart_attempts=5,
    )
    xml = first.artifacts[0].content

    assert first == second
    assert first.platform is StartupPlatform.WINDOWS_TASK
    assert "<LogonTrigger>" in xml
    assert "<Delay>PT45S</Delay>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<Interval>PT120S</Interval>" in xml
    assert "<Count>5</Count>" in xml
    assert "<Hidden>true</Hidden>" in xml
    assert "password" not in xml.casefold()
    assert first.create_commands[0].argv[0] == "schtasks.exe"
    assert first.create_commands[0].as_dict()["shell"] is False
    assert not startup_plan_contains_secrets(first)
    assert assert_safe_startup_plan(first) is first
    assert startup_manifest(first) == startup_manifest(second)
    assert not (tmp_path / "startup").exists(), "generation must be a pure dry run"


def test_windows_task_escapes_shell_metacharacters_as_one_argument(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path,
        executable=(tmp_path / "odd & quoted.exe").resolve(),
        arguments=(
            "watch",
            "--headless",
            "--profile",
            'Exam & whoami | echo "quoted" %TEMP% $HOME',
        ),
    )
    plan = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
    )
    xml = plan.artifacts[0].content

    assert "odd &amp; quoted.exe" in xml
    assert "Exam &amp; whoami | echo" in xml
    assert "&quot;" in xml
    assert plan.process.arguments[-1] == 'Exam & whoami | echo "quoted" %TEMP% $HOME'
    assert all(command.argv[0] != "cmd.exe" for command in plan.create_commands)


def test_password_arguments_are_rejected_before_startup_generation(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Passwords"):
        build_watcher_process_spec(
            account_id="synthetic@example.test",
            executable=Path(sys.executable).resolve(),
            arguments=("watch", "--password=secret"),
            working_directory=tmp_path.resolve(),
        )


def test_default_python_process_spec_uses_a_valid_module_invocation(tmp_path: Path) -> None:
    spec = build_watcher_process_spec(
        account_id="synthetic@example.test",
        executable=Path(sys.executable).resolve(),
        working_directory=tmp_path.resolve(),
    )

    assert spec.command[:3] == (str(Path(sys.executable).resolve()), "-m", "gucmail")
    assert spec.arguments[-6:] == (
        "--non-interactive",
        "headless",
        "run",
        "--profile",
        "default",
        "--forever",
    )
    assert "--password" not in spec.arguments


def test_detached_watcher_launcher_never_uses_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 4321

    def fake_popen(command: tuple[str, ...], **options: object) -> Process:
        captured["command"] = command
        captured.update(options)
        return Process()

    monkeypatch.setattr(headless_module.subprocess, "Popen", fake_popen)
    spec = make_spec(tmp_path)

    assert start_watcher_process(spec) == 4321
    assert captured["command"] == spec.command
    assert captured["shell"] is False
    assert captured["cwd"] == tmp_path.resolve()


def test_inspection_detects_broken_executable_and_blocks_repair(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, executable=(tmp_path / "missing" / "gucmail.exe").resolve())
    plan = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
    )
    snapshot = InstalledStartupSnapshot(
        installed=True,
        enabled=True,
        definitions=(plan.artifacts[0].content,),
        executable_exists=False,
    )
    inspection = inspect_startup(plan, snapshot)

    assert StartupIssue.BROKEN_EXECUTABLE in inspection.issues
    assert not inspection.healthy
    assert not plan_repair_startup(plan, inspection).can_apply
    assert not plan_enable_startup(plan, inspection).can_apply
    assert executable_problem(spec) == "Watcher executable does not exist."


def test_startup_operation_plans_are_idempotent(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    plan = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
    )
    healthy = inspect_startup(
        plan,
        InstalledStartupSnapshot(
            installed=True,
            enabled=True,
            definitions=(plan.artifacts[0].content,),
            executable_exists=True,
        ),
    )

    assert healthy.healthy
    assert plan_enable_startup(plan, healthy).operation is StartupOperation.NOOP
    assert plan_repair_startup(plan, healthy).operation is StartupOperation.NOOP
    disable = plan_disable_startup(plan, healthy)
    uninstall = plan_uninstall_startup(plan, healthy)
    assert disable.operation is StartupOperation.DISABLE
    assert uninstall.operation is StartupOperation.UNINSTALL
    assert uninstall.commands[0].ignore_not_found

    absent = inspect_startup(
        plan,
        InstalledStartupSnapshot(
            installed=False,
            executable_exists=True,
        ),
    )
    first_enable = plan_enable_startup(plan, absent)
    second_enable = plan_enable_startup(plan, absent)
    assert first_enable == second_enable
    assert first_enable.artifacts_to_write == plan.artifacts


def test_inspection_detects_drift_duplicate_and_disabled_entries(tmp_path: Path) -> None:
    plan = generate_startup_plan(
        make_spec(tmp_path),
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=(tmp_path / "startup").resolve(),
    )
    inspection = inspect_startup(
        plan,
        InstalledStartupSnapshot(
            installed=True,
            enabled=False,
            definitions=("changed", "duplicate"),
            executable_exists=True,
        ),
    )

    assert set(inspection.issues) == {
        StartupIssue.DISABLED,
        StartupIssue.DEFINITION_DRIFT,
        StartupIssue.DUPLICATE_ENTRIES,
    }
    repair = plan_repair_startup(plan, inspection)
    assert repair.can_apply
    assert repair.requires_confirmation
    assert repair.artifacts_to_write == plan.artifacts


def test_systemd_user_plan_is_shell_free_quoted_and_restartable(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path,
        arguments=("watch", "--headless", "--profile", "x; touch /tmp/no %n $HOME"),
    )
    plan = generate_startup_plan(
        spec,
        platform_name="linux",
        output_directory=(tmp_path / "systemd").resolve(),
        restart_delay_seconds=75,
    )
    unit = plan.artifacts[0].content

    assert plan.platform is StartupPlatform.SYSTEMD_USER
    assert "Type=simple" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=75s" in unit
    assert '"x; touch /tmp/no %%n $$HOME"' in unit
    assert "/bin/sh" not in unit
    assert "--user" in plan.create_commands[0].argv
    assert plan.enable_commands[0].argv[-2:] == ("--now", plan.service_name)
    assert not (tmp_path / "systemd").exists()


def test_systemd_system_plan_requires_explicit_service_user(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    with pytest.raises(ConfigurationError, match="explicit service user"):
        generate_startup_plan(
            spec,
            platform_name="linux",
            system_service=True,
            output_directory=(tmp_path / "systemd").resolve(),
        )

    plan = generate_startup_plan(
        spec,
        platform_name="linux",
        system_service=True,
        service_user="gucmail_watcher",
        output_directory=(tmp_path / "systemd").resolve(),
    )
    assert plan.platform is StartupPlatform.SYSTEMD_SYSTEM
    assert "User=gucmail_watcher" in plan.artifacts[0].content
    assert "WantedBy=multi-user.target" in plan.artifacts[0].content
    assert "--user" not in plan.create_commands[0].argv


def test_sanitized_environment_excludes_credentials_and_control_lines() -> None:
    selected = sanitized_environment(
        {
            "PATH": "C:\\Tools",
            "GUCMAIL_CONFIG": "C:\\Config\\config.toml",
            "GUCMAIL_PASSWORD": "never-copy-this",
            "API_TOKEN": "never-copy-this-either",
            "HOME": "bad\nvalue",
            "UNRELATED": "ignored",
        }
    )

    assert selected == {
        "PATH": "C:\\Tools",
        "GUCMAIL_CONFIG": "C:\\Config\\config.toml",
    }
