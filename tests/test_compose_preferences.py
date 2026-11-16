"""Saved defaults, exact send reviews, local CLI queues, and mounted composer consumers."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from textual.widgets import Button, Checkbox, Input, Select, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError, ConfirmationRequiredError, ConflictError
from gucmail.models import Address, DraftData, Importance, MessageDetail, MessageSummary, SendState
from gucmail.services.compose import ComposeService, ComposeValidationError
from gucmail.services.compose_preferences import BodyFormat, ComposerPreferences
from gucmail.services.drafts import DraftService
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen as OperationalConfirmation
from gucmail.tui.area_common import ViewState
from gucmail.tui.scheduled_screen import ScheduledScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_mail_services import ComposeFake
from test_scheduler import NOW, make_services
from test_settings_secondpass import _context
from test_tui import _ready_composer_for_editing
from waiting import press_button, rounds


def draft(**changes):
    return replace(
        DraftData(
            "preview:new",
            to=[Address("recipient@example.test")],
            subject="Synthetic",
            body_text="Body",
        ),
        **changes,
    )


DEFAULTS = {
    "composer.default_body_format": "html",
    "composer.default_importance": "high",
    "composer.request_read_receipt": True,
    "composer.request_delivery_receipt": True,
    "composer.signature_enabled": True,
    "composer.signature": "Synthetic signature <safe>",
    "composer.warn_missing_attachment": False,
    "composer.recipient_warning_threshold": 3,
    "composer.empty_subject_policy": "warn",
}


def configured(tmp_path):
    context = _context(tmp_path)
    settings = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    session = settings.begin()
    for key, value in DEFAULTS.items():
        session.set(key, value)
    session.apply()
    return context, settings


def test_preferences_persist_export_import_reset_undo_and_account_isolation(tmp_path):
    context, service = configured(tmp_path)
    preferences = ComposerPreferences.from_settings(SettingsService(context.config_store))
    assert preferences.body_format is BodyFormat.HTML
    assert preferences.importance is Importance.HIGH
    assert preferences.read_receipt and preferences.delivery_receipt
    assert preferences.signature_enabled and preferences.allow_empty_subject
    exported = service.export()
    for key, value in DEFAULTS.items():
        if key != "composer.signature":
            assert exported["settings"][key] == value
    assert "composer.signature" in exported["excluded_private"]
    assert "composer.signature" not in exported["settings"]
    service.reset("composer.request_read_receipt")
    assert not ComposerPreferences.from_settings(service).read_receipt
    service.undo()
    assert ComposerPreferences.from_settings(service).read_receipt
    other = SettingsService(_context(tmp_path / "other").config_store)
    other.apply_import(other.plan_import(exported))
    assert ComposerPreferences.from_settings(other) == replace(preferences, signature="")
    account = SettingsService(context.config_store, context=SettingsContext(account_id="account-A"))
    account.set("composer.default_importance", "low")
    assert ComposerPreferences.from_settings(account).importance is Importance.LOW
    second = SettingsService(context.config_store, context=SettingsContext(account_id="account-B"))
    assert ComposerPreferences.from_settings(second).importance is Importance.HIGH


@pytest.mark.parametrize(
    "key,value",
    [
        ("composer.default_body_format", "rtf"),
        ("composer.default_importance", "urgent"),
        ("composer.request_read_receipt", "maybe"),
        ("composer.empty_subject_policy", "skip"),
        ("composer.recipient_warning_threshold", -1),
        ("composer.recipient_warning_threshold", 501),
        ("composer.recipient_warning_threshold", True),
    ],
)
def test_invalid_preferences_preserve_configuration(tmp_path, key, value):
    context = _context(tmp_path)
    original = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set(key, value)
    assert context.paths.config_file.read_bytes() == original


def test_new_defaults_preserve_input_and_insert_escaped_signature_above_quotes():
    original = draft(body_text="Answer <safe>\n\nOn yesterday, Person wrote:\n> Quoted text")
    preferences = ComposerPreferences(
        body_format=BodyFormat.HTML,
        importance=Importance.HIGH,
        read_receipt=True,
        delivery_receipt=True,
        signature_enabled=True,
        signature="Signature <safe>\nSecond line",
    )
    seeded = preferences.seed_new(original)
    assert "Signature" not in original.body_text and original.body_html is None
    assert seeded.body_text.index("Signature") < seeded.body_text.index("On yesterday")
    assert seeded.body_text.count("Signature") == 1
    assert "Answer &lt;safe&gt;" in seeded.body_html
    assert "Signature &lt;safe&gt;<br>Second line" in seeded.body_html
    assert seeded.request_read_receipt and seeded.request_delivery_receipt
    assert seeded.importance is Importance.HIGH
    override = preferences.seed_new(
        draft(body_html="<b>Original HTML</b>"),
        body_format=BodyFormat.PLAIN,
        importance=Importance.LOW,
        read_receipt=False,
        delivery_receipt=False,
        signature_enabled=False,
    )
    assert override.body_html is None and override.body_text == "Body"
    assert not override.request_read_receipt and not override.request_delivery_receipt
    assert override.importance is Importance.LOW


def test_saved_warning_policies_do_not_disable_validation_or_exact_confirmation(tmp_path):
    preferences = ComposerPreferences(
        recipient_warning_threshold=2, empty_subject_policy="warn", warn_missing_attachment=False
    )
    service = ComposeService(preferences=preferences)
    composed = draft(
        to=[Address("recipient@example.test"), Address("RECIPIENT@example.test")],
        bcc=[Address("second@example.test")],
        subject="",
        body_text="See the attachment",
    )
    plan = service.plan_send(composed)
    assert len(plan.draft.to) == 1 and len(plan.draft.bcc) == 1
    assert {w.code for w in plan.validation.warnings} == {
        "empty_subject",
        "recipient_count_warning",
        "duplicate_recipient",
    }
    with pytest.raises(ConfirmationRequiredError):
        service.execute_send(plan)
    with pytest.raises(ComposeValidationError):
        service.plan_send(composed, allow_empty_subject=False)
    plan.draft.request_read_receipt = True
    with pytest.raises(ConflictError, match="changed"):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    suppressed = ComposeService(
        preferences=replace(preferences, recipient_warning_threshold=0)
    ).validate(composed)
    assert {w.code for w in suppressed.warnings} == {"empty_subject", "duplicate_recipient"}
    missing = service.validate(draft(attachment_paths=[tmp_path / "missing.txt"]))
    assert not missing.can_send and any(i.field == "attachments" for i in missing.errors)
    assert not service.validate(draft(to=[Address("invalid")])).can_send
    assert not service.validate(draft(body_text="")).can_send
    assert not service.validate(
        draft(to=[Address(f"recipient-{n}@example.test") for n in range(501)])
    ).can_send


def test_missing_attachment_warning_ignores_quoted_mail_and_uses_configured_policy():
    service = ComposeService()
    assert "attachment_mentioned_but_missing" in {
        w.code for w in service.validate(draft(body_text="Please find attached report")).warnings
    }
    reply = draft(body_text="Thank you\n\n--- Original message ---\nPlease find attached report")
    assert not service.validate(reply).warnings
    assert not service.validate(
        draft(body_text="Please find attached report"), source_has_attachments=True
    ).warnings


def test_html_validation_uses_delivered_content_and_confirmation_binds_body_format():
    service = ComposeService()
    empty_html = service.validate(
        draft(body_text="Not the delivered body", body_html="<script>x</script>")
    )
    assert "empty_body" in {issue.code for issue in empty_html.errors}
    html = service.validate(
        draft(body_text="Different plain text", body_html="<p>See attached report</p>")
    )
    assert "attachment_mentioned_but_missing" in {issue.code for issue in html.warnings}
    plan = service.plan_send(draft())
    plan.draft.body_html = ""
    with pytest.raises(ConflictError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)


@pytest.mark.parametrize("kind", ["reply", "reply-all", "forward"])
def test_cli_reply_routes_override_defaults_without_mutating_source_or_sending(
    tmp_path, monkeypatch, kind
):
    configured(tmp_path)
    source = MessageDetail(
        MessageSummary("m-synthetic", "Synthetic original", Address("person@example.test"), NOW),
        to=[Address("student@example.test")],
        body_text="Quoted message <safe>",
    )
    gateway = ComposeFake(source)
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            kind,
            "m-synthetic",
            "--body",
            "Answer <safe>",
            "--body-format",
            "plain",
            "--importance",
            "low",
            "--no-read-receipt",
            "--no-delivery-receipt",
            "--no-signature",
            "--dry-run",
            *(["--to", "recipient@example.test"] if kind == "forward" else []),
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    message = json.loads(result.output)["data"]["plan"]["draft"]
    assert message["body_html"] is None and message["body_text"].startswith("Answer <safe>")
    assert message["importance"] == "low" and "Synthetic signature" not in message["body_text"]
    assert not message["request_read_receipt"] and not message["request_delivery_receipt"]
    assert gateway.calls == [("show", "m-synthetic", False)]
    assert source.body_text == "Quoted message <safe>"


def test_cli_draft_create_seeds_once_and_reopen_keeps_reviewed_fields(tmp_path, monkeypatch):
    context, _settings = configured(tmp_path)
    monkeypatch.setattr(cli.core, "gateway", lambda *_: pytest.fail("Local drafts cannot connect"))
    created = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "drafts",
            "create",
            "--subject",
            "Draft",
            "--body",
            "<p>Body</p>",
            "--no-delivery-receipt",
        ],
        env=_environment(tmp_path),
    )
    assert created.exit_code == 0, created.output
    reference = json.loads(created.output)["data"]["reference"]
    stored = DraftService(context.state, context.paths.recovery_dir).get(reference)
    assert stored.body_html == "<p>Body</p><p>Synthetic signature &lt;safe&gt;</p>"
    assert stored.body_text.count("Synthetic signature") == 1
    assert stored.request_read_receipt and not stored.request_delivery_receipt
    assert stored.importance is Importance.HIGH


@pytest.mark.parametrize("queue", [[], ["--schedule", "in 1 hour"], ["--undo-delay", "30"]])
def test_cli_preview_is_local_and_preserves_html_receipts_and_defaults(
    tmp_path, monkeypatch, queue
):
    context, _settings = configured(tmp_path)
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("A composition preview is local")
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "send",
            "--to",
            "synthetic@example.test",
            "--body",
            "<b>Body</b>",
            "--dry-run",
            *queue,
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)["data"]
    message = output["plan"]["draft"]
    assert message["body_html"] == "<b>Body</b><p>Synthetic signature &lt;safe&gt;</p>"
    assert message["request_read_receipt"] and message["request_delivery_receipt"]
    assert message["importance"] == "high"
    assert not DraftService(context.state, context.paths.recovery_dir).list()
    with context.state.connection() as connection:
        assert connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0] == 0


def test_cli_message_overrides_and_scheduled_roundtrip_keep_exact_fields(tmp_path, monkeypatch):
    context, _settings = configured(tmp_path)
    monkeypatch.setattr(cli.core, "gateway", lambda *_: pytest.fail("Scheduling is local"))
    runner = CliRunner()
    env = _environment(tmp_path)
    base = ["send", "--to", "synthetic@example.test", "--subject", "Synthetic", "--body", "Body"]
    overrides = [
        "--body-format",
        "plain",
        "--importance",
        "low",
        "--no-read-receipt",
        "--no-delivery-receipt",
        "--no-signature",
    ]
    preview = runner.invoke(cli.app, ["--json", *base, *overrides, "--dry-run"], env=env)
    assert preview.exit_code == 0, preview.output
    message = json.loads(preview.output)["data"]["plan"]["draft"]
    assert message["body_html"] is None and message["body_text"] == "Body"
    assert message["importance"] == "low"
    assert not message["request_read_receipt"] and not message["request_delivery_receipt"]
    queued = runner.invoke(cli.app, ["--json", "--yes", *base, "--schedule", "in 1 hour"], env=env)
    assert queued.exit_code == 0, queued.output
    reference = json.loads(queued.output)["data"]["draft_reference"]
    stored = DraftService(context.state, context.paths.recovery_dir).get(reference)
    assert stored.body_html == "Body<p>Synthetic signature &lt;safe&gt;</p>"
    assert stored.request_read_receipt and stored.request_delivery_receipt
    assert stored.importance is Importance.HIGH


@pytest.mark.parametrize("schedule", [False, True])
def test_draft_send_preview_is_local_and_never_reseeds_or_queues(tmp_path, monkeypatch, schedule):
    context, _settings = configured(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    stored = drafts.create(draft(reference="dft-existing", importance=Importance.LOW))
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("A local draft preview is local")
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "drafts",
            "send",
            stored.reference,
            "--dry-run",
            *(["--schedule", "in 1 hour"] if schedule else []),
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    message = json.loads(result.output)["data"]["plan"]["draft"]
    assert message["body_html"] is None and message["body_text"] == "Body"
    assert message["importance"] == "low"
    assert not message["request_read_receipt"] and not message["request_delivery_receipt"]
    assert drafts.get(stored.reference) == stored
    with context.state.connection() as connection:
        assert connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0] == 0


def test_draft_schedule_requires_review_and_empty_subject_permission_survives_queue(
    tmp_path, monkeypatch
):
    context, _settings = configured(tmp_path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    stored = drafts.create(draft(reference="dft-existing", subject=""))
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("A local draft schedule is local")
    )
    args = ["drafts", "send", stored.reference, "--schedule", "in 1 hour"]
    denied = CliRunner().invoke(cli.app, args, env=_environment(tmp_path))
    assert denied.exit_code != 0 and "Warning:" in denied.output
    assert "empty subject" in denied.output
    queued = CliRunner().invoke(cli.app, ["--json", "--yes", *args], env=_environment(tmp_path))
    assert queued.exit_code == 0, queued.output
    with context.state.connection() as connection:
        manifest = json.loads(
            connection.execute("SELECT attachment_manifest_json FROM scheduled_jobs").fetchone()[0]
        )
    assert manifest[0]["allow_empty_subject"] is True


def test_undo_queue_records_reviewed_empty_subject_permission(tmp_path):
    drafts, scheduler = make_services(tmp_path, lambda *_: pytest.fail("No delivery requested"))
    stored = drafts.create(draft(reference="dft-undo", subject=""))
    job = scheduler.queue_undo_send(stored.reference, 30, now=NOW, allow_empty_subject=True)
    assert job.state is SendState.UNDO_DELAY
    manifest = scheduler.get(job.reference).attachment_manifest
    assert manifest[0]["allow_empty_subject"] is True
    assert scheduler.run_reference_if_due(job.reference, now=NOW + timedelta(seconds=10)) is None
    scheduler.undo(job.reference)


async def wait_for(pilot, condition):
    for _ in rounds(150):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError("Composer workflow did not reach the expected state")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_mounted_new_composer_uses_defaults_and_reviewed_overrides_at_three_sizes(
    tmp_path, size, monkeypatch
):
    monkeypatch.setenv("NO_COLOR", "1")
    context, _settings = configured(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_compose()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        composer = app.screen
        await wait_for(pilot, lambda: not composer._attempt_pending)
        assert composer.query_one("#compose-body-mode", Select).value == "html"
        assert composer.query_one("#compose-importance", Select).value == "high"
        assert composer.query_one("#compose-read-receipt", Checkbox).value
        assert composer.query_one("#compose-delivery-receipt", Checkbox).value
        for name, label in (("read", "Read"), ("delivery", "Delivery")):
            receipt = composer.query_one(f"#compose-{name}-receipt", Checkbox)
            assert str(receipt.label) == f"{label} receipt: on"
            receipt.focus()
            await pilot.press("space")
            await pilot.pause()
            assert not receipt.value and str(receipt.label) == f"{label} receipt: off"
            assert receipt.region.right <= size[0]
            await pilot.press("space")
            await pilot.pause()
            assert receipt.value and str(receipt.label) == f"{label} receipt: on"
        assert (
            composer.query_one("#compose-body", TextArea).text
            == "<p>Synthetic signature &lt;safe&gt;</p>"
        )
        for selector in (
            "#compose-send",
            "#compose-save",
            "#compose-cancel",
            "#compose-external-editor",
        ):
            button = composer.query_one(selector, Button)
            assert button.region.right <= size[0] and button.region.bottom <= size[1]
        composer.query_one("#compose-to", Input).value = "synthetic@example.test"
        composer.query_one("#compose-body-mode", Select).value = "plain"
        composer.query_one("#compose-body", TextArea).text = "Please find attached report"
        composer.query_one("#compose-read-receipt", Checkbox).value = False
        composer.query_one("#compose-delivery-receipt", Checkbox).value = False
        composer.query_one("#compose-importance", Select).value = "low"
        composer.query_one("#compose-send", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        review = app.screen.detail
        assert "Format: Plain text" in review and "Importance: low" in review
        assert "Read receipt: off" in review and "Delivery receipt: off" in review
        assert "empty subject" in review and "no file is attached" not in review
        app.screen.dismiss(False)
        await pilot.pause()
        assert app.screen is composer
        assert not DraftService(context.state, context.paths.recovery_dir).list()


@pytest.mark.asyncio
async def test_settings_stage_apply_then_new_composer_and_resume_preserve_distinct_values(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    stored = DraftService(context.state, context.paths.recovery_dir).create(
        draft(reference="dft-existing", importance=Importance.LOW)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen
        assert settings.action_select_setting("composer.default_body_format")
        settings.query_one("#settings-choice", Select).value = "html"
        settings.query_one("#settings-stage", Button).press()
        await wait_for(pilot, lambda: "composer.default_body_format" in settings._staged)
        assert (
            ComposerPreferences.from_settings(SettingsService(context.config_store)).body_format
            is BodyFormat.PLAIN
        )
        settings.query_one("#settings-apply", Button).press()
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        app._open_compose()
        await wait_for(pilot, lambda: isinstance(app.screen, ComposeScreen))
        await pilot.pause()
        assert app.screen.query_one("#compose-body-mode", Select).value == "html"
        app.screen.dismiss(None)
        await pilot.pause()
        app._open_compose(stored)
        await wait_for(pilot, lambda: isinstance(app.screen, ComposeScreen))
        await pilot.pause()
        assert app.screen.query_one("#compose-body-mode", Select).value == "plain"
        assert app.screen.query_one("#compose-importance", Select).value == "low"
        assert app.screen.query_one("#compose-body", TextArea).text == "Body"


@pytest.mark.asyncio
async def test_changed_attachment_after_review_prevents_queue_and_preserves_recovery(
    tmp_path, monkeypatch
):
    context, settings = configured(tmp_path)
    settings.set("composer.undo_send_seconds", 30)
    attachment = tmp_path / "synthetic.txt"
    attachment.write_text("Original", encoding="utf-8")
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Changed files must stop before EWS")
    )
    statuses = []
    monkeypatch.setattr(app, "_set_status", statuses.append)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(draft(attachment_paths=[attachment]), new=True)
        await wait_for(pilot, lambda: isinstance(app.screen, ComposeScreen))
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        payload = composer._payload(action="send")
        plan = ComposeService(preferences=ComposerPreferences.from_settings(settings)).plan_send(
            app._draft_from_payload(payload)
        )
        payload.update(confirmation_scope=plan.confirmation_scope, allow_empty_subject=True)
        attachment.write_text("Replacement", encoding="utf-8")
        await app.submit_composer(payload).wait()
        assert any("changed" in status for status in statuses)
        assert not app._scheduler_service().list()
        recovered = DraftService(context.state, context.paths.recovery_dir).read_recovery(
            composer.draft.reference
        )
        assert recovered.subject == "Synthetic"


@pytest.mark.asyncio
async def test_normal_send_review_queues_approved_empty_subject_and_persists_receipts(
    tmp_path, monkeypatch
):
    context, settings = configured(tmp_path)
    settings.set("composer.undo_send_seconds", 30)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: pytest.fail("Undo window is local"))
    async with app.run_test(size=(70, 20)) as pilot:
        app._open_compose(draft(subject=""), new=True)
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        # Send is deliberately unavailable until the persisted send/queue status
        # has loaded. Mounting alone does not establish readiness.
        await wait_for(pilot, lambda: not app.screen._attempt_pending)
        assert not app.screen.query_one("#compose-send", Button).disabled
        app.screen.query_one("#compose-send", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert "empty subject" in app.screen.detail
        assert "Read receipt: requested" in app.screen.detail
        await pilot.pause()
        app.screen.query_one("#confirm", Button).press()
        await wait_for(pilot, lambda: bool(app._scheduler_service().list()))
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ScheduledScreen) and app.screen.view_state is ViewState.READY
            ),
        )
        scheduled = app.screen
        job = app._scheduler_service().list()[0]
        assert job.message.state is SendState.UNDO_DELAY
        assert job.attachment_manifest[0]["allow_empty_subject"] is True
        stored = DraftService(context.state, context.paths.recovery_dir).get(
            job.message.draft_reference
        )
        assert stored.subject == "" and stored.body_html is not None
        assert stored.request_read_receipt and stored.request_delivery_receipt
        await press_button(pilot, scheduled.query_one("#scheduled-undo", Button))
        await wait_for(pilot, lambda: isinstance(app.screen, OperationalConfirmation))
        await pilot.pause()
        app.screen.query_one("#operational-confirm-yes", Button).press()
        await wait_for(
            pilot,
            lambda: (
                app.screen is scheduled
                and scheduled.view_state is ViewState.READY
                and scheduled._jobs[job.message.reference].state == "cancelled"
            ),
        )
        assert (
            app._scheduler_service().get(job.message.reference).message.state is SendState.CANCELLED
        )
        assert (
            DraftService(context.state, context.paths.recovery_dir).get(stored.reference) == stored
        )


@pytest.mark.asyncio
async def test_unreviewed_worker_payload_cannot_queue_or_send(tmp_path, monkeypatch):
    context, settings = configured(tmp_path)
    settings.set("composer.undo_send_seconds", 30)
    app = GUCmailApp(context, offline=True)
    statuses = []
    monkeypatch.setattr(app, "_set_status", statuses.append)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Missing confirmation must stop")
    )
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(draft(), new=True)
        await pilot.pause()
        await _ready_composer_for_editing(pilot)
        await app.submit_composer(app.screen._payload(action="send")).wait()
        assert not app._scheduler_service().list()

        assert any("review send" in status.lower() for status in statuses)
