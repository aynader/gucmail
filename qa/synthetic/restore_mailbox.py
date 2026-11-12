"""Reproduce the open restore replay gap through actual Backup Center controls.

Owned SQL destination only. Captures document a defect, not accepted restore or
server behavior. The application guard forbids credentials and real transports.
"""

import asyncio
import json
from uuid import uuid4

from textual.widgets import Button, Static

from gucmail.services.backup import BackupItem, BackupService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.backup_screen import BackupScreen, RestorePlanScreen
from qa.offline.guard import install_from_env
from qa.synthetic.context import save_capture, synthetic_context
from qa.synthetic.mailbox import LocalMailbox
from qa.synthetic.restore_target import OwnedRestoreTarget


class OwnedRestoreMailbox(LocalMailbox):
    """The explicit source factory can only return this owned local target."""

    backup_source_identity = "synthetic-ui-restore-target"

    def __init__(self, path, mode):
        super().__init__(path)
        self.target = OwnedRestoreTarget(path, mode)

    def iter_backup_items(self, request, completed_item_keys):
        raise AssertionError("This restore probe must not enumerate a backup source")

    def restore_item(self, item, destination_folder):
        return self.target.restore_item(item, destination_folder)


async def until(pilot, predicate):
    async with asyncio.timeout(25):
        while not predicate():
            await asyncio.sleep(0.005)
    await pilot.pause()


async def scenario(root, output, captures, size):
    profile = root / f"profile-{size[0]}"
    context = synthetic_context(profile)
    backup = BackupService(context.paths.backup_dir, state=context.state).create(
        (
            BackupItem(
                "msg-synthetic-ui-restore",
                "message",
                {"subject": "Synthetic [literal] restore", "body_text": "Owned restore content"},
                folder_path="Synthetic/Inbox",
            ),
        ),
        backup_id=f"synthetic-ui-replay-{size[0]}",
    )
    destination = profile / "synthetic-destination.sqlite"
    observed, first_token = [], None
    for mode in ("lost-ack", "accepted"):
        if mode == "accepted":
            context = synthetic_context(profile)
        gateway = OwnedRestoreMailbox(destination, mode)
        context._gateway = gateway
        app = GUCmailApp(context, gateway=gateway)
        phase = "first" if mode == "lost-ack" else "restarted"

        def capture(state, *, app=app, phase=phase):
            save_capture(app, output, phase + "-" + state, *size, captures)
            captures[-1].update(
                theme=app.active_theme.id,
                screen=type(app.screen).__name__,
                backend="Known F14 defect reproduction; owned persistent SQL restore target; "
                "no Exchange or delivery",
            )

        try:
            async with app.run_test(size=size) as pilot:
                app._open_area("backups")
                await until(pilot, lambda app=app: isinstance(app.screen, BackupScreen))
                center = app.screen
                await until(
                    pilot,
                    lambda center=center: (
                        center._selected_id == backup.backup_id and not center._operation_active
                    ),
                )
                center.query_one("#backup-restore-plan", Button).focus()
                await pilot.press("enter")
                await until(pilot, lambda app=app: isinstance(app.screen, RestorePlanScreen))
                plan = app.screen
                assert plan.plan.create_count == 1 and plan.plan.duplicate_count == 0
                execute = plan.query_one("#restore-execute", Button)
                assert not execute.disabled
                capture("preview")
                if first_token is None:
                    first_token = plan.plan.confirmation_token
                else:
                    assert plan.plan.confirmation_token == first_token
                    assert gateway.target.count() == 1
                execute.focus()
                await pilot.press("enter")
                await until(pilot, lambda app=app: isinstance(app.screen, ConfirmationScreen))
                capture("explicit-confirmation")
                await pilot.press("y")
                expected = 1 if mode == "lost-ack" else 2
                await until(
                    pilot,
                    lambda app=app, plan=plan, gateway=gateway, expected=expected: (
                        app.screen is plan
                        and not plan._operation_active
                        and gateway.target.count() == expected
                    ),
                )
                detail = str(plan.query_one("#restore-plan-detail", Static).content)
                if mode == "lost-ack":
                    assert "may have created" in detail and execute.disabled
                else:
                    assert "Restored: 1" in detail
                capture("uncertain-result" if mode == "lost-ack" else "duplicate-created")
                observed.append(
                    {
                        "phase": phase,
                        "create_offered": plan.plan.create_count,
                        "same_confirmation_token": plan.plan.confirmation_token == first_token,
                        "durable_rows": gateway.target.count(),
                        "current_execute_disabled": execute.disabled,
                    }
                )
        finally:
            context.close()
    assert BackupService(context.paths.backup_dir).verify(backup.backup_id).valid
    return {"size": size, "phases": observed, "archive_still_valid": True}


async def main():
    guard = install_from_env()
    root = guard.private / ("restore-ui-probe-" + uuid4().hex)
    root.mkdir()
    output = guard.output / "restore-replay-gallery"
    output.mkdir(exist_ok=True)
    captures, results = [], []
    for size in ((80, 24), (120, 36)):
        results.append(await scenario(root, output, captures, size))
    (output / "manifest.json").write_text(json.dumps(captures, indent=2) + "\n", encoding="utf-8")
    (guard.output / "restore-ui-replay-results.json").write_text(
        json.dumps(
            {
                "defect_reproduction_only": True,
                "scenarios": results,
                "boundary": "Actual GUCmailApp Backup Center, keyboard review/confirmation and "
                "production backup services with an owned persistent SQL target. Fresh app/service "
                "objects reopen the profile. No real OS kill, Exchange, credentials, send or "
                "complete backup workflow acceptance.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"reproduced_sizes": len(results), "captures": len(captures)}))


if __name__ == "__main__":
    asyncio.run(main())
