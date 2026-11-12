from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.models import Address, DraftData, SendState
from gucmail.services.compose import ComposeService
from gucmail.services.drafts import AttachmentIntegrityError, DraftService
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from test_cli import _environment
from test_scheduler import NOW, create_sendable_draft, make_services
from test_settings_secondpass import _context
from test_tui import FakeGateway


def reviewed_payload(app):
    """Model a completed review for direct worker boundary tests."""
    payload = app.screen._payload(action="send")
    plan = ComposeService().plan_send(app._draft_from_payload(payload), kind=app.screen.kind)
    return {**payload, "confirmation_scope": plan.confirmation_scope, "allow_empty_subject": False}


@pytest.mark.parametrize("options", [[], ["--schedule", "in 1 hour"], ["--undo-delay", "5"]])
def test_send_dry_run_never_resolves_gateway_or_creates_draft_or_job(
    tmp_path, monkeypatch, options
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("A send preview must be local")

    monkeypatch.setattr(cli, "_gateway", forbidden)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "send",
            "--to",
            "synthetic@example.test",
            "--subject",
            "Synthetic",
            "--body",
            "Body",
            "--dry-run",
            *options,
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["dry_run"] is True
    for path in (tmp_path / "data").glob("*.sqlite3"):
        with StateStore(path).connection() as connection:
            assert connection.execute("SELECT count(*) FROM local_drafts").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0] == 0


def test_scheduling_requires_confirmation_and_validates_before_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Scheduling is local"))
    args = [
        "--non-interactive",
        "send",
        "--to",
        "synthetic@example.test",
        "--subject",
        "Synthetic",
        "--body",
        "Body",
        "--schedule",
        "in 1 hour",
    ]
    denied = CliRunner().invoke(cli.app, args, env=_environment(tmp_path))
    assert denied.exit_code != 0 and "Confirmation required" in denied.output
    accepted = CliRunner().invoke(cli.app, ["--yes", *args], env=_environment(tmp_path))
    assert accepted.exit_code == 0, accepted.output
    invalid = CliRunner().invoke(
        cli.app,
        ["--yes", *args[:-2], "--attach", str(tmp_path / "missing.txt"), *args[-2:]],
        env=_environment(tmp_path),
    )
    assert invalid.exit_code != 0


def test_countdown_only_claims_due_target_and_honors_cancel_reschedule_pause(tmp_path):
    sent = []
    drafts, scheduler = make_services(
        tmp_path, lambda draft: sent.append(draft.reference) or "sent"
    )
    one = create_sendable_draft(drafts, "one")
    two = create_sendable_draft(drafts, "two")
    job = scheduler.queue_undo_send(one.reference, 5, now=NOW)
    other = scheduler.queue_undo_send(two.reference, 5, now=NOW)
    assert scheduler.run_reference_if_due(job.reference, now=NOW) is None
    scheduler.pause()
    assert scheduler.run_reference_if_due(job.reference, now=NOW + timedelta(seconds=6)) is None
    scheduler.resume()
    result = scheduler.run_reference_if_due(job.reference, now=NOW + timedelta(seconds=6))
    assert result.state is SendState.SENT and sent == ["one"]
    assert scheduler.run_reference_if_due(job.reference, now=NOW + timedelta(seconds=7)) is None
    scheduler.reschedule_at(other.reference, NOW + timedelta(hours=1), now=NOW)
    assert scheduler.run_reference_if_due(other.reference, now=NOW + timedelta(seconds=7)) is None
    scheduler.cancel(other.reference)
    assert scheduler.run_reference_if_due(other.reference, now=NOW + timedelta(hours=2)) is None
    assert sent == ["one"]


def test_missing_attachment_does_not_destroy_recovery_text(tmp_path):
    drafts = DraftService(StateStore(tmp_path / "state.sqlite3"), tmp_path / "recovery")
    draft = DraftData(
        "synthetic-recovery",
        subject="Keep this",
        body_text="Unsaved text",
        attachment_paths=[tmp_path / "gone.txt"],
    )
    drafts.write_recovery(draft)
    assert drafts.read_recovery(draft.reference).body_text == "Unsaved text"
    with pytest.raises(AttachmentIntegrityError, match="need review"):
        drafts.read_recovery(draft.reference, validate_attachments=True)


@pytest.mark.asyncio
async def test_composer_immediate_failure_preserves_latest_content_before_autosave(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    statuses = []
    monkeypatch.setattr(app, "_set_status", statuses.append)
    draft = DraftData(
        "synthetic-failure",
        to=[Address("synthetic@example.test")],
        subject="Latest subject",
        body_text="Latest body",
    )
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_compose(draft)
        await pilot.pause()
        payload = app.screen._payload(action="send")
        await app.submit_composer(payload).wait()
        recovered = DraftService(context.state, context.paths.recovery_dir).read_recovery(
            draft.reference
        )
        assert recovered.body_text == draft.body_text
        assert recovered.subject == draft.subject


@pytest.mark.asyncio
async def test_saved_undo_send_preference_queues_before_gateway_and_can_cancel(
    tmp_path, monkeypatch
):
    from gucmail.settings import SettingsService

    context = _context(tmp_path)
    SettingsService(context.config_store).set("composer.undo_send_seconds", 30)
    app = GUCmailApp(context, gateway=FakeGateway())
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Undo window must precede gateway access")
    )
    app.offline = True  # Keep startup local; the queue itself also works offline.
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_compose(
            DraftData(
                "synthetic-delay",
                to=[Address("synthetic@example.test")],
                subject="Delayed",
                body_text="Body",
            )
        )
        await pilot.pause()
        await app.submit_composer(reviewed_payload(app)).wait()
        service = app._scheduler_service()
        jobs = service.list()
        assert len(jobs) == 1 and jobs[0].message.state is SendState.UNDO_DELAY
        service.undo(jobs[0].message.reference)
        assert service.get(jobs[0].message.reference).message.state is SendState.CANCELLED


@pytest.mark.asyncio
async def test_tui_countdown_sends_exactly_once_after_delay(tmp_path):
    from gucmail.settings import SettingsService

    sent = []
    queued_at = []
    sent_at = []

    class SendingGateway(FakeGateway):
        def send(self, draft):
            sent.append(draft.reference)
            sent_at.append(time.monotonic())
            return {"accepted": True}

    class CountdownApp(GUCmailApp):
        def _queued_undo_send(self, reference, delay):
            queued_at.append(time.monotonic())
            super()._queued_undo_send(reference, delay)

    context = _context(tmp_path)
    SettingsService(context.config_store).set("composer.undo_send_seconds", 1)
    app = CountdownApp(context, gateway=SendingGateway())
    async with app.run_test(size=(120, 40)) as pilot:
        app._open_compose(
            DraftData(
                "synthetic-countdown",
                to=[Address("synthetic@example.test")],
                subject="Delayed",
                body_text="Body",
            )
        )
        await pilot.pause()
        await app.submit_composer(reviewed_payload(app)).wait()
        # Worker.wait() also yields to the UI. On a loaded host the real timer
        # can legitimately finish before it returns; check the send boundary's
        # elapsed time instead of assuming this assertion runs within a second.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await pilot.pause(0.025)
            if sent and app._scheduler_service().list()[0].message.state is SendState.SENT:
                break
        assert sent == ["synthetic-countdown"]
        assert sent_at[0] - queued_at[0] >= 1
        service = app._scheduler_service()
        job = service.list()[0].message
        assert job.state is SendState.SENT
        assert service.run_reference_if_due(job.reference) is None
        assert sent == ["synthetic-countdown"]


@pytest.mark.parametrize("changes", [{"to": []}, {"subject": ""}, {"body_text": ""}])
def test_incomplete_scheduled_draft_requires_review_before_send_boundary(tmp_path, changes):
    calls = []
    drafts, scheduler = make_services(tmp_path, lambda draft: calls.append(draft))
    draft = create_sendable_draft(drafts)
    draft = drafts.edit(draft.reference, expected_revision=draft.revision, **changes)
    job = scheduler.schedule_at(draft.reference, NOW + timedelta(seconds=5), now=NOW)
    result = scheduler.run_once(now=NOW + timedelta(seconds=6))
    assert result.needs_review == 1 and not calls
    assert scheduler.get(job.reference).message.state is SendState.MANUAL_REVIEW
    with drafts.state.connection() as connection:
        assert connection.execute("SELECT count(*) FROM send_attempts").fetchone()[0] == 0


def test_explicit_empty_send_authorization_survives_scheduler_reopen(tmp_path):
    from gucmail.services.scheduler import SchedulerService

    calls = []
    drafts, scheduler = make_services(tmp_path, lambda draft: calls.append(draft))
    draft = drafts.create(DraftData("empty-authorized", to=[Address("synthetic@example.test")]))
    job = scheduler.schedule_at(
        draft.reference,
        NOW + timedelta(seconds=5),
        now=NOW,
        allow_empty_subject=True,
        allow_empty_body=True,
    )
    reopened = SchedulerService(drafts.state, drafts, lambda draft: calls.append(draft))
    assert reopened.run_once(now=NOW + timedelta(seconds=6)).sent == 1
    assert len(calls) == 1
    assert reopened.get(job.reference).message.state is SendState.SENT
