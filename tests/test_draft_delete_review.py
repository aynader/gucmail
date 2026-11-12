"""Delete only reviewed draft revisions and serialize recovery-file cleanup."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from textual.widgets import Button, DataTable

from gucmail.errors import LocalStateError
from gucmail.models import DraftData
from gucmail.services.drafts import DraftService
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from test_tui import context_for


async def until(pilot, predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.005)
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, "edit", "recreate"])
async def test_delete_confirmation_is_scoped_to_the_reviewed_revision(tmp_path, changed):
    context = context_for(tmp_path / "profile")
    service = DraftService(context.state, context.paths.recovery_dir)
    saved = service.create(
        DraftData("dft-reviewed", subject="Reviewed subject", body_text="Original")
    )
    screen = DraftAttachmentScreen(
        ServiceDraftAttachmentBackend(service, online=False),
        offline=True,
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await until(pilot, lambda: not screen._operation_active and bool(screen._drafts))
        await pilot.click("#draft-delete")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        other = DraftService(
            StateStore(context.state.path, account_key=context.state.account_key),
            context.paths.recovery_dir,
        )
        if changed == "edit":
            latest = other.edit(saved.reference, body_text="Newer work from another session")
        elif changed == "recreate":
            assert other.delete(saved.reference)
            latest = other.create(
                DraftData(saved.reference, body_text="New copy with revision one")
            )
            assert latest.revision == saved.revision
        else:
            latest = saved
        await pilot.press("y")
        await until(pilot, lambda: app.screen is screen and not screen._operation_active)
        if changed:
            assert other.exists(saved.reference), "An old review must not delete newer work"
            assert other.get(saved.reference) == latest
            assert other.read_recovery(saved.reference) == latest
            assert screen.view_state is ViewState.ERROR
            assert screen.selected_draft_reference is None
            assert screen.query_one("#draft-delete", Button).disabled
        else:
            assert not other.exists(saved.reference)
            assert not other.recovery_path(saved.reference).exists()
            assert screen.query_one("#draft-table", DataTable).row_count == 0


def test_recovery_cleanup_failure_preserves_saved_draft_and_revision_history(tmp_path, monkeypatch):
    service = DraftService(StateStore(tmp_path / "state.sqlite"), tmp_path / "recovery")
    saved = service.create(DraftData("dft-cleanup", subject="Retain on failure"))
    before = service.revisions(saved.reference)

    def rejected(_reference):
        raise LocalStateError("Synthetic recovery cleanup rejected")

    monkeypatch.setattr(service, "remove_recovery", rejected)
    with pytest.raises(LocalStateError, match="cleanup rejected"):
        service.delete(saved.reference)
    assert service.get(saved.reference) == saved
    assert service.revisions(saved.reference) == before
    assert service.read_recovery(saved.reference) == saved


def test_delete_holds_writer_lock_until_recovery_cleanup_finishes(tmp_path, monkeypatch):
    path, recovery = tmp_path / "state.sqlite", tmp_path / "recovery"
    deleting = DraftService(StateStore(path), recovery)
    creating = DraftService(StateStore(path), recovery)
    old = deleting.create(DraftData("dft-reused", subject="Old copy"))
    creating.state.initialize()
    original_connect = creating.state._connect

    def no_wait():
        connection = original_connect()
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    monkeypatch.setattr(creating.state, "_connect", no_wait)
    entered, release = Event(), Event()
    remove = deleting.remove_recovery

    def held(reference):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Controlled cleanup was not released")
        return remove(reference)

    monkeypatch.setattr(deleting, "remove_recovery", held)
    new = DraftData(old.reference, subject="New copy after deletion")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(deleting.delete, old.reference)
        assert entered.wait(5)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                creating.create(new)
        finally:
            release.set()
        assert future.result(timeout=10)
    saved = creating.create(new)
    assert creating.read_recovery(saved.reference) == saved
