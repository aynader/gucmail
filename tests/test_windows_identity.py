"""Task identity is explicit, current-user bound, and part of drift detection."""

import html
import os
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest

from gucmail.errors import ConfigurationError
from gucmail.platform_scheduler import generate_scheduler_plan
from gucmail.startup import (
    InstalledStartupSnapshot,
    StartupIssue,
    generate_startup_plan,
    inspect_startup,
)
from gucmail.windows_identity import current_windows_user_sid, validate_windows_sid
from test_headless_startup import make_spec

SID = "S-1-5-21-100-200-300-1001"


def test_watcher_trigger_and_principal_share_explicit_identity(tmp_path):
    plan = generate_startup_plan(
        make_spec(tmp_path),
        platform_name="windows",
        windows_user_sid=SID,
    )
    root = ET.fromstring(plan.artifacts[0].content)  # noqa: S314 - generated fixture
    assert root.find("{*}Triggers/{*}LogonTrigger/{*}UserId").text == SID
    assert root.find("{*}Principals/{*}Principal/{*}UserId").text == SID
    assert root.find("{*}Principals/{*}Principal/{*}LogonType").text == "InteractiveToken"
    expected = InstalledStartupSnapshot(True, True, (plan.artifacts[0].content,), True)
    assert inspect_startup(plan, expected).healthy
    changed_user = replace(expected, definitions=(expected.definitions[0].replace(SID, SID + "2"),))
    assert StartupIssue.DEFINITION_DRIFT in inspect_startup(plan, changed_user).issues


def test_scheduler_principal_identity_is_explicit(tmp_path):
    plan = generate_scheduler_plan(
        platform_name="windows",
        windows_user_sid=SID,
        output_directory=tmp_path,
    )
    root = ET.fromstring(plan.artifacts[0].content)  # noqa: S314 - generated fixture
    assert root.find("{*}Principals/{*}Principal/{*}UserId").text == SID
    assert not list(tmp_path.iterdir()), "generation must not install a task"


@pytest.mark.parametrize(
    "sid",
    [
        "",
        "student@example.test",
        "DOMAIN\\user",
        "S-1-5-18",
        "S-1-5-19",
        "S-1-5-20",
        "S-1-5-21</UserId>",
        "S-1-5-21\n",
        "S-1-5--1",
    ],
)
def test_invalid_or_service_identity_is_rejected(sid):
    with pytest.raises(ConfigurationError):
        validate_windows_sid(sid)


def test_native_process_identity_does_not_use_username_environment(monkeypatch):
    monkeypatch.setenv("USERNAME", "synthetic-spoof")
    monkeypatch.setenv("USERDOMAIN", "synthetic-domain")
    if os.name == "nt":
        first = current_windows_user_sid()
        assert first == validate_windows_sid(first)
        assert current_windows_user_sid() == first
    else:
        with pytest.raises(ConfigurationError, match="Supply a Windows user SID"):
            current_windows_user_sid()


def test_task_scheduler_reserialization_is_not_reported_as_drift(tmp_path, monkeypatch):
    """Windows rewrites an imported task; only real changes may count as drift."""

    import gucmail.startup as startup

    monkeypatch.setenv("USERDOMAIN", "EXAMPLE")
    monkeypatch.setenv("USERNAME", "student")
    monkeypatch.setattr(startup, "current_windows_user_sid", lambda: SID)
    plan = generate_startup_plan(make_spec(tmp_path), platform_name="windows", windows_user_sid=SID)
    generated = plan.artifacts[0].content
    root = ET.fromstring(generated)  # noqa: S314 - generated fixture
    command = root.find("{*}Actions/{*}Exec/{*}Command").text
    arguments = root.find("{*}Actions/{*}Exec/{*}Arguments").text
    workdir = root.find("{*}Actions/{*}Exec/{*}WorkingDirectory").text
    # The shape Task Scheduler returns: reordered, defaults dropped, PT1M, name not SID.
    installed = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\GUC Mail Watcher (abc)</URI></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>{SID}</UserId>
    <LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Count>3</Count><Interval>PT1M</Interval></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd></IdleSettings>
  </Settings>
  <Triggers><LogonTrigger><Delay>PT30S</Delay><UserId>EXAMPLE\\student</UserId>
  </LogonTrigger></Triggers>
  <Actions Context="Author"><Exec><Command>{command}</Command>
    <Arguments>{html.escape(arguments)}</Arguments>
    <WorkingDirectory>{workdir}</WorkingDirectory></Exec></Actions>
</Task>"""
    snapshot = InstalledStartupSnapshot(True, True, (installed,), True)
    assert inspect_startup(plan, snapshot).healthy
    other_user = replace(snapshot, definitions=(installed.replace("student", "intruder"),))
    assert StartupIssue.DEFINITION_DRIFT in inspect_startup(plan, other_user).issues
    tampered = installed.replace(f"<Command>{command}</Command>", r"<Command>C:\evil.exe</Command>")
    assert tampered != installed
    changed_command = replace(snapshot, definitions=(tampered,))
    assert StartupIssue.DEFINITION_DRIFT in inspect_startup(plan, changed_command).issues


def test_background_tasks_prefer_the_windowless_interpreter(tmp_path):
    """Task Scheduler opens a console window for python.exe; pythonw.exe has none."""

    from gucmail.windows_identity import windowless_python

    (tmp_path / "python.exe").write_bytes(b"")
    assert windowless_python(tmp_path / "python.exe") == str(tmp_path / "python.exe")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert windowless_python(tmp_path / "python.exe") == str(tmp_path / "pythonw.exe")
    assert windowless_python(tmp_path / "gucmail.exe") == str(tmp_path / "gucmail.exe")


def test_entry_points_survive_pythonw_without_console_streams(monkeypatch):
    import sys

    from gucmail.watcher_entry import ensure_output_streams

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    ensure_output_streams()
    print("no console", file=sys.stdout)
    print("no console", file=sys.stderr)
    sys.stdout.close()
    sys.stderr.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows PID reuse check")
def test_stale_watcher_lock_naming_a_reused_pid_is_recovered(tmp_path):
    """A crashed watcher's lock must not block forever when Windows reuses its PID."""

    import json
    from datetime import UTC, datetime

    from gucmail.headless import SingleInstanceLock

    lock_path = tmp_path / "watcher.lock"
    # Owner PID is a live process (this one) that started long after the lock time.
    lock_path.write_text(
        json.dumps(
            {
                "acquired_at": datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
                "instance_id": "old",
                "pid": os.getpid(),
                "schema_version": 1,
                "scope_id": "scope",
            }
        ),
        encoding="utf-8",
    )
    owner = SingleInstanceLock(lock_path, "scope", pid=os.getpid() + 1).acquire()
    assert owner.instance_id != "old"
