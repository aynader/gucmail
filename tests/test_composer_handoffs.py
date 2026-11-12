"""Cross-workspace reviews and palette saves use the same retained editor contract."""

import asyncio
from threading import Event

import pytest
from textual.widgets import Button, Input, Select, Static

from gucmail.commands import CommandInvocation
from gucmail.errors import LocalStateError
from gucmail.models import Address, DraftData
from gucmail.services.drafts import DraftService
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, PaletteScreen
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from gucmail.tui.palette_routes import catalog_route
from test_composer_submission_recovery import open_composer, prepared, until


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
async def test_drafts_send_handoff_waits_for_status_without_dropping_review(
    tmp_path, monkeypatch, overlay, blocked
):
    context, gateway, app = prepared(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    drafts.create(DraftData(
        "dft-handoff", to=[Address("course@example.test")], subject="Reviewed handoff",
        body_text="Preserved handoff body",
    ))
    if blocked:
        attempts = SendAttemptStore(context.state)
        attempts.claim("dft-handoff", "synthetic-uncertain-handoff")
        attempts.finish("dft-handoff", "synthetic-uncertain-handoff", "uncertain")
    started, release = Event(), Event()
    original = SendAttemptStore.composition_status

    def held_status(self, reference):
        started.set()
        assert release.wait(15)
        return original(self, reference)

    monkeypatch.setattr(SendAttemptStore, "composition_status", held_status)
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            app._open_area("drafts")
            await until(pilot, lambda: isinstance(app.screen, DraftAttachmentScreen))
            workspace = app.screen
            await until(
                pilot,
                lambda: not workspace._operation_active and workspace.selected_draft_reference,
            )
            route = catalog_route("drafts.send_draft")
            app._catalog_screen_action(route, CommandInvocation(route.command_id))
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen) and started.is_set())
            composer = app.screen
            assert composer.query_one("#compose-send", Button).disabled
            # Ensure the old fixed handoff timer has fired while status is held.
            await asyncio.sleep(0.12)
            if overlay:
                app.action_palette()
                await until(pilot, lambda: isinstance(app.screen, PaletteScreen))
            release.set()
            if overlay:
                await until(pilot, lambda: not composer._attempt_pending)
                assert isinstance(app.screen, PaletteScreen)
                await pilot.press("escape")
            if blocked:
                await until(pilot, lambda: not composer._attempt_pending)
                assert app.screen is composer
                assert composer.query_one("#compose-send", Button).disabled
                assert not gateway.deliveries()
                return
            await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "Reviewed handoff" in app.screen.detail and not gateway.deliveries()
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is composer and not composer._attempt_pending)
            assert app.screen is composer and not gateway.deliveries()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_palette_local_save_keeps_form_through_rejection(tmp_path, monkeypatch):
    _context, gateway, app = prepared(tmp_path)
    started, release = Event(), Event()

    def held_rejection(*args, **kwargs):
        started.set()
        assert release.wait(15)
        raise LocalStateError("Synthetic write rejected")

    monkeypatch.setattr(DraftService, "create", held_rejection)
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            composer = await open_composer(app, pilot)
            composer.query_one("#compose-subject", Input).value = "Retained palette Save"
            route = catalog_route("compose.save_local_draft")
            app._catalog_direct(route, CommandInvocation(route.command_id))
            await until(pilot, started.is_set)
            assert app.screen is composer, "Palette Save dismissed the pending form"
            release.set()
            await until(
                pilot, lambda: not composer._submission_pending and not composer._attempt_pending
            )
            assert app.screen is composer
            assert composer.query_one("#compose-subject", Input).value == "Retained palette Save"
            assert "retained" in str(composer.query_one("#compose-note", Static).content)
            assert not gateway.deliveries()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_palette_cannot_edit_an_uncertain_composition(tmp_path):
    context, gateway, app = prepared(tmp_path)
    claims = SendAttemptStore(context.state)
    claims.claim("dft-reviewed-submission", "synthetic")
    claims.finish("dft-reviewed-submission", "synthetic", "uncertain")
    async with app.run_test(size=(120, 36)) as pilot:
        composer = await open_composer(app, pilot)
        importance = composer.query_one("#compose-importance", Select)
        before = importance.value
        route = catalog_route("compose.set_importance")
        app._catalog_direct(route, CommandInvocation(route.command_id))
        await pilot.pause()
        assert importance.value == before and not gateway.deliveries()
