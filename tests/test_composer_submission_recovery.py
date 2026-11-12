"""Persistent UI rejection/uncertainty recovery and in-flight composition ownership."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from qa.synthetic.context import synthetic_context
from qa.synthetic.mail import PersistentMail
from textual.widgets import Button, Collapsible, Input, Static, TextArea

from gucmail.errors import LocalStateError
from gucmail.models import Address, DraftData
from gucmail.services.drafts import DraftService
from gucmail.services.scheduler import SchedulerService
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.settings import SettingsService
from gucmail.state import StateStore
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp, PaletteScreen
from gucmail.tui.composer_submission import ComposerSendStatus
from gucmail.tui.write_coordinator import app_write_coordinator


async def until(pilot, predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.005)
    await pilot.pause()


def prepared(tmp_path, mode="accepted", gateway_type=PersistentMail):
    context = synthetic_context(tmp_path / "profile")
    SettingsService(context.config_store).set("composer.server_draft_sync", False)
    gateway = gateway_type(tmp_path / "mail.sqlite")
    gateway.mode = mode
    context._gateway = gateway
    return context, gateway, GUCmailApp(context, gateway=gateway)


async def open_composer(app, pilot):
    app._open_compose(
        DraftData(
            "dft-reviewed-submission",
            to=[Address("course@example.test")],
            subject="Synthetic reviewed composition",
            body_text="Current body\nSecond line",
        )
    )
    await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
    composer = app.screen
    await until(pilot, lambda: not composer._attempt_pending)
    return composer


async def review(pilot, app, composer):
    composer.action_review_send()
    await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))


@pytest.mark.asyncio
async def test_rejected_send_retains_editor_cursor_and_latest_recovery_before_corrected_send(
    tmp_path,
):
    context, gateway, app = prepared(tmp_path, "rejected")
    drafts = DraftService(context.state, context.paths.recovery_dir)
    async with app.run_test(size=(120, 36)) as pilot:
        composer = await open_composer(app, pilot)
        old_autosave = composer._payload(action="autosave")
        body = composer.query_one("#compose-body", TextArea)
        body.load_text("Latest submitted body\nRetained line")
        body.cursor_location = (1, 4)
        body.focus()
        cursor = body.cursor_location
        await review(pilot, app, composer)
        await pilot.press("y")
        await until(
            pilot, lambda: not composer._submission_pending and not composer._attempt_pending
        )
        assert app.screen is composer and body.text == "Latest submitted body\nRetained line"
        assert body.cursor_location == cursor and body.has_focus
        assert "rejected before acceptance" in str(
            composer.query_one("#compose-note", Static).content
        )
        assert not gateway.deliveries()
        assert SendAttemptStore(context.state).get(composer.draft.reference).state == "failed"
        await app.autosave_composer(old_autosave).wait()
        assert drafts.read_recovery(composer.draft.reference).body_text == body.text
        gateway.mode = "accepted"
        body.load_text("Corrected and explicitly reviewed body")
        await review(pilot, app, composer)
        await pilot.press("y")
        await until(pilot, lambda: app.screen is not composer)
        delivered = gateway.deliveries(composer.draft.reference)
        assert len(delivered) == 1 and gateway.counts["simulated_send"] == 2
        assert json.loads(delivered[0]["payload"])["body_text"] == body.text
        fresh = StateStore(context.state.path, account_key=context.state.account_key)
        assert SendAttemptStore(fresh).get(composer.draft.reference).state == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_lost_ack_retains_read_only_editor_and_restart_blocks_review_before_dispatch(
    tmp_path, size
):
    context, gateway, app = prepared(tmp_path, "lost-ack")
    drafts = DraftService(context.state, context.paths.recovery_dir)
    async with app.run_test(size=size) as pilot:
        composer = await open_composer(app, pilot)
        await review(pilot, app, composer)
        await pilot.press("y")
        await until(pilot, lambda: not composer._submission_pending)
        assert app.screen is composer and composer._submission_uncertain
        assert composer.query_one("#compose-send", Button).disabled
        assert composer.query_one("#compose-save", Button).disabled
        assert composer.query_one("#compose-body", TextArea).read_only
        assert len(gateway.deliveries()) == gateway.counts["simulated_send"] == 1
        before = drafts.read_recovery(composer.draft.reference)
        composer.action_review_send()
        assert app.screen is composer
        assert drafts.read_recovery(composer.draft.reference) == before
    restarted_context = replace(
        context, state=StateStore(context.state.path, account_key=context.state.account_key)
    )
    restarted = GUCmailApp(restarted_context, offline=True)
    async with restarted.run_test(size=size) as pilot:
        restarted._open_compose(before)
        await until(pilot, lambda: isinstance(restarted.screen, ComposeScreen))
        composer = restarted.screen
        await until(pilot, lambda: not composer._attempt_pending)
        assert composer.query_one("#compose-send", Button).disabled
        status = composer.query_one("#compose-send-status", Button)
        assert status.display and not status.disabled and status.region.right <= size[0]
        status.focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(restarted.screen, ComposerSendStatus))
        details = restarted.screen.query_one("#composer-status-technical", Collapsible)
        assert details.collapsed
        close = restarted.screen.query_one(".area-close", Button)
        assert close.region.width >= 5 and close.region.height >= 1
        footer = restarted.screen.query_one("#composer-status-actions")
        assert footer.content_region.contains_region(close.region)
        assert close.region.bottom <= size[1] and close.region.right <= size[0]
        await pilot.click("#composer-status-technical CollapsibleTitle")
        assert not details.collapsed
        assert "State: uncertain" in str(
            restarted.screen.query_one("#composer-status-text", Static).content
        )
        assert gateway.counts["simulated_send"] == 1
        await pilot.click(".area-close")
        await until(pilot, lambda: restarted.screen is composer)
        assert composer.query_one("#compose-send", Button).disabled


@pytest.mark.asyncio
async def test_failed_local_save_retains_input_until_real_sql_save_succeeds(tmp_path, monkeypatch):
    context, gateway, app = prepared(tmp_path)
    original = DraftService.create

    def rejected(self, *args, **kwargs):
        raise LocalStateError("Synthetic local write rejected")

    async with app.run_test(size=(120, 36)) as pilot:
        composer = await open_composer(app, pilot)
        monkeypatch.setattr(DraftService, "create", rejected)
        composer.query_one("#compose-save", Button).focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                not composer._submission_pending
                and "save could not" in str(composer.query_one("#compose-note", Static).content)
            ),
        )
        assert app.screen is composer and composer.query_one("#compose-body", TextArea).text
        assert not gateway.deliveries()
        save = composer.query_one("#compose-save", Button)
        await until(
            pilot,
            lambda: not app_write_coordinator(app).pending and not save.has_class("-active"),
        )
        monkeypatch.setattr(DraftService, "create", original)
        composer.query_one("#compose-subject", Input).value = "Corrected local save"
        save.focus()
        await until(pilot, lambda: save.has_focus)
        await pilot.press("enter")
        await until(pilot, lambda: app.screen is not composer)
        assert (
            DraftService(context.state, context.paths.recovery_dir)
            .get(composer.draft.reference)
            .subject
            == "Corrected local save"
        )


@pytest.mark.asyncio
async def test_pending_send_ignores_duplicate_submit_and_close_and_finishes_beneath_overlay(
    tmp_path,
):
    class HeldMail(PersistentMail):
        started, release = Event(), Event()

        def send(self, draft):
            self.started.set()
            if not self.release.wait(15):
                raise RuntimeError("Controlled send was not released")
            return super().send(draft)

    _context, gateway, app = prepared(tmp_path, gateway_type=HeldMail)
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            composer = await open_composer(app, pilot)
            await review(pilot, app, composer)
            await pilot.press("y")
            await until(pilot, gateway.started.is_set)
            composer.action_review_send()
            await pilot.press("escape")
            assert app.screen is composer and composer._submission_pending
            app.action_palette()
            await until(pilot, lambda: isinstance(app.screen, PaletteScreen))
            gateway.release.set()
            await until(pilot, lambda: composer._submission_complete)
            assert isinstance(app.screen, PaletteScreen)
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is app.screen_stack[0])
            assert len(gateway.deliveries()) == gateway.counts["simulated_send"] == 1
            assert not app_write_coordinator(app).pending
    finally:
        gateway.release.set()


@pytest.mark.asyncio
async def test_unavailable_local_send_status_blocks_review_without_any_provider_call(
    tmp_path, monkeypatch
):
    _context, gateway, app = prepared(tmp_path)

    def rejected(*_args, **_kwargs):
        raise LocalStateError("Synthetic local status unavailable")

    monkeypatch.setattr(SendAttemptStore, "get", rejected)
    async with app.run_test(size=(120, 36)) as pilot:
        composer = await open_composer(app, pilot)
        assert composer.query_one("#compose-send", Button).disabled
        composer.action_review_send()
        assert app.screen is composer and not gateway.deliveries()
        assert gateway.counts["simulated_send"] == 0


@pytest.mark.asyncio
async def test_queued_draft_reopened_after_restart_blocks_review_and_shows_job(tmp_path):
    context, gateway, _app = prepared(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    draft = drafts.create(
        DraftData(
            "dft-queued-recovery", to=[Address("course@example.test")],
            subject="Already queued", body_text="Retained queue content",
        )
    )
    now = datetime.now(UTC)
    scheduler = SchedulerService(context.state, drafts, gateway.send)
    job = scheduler.schedule_at(draft.reference, now + timedelta(hours=1), now=now)
    restarted_context = replace(
        context, state=StateStore(context.state.path, account_key=context.state.account_key)
    )
    app = GUCmailApp(restarted_context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(draft)
        await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
        composer = app.screen
        await until(pilot, lambda: not composer._attempt_pending)
        assert composer.query_one("#compose-send", Button).disabled
        composer.action_review_send()
        assert app.screen is composer and not gateway.deliveries()
        composer.query_one("#compose-send-status", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ComposerSendStatus))
        text = str(app.screen.query_one("#composer-status-text", Static).content)
        assert "State: queued" in text and f"Job: {job.reference}" in text
        assert scheduler.get(job.reference).message.state.value == "scheduled"
        assert not gateway.deliveries()


@pytest.mark.asyncio
async def test_queue_commit_with_lost_reply_retains_form_and_cannot_create_second_job(
    tmp_path, monkeypatch
):
    context, gateway, app = prepared(tmp_path)
    SettingsService(context.config_store).set("composer.undo_send_seconds", 30)
    original = SchedulerService.queue_undo_send

    def committed_without_reply(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise LocalStateError("Synthetic reply lost after queue commit")

    monkeypatch.setattr(SchedulerService, "queue_undo_send", committed_without_reply)
    async with app.run_test(size=(120, 36)) as pilot:
        composer = await open_composer(app, pilot)
        await review(pilot, app, composer)
        await pilot.press("y")
        await until(pilot, lambda: not composer._submission_pending)
        assert app.screen is composer and composer._submission_uncertain
        assert "queue result could not be confirmed" in str(
            composer.query_one("#compose-note", Static).content
        )
        jobs = context.state.list_scheduled_jobs()
        assert len(jobs) == 1 and jobs[0]["draft_reference"] == composer.draft.reference
        assert not gateway.deliveries() and gateway.counts["simulated_send"] == 0
        composer.action_review_send()
        assert len(context.state.list_scheduled_jobs()) == 1
        composer.query_one("#compose-send-status", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ComposerSendStatus))
        assert "State: queued" in str(
            app.screen.query_one("#composer-status-text", Static).content
        )
        fresh = StateStore(context.state.path, account_key=context.state.account_key)
        status = SendAttemptStore(fresh).composition_status(composer.draft.reference)
        assert status.source == "scheduled" and status.state == "queued"
        assert len(fresh.list_scheduled_jobs()) == 1
