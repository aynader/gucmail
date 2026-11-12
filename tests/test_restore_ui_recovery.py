"""Actual Backup Center confirmation, restart, local status and deliberate copies."""

import asyncio

import pytest
from qa.synthetic.context import synthetic_context
from qa.synthetic.restore_mailbox import OwnedRestoreMailbox
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Static

from gucmail.services.backup import BackupItem, BackupService
from gucmail.services.restore_journal import RestoreJournal, RestoreTargetIntent
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_backends import BackupAreaBackend
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.backup_screen import (
    BackupPassphraseScreen,
    BackupScreen,
    RestorePlanScreen,
    RestoreStatusScreen,
)


@pytest.fixture(autouse=True)
def _truecolor_terminal(monkeypatch):
    # Overlay colour assertions compare exact theme hex values, which requires a
    # truecolor terminal; otherwise Rich quantizes to the xterm-256 palette.
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    # On Windows Rich ignores COLORTERM and probes the console; Textual reads its
    # override at import time, so pin the already-loaded constant as well.
    import textual.constants

    monkeypatch.setattr(textual.constants, "COLOR_SYSTEM", "truecolor")


async def until(pilot, predicate):
    async with asyncio.timeout(25):
        while not predicate():
            await asyncio.sleep(0.005)
    await pilot.pause()


def prepared(tmp_path, mode, *, create=True, count=1):
    context = synthetic_context(tmp_path / "profile")
    backup_id = "synthetic-ui-protected"
    if create:
        BackupService(context.paths.backup_dir, context.state).create((
            BackupItem(f"msg-{i}", "message", {"subject": "Synthetic [literal] restore"},
                       folder_path="Synthetic/Inbox") for i in range(count)
        ), backup_id=backup_id)
    gateway = OwnedRestoreMailbox(tmp_path / "destination.sqlite", mode)
    context._gateway = gateway
    return context, gateway, GUCmailApp(context, gateway=gateway), backup_id


async def open_plan(app, pilot, backup_id, *, passphrase=None):
    app._open_area("backups")
    await until(pilot, lambda: isinstance(app.screen, BackupScreen))
    center = app.screen
    await until(pilot, lambda: center._selected_id == backup_id and not center._operation_active)
    center.query_one("#backup-restore-plan", Button).focus()
    await pilot.press("enter")
    if passphrase is not None:
        await until(pilot, lambda: isinstance(app.screen, BackupPassphraseScreen))
        app.screen.query_one("#backup-passphrase-input", Input).value = passphrase
        await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, RestorePlanScreen))
    return app.screen


async def confirm_restore(app, pilot, plan, expected_rows, gateway):
    plan.query_one("#restore-execute", Button).focus()
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
    await pilot.press("y")
    await until(pilot, lambda: app.screen is plan and not plan._operation_active
                and gateway.target.count() == expected_rows)


def assert_footer(screen, size):
    footer = screen.query_one("#restore-plan-actions")
    for button in footer.query(Button):
        if button.display and all(
            node.display for node in button.ancestors if isinstance(node, Widget)
        ):
            assert button.region.width and button.region.height
            assert footer.content_region.contains_region(button.region)
            assert button.region.bottom <= size[1] and button.region.right <= size[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_lost_ack_restarted_plan_blocks_replay_and_opens_read_only_status(tmp_path, size):
    context, gateway, app, backup_id = prepared(tmp_path, "lost-ack")
    async with app.run_test(size=size) as pilot:
        plan = await open_plan(app, pilot, backup_id)
        await confirm_restore(app, pilot, plan, 1, gateway)
        assert plan.query_one("#restore-execute", Button).disabled
        assert "unconfirmed" in str(plan.query_one("#area-state", Static).content).casefold()
        assert_footer(plan, size)
        plan.query_one("#restore-status", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, RestoreStatusScreen))
        status = app.screen
        await until(pilot, lambda: not status._operation_active
                    and status.query_one("#restore-status-table", DataTable).row_count == 1)
        assert "Unconfirmed: 1" in str(status.query_one("#restore-status-summary", Static).content)
        assert gateway.target.count() == 1
    context.close()
    context, gateway, app, backup_id = prepared(tmp_path, "accepted", create=False)
    async with app.run_test(size=size) as pilot:
        plan = await open_plan(app, pilot, backup_id)
        assert plan.plan.blocked_count == 1 and plan.plan.create_count == 0
        assert plan.query_one("#restore-execute", Button).disabled
        assert not plan.query_one("#restore-another-copy", Button).display
        assert plan.query_one("#restore-status", Button).display
        assert_footer(plan, size)
        plan.query_one("#restore-status", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, RestoreStatusScreen))
        status = app.screen
        await until(pilot, lambda: not status._operation_active
                    and status.query_one("#restore-status-table", DataTable).row_count == 1)
        assert gateway.target.count() == 1
    context.close()


@pytest.mark.asyncio
async def test_local_status_pages_without_provider_and_open_overlay_follows_theme(tmp_path):
    context = synthetic_context(tmp_path / "local-status-profile")
    journal = RestoreJournal(context.state)
    journal.begin("synthetic-status", "synthetic-archive", "a" * 64, (
        RestoreTargetIntent(f"item-{i}", f"source-{i}", "Restored/Synthetic") for i in range(251)
    ))
    journal.dispatch("synthetic-status", 0)
    journal.finish("synthetic-status", 0, "uncertain", error_type="SyntheticLostReply")

    def forbidden_provider():
        pytest.fail("Read-only local status must not initialize a provider")

    backend = BackupAreaBackend(context.paths.backup_dir, state=context.state,
                                source_factory=forbidden_provider)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = RestoreStatusScreen(backend, "synthetic-status")
        await app.push_screen(screen)
        await until(pilot, lambda: not screen._operation_active and len(screen._status_rows) == 100)
        table = screen.query_one("#restore-status-table", DataTable)
        assert table.row_count == 100
        footer = screen.query_one("#restore-status-actions")
        assert footer.region.bottom <= 24
        original = app.active_theme.id
        app.preview_theme("builtin.default-light")
        await pilot.pause()
        assert screen.query_one("#restore-status-root").styles.background.hex.casefold() == (
            app.active_theme.palette.modal_background.casefold()
        )
        app.preview_theme(original)
        await pilot.pause()
        screen.query_one("#restore-status-next", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: not screen._operation_active and screen._status_offset == 100)
        assert table.row_count == len(screen._status_rows) == 100
        assert "101-200 of 251" in str(screen.query_one("#restore-status-range", Static).content)
        await until(
            pilot, lambda: not screen.query_one("#restore-status-next").has_class("-active")
        )
        screen.query_one("#restore-status-next", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: not screen._operation_active and screen._status_offset == 200)
        assert table.row_count == len(screen._status_rows) == 51
        assert screen.query_one("#restore-status-next", Button).disabled
        assert journal.operation("synthetic-status")["counts"] == {
            "not_started": 250, "uncertain": 1,
        }
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_completed_restore_needs_new_extra_copy_review_and_final_confirmation(tmp_path, size):
    context, gateway, app, backup_id = prepared(tmp_path, "accepted")
    async with app.run_test(size=size) as pilot:
        plan = await open_plan(app, pilot, backup_id)
        await confirm_restore(app, pilot, plan, 1, gateway)
    context.close()
    context, gateway, app, backup_id = prepared(tmp_path, "accepted", create=False)
    async with app.run_test(size=size) as pilot:
        plan = await open_plan(app, pilot, backup_id)
        assert plan.plan.create_count == 0 and plan.plan.duplicate_count == 1
        assert not plan.query_one("#restore-another-copy", Button).disabled
        plan.query_one("#restore-another-copy", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is plan)
        assert gateway.target.count() == 1 and plan.plan.create_count == 0
        # Textual ignores keyboard activation while the previous active effect runs.
        await until(pilot, lambda: not plan.query_one("#restore-another-copy").has_class("-active"))
        plan.query_one("#restore-another-copy", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await until(pilot, lambda: app.screen is plan and not plan._operation_active
                    and plan.plan.allow_completed_duplicates)
        assert plan.plan.create_count == 1 and gateway.target.count() == 1
        assert_footer(plan, size)
        plan.query_one("#restore-execute", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert "another copy" in app.screen.detail_text
        await pilot.press("y")
        await until(pilot, lambda: app.screen is plan and not plan._operation_active
                    and gateway.target.count() == 2)
        assert_footer(plan, size)
    context.close()


@pytest.mark.asyncio
async def test_large_plan_uses_bounded_widget_pages_and_pinned_compact_actions(tmp_path):
    context, gateway, app, backup_id = prepared(tmp_path, "accepted", count=501)
    async with app.run_test(size=(80, 24)) as pilot:
        plan = await open_plan(app, pilot, backup_id)
        table = plan.query_one("#restore-plan-table", DataTable)
        assert len(plan.plan.entries) == 501 and table.row_count == 100
        assert len(plan._entries) == 100
        assert_footer(plan, (80, 24))
        plan.query_one("#restore-plan-next", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: plan._entry_offset == 100)
        assert table.row_count == 100
        assert "101-200 of 501" in str(plan.query_one("#restore-plan-range", Static).content)
        plan.query_one("#restore-plan-previous", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: plan._entry_offset == 0)
        assert table.row_count == 100 and gateway.target.count() == 0
    context.close()


@pytest.mark.asyncio
async def test_encrypted_additional_copy_requires_fresh_key_cancel_preserves_scope(tmp_path):
    context = synthetic_context(tmp_path / "profile")
    backup_id, passphrase = "synthetic-encrypted-recovery", "synthetic-only-key"
    service = BackupService(context.paths.backup_dir, context.state)
    service.create((BackupItem("msg-encrypted", "message", {"subject": "Synthetic encrypted"}),),
                   backup_id=backup_id, passphrase=passphrase)
    gateway = OwnedRestoreMailbox(tmp_path / "destination.sqlite", "accepted")
    context._gateway = gateway
    plan = service.plan_restore(backup_id, passphrase=passphrase)
    result = service.restore(backup_id, passphrase=passphrase, target=gateway,
                             confirmed=True, confirmation_token=plan.confirmation_token)
    assert result.restored_count == gateway.target.count() == 1
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await open_plan(app, pilot, backup_id, passphrase=passphrase)
        assert screen.plan.encrypted and screen.plan.create_count == 0
        original_token = screen.plan.confirmation_token
        screen.query_one("#restore-another-copy", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, BackupPassphraseScreen))
        assert app.screen.query_one("#backup-passphrase-input", Input).value == ""
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is screen)
        assert screen.plan.confirmation_token == original_token and gateway.target.count() == 1
        await until(
            pilot, lambda: not screen.query_one("#restore-another-copy").has_class("-active")
        )
        screen.query_one("#restore-another-copy", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, BackupPassphraseScreen))
        app.screen.query_one("#backup-passphrase-input", Input).value = passphrase
        await pilot.press("enter")
        await until(pilot, lambda: app.screen is screen and not screen._operation_active
                    and screen.plan.allow_completed_duplicates)
        assert screen.plan.create_count == 1 and gateway.target.count() == 1
        screen.query_one("#restore-execute", Button).focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, BackupPassphraseScreen))
        assert app.screen.query_one("#backup-passphrase-input", Input).value == ""
        app.screen.query_one("#backup-passphrase-input", Input).value = passphrase
        await pilot.press("enter")
        await until(pilot, lambda: app.screen is screen and not screen._operation_active
                    and gateway.target.count() == 2)
        assert screen.query_one("#restore-execute", Button).disabled
    context.close()
