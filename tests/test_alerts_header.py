"""The Alerts label follows the background watcher's own status file."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from gucmail.headless import WatcherRunState, WatcherStatus, WatcherStatusStore
from gucmail.tui.app import GUCmailApp
from test_tui import context_for


def _write(app: GUCmailApp, *, state: WatcherRunState, pid: int) -> None:
    now = datetime.now(UTC)
    directory = (app.context.paths.data_dir / "watcher").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    WatcherStatusStore(directory, "abc123").write(
        WatcherStatus(
            scope_id="abc123",
            profile="default",
            state=state,
            pid=pid,
            instance_id="test",
            started_at=now,
            heartbeat_at=now,
        )
    )


def test_running_background_watcher_shows_alerts_on(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    assert app._watcher_header_label() == "Alerts off"
    _write(app, state=WatcherRunState.RUNNING, pid=os.getpid())
    assert app._watcher_header_label() == "Alerts on"


def test_stopped_or_dead_watcher_does_not_count(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    _write(app, state=WatcherRunState.STOPPED, pid=os.getpid())
    assert app._watcher_header_label() == "Alerts off"
    _write(app, state=WatcherRunState.RUNNING, pid=2**22 - 3)
    assert app._watcher_header_label() == "Alerts off"
