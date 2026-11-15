from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button, DataTable, Select, Static

from gucmail.models import Address, DraftData, SendState
from gucmail.services.drafts import DraftService
from gucmail.services.scheduler import SchedulerService
from gucmail.state import StateStore
from gucmail.tui.area_common import OfflineOperationError
from gucmail.tui.scheduled_screen import (
    ScheduledEditValue,
    ScheduledScreen,
    SchedulerServiceBackend,
)
from waiting import wait_until

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _services(tmp_path: Path) -> tuple[DraftService, SchedulerService]:
    state = StateStore(tmp_path / "state.sqlite3")
    drafts = DraftService(state, tmp_path / "recovery")
    scheduler = SchedulerService(
        state,
        drafts,
        lambda _draft, **_kwargs: "sent-item",
        timezone="UTC",
    )
    return drafts, scheduler


def _job(tmp_path: Path) -> tuple[SchedulerServiceBackend, str]:
    drafts, scheduler = _services(tmp_path)
    draft = drafts.create(
        DraftData(
            reference="dft-screen",
            to=[Address("recipient@example.edu")],
            subject="Synthetic scheduled message",
            body_text="Synthetic body",
        )
    )
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(days=2),
        now=NOW,
        timezone="UTC",
    )
    return SchedulerServiceBackend(scheduler, online=True, clock=lambda: NOW), job.reference


def _progress(_value: object) -> None:
    return None


def test_scheduler_backend_exposes_and_updates_durable_job(tmp_path: Path) -> None:
    backend, reference = _job(tmp_path)

    status, jobs = backend.list_jobs(
        states=(),
        query="synthetic",
        limit=20,
        cancel=Event(),
        progress=_progress,
    )

    assert not status.paused
    assert len(jobs) == 1
    assert jobs[0].reference == reference
    assert jobs[0].body == "Synthetic body"

    edited = backend.edit_job(
        reference,
        ScheduledEditValue(
            to=("updated@example.edu",),
            cc=(),
            bcc=(),
            subject="Updated subject",
            body="Updated body",
            importance="high",
            require_final_confirmation=True,
        ),
        cancel=Event(),
        progress=_progress,
    )
    assert edited.subject == "Updated subject"
    assert edited.to == ("updated@example.edu",)
    assert edited.require_final_confirmation

    rescheduled = backend.reschedule(
        reference,
        "in 5 days",
        cancel=Event(),
        progress=_progress,
    )
    assert rescheduled.reference == reference
    assert rescheduled.state == SendState.SCHEDULED.value

    duplicate = backend.duplicate(
        reference,
        "in 7 days",
        cancel=Event(),
        progress=_progress,
    )
    assert duplicate.reference != reference
    assert duplicate.subject == "Updated subject"


def test_scheduler_backend_offline_send_boundary_is_rejected(tmp_path: Path) -> None:
    backend, reference = _job(tmp_path)
    backend.online = False

    with pytest.raises(OfflineOperationError):
        backend.send_now(reference, cancel=Event(), progress=_progress)
    with pytest.raises(OfflineOperationError):
        backend.run_due(limit=20, cancel=Event(), progress=_progress)

    cancelled = backend.cancel_job(reference, cancel=Event(), progress=_progress)
    assert cancelled.state == SendState.CANCELLED.value


def test_scheduler_backend_pause_and_history_are_persistent(tmp_path: Path) -> None:
    backend, reference = _job(tmp_path)

    status = backend.set_paused(True, cancel=Event(), progress=_progress)
    assert status.paused
    status = backend.set_paused(False, cancel=Event(), progress=_progress)
    assert not status.paused

    backend.reschedule(reference, "in 4 days", cancel=Event(), progress=_progress)
    history = backend.history(reference, cancel=Event(), progress=_progress)
    assert any(row.get("event") == "rescheduled" for row in history)


def test_scheduler_backend_applies_next_today_and_week_windows(tmp_path: Path) -> None:
    drafts, scheduler = _services(tmp_path)
    references: list[str] = []
    for index, offset in enumerate((timedelta(hours=2), timedelta(days=1), timedelta(days=8))):
        draft = drafts.create(
            DraftData(
                reference=f"draft-window-{index}",
                to=[Address("recipient@example.edu")],
                subject=f"Window {index}",
            )
        )
        scheduled = scheduler.schedule_at(
            draft.reference,
            NOW + offset,
            now=NOW,
            timezone="UTC",
        )
        references.append(scheduled.reference)
    backend = SchedulerServiceBackend(scheduler, online=False, clock=lambda: NOW)

    def listed(window: str) -> tuple[str, ...]:
        _status, jobs = backend.list_jobs(
            states=(),
            query="",
            limit=20,
            cancel=Event(),
            progress=_progress,
            window=window,
        )
        return tuple(job.reference for job in jobs)

    assert listed("next") == (references[0],)
    assert listed("today") == (references[0],)
    assert listed("this_week") == tuple(references[:2])


@pytest.mark.asyncio
async def test_scheduled_screen_is_full_height_and_offline_send_controls_are_disabled(
    tmp_path: Path,
) -> None:
    backend, _reference = _job(tmp_path)
    backend.online = False
    screen = ScheduledScreen(backend, page_size=50, offline=True)

    class Harness(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)

    app = Harness()
    async with app.run_test(size=(120, 36)) as pilot:
        table = screen.query_one("#scheduled-table", DataTable)
        await wait_until(pilot, lambda: table.row_count == 1, pause_first=True)
        assert table.row_count == 1
        assert table.region.height >= 7
        assert screen.query_one("#scheduled-send-now", Button).disabled
        assert screen.query_one("#scheduled-run-due", Button).disabled
        assert "Synthetic scheduled message" in str(
            screen.query_one("#scheduled-detail", Static).render()
        )
        screen.configure_view(window="next")
        await wait_until(
            pilot,
            lambda: screen.query_one("#scheduled-window", Select).value == "next",
            interval=0.05,
            pause_first=True,
        )
        assert screen.query_one("#scheduled-window", Select).value == "next"
        assert table.row_count == 1


def test_job_view_uses_named_timezone_wall_time(tmp_path: Path) -> None:
    from zoneinfo import ZoneInfo

    drafts, scheduler = _services(tmp_path)
    draft = drafts.create(DraftData(reference="dft-zone", subject="Synthetic timezone check"))
    intended = datetime(2026, 9, 20, 9, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    job = scheduler.schedule_at(draft.reference, intended, now=NOW, timezone="Africa/Cairo")
    backend = SchedulerServiceBackend(scheduler, online=False, clock=lambda: NOW)
    _, jobs = backend.list_jobs(states=(), query="", limit=10, cancel=Event(), progress=_progress)
    view = next(row for row in jobs if row.reference == job.reference)
    assert view.scheduled_for == "2026-09-20T09:00:00+03:00"
    assert view.timezone == "Africa/Cairo"
