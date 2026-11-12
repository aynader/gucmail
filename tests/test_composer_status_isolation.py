"""Slow, unavailable and stale local status cannot replace protected recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from textual.widgets import Button, Input, ListView, Static, TextArea

from gucmail.commands import CommandInvocation
from gucmail.errors import LocalStateError
from gucmail.models import Address, DraftData
from gucmail.services.compose import ComposeService
from gucmail.services.drafts import DraftService
from gucmail.services.scheduler import SchedulerService
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.state import StateStore
from gucmail.tui.app import (
    ComposeScreen,
    ConfirmationScreen,
    PaletteParameterReviewScreen,
    PaletteScreen,
)
from gucmail.tui.palette_routes import catalog_route
from gucmail.tui.write_coordinator import app_write_coordinator
from test_composer_submission_recovery import prepared, until


def persisted(context):
    drafts = DraftService(context.state, context.paths.recovery_dir)
    draft = drafts.create(DraftData(
        "dft-status-isolation", to=[Address("course@example.test")],
        subject="Exact synthetic recovery", body_text="Exact attempted body\nLast line",
    ))
    drafts.write_recovery(draft)
    return drafts, draft


def block(context, draft, state):
    if state == "queued":
        SchedulerService(
            context.state, DraftService(context.state, context.paths.recovery_dir),
            lambda _draft: pytest.fail("This check must never run the scheduler sender"),
        ).schedule_at(
            draft.reference, datetime.now(UTC) + timedelta(days=1)
        )
    else:
        attempts = SendAttemptStore(context.state)
        attempts.claim(draft.reference, "synthetic-blocking-status")
        attempts.finish(draft.reference, "synthetic-blocking-status", state)


def hold_status(monkeypatch, finished=None):
    started, release = Event(), Event()
    original = ComposeScreen.read_send_attempt

    def held(self):
        started.set()
        assert release.wait(20)
        try:
            return original(self)
        finally:
            if finished is not None:
                finished.set()

    monkeypatch.setattr(ComposeScreen, "read_send_attempt", held)
    return started, release


@pytest.mark.asyncio
async def test_new_composer_warning_during_status_read_survives_safe_completion(
    tmp_path, monkeypatch
):
    context, gateway, app = prepared(tmp_path)
    _drafts, draft = persisted(context)
    started, release = hold_status(monkeypatch)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen) and started.is_set())
            composer = app.screen
            message = "Another change is pending. The composer remains open."
            composer._set_note(message)
            assert str(composer.query_one("#compose-note", Static).content) == message
            release.set()
            await until(pilot, lambda: not composer._attempt_pending)
            assert str(composer.query_one("#compose-note", Static).content) == message
            assert not composer.query_one("#compose-body", TextArea).read_only
            assert not gateway.deliveries() and gateway.counts["simulated_send"] == 0
    finally:
        release.set()
        context.close()


@pytest.mark.asyncio
async def test_status_only_wait_notice_is_removed_after_safe_completion(tmp_path, monkeypatch):
    context, gateway, app = prepared(tmp_path)
    _drafts, draft = persisted(context)
    started, release = hold_status(monkeypatch)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen) and started.is_set())
            composer = app.screen
            composer.action_review_send()
            assert "Wait before changing" in str(
                composer.query_one("#compose-note", Static).content
            )
            release.set()
            await until(pilot, lambda: not composer._attempt_pending)
            assert "Checking local send status" not in str(
                composer.query_one("#compose-note", Static).content
            )
            assert not composer.query_one("#compose-body", TextArea).read_only
            assert not gateway.deliveries() and gateway.counts["simulated_send"] == 0
    finally:
        release.set()
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["uncertain", "queued"])
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_pending_restart_status_locks_all_save_routes_and_keeps_exact_recovery(
    tmp_path, monkeypatch, state, size
):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    block(context, draft, state)
    before = drafts.recovery_path(draft.reference).read_bytes()
    started, release = hold_status(monkeypatch)
    try:
        async with app.run_test(size=size) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen) and started.is_set())
            composer = app.screen
            body = composer.query_one("#compose-body", TextArea)
            assert composer._attempt_pending and body.read_only
            assert composer.query_one("#compose-to", Input).disabled
            assert composer.query_one("#compose-save", Button).disabled
            body.focus()
            await pilot.press("x")
            assert body.text == draft.body_text
            composer._request_autosave()
            route = catalog_route("compose.save_local_draft")
            app._catalog_direct(route, CommandInvocation(route.command_id))
            app._palette_save_current_draft(CommandInvocation("draft.save"), None)
            app._preview_discard_composer(composer)
            composer._submit_value(composer._payload(action="save"))
            await asyncio.sleep(0.05)
            assert not composer._submission_pending and app.screen is composer
            assert drafts.recovery_path(draft.reference).read_bytes() == before
            assert drafts.get(draft.reference).revision == 1
            release.set()
            await until(pilot, lambda: not composer._attempt_pending)
            assert composer.query_one("#compose-save", Button).disabled and body.read_only
            app._preview_discard_composer(composer)
            assert app.screen is composer
            composer.action_close()
            await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
            await until(pilot, lambda: app.screen is not composer)
    finally:
        release.set()
    fresh = StateStore(context.state.path, account_key=context.state.account_key)
    assert DraftService(fresh, context.paths.recovery_dir).get(draft.reference).revision == 1
    assert drafts.recovery_path(draft.reference).read_bytes() == before
    assert not gateway.deliveries()


@pytest.mark.asyncio
async def test_superseded_safe_status_cannot_unlock_newer_uncertain_state(tmp_path, monkeypatch):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    before = drafts.recovery_path(draft.reference).read_bytes()
    started, release, finished = Event(), Event(), Event()
    original = ComposeScreen.read_send_attempt

    def stale_first_read(self):
        snapshot = original(self)
        if not started.is_set():
            started.set()
            assert release.wait(20)
            finished.set()
        return snapshot

    try:
        async with app.run_test(size=(120, 36)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
            composer = app.screen
            await until(pilot, lambda: not composer._attempt_pending)
            monkeypatch.setattr(ComposeScreen, "read_send_attempt", stale_first_read)
            composer.request_send_status()
            await until(pilot, started.is_set)
            block(context, draft, "uncertain")
            composer.request_send_status()
            await until(pilot, lambda: composer._attempt_blocked and not composer._attempt_pending)
            release.set()
            await until(pilot, finished.is_set)
            assert composer._attempt_state == "uncertain"
            assert composer.query_one("#compose-body", TextArea).read_only
            assert composer.query_one("#compose-save", Button).disabled
            assert drafts.recovery_path(draft.reference).read_bytes() == before
            assert not gateway.deliveries()
    finally:
        release.set()
    fresh = StateStore(context.state.path, account_key=context.state.account_key)
    assert DraftService(fresh, context.paths.recovery_dir).get(draft.reference).revision == 1
    assert drafts.recovery_path(draft.reference).read_bytes() == before
    assert not gateway.deliveries()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["safe", "blocked", "closed"])
async def test_real_palette_save_waits_for_resume_status_once_and_honors_block_or_close(
    tmp_path, monkeypatch, outcome
):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    before = drafts.recovery_path(draft.reference).read_bytes()
    release = Event()
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
            composer = app.screen
            await until(pilot, lambda: not composer._attempt_pending)
            if outcome == "safe":
                composer.query_one("#compose-body", TextArea).focus()
                await pilot.press("x")
            await pilot.press("ctrl+k")
            await until(pilot, lambda: isinstance(app.screen, PaletteScreen))
            palette = app.screen
            palette.query_one("#palette-input", Input).value = "save local draft"
            await until(pilot, lambda: bool(palette._suggestions))
            matching = [
                suggestion.command.command_id for suggestion in palette._suggestions
            ]
            assert "draft.save" in matching, matching
            index = next(
                index for index, suggestion in enumerate(palette._display_suggestions)
                if suggestion is not None
                and suggestion.command.command_id == "draft.save"
            )
            choices = palette.query_one("#palette-suggestions", ListView)
            choices.index = index
            choices.focus()
            finished = Event()
            started, release = hold_status(monkeypatch, finished)
            if outcome == "blocked":
                block(context, draft, "uncertain")
            await pilot.press("enter")
            await pilot.pause()
            if isinstance(app.screen, PaletteParameterReviewScreen):
                await pilot.click("#parameter-review-continue")
                await pilot.pause()
            if isinstance(app.screen, ConfirmationScreen):
                await pilot.press("y")
            await until(
                pilot,
                lambda: started.is_set() and composer._status_ready_action is not None,
            )
            assert app.screen is composer and composer._attempt_pending
            assert drafts.get(draft.reference).revision == 1
            if outcome == "closed":
                composer.action_close()
                await until(pilot, lambda: app.screen is not composer)
            release.set()
            if outcome == "safe":
                await until(pilot, lambda: app.screen is not composer)
                assert drafts.get(draft.reference).revision == 2
            elif outcome == "blocked":
                await until(
                    pilot,
                    lambda: composer._attempt_blocked and composer._status_ready_action is None,
                )
                assert app.screen is composer
            else:
                await until(pilot, finished.is_set)
                assert composer not in app.screen_stack
                assert composer._status_ready_action is None
            fresh = StateStore(context.state.path, account_key=context.state.account_key)
            stored = DraftService(fresh, context.paths.recovery_dir).get(draft.reference)
            assert stored.revision == (2 if outcome == "safe" else 1)
            if outcome != "safe":
                assert drafts.recovery_path(draft.reference).read_bytes() == before
            assert not gateway.deliveries()
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "failed"])
async def test_safe_status_restores_ordinary_edit_save_and_fresh_readback(
    tmp_path, monkeypatch, state
):
    context, gateway, app = prepared(tmp_path)
    _drafts, draft = persisted(context)
    if state:
        block(context, draft, state)
    started, release = hold_status(monkeypatch)
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen) and started.is_set())
            composer = app.screen
            body = composer.query_one("#compose-body", TextArea)
            assert body.read_only and composer.query_one("#compose-save", Button).disabled
            release.set()
            await until(pilot, lambda: not composer._attempt_pending)
            assert not body.read_only
            assert not composer.query_one("#compose-to", Input).disabled
            assert not composer.query_one("#compose-save", Button).disabled
            body.focus()
            await pilot.press("x")
            assert body.text != draft.body_text
            composer.query_one("#compose-save", Button).focus()
            await pilot.press("enter")
            await until(pilot, lambda: app.screen is not composer)
            fresh = StateStore(context.state.path, account_key=context.state.account_key)
            stored = DraftService(fresh, context.paths.recovery_dir).get(draft.reference)
            assert stored.body_text == body.text and stored.revision == 2
            assert not gateway.deliveries()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_unavailable_status_stays_read_only_until_successful_explicit_recheck(
    tmp_path, monkeypatch
):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    before = drafts.recovery_path(draft.reference).read_bytes()
    original = ComposeScreen.read_send_attempt

    def unavailable(self):
        raise LocalStateError("Synthetic status read rejected")

    monkeypatch.setattr(ComposeScreen, "read_send_attempt", unavailable)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(draft)
        await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
        composer = app.screen
        await until(pilot, lambda: not composer._attempt_pending)
        body = composer.query_one("#compose-body", TextArea)
        assert body.read_only and composer.query_one("#compose-save", Button).disabled
        body.focus()
        await pilot.press("x")
        composer._request_autosave()
        assert body.text == draft.body_text
        assert drafts.recovery_path(draft.reference).read_bytes() == before
        monkeypatch.setattr(ComposeScreen, "read_send_attempt", original)
        composer.request_send_status()
        await until(pilot, lambda: not composer._attempt_pending)
        assert not body.read_only and not composer.query_one("#compose-save", Button).disabled
        assert not gateway.deliveries()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["uncertain", "queued"])
async def test_stale_autosave_and_save_consult_durable_claim_before_replacing_recovery(
    tmp_path, state
):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    before = drafts.recovery_path(draft.reference).read_bytes()
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_compose(draft)
        await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
        composer = app.screen
        await until(pilot, lambda: not composer._attempt_pending)
        composer.query_one("#compose-body", TextArea).load_text("Stale edited autosave payload")
        stale = composer._payload(action="autosave")
        block(context, draft, state)
        await app.autosave_composer(stale).wait()
        assert drafts.recovery_path(draft.reference).read_bytes() == before
        app._palette_save_current_draft(CommandInvocation("draft.save"), None)
        await until(
            pilot,
            lambda: composer._attempt_blocked and not composer._submission_pending,
        )
        assert app.screen is composer and not app_write_coordinator(app).pending
        assert drafts.get(draft.reference).revision == 1
        assert drafts.recovery_path(draft.reference).read_bytes() == before
        assert not gateway.deliveries()


@pytest.mark.asyncio
async def test_late_save_preflight_cannot_reenable_or_replace_newly_blocked_composition(
    tmp_path, monkeypatch
):
    context, gateway, app = prepared(tmp_path)
    drafts, draft = persisted(context)
    before = drafts.recovery_path(draft.reference).read_bytes()
    original = ComposeService.validate
    started, release = Event(), Event()

    def held_validation(self, *args, **kwargs):
        started.set()
        assert release.wait(20)
        return original(self, *args, **kwargs)

    try:
        async with app.run_test(size=(120, 36)) as pilot:
            app._open_compose(draft)
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
            composer = app.screen
            await until(pilot, lambda: not composer._attempt_pending)
            monkeypatch.setattr(ComposeService, "validate", held_validation)
            composer.query_one("#compose-save", Button).focus()
            await pilot.press("enter")
            await until(pilot, started.is_set)
            block(context, draft, "uncertain")
            composer.request_send_status()
            await until(pilot, lambda: composer._attempt_blocked)
            release.set()
            async with asyncio.timeout(15):
                while any(
                    not worker.is_finished for worker in app.workers
                    if worker.group == "compose-save-preflight"
                ):
                    await asyncio.sleep(0.01)
            await pilot.pause()
            assert app.screen is composer
            assert composer.query_one("#compose-save", Button).disabled
            assert drafts.get(draft.reference).revision == 1
            assert drafts.recovery_path(draft.reference).read_bytes() == before
            assert not gateway.deliveries()
    finally:
        release.set()
