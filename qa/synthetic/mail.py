"""Exercise mail/compose entry points with durable synthetic delivery and readback.

This is a functional/visual campaign, never an Exchange or timing qualification.
The process guard must already be installed; the explicit local gateway cannot
fall back to credentials, a real provider, notifications or external delivery.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from textual.widgets import Button, DataTable, Input, Static, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.commands import CommandInvocation
from gucmail.errors import ConflictError, NetworkError
from gucmail.models import Address, MessageDetail, MessageSummary
from gucmail.services.drafts import DraftService, draft_to_dict
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.settings import SettingsService
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp, ReaderScreen
from gucmail.tui.composer_submission import ComposerSendStatus
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from gucmail.tui.palette_routes import catalog_route
from gucmail.tui.write_coordinator import app_write_coordinator
from qa.offline.guard import install_from_env
from qa.synthetic.context import save_capture, synthetic_context
from qa.synthetic.mailbox import LocalMailbox
from qa.synthetic.workflows import copy_database


class PersistentMail(LocalMailbox):
    """Explicit owned SQLite provider: simulated accepted/rejected/lost-ack sends."""

    mode = "accepted"

    def __init__(self, path):
        super().__init__(path)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS simulated_deliveries "
                "(reference TEXT PRIMARY KEY, payload TEXT NOT NULL, hashes TEXT NOT NULL)"
            )

    def show_message(self, reference, mark_read=False):
        if reference != "msg-synthetic-reviewed":
            return super().show_message(reference, mark_read=mark_read)
        self.before("show_message")
        if mark_read:
            self.mark_read(reference, True)
        return MessageDetail(
            summary=self.get("mail", reference),
            to=(Address("synthetic@example.test"), Address("team@example.test")),
            cc=(Address("TEAM@example.test"), Address("assistant@example.test")),
            reply_to=(Address("reply@example.test"),),
            body_text=("Synthetic long-content reader. [red] Literal tags [/unknown]\n" * 300),
            body_html='<p>Local body</p><img src="https://tracking.invalid/pixel">'
            '<script>throw new Error("hostile");</script>',
            raw_headers={"X-Audit": "Synthetic only"},
        )

    def send(self, draft):
        self.before("simulated_send")
        if self.mode == "rejected":
            raise ConflictError("Synthetic provider rejected the send before acceptance.")
        hashes = {
            str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in draft.attachment_paths
        }
        with self.db() as db:
            db.execute(
                "INSERT INTO simulated_deliveries VALUES (?,?,?)",
                (draft.reference, json.dumps(draft_to_dict(draft)), json.dumps(hashes)),
            )
        if self.mode == "lost-ack":
            raise NetworkError("Synthetic acknowledgment lost after durable local acceptance.")
        return {"accepted": True, "boundary": "simulated local persistence only"}

    def deliveries(self, reference=None):
        with self.db() as db:
            if reference:
                rows = db.execute(
                    "SELECT * FROM simulated_deliveries WHERE reference=?", (reference,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM simulated_deliveries").fetchall()
        return [dict(row) for row in rows]


async def until(pilot, predicate):
    async with asyncio.timeout(25):
        while not predicate():
            await asyncio.sleep(0.01)
    await pilot.pause()


async def main():
    guard = install_from_env()
    root = guard.private / ("mail-readiness-" + uuid4().hex)
    context = synthetic_context(root / "profile")
    settings = SettingsService(context.config_store)
    settings.set("composer.server_draft_sync", False)
    settings.set("mail.mark_read_on_open", False)
    copy_database(guard.private / "datasets/large/mailbox.sqlite", root / "mailbox.sqlite")
    gateway = PersistentMail(root / "mailbox.sqlite")
    gateway.insert_many(
        "mail",
        [
            MessageSummary(
                "msg-synthetic-reviewed",
                "[red] Synthetic course notice [/unknown]",
                Address("teacher@example.test", "Synthetic Course Team"),
                datetime.now(UTC),
                folder_path="/Inbox",
            )
        ],
    )
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    attempts = SendAttemptStore(context.state)
    output = guard.output / "mail-gallery"
    output.mkdir(exist_ok=True)
    captures, results = [], []

    def record(name, **details):
        results.append({"id": name, "outcome": "passed", **details})
        (guard.output / "mail-results.json").write_text(
            json.dumps(
                {
                    "boundary": "Actual UI/services/state with synthetic SQL provider. "
                    "No Exchange or external delivery verified; no timing claim.",
                    "results": results,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    original_build = cli.core.build_context
    cli.core.build_context = lambda **_kwargs: context
    try:
        async with app.run_test(size=(120, 36)) as pilot:

            async def capture(name):
                await pilot.pause()
                save_capture(app, output, name, app.size.width, app.size.height, captures)
                captures[-1].update(
                    theme=app.active_theme.id,
                    screen=type(app.screen).__name__,
                    backend=(
                        "Explicit persistent SQLite simulation; no Exchange or external delivery"
                    ),
                )
                (output / "manifest.json").write_text(
                    json.dumps(captures, indent=2) + "\n", encoding="utf-8"
                )

            async def open_new(subject):
                app.screen_stack[0].query_one("#message-table", DataTable).focus()
                await pilot.press("c")
                await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
                composer = app.screen
                await until(pilot, lambda: not composer._attempt_pending)
                composer.query_one("#compose-to", Input).value = "course@example.test"
                composer.query_one("#compose-subject", Input).value = subject
                composer.query_one("#compose-body", TextArea).load_text(
                    "Synthetic content, never delivered outside this private SQL database."
                )
                return composer

            async def close_composer(composer):
                assert app.screen is composer
                await pilot.press("escape")
                await pilot.pause()
                if isinstance(app.screen, ConfirmationScreen):
                    await pilot.press("y")
                await until(pilot, lambda: app.screen is app.screen_stack[0])

            async def review(composer):
                composer.query_one("#compose-send", Button).focus()
                await pilot.press("enter")
                await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))

            async def send_finished(reference, state):
                await until(
                    pilot,
                    lambda: (
                        attempts.get(reference) is not None
                        and attempts.get(reference).state == state
                    ),
                )
                await app.workers.wait_for_complete()
                await pilot.pause()

            await until(pilot, lambda: not app.ui.busy and bool(app.ui.selected_reference))
            assert gateway.count("mail") == 100001
            table = app.query_one("#message-table", DataTable)
            assert 0 < table.row_count <= 200
            assert app.ui.selected_reference == "msg-synthetic-reviewed"
            await capture("large-mail-bounded-initial-page")
            table.focus()
            await pilot.press("enter")
            await until(pilot, lambda: not app.ui.busy and app._message_detail is not None)
            assert app._message_detail.summary.reference == "msg-synthetic-reviewed"
            assert not gateway.get("mail", "msg-synthetic-reviewed").is_read
            for width, height in ((80, 24), (120, 36), (160, 48)):
                await pilot.resize_terminal(width, height)
                if width == 80:
                    # Resizing the mail shell hides its preview. Re-enter via
                    # the normal selected-message route to prove the compact
                    # full-screen reader, rather than captioning a hidden pane.
                    table.focus()
                    await pilot.press("enter")
                    await until(pilot, lambda: isinstance(app.screen, ReaderScreen))
                    content = app.screen.query_one("#reader-screen-content", Static)
                    assert "Synthetic long-content reader" in str(content.content)
                    scroll = app.screen.query_one("#reader-screen-scroll")
                    scroll.focus()
                    await pilot.press("end")
                    await until(pilot, lambda scroll=scroll: scroll.scroll_y > 0)
                    await capture("compact-full-reader-scrolled-to-end")
                    await pilot.press("home")
                    await until(pilot, lambda scroll=scroll: scroll.scroll_y == 0)
                await capture("long-hostile-message-reader")
                if isinstance(app.screen, ReaderScreen):
                    await pilot.press("escape")
            await pilot.resize_terminal(120, 36)
            if app.screen is not app.screen_stack[0]:
                await pilot.press("escape")
            record("mail.reader.hostile-long-content-no-read-mutation", messages=100001)

            table.focus()
            await pilot.press("a")
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
            reply = app.screen
            to = reply.query_one("#compose-to", Input).value.casefold()
            cc = reply.query_one("#compose-cc", Input).value.casefold()
            assert "reply@example.test" in to
            assert "synthetic@example.test" not in to + cc
            assert (to + cc).count("team@example.test") == 1
            assert "assistant@example.test" in to + cc
            await capture("reply-all-own-address-excluded-and-deduplicated")
            await pilot.press("escape")
            if isinstance(app.screen, ConfirmationScreen):
                await pilot.press("y")
            assert not gateway.deliveries()
            record("compose.reply-all-prefill-cancel", simulated_deliveries=0)

            composer = await open_new("Local save and restart readback")
            reference = composer.draft.reference
            local_saved_reference = reference
            composer.query_one("#compose-to", Input).value = "not-an-address"
            composer.query_one("#compose-send", Button).focus()
            await pilot.press("enter")
            await until(pilot, lambda: not composer.query_one("#compose-send", Button).disabled)
            assert app.screen is composer and not gateway.deliveries()
            assert str(composer.query_one("#compose-note", Static).content)
            await pilot.resize_terminal(80, 24)
            await capture("compact-invalid-recipient-retained")
            composer.query_one("#compose-to", Input).value = "course@example.test"
            await pilot.resize_terminal(120, 36)
            composer.query_one("#compose-save", Button).focus()
            await pilot.press("enter")
            await until(pilot, lambda: drafts.exists(reference) and app.screen is not composer)
            await app.workers.wait_for_complete()
            fresh = DraftService(
                type(context.state)(context.state.path, account_key=context.state.account_key),
                context.paths.recovery_dir,
            )
            assert fresh.get(reference).subject == "Local save and restart readback"
            value = CliRunner().invoke(cli.app, ["--json", "drafts", "show", reference])
            assert value.exit_code == 0, value.output
            assert reference in value.output
            context._gateway = gateway
            record("compose.validation-local-save-cli-fresh-store-readback", reference=reference)

            composer = await open_new("Attachment scope changes after review")
            reference = composer.draft.reference
            attachment = root / "synthetic-notes.txt"
            attachment.write_text("Reviewed content", encoding="utf-8")
            composer.add_attachment_path(attachment)
            await review(composer)
            await capture("send-review-exact-recipients-and-attachment")
            await pilot.press("escape")
            assert app.screen is composer and not gateway.deliveries()
            await review(composer)
            attachment.write_text("Changed content after review", encoding="utf-8")
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not gateway.deliveries() and attempts.get(reference) is None
            assert (
                drafts.read_recovery(reference).subject == "Attachment scope changes after review"
            )
            await capture("attachment-changed-after-review-not-sent")
            assert app.screen is composer and not composer._submission_pending
            assert not composer.query_one("#compose-send", Button).disabled
            await close_composer(composer)
            record("compose.review-cancel-and-attachment-change", simulated_deliveries=0)

            for mode, outcome, expected_count in (
                ("accepted", "sent", 1),
                ("rejected", "failed", 0),
                ("lost-ack", "uncertain", 1),
            ):
                gateway.mode = mode
                composer = await open_new("Synthetic " + mode)
                reference = composer.draft.reference
                await review(composer)
                await pilot.press("y")
                await send_finished(reference, outcome)
                reopened = PersistentMail(gateway.path)
                assert len(reopened.deliveries(reference)) == expected_count
                reopened.close()
                if mode != "accepted":
                    recovered = drafts.read_recovery(reference)
                    assert recovered.subject == "Synthetic " + mode
                    assert app.screen is composer
                    assert composer.query_one("#compose-body", TextArea).text == recovered.body_text
                    for width, height in ((80, 24), (120, 36)):
                        await pilot.resize_terminal(width, height)
                        await capture("simulated-send-" + mode + "-retained-editor")
                    assert composer.query_one("#compose-save", Button).disabled is (
                        mode == "lost-ack"
                    )
                await capture("simulated-send-" + mode + "-" + outcome)
                if mode == "lost-ack":
                    before = gateway.counts["simulated_send"]
                    await close_composer(composer)
                    # Recovery is the actual application entry point; its claim
                    # must still block replay after a second StateStore opens.
                    app._open_compose(recovered)
                    await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
                    composer = app.screen
                    await until(pilot, lambda composer=composer: not composer._attempt_pending)
                    assert composer.query_one("#compose-send", Button).disabled
                    composer.action_review_send()
                    assert app.screen is composer
                    assert gateway.counts["simulated_send"] == before
                    assert len(gateway.deliveries(reference)) == 1
                    assert attempts.get(reference).state == "uncertain"
                    await capture("uncertain-recovery-replay-blocked")
                    composer.query_one("#compose-send-status", Button).focus()
                    await pilot.press("enter")
                    await until(pilot, lambda: isinstance(app.screen, ComposerSendStatus))
                    assert "State: uncertain" in str(
                        app.screen.query_one("#composer-status-text", Static).content
                    )
                    for width, height in ((80, 24), (120, 36)):
                        await pilot.resize_terminal(width, height)
                        await capture("uncertain-send-status-read-only")
                    details = app.screen.query_one("#composer-status-technical")
                    details.collapsed = False
                    await capture("uncertain-send-status-local-record-details")
                    await pilot.press("escape")
                    await until(pilot, lambda composer=composer: app.screen is composer)
                    assert gateway.counts["simulated_send"] == before
                if mode != "accepted":
                    await close_composer(composer)
                record(
                    "compose.simulated-" + mode,
                    reference=reference,
                    durable_attempt=outcome,
                    simulated_deliveries=expected_count,
                )

            # Use the actual selected-draft handoff; review must wait for the
            # persisted status check rather than a fixed post-mount timer.
            app._open_area("drafts")
            await until(pilot, lambda: isinstance(app.screen, DraftAttachmentScreen))
            workspace = app.screen
            await until(
                pilot,
                lambda: not workspace._operation_active and workspace.selected_draft_reference,
            )
            assert workspace.selected_draft_reference == local_saved_reference
            route = catalog_route("drafts.send_draft")
            app._catalog_screen_action(route, CommandInvocation(route.command_id))
            await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert "Local save and restart readback" in app.screen.detail
            await capture("drafts-send-handoff-ready-review")
            before_count = len(gateway.deliveries())
            await pilot.press("escape")
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen))
            await close_composer(app.screen)
            assert len(gateway.deliveries()) == before_count
            record("drafts.send-handoff-review-cancel", simulated_deliveries_added=0)

            composer = await open_new("Palette Save rejected by local SQL")
            reference = composer.draft.reference
            with context.state.transaction(immediate=True) as connection:
                # Owned synthetic SQL fault: exercise the real draft writer,
                # not a Save callback that returns a fabricated error.
                connection.execute(
                    "CREATE TRIGGER reject_synthetic_palette_save BEFORE INSERT ON local_drafts "
                    "WHEN NEW.reference='" + reference + "' BEGIN "
                    "SELECT RAISE(ABORT, 'Synthetic draft write rejected'); END"
                )
            try:
                route = catalog_route("compose.save_local_draft")
                app._catalog_direct(route, CommandInvocation(route.command_id))
                await until(
                    pilot,
                    lambda: (
                        not composer._submission_pending
                        and "save could not"
                        in str(composer.query_one("#compose-note", Static).content)
                    ),
                )
                assert app.screen is composer and not drafts.exists(reference)
                for width, height in ((80, 24), (120, 36)):
                    await pilot.resize_terminal(width, height)
                    await capture("palette-save-sql-rejection-retained")
            finally:
                with context.state.transaction(immediate=True) as connection:
                    connection.execute("DROP TRIGGER reject_synthetic_palette_save")
            await until(pilot, lambda: not app_write_coordinator(app).pending)
            app._catalog_direct(route, CommandInvocation(route.command_id))
            await until(pilot, lambda: app.screen is not composer and drafts.exists(reference))
            fresh = DraftService(
                type(context.state)(context.state.path, account_key=context.state.account_key),
                context.paths.recovery_dir,
            )
            assert fresh.get(reference).subject == "Palette Save rejected by local SQL"
            assert len(gateway.deliveries()) == before_count
            record("compose.palette-save-sql-rejection-retry", fresh_readback=True)
    finally:
        cli.core.build_context = original_build
        context.close()
    print(json.dumps({"checks": len(results), "captures": len(captures)}))


if __name__ == "__main__":
    asyncio.run(main())
