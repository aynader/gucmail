from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gucmail.models import Address, DraftData, SendState
from gucmail.platform_scheduler import generate_scheduler_plan, install_scheduler
from gucmail.services.drafts import DraftService
from gucmail.services.recurring import (
    RecurrenceFrequency,
    RecurrenceRule,
    iter_occurrences,
)
from gucmail.services.scheduler import RetryableSendFailure, SchedulerService
from gucmail.state import StateStore

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def make_services(
    tmp_path: Path,
    sender: object,
    **scheduler_options: object,
) -> tuple[DraftService, SchedulerService]:
    state = StateStore(tmp_path / "state.sqlite3")
    drafts = DraftService(state, tmp_path / "recovery")
    scheduler = SchedulerService(
        state,
        drafts,
        sender,  # type: ignore[arg-type]
        timezone="UTC",
        **scheduler_options,  # type: ignore[arg-type]
    )
    return drafts, scheduler


def create_sendable_draft(drafts: DraftService, reference: str = "dft-send") -> DraftData:
    return drafts.create(
        DraftData(
            reference=reference,
            to=[Address("recipient@example.edu")],
            subject="Scheduled",
            body_text="Hello",
        )
    )


def test_due_job_is_claimed_once_and_records_idempotency_key(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def sender(draft: DraftData, idempotency_key: str) -> str:
        calls.append((draft.reference, idempotency_key))
        return "sent-item-1"

    drafts, scheduler = make_services(tmp_path, sender)
    draft = create_sendable_draft(drafts)
    scheduled = scheduler.schedule(draft.reference, "in 1 hour", now=NOW)

    first = scheduler.run_once(now=NOW + timedelta(hours=2))
    second = scheduler.run_once(now=NOW + timedelta(hours=3))

    assert first.sent == 1
    assert second.claimed == 0
    assert len(calls) == 1
    assert calls[0][1] == scheduled.idempotency_key
    assert scheduler.get(scheduled.reference).message.state is SendState.SENT
    assert scheduler.get(scheduled.reference).message.outcome_reference == "sent-item-1"
    assert not drafts.recovery_path(draft.reference).exists()


def test_ambiguous_send_is_never_retried_automatically(tmp_path: Path) -> None:
    calls = 0

    def sender(_draft: DraftData) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("connection vanished after upload")

    drafts, scheduler = make_services(tmp_path, sender)
    draft = create_sendable_draft(drafts)
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(minutes=1),
        now=NOW,
        timezone="UTC",
    )

    result = scheduler.run_once(now=NOW + timedelta(minutes=2))
    later = scheduler.run_once(now=NOW + timedelta(days=1))

    assert result.needs_review == 1
    assert later.claimed == 0
    assert calls == 1
    stored = scheduler.get(job.reference).message
    assert stored.state is SendState.MANUAL_REVIEW
    assert "automatic retry is disabled" in (stored.last_error or "")


def test_known_pre_send_failure_uses_backoff_then_retries(tmp_path: Path) -> None:
    calls = 0

    def sender(_draft: DraftData) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableSendFailure("offline before request")
        return "sent-after-retry"

    drafts, scheduler = make_services(
        tmp_path,
        sender,
        retry_attempts=2,
        retry_base_seconds=0.0,
        retry_max_seconds=0.0,
    )
    draft = create_sendable_draft(drafts)
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(minutes=1),
        now=NOW,
        timezone="UTC",
    )

    first = scheduler.run_once(now=NOW + timedelta(minutes=2))
    second = scheduler.run_once(now=NOW + timedelta(minutes=3))

    assert first.executions[0].state is SendState.WAITING_CONNECTIVITY
    assert second.sent == 1
    assert calls == 2
    assert scheduler.get(job.reference).message.state is SendState.SENT


def test_changed_scheduled_attachment_requires_manual_review(tmp_path: Path) -> None:
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("original", encoding="utf-8")
    calls = 0

    def sender(_draft: DraftData) -> None:
        nonlocal calls
        calls += 1

    drafts, scheduler = make_services(tmp_path, sender)
    draft = create_sendable_draft(drafts)
    draft = drafts.edit(
        draft.reference,
        expected_revision=draft.revision,
        attachment_paths=[attachment],
    )
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(minutes=1),
        now=NOW,
        timezone="UTC",
    )
    attachment.write_text("modified", encoding="utf-8")

    result = scheduler.run_once(now=NOW + timedelta(minutes=2))

    assert result.needs_review == 1
    assert calls == 0
    assert scheduler.get(job.reference).message.state is SendState.MANUAL_REVIEW


def test_undo_send_can_only_cancel_local_delay(tmp_path: Path) -> None:
    drafts, scheduler = make_services(tmp_path, lambda _draft: None)
    draft = create_sendable_draft(drafts)
    job = scheduler.queue_undo_send(draft.reference, 30, now=NOW)

    cancelled = scheduler.undo(job.reference)

    assert cancelled.state is SendState.CANCELLED
    assert scheduler.run_once(now=NOW + timedelta(minutes=1)).claimed == 0


def test_edit_reschedule_list_and_send_now_are_durable(tmp_path: Path) -> None:
    calls: list[str] = []

    def sender(draft: DraftData) -> str:
        calls.append(draft.reference)
        return f"sent-{draft.reference}"

    drafts, scheduler = make_services(tmp_path, sender)
    first_draft = create_sendable_draft(drafts, "dft-first")
    second_draft = create_sendable_draft(drafts, "dft-second")
    first = scheduler.schedule_at(
        first_draft.reference,
        NOW + timedelta(minutes=1),
        now=NOW,
        timezone="UTC",
    )
    second = scheduler.schedule_at(
        second_draft.reference,
        NOW + timedelta(hours=1),
        now=NOW,
        timezone="UTC",
    )

    scheduler.edit(second.reference, draft_changes={"subject": "Edited while queued"})
    rescheduled = scheduler.reschedule(
        second.reference,
        "in 3 hours",
        timezone="UTC",
        now=NOW,
    )
    execution = scheduler.send_now(second.reference, now=NOW + timedelta(seconds=1))

    assert rescheduled.scheduled_for == NOW + timedelta(hours=3)
    assert execution.state is SendState.SENT
    assert calls == [second_draft.reference]
    assert scheduler.get(first.reference).message.state is SendState.SCHEDULED
    assert {item.message.reference for item in scheduler.list()} == {
        first.reference,
        second.reference,
    }
    assert len(scheduler.list(limit=1)) == 1
    assert drafts.get(second_draft.reference).subject == "Edited while queued"


def test_expired_send_lease_moves_to_manual_review_without_resend(tmp_path: Path) -> None:
    calls = 0

    def sender(_draft: DraftData) -> None:
        nonlocal calls
        calls += 1

    drafts, scheduler = make_services(tmp_path, sender, lease_seconds=10)
    draft = create_sendable_draft(drafts)
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(seconds=1),
        now=NOW,
        timezone="UTC",
    )
    scheduler.state.claim_due_jobs(
        worker_id="crashed-worker",
        now=NOW + timedelta(seconds=2),
        lease_seconds=10,
    )

    result = scheduler.run_once(now=NOW + timedelta(seconds=20))

    assert result.stale_recovered == 1
    assert result.claimed == 0
    assert calls == 0
    assert scheduler.get(job.reference).message.state is SendState.MANUAL_REVIEW


def test_recurring_helpers_generate_independent_weekly_dates() -> None:
    rule = RecurrenceRule(
        start_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        frequency=RecurrenceFrequency.WEEKLY,
        timezone="UTC",
        weekdays=(0, 2),
        count=4,
    )

    values = iter_occurrences(
        rule,
        after=datetime(2026, 8, 3, 8, 0, tzinfo=UTC),
        limit=10,
    )

    assert values == (
        datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


def test_platform_scheduler_is_a_pure_dry_run_by_default(tmp_path: Path) -> None:
    target = tmp_path / "systemd"
    plan = generate_scheduler_plan(
        platform_name="linux",
        output_directory=target,
        arguments=("python", "-m", "gucmail", "scheduler", "run-once"),
    )

    result = install_scheduler(plan)

    assert result.dry_run is True
    assert not target.exists()
    assert "Persistent=true" in plan.artifacts[1].content
