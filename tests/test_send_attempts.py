from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import (
    ConflictError,
    GUCmailError,
    LocalStateError,
    NetworkError,
    SendOutcomeUncertainError,
)
from gucmail.models import Address, DraftData, SendState
from gucmail.services.compose import ComposeService
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.state import MIGRATIONS, StateStore


@pytest.mark.parametrize(
    "job_state,expected",
    [(state, {"sent": "sent", "sending": "sending", "send_state_uncertain": "uncertain"}
      .get(state.value, "queued"))
     for state in SendState if state not in {SendState.CANCELLED, SendState.FAILED}],
)
def test_composition_status_projects_queue_state_without_dispatch(tmp_path, job_state, expected):
    from datetime import UTC, datetime

    state = StateStore(tmp_path / "state.db")
    state.initialize()
    state.add_scheduled_job(
        reference="job-retained", draft_reference="draft-retained",
        scheduled_for=datetime(2026, 11, 2, tzinfo=UTC), timezone="UTC",
        original_expression="Synthetic", idempotency_key="synthetic", state=job_state,
    )
    attempt = SendAttemptStore(StateStore(state.path)).composition_status("draft-retained")
    assert attempt.state == expected and attempt.source == "scheduled"
    assert attempt.attempt_id == "job-retained" and attempt.queue_state == job_state.value
    state.update_job_state("job-retained", SendState.CANCELLED)
    assert SendAttemptStore(state).composition_status("draft-retained") is None


def test_immediate_status_precedes_queued_job_and_failed_attempt_does_not_hide_queue(tmp_path):
    from datetime import UTC, datetime

    state = StateStore(tmp_path / "state.db")
    attempts = SendAttemptStore(state)
    attempts.claim("draft", "attempt")
    state.add_scheduled_job(
        reference="job", draft_reference="draft", scheduled_for=datetime(2026, 11, 2, tzinfo=UTC),
        timezone="UTC", original_expression="Synthetic", idempotency_key="synthetic",
    )
    assert attempts.composition_status("draft").state == "sending"
    attempts.finish("draft", "attempt", "failed")
    assert attempts.composition_status("draft").state == "queued"
    state.update_job_state("job", SendState.FAILED)
    assert attempts.composition_status("draft").state == "failed"


def test_queue_projection_upgrades_v14_and_uses_bounded_indexed_lookup(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 15):
            connection.executescript(MIGRATIONS[version])
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic')", (version,))
        connection.execute("""
            WITH RECURSIVE n(value) AS (VALUES(1) UNION ALL SELECT value+1 FROM n WHERE value<50000)
            INSERT INTO scheduled_jobs(reference,draft_reference,scheduled_for,timezone,
                original_expression,state,idempotency_key,created_at,updated_at)
            SELECT 'job-'||value,'draft-'||value,'2026-11-02','UTC','Synthetic','scheduled',
                'key-'||value,'2026-11-02','2026-11-02' FROM n
        """)
    state = StateStore(path)
    state.initialize()
    original = state.connection
    from contextlib import contextmanager

    steps = 0

    @contextmanager
    def bounded_connection():
        nonlocal steps
        with original() as connection:
            def progress():
                nonlocal steps
                steps += 1
                return int(steps > 500)

            connection.set_progress_handler(progress, 1)
            yield connection

    monkeypatch.setattr(state, "connection", bounded_connection)
    result = state.blocking_job_for_draft("draft-50000")
    assert result == {"reference": "job-50000", "state": "scheduled", "updated_at": "2026-11-02"}
    assert state.blocking_job_for_draft("absent") is None
    assert steps <= 500, "Opening a composer must not scan the full scheduling history"
    with original() as connection:
        assert connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0] == 50000


class Sender:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    def send(self, _draft: DraftData) -> dict[str, bool]:
        self.calls += 1
        if self.error:
            raise self.error
        return {"accepted": True}


def _draft() -> DraftData:
    return DraftData(
        reference="dft-synthetic",
        to=[Address("synthetic@example.test")],
        subject="Synthetic",
        body_text="Only synthetic content",
    )


def test_successful_send_claim_survives_restart_and_prevents_resubmission(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    sender = Sender()
    service = ComposeService(sender, state=state)  # type: ignore[arg-type]
    plan = service.plan_send(_draft())
    assert service.execute_send(plan, confirmation_scope=plan.confirmation_scope).sent

    restarted = ComposeService(sender, state=StateStore(state.path))  # type: ignore[arg-type]
    new_plan = restarted.plan_send(_draft())
    with pytest.raises(ConflictError, match="not sent again"):
        restarted.execute_send(new_plan, confirmation_scope=new_plan.confirmation_scope)
    assert sender.calls == 1
    assert SendAttemptStore(state).get(_draft().reference).state == "sent"


def test_uncertain_send_remains_blocked_after_restart(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    sender = Sender()
    sender.error = SendOutcomeUncertainError("Synthetic connection interruption")
    service = ComposeService(sender, state=state)  # type: ignore[arg-type]
    plan = service.plan_send(_draft())
    with pytest.raises(SendOutcomeUncertainError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    sender.error = None
    restarted = ComposeService(sender, state=StateStore(state.path))  # type: ignore[arg-type]
    with pytest.raises(ConflictError) as error:
        restarted.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert error.value.context.mutation_uncertain
    assert sender.calls == 1
    assert SendAttemptStore(state).get(_draft().reference).state == "uncertain"


def test_definite_rejection_allows_an_explicit_new_attempt(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    sender = Sender()
    sender.error = ConflictError("Synthetic quota rejection")
    service = ComposeService(sender, state=state)  # type: ignore[arg-type]
    plan = service.plan_send(_draft())
    with pytest.raises(ConflictError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert SendAttemptStore(state).get(_draft().reference).state == "failed"
    sender.error = None
    assert service.execute_send(plan, confirmation_scope=plan.confirmation_scope).sent
    assert sender.calls == 2


def test_dry_run_never_claims_or_sends(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    sender = Sender()
    service = ComposeService(sender, state=state)  # type: ignore[arg-type]
    plan = service.plan_send(_draft())
    assert not service.execute_send(plan, dry_run=True).sent
    assert SendAttemptStore(state).get(_draft().reference) is None
    assert sender.calls == 0


@pytest.mark.parametrize("error_type", [GUCmailError, LocalStateError, NetworkError, RuntimeError])
def test_unclassified_failure_cannot_make_an_accepted_send_retryable(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    state = StateStore(tmp_path / "state.db")
    sender = Sender()
    sender.error = error_type("Synthetic failure after submission")
    service = ComposeService(sender, state=state)
    plan = service.plan_send(_draft())
    with pytest.raises(SendOutcomeUncertainError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert SendAttemptStore(state).get(_draft().reference).state == "uncertain"
    sender.error = None
    with pytest.raises(ConflictError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert sender.calls == 1


def test_concurrent_submissions_cross_process_store_boundary_once(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    started, release = Event(), Event()
    calls: list[str] = []

    class BlockingSender:
        def send(self, draft: DraftData) -> dict[str, bool]:
            calls.append(draft.reference)
            started.set()
            assert release.wait(5)
            return {"accepted": True}

    first = ComposeService(BlockingSender(), state=StateStore(path))  # type: ignore[arg-type]
    second = ComposeService(BlockingSender(), state=StateStore(path))  # type: ignore[arg-type]
    plan = first.plan_send(_draft())
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            first.execute_send, plan, confirmation_scope=plan.confirmation_scope
        )
        try:
            assert started.wait(5)
            with pytest.raises(ConflictError):
                second.execute_send(plan, confirmation_scope=plan.confirmation_scope)
        finally:
            release.set()
        assert pending.result(timeout=5).sent
    assert calls == ["dft-synthetic"]


def test_current_schema_upgrades_populated_version_seven_and_preserves_send_claims_and_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 8):
            connection.executescript(MIGRATIONS[version])
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic')", (version,))
        connection.execute("INSERT INTO metadata VALUES (?, ?)", ("keep", '{"value":"synthetic"}'))
        connection.execute(
            "INSERT INTO item_references VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("default", "msg-keep", "message", "remote-id", "key", "/Inbox", "now", "now"),
        )
    state = StateStore(path)
    claims = SendAttemptStore(state)
    claims.claim("dft-synthetic", "attempt-synthetic")
    claims.finish("dft-synthetic", "attempt-synthetic", "sent")
    assert state.resolve_reference("msg-keep")["ews_id"] == "remote-id"
    with state.connection() as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='keep'").fetchone()[0]
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
    restored = SendAttemptStore(StateStore(path)).get("dft-synthetic")
    assert restored is not None and restored.state == "sent"


def test_account_scoped_claims_do_not_block_a_different_account(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = SendAttemptStore(StateStore(path, account_key="synthetic-first"))
    second = SendAttemptStore(StateStore(path, account_key="synthetic-second"))
    first.claim("same-reference", "first-attempt")
    second.claim("same-reference", "second-attempt")
    assert first.get("same-reference").attempt_id == "first-attempt"
    assert second.get("same-reference").attempt_id == "second-attempt"


@pytest.mark.parametrize("first_path", ["scheduled", "immediate"])
def test_scheduler_and_immediate_send_share_durable_draft_claim(
    tmp_path: Path, first_path: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from gucmail.services.drafts import DraftService
    from gucmail.services.scheduler import SchedulerService

    now = datetime(2026, 9, 8, tzinfo=UTC)
    state = StateStore(tmp_path / "state.db")
    drafts = DraftService(state, tmp_path / "recovery")
    draft = drafts.create(_draft())
    sender = Sender()
    scheduler = SchedulerService(state, drafts, sender.send)
    scheduler.schedule_at(draft.reference, now + timedelta(minutes=1), now=now)
    compose = ComposeService(sender, state=StateStore(state.path))  # type: ignore[arg-type]
    plan = compose.plan_send(draft)
    if first_path == "scheduled":
        assert scheduler.run_once(now=now + timedelta(minutes=2)).sent == 1
        with pytest.raises(ConflictError):
            compose.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    else:
        assert compose.execute_send(plan, confirmation_scope=plan.confirmation_scope).sent
        assert scheduler.run_once(now=now + timedelta(minutes=2)).sent == 0
    assert sender.calls == 1
    assert SendAttemptStore(state).get(draft.reference).state == "sent"


def test_reconciliation_cannot_clear_a_different_or_successful_attempt(tmp_path: Path) -> None:
    attempts = SendAttemptStore(StateStore(tmp_path / "state.db"))
    attempts.claim("draft", "first")
    attempts.finish("draft", "first", "uncertain")
    with pytest.raises(ConflictError):
        attempts.reconcile("draft", "different", "failed")
    attempts.reconcile("draft", "first", "sent")
    with pytest.raises(ConflictError):
        attempts.reconcile("draft", "first", "failed")
    assert attempts.get("draft").state == "sent"


def test_scheduler_definite_reconciliation_updates_shared_send_claim(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from gucmail.services.drafts import DraftService
    from gucmail.services.scheduler import SchedulerService, SendCertainty, SendResult

    now = datetime(2026, 9, 8, tzinfo=UTC)
    state = StateStore(tmp_path / "state.db")
    drafts = DraftService(state, tmp_path / "recovery")
    draft = drafts.create(_draft())
    sender = Sender()
    sender.error = SendOutcomeUncertainError("Synthetic lost response")
    scheduler = SchedulerService(state, drafts, sender.send)
    job = scheduler.schedule_at(draft.reference, now + timedelta(minutes=1), now=now)
    assert scheduler.run_once(now=now + timedelta(minutes=2)).sent == 0
    assert SendAttemptStore(state).get(draft.reference).state == "uncertain"

    class ProvenReconciler:
        def reconcile_send(self, **kwargs):
            return SendResult(
                certainty=SendCertainty.DEFINITELY_NOT_SENT,
                idempotency_marker=kwargs["idempotency_key"],
            )

    scheduler.reconcile(job.reference, ProvenReconciler())
    assert sender.calls == 1
    assert SendAttemptStore(state).get(draft.reference).state == "failed"
    sender.error = None
    compose = ComposeService(sender, state=StateStore(state.path))  # type: ignore[arg-type]
    plan = compose.plan_send(draft)
    assert compose.execute_send(plan, confirmation_scope=plan.confirmation_scope).sent
    assert sender.calls == 2
