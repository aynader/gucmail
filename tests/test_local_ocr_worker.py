from __future__ import annotations

import io
import json
from threading import Event

import pytest

from gucmail.services import ocr


class Child:
    def __init__(self, mode, cancel):
        self.mode = mode
        self.cancel = cancel
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.payloads = []

    def communicate(self, payload, timeout):
        self.payloads.append(payload)
        if self.mode == "cancel":
            self.cancel.set()
            raise ocr.subprocess.TimeoutExpired("owned", timeout)
        if self.mode in {"timeout", "stubborn"}:
            raise ocr.subprocess.TimeoutExpired("owned", timeout)
        if self.mode == "error":
            raise OSError("secret-placeholder")
        self.returncode = 0
        data = (
            b"not JSON"
            if self.mode == "malformed"
            else b"x" * 20_000
            if self.mode == "oversized"
            else json.dumps({"status": "available", "languages": ["en-US"]}).encode()
        )
        return data, None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.mode != "stubborn":
            self.returncode = -15

    def wait(self, timeout):
        if self.returncode is None:
            raise ocr.subprocess.TimeoutExpired("owned", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("success", "available"),
        ("cancel", "cancelled"),
        ("timeout", "timeout"),
        ("stubborn", "timeout"),
        ("malformed", "failed"),
        ("oversized", "failed"),
        ("error", "failed"),
    ],
)
def test_owned_child_protocol_deadline_cancellation_cleanup_and_safe_failures(
    monkeypatch, mode, expected
):
    cancelled = Event()
    child = Child(mode, cancelled)
    calls = []

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(ocr.sys, "platform", "win32")
    monkeypatch.setattr(ocr.Path, "is_file", lambda path: True)
    monkeypatch.setattr(ocr.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(ocr.subprocess, "Popen", launch)
    monkeypatch.setenv("GUCMAIL_PASSWORD", "secret-placeholder")
    if mode in {"timeout", "stubborn"}:
        clock = iter([0.0, 0.0, 2.0])
        monkeypatch.setattr(ocr.time, "perf_counter", lambda: next(clock))
    result = ocr.WindowsOcrBackend().invoke(
        {"mode": "status", "max_chars": 10}, timeout=1, cancel_event=cancelled
    )
    assert result["status"] == expected and "placeholder" not in str(result)
    assert child.stdin.closed and child.stdout.closed
    command, options = calls[0]
    assert command[-1] == ocr._WINDOWS_SCRIPT and "-NoProfile" in command
    assert "-ExecutionPolicy" not in command and options.get("shell") is not True
    assert options["creationflags"] == 0x08000000
    assert "GUCMAIL_PASSWORD" not in options["env"]
    if mode in {"cancel", "timeout", "stubborn", "error"}:
        assert child.terminated
    else:
        assert not child.terminated
    assert child.killed == (mode == "stubborn")
    if child.payloads:
        assert json.loads(child.payloads[0]) == {"mode": "status", "max_chars": 10}


def test_start_failure_is_safe_and_does_not_expose_exception(monkeypatch):
    def launch(*args, **kwargs):
        raise OSError("secret-placeholder")

    monkeypatch.setattr(ocr.sys, "platform", "win32")
    monkeypatch.setattr(ocr.Path, "is_file", lambda path: True)
    monkeypatch.setattr(ocr.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(ocr.subprocess, "Popen", launch)
    result = ocr.WindowsOcrBackend().invoke({"mode": "status"}, timeout=1, cancel_event=None)
    assert result == {"status": "failed"}
