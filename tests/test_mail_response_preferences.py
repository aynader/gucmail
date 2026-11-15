"""Persistent response choices, old draft hashes and exact Sent-copy execution."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Select, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    LocalStateError,
    SendOutcomeUncertainError,
)
from gucmail.models import Address, DraftData, MessageDetail, MessageSummary, SendState
from gucmail.services.compose import ComposeKind, ComposeService, ComposeValidationError
from gucmail.services.compose_preferences import ComposerPreferences
from gucmail.services.drafts import (
    DraftService,
    DraftSyncCoordinator,
    ServerDraftSnapshot,
    draft_content_hash,
    draft_from_dict,
    draft_to_dict,
)
from gucmail.services.mail_preferences import MailResponsePreferences, ReplyMode
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_drafts import _ServerDraftGateway
from test_ews_adapter import _compose_gateway, _FakeReplySource, _FakeServerDraft
from test_mail_services import ComposeFake
from test_scheduler import NOW, make_services
from test_settings_secondpass import _context
from test_tui import _ready_composer_for_editing
from waiting import press_button

VALUES = {
    "mail.default_reply_mode": "all",
    "mail.reply_include_history": False,
    "mail.reply_all_include_history": False,
    "mail.forward_include_history": False,
    "mail.save_sent_copy": False,
}


def configured(tmp_path):
    context = _context(tmp_path)
    settings = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    session = settings.begin()
    for identifier, value in VALUES.items():
        session.set(identifier, value)
    session.apply()
    return context, settings


def original():
    return MessageDetail(
        MessageSummary("msg-original", "Synthetic original", Address("sender@example.test"), NOW),
        reply_to=(Address("reply@example.test"),),
        to=(Address("student@example.test"), Address("other@example.test")),
        cc=(Address("OTHER@example.test"), Address("cc@example.test")),
        bcc=(Address("private@example.test"),),
        body_text="Original synthetic history",
    )


def sendable(**changes):
    return replace(
        DraftData(
            "dft-legacy-response",
            to=[Address("recipient@example.test")],
            subject="Synthetic",
            body_text="Body",
        ),
        **changes,
    )


def test_response_defaults_stage_cancel_persist_transfer_reset_undo_and_account_scope(tmp_path):
    context, service = configured(tmp_path)
    assert MailResponsePreferences.from_settings(service).reply_mode is ReplyMode.ALL
    assert not ComposerPreferences.from_settings(service).save_sent_copy
    before = context.paths.config_file.read_bytes()
    session = service.begin()
    session.set("mail.default_reply_mode", "sender")
    session.cancel()
    assert context.paths.config_file.read_bytes() == before
    assert (
        MailResponsePreferences.from_settings(SettingsService(context.config_store)).reply_mode
        is ReplyMode.ALL
    )
    service.reset("mail.save_sent_copy")
    assert ComposerPreferences.from_settings(service).save_sent_copy
    service.undo()
    assert not ComposerPreferences.from_settings(service).save_sent_copy
    exported = service.export(setting_ids=tuple(VALUES))
    other = SettingsService(_context(tmp_path / "other").config_store)
    other.apply_import(other.plan_import(exported))
    assert MailResponsePreferences.from_settings(other) == MailResponsePreferences.from_settings(
        service
    )
    assert not ComposerPreferences.from_settings(other).save_sent_copy
    account = SettingsService(context.config_store, context=SettingsContext(account_id="A"))
    account.set("mail.default_reply_mode", "sender")
    account.set("mail.save_sent_copy", True)
    second = SettingsService(context.config_store, context=SettingsContext(account_id="B"))
    assert MailResponsePreferences.from_settings(account).reply_mode is ReplyMode.SENDER
    assert MailResponsePreferences.from_settings(second).reply_mode is ReplyMode.ALL
    assert ComposerPreferences.from_settings(account).save_sent_copy
    assert not ComposerPreferences.from_settings(second).save_sent_copy


@pytest.mark.parametrize(
    "identifier,value",
    [
        ("mail.default_reply_mode", "everyone"),
        ("mail.reply_include_history", "maybe"),
        ("mail.reply_all_include_history", "maybe"),
        ("mail.forward_include_history", "maybe"),
        ("mail.save_sent_copy", "maybe"),
    ],
)
def test_invalid_response_preferences_preserve_configuration(tmp_path, identifier, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set(identifier, value)
    assert context.paths.config_file.read_bytes() == before


def test_response_mode_and_independent_history_defaults_preserve_source_and_recipients():
    source = original()
    gateway = ComposeFake(source)
    service = ComposeService(
        gateway,
        response_preferences=MailResponsePreferences(
            reply_mode=ReplyMode.ALL,
            reply_include_history=False,
            reply_all_include_history=True,
            forward_include_history=False,
        ),
    )
    all_preview = service.preview_reply("msg-original", account_addresses=("student@example.test",))
    assert all_preview.kind is ComposeKind.REPLY_ALL
    assert [a.email for a in all_preview.draft.to] == ["reply@example.test", "other@example.test"]
    assert [a.email for a in all_preview.draft.cc] == ["cc@example.test"]
    assert not all_preview.draft.bcc and "Original synthetic history" in all_preview.draft.body_text
    sender = service.preview_reply("msg-original", reply_all=False)
    assert sender.kind is ComposeKind.REPLY and sender.draft.body_text == ""
    assert [a.email for a in sender.draft.to] == ["reply@example.test"]
    override = service.preview_reply("msg-original", reply_all=True, include_history=False)
    assert override.kind is ComposeKind.REPLY_ALL and override.draft.body_text == ""
    forward = service.preview_forward("msg-original", recipients=("forward@example.test",))
    assert forward.draft.body_text == "" and forward.draft.forward_of_reference == "msg-original"
    assert (
        "Original synthetic history"
        in service.preview_forward("msg-original", include_history=True).draft.body_text
    )
    assert source == original()
    assert gateway.calls == [("show", "msg-original", False)] * 5


def test_actual_checkpoint14_draft_hash_defaults_remain_compatible_and_false_is_bound(tmp_path):
    # Produced by the frozen checkpoint14 wheel, before save_sent_copy existed.
    expected = "efbcfd42284c2faee7660f94ee3071d95bea0aaaf4ee5212cad6efccfc72e696"
    draft = sendable()
    assert draft_content_hash(draft) == expected
    legacy = draft_to_dict(draft)
    legacy.pop("save_sent_copy")
    assert draft_from_dict(legacy).save_sent_copy is True
    assert draft_content_hash(draft_from_dict(legacy)) == expected
    assert draft_content_hash(replace(draft, save_sent_copy=False)) != expected
    drafts, _ = make_services(tmp_path, lambda *_: pytest.fail("No send requested"))
    stored = drafts.create(replace(draft, save_sent_copy=False))
    assert (
        not DraftService(drafts.state, drafts.recovery_directory)
        .get(stored.reference)
        .save_sent_copy
    )
    assert not drafts.read_recovery(stored.reference).save_sent_copy
    envelope = json.loads(drafts.recovery_path(stored.reference).read_text())
    envelope["draft"]["save_sent_copy"] = True
    drafts.recovery_path(stored.reference).write_text(json.dumps(envelope))
    with pytest.raises(LocalStateError, match="content hash"):
        drafts.read_recovery(stored.reference)
    # Reopen an actual old-format envelope with the pre-change wheel's fixed hash.
    drafts.recovery_path(stored.reference).write_text(
        json.dumps(
            {
                "format_version": 1,
                "draft": legacy,
                "content_hash": expected,
                "attachment_manifest": [],
            }
        )
    )
    assert drafts.read_recovery(stored.reference).save_sent_copy


@pytest.mark.parametrize("value", ["false", 0, None])
def test_sent_copy_type_rejected_by_recovery_compose_and_adapter_before_mutation(value):
    draft = sendable(save_sent_copy=value)
    with pytest.raises(LocalStateError, match="boolean"):
        draft_from_dict(draft_to_dict(draft))
    with pytest.raises(ComposeValidationError):
        ComposeService().plan_send(draft)
    gateway, created, _ = _compose_gateway()
    for operation in (lambda: gateway.send(draft), lambda: gateway.reply("missing", draft)):
        with pytest.raises(ConfigurationError, match="boolean"):
            operation()
    assert created == []


def test_no_sent_copy_is_reviewed_and_changed_choice_cannot_send():
    gateway = ComposeFake()
    service = ComposeService(gateway)
    plan = service.plan_send(sendable(save_sent_copy=False))
    assert "no_sent_copy" in {i.code for i in plan.validation.warnings}
    plan.draft.save_sent_copy = True
    with pytest.raises(ConflictError, match="changed"):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert not gateway.calls


def test_no_history_forward_still_discloses_inherited_attachment_indicator():
    source = original()
    source = replace(source, summary=replace(source.summary, has_attachments=True))
    gateway = ComposeFake(source)
    service = ComposeService(
        gateway,
        response_preferences=MailResponsePreferences(
            forward_include_history=False,
        ),
    )
    preview = service.preview_forward("msg-original", recipients=("recipient@example.test",))
    preview.draft.body_text = "New text"
    plan = service.plan_send(preview)
    assert plan.source_has_attachments and "Original synthetic history" not in plan.draft.body_text
    assert "Inherited attachments: retained on Exchange" in cli.core.compose_review(plan)
    assert gateway.calls == [("show", "msg-original", False)]


def test_uncertain_send_without_copy_reports_manual_reconciliation_without_replay():
    class Uncertain(ComposeFake):
        def send(self, draft):
            self.calls.append(("send", draft))
            raise OSError("synthetic connection loss")

    gateway = Uncertain()
    service = ComposeService(gateway)
    plan = service.plan_send(sendable(save_sent_copy=False))
    with pytest.raises(SendOutcomeUncertainError, match="establish delivery manually"):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    with pytest.raises(ConflictError, match="already"):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("operation", ["send", "reply", "reply_all", "forward"])
@pytest.mark.parametrize("save_copy", [False, True])
def test_all_ews_send_paths_receive_exact_reviewed_copy_policy(monkeypatch, operation, save_copy):
    calls = []
    monkeypatch.setattr(
        _FakeServerDraft, "send", lambda self, *, save_copy: calls.append((self, save_copy))
    )
    gateway, created, _ = _compose_gateway()
    draft = sendable(save_sent_copy=save_copy)
    if operation == "send":
        result = gateway.send(draft)
    else:
        gateway._remember("message", _FakeReplySource(), reference="msg-source")
        result = getattr(gateway, operation)("msg-source", draft)
    assert calls == [(created[-1], save_copy)]
    assert result["accepted"] and result["sent_copy_requested"] is save_copy


def test_server_content_merge_and_conflict_restore_preserve_local_send_policy(tmp_path):
    drafts, _ = make_services(tmp_path, lambda *_: pytest.fail("No send requested"))
    local = drafts.create(sendable(save_sent_copy=False))
    gateway = _ServerDraftGateway()
    coordinator = DraftSyncCoordinator(drafts, gateway)
    synced = coordinator.sync_to_server(local.reference)
    assert not synced.save_sent_copy
    remote = replace(synced, subject="Server edit", save_sent_copy=True)
    coordinator.observe_server(
        local.reference,
        ServerDraftSnapshot(remote, remote.server_id, remote.server_change_key),
    )
    merged = drafts.get(local.reference)
    assert merged.subject == "Server edit" and not merged.save_sent_copy
    drafts.edit(merged.reference, subject="Local edit")
    remote = replace(remote, subject="Concurrent server edit")
    coordinator.observe_server(
        local.reference,
        ServerDraftSnapshot(remote, remote.server_id, remote.server_change_key),
    )
    restored = coordinator.resolve_conflict(local.reference, use="server")
    assert restored.subject == "Concurrent server edit" and not restored.save_sent_copy


def test_due_send_keeps_stored_policy_and_changed_policy_stops_old_job(tmp_path):
    calls = []
    drafts, scheduler = make_services(tmp_path, lambda d, **_: calls.append(d.save_sent_copy))
    stored = drafts.create(sendable(save_sent_copy=False))
    scheduler.schedule(stored.reference, "in 1 hour", now=NOW)
    assert scheduler.run_once(now=NOW + timedelta(hours=2)).sent == 1
    assert calls == [False]
    changed = drafts.create(sendable(reference="dft-changed"))
    job = scheduler.schedule(changed.reference, "in 1 hour", now=NOW)
    drafts.edit(changed.reference, save_sent_copy=False)
    scheduler.run_once(now=NOW + timedelta(hours=2))
    assert scheduler.get(job.reference).message.state is SendState.MANUAL_REVIEW
    assert calls == [False]


@pytest.mark.parametrize("route", ["reply", "reply-all", "forward"])
def test_cli_response_defaults_and_explicit_history_sent_copy_overrides(
    tmp_path, monkeypatch, route
):
    configured(tmp_path)
    gateway = ComposeFake(original())
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    args = ["--json", route, "msg-original", "--body", "New synthetic text", "--dry-run"]
    if route == "forward":
        args += ["--to", "forward@example.test"]
    runner = CliRunner()
    result = runner.invoke(cli.app, args, env=_environment(tmp_path))
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)["data"]["plan"]
    assert plan["draft"]["save_sent_copy"] is False
    assert "Original synthetic history" not in plan["draft"]["body_text"]
    assert plan["kind"] == ("forward" if route == "forward" else "reply_all")
    extra = ["--include-history", "--save-sent-copy"]
    if route == "reply":
        extra += ["--reply-mode", "sender"]
    explicit = runner.invoke(cli.app, [*args, *extra], env=_environment(tmp_path))
    assert explicit.exit_code == 0, explicit.output
    output = json.loads(explicit.output)["data"]["plan"]
    assert output["draft"]["save_sent_copy"] is True
    assert "Original synthetic history" in output["draft"]["body_text"]
    if route == "reply":
        assert output["kind"] == "reply"
    assert all(c == ("show", "msg-original", False) for c in gateway.calls)


def test_cli_send_draft_edit_preview_and_schedule_keep_the_reviewed_copy_choice(
    tmp_path, monkeypatch
):
    context, settings = configured(tmp_path)
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("All operations here are local")
    )
    runner = CliRunner()
    env = _environment(tmp_path)
    values = ["--to", "recipient@example.test", "--subject", "Synthetic", "--body", "Body"]
    preview = runner.invoke(cli.app, ["--json", "send", *values, "--dry-run"], env=env)
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["plan"]["draft"]["save_sent_copy"] is False
    queued = runner.invoke(
        cli.app, ["--json", "--yes", "send", *values, "--schedule", "in 1 hour"], env=env
    )
    assert queued.exit_code == 0, queued.output
    reference = json.loads(queued.output)["data"]["draft_reference"]
    drafts = DraftService(context.state, context.paths.recovery_dir)
    assert not drafts.get(reference).save_sent_copy
    local = runner.invoke(cli.app, ["--json", "drafts", "create", *values], env=env)
    assert local.exit_code == 0, local.output
    reference = json.loads(local.output)["data"]["reference"]
    settings.set("mail.save_sent_copy", True)
    resumed = runner.invoke(cli.app, ["--json", "drafts", "send", reference, "--dry-run"], env=env)
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["data"]["plan"]["draft"]["save_sent_copy"] is False
    edited = runner.invoke(
        cli.app, ["--json", "drafts", "edit", reference, "--save-sent-copy"], env=env
    )
    assert edited.exit_code == 0, edited.output
    assert drafts.get(reference).save_sent_copy


def test_cli_invalid_reply_mode_rejected_without_account_or_source_read(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("No gateway for invalid options")
    )
    result = CliRunner().invoke(
        cli.app, ["reply", "msg-original", "--reply-mode", "everyone"], env=_environment(tmp_path)
    )
    assert result.exit_code == 2 and "sender" in result.output and "all" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_stage_cancel_apply_and_mail_response_consumers(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    context.config = replace(context.config, email="student@example.test")
    gateway = ComposeFake(original())
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen

        async def activate(identifier):
            await press_button(pilot, settings.query_one(identifier, Button))

        async def stage():
            for identifier, value in VALUES.items():
                assert settings.action_select_setting(identifier)
                await pilot.pause()
                settings.query_one("#settings-choice", Select).value = (
                    value if isinstance(value, str) else "off"
                )
                await activate("#settings-stage")
                await wait_for(pilot, lambda identifier=identifier: identifier in settings._staged)

        await stage()
        assert (
            MailResponsePreferences.from_settings(
                SettingsService(
                    context.config_store, context=SettingsContext(account_id="student@example.test")
                )
            ).reply_mode
            is ReplyMode.SENDER
        )
        assert not gateway.calls
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        assert ComposerPreferences.from_settings(
            SettingsService(
                context.config_store, context=SettingsContext(account_id="student@example.test")
            )
        ).save_sent_copy
        await stage()
        await activate("#settings-apply")
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        assert (
            MailResponsePreferences.from_settings(
                SettingsService(
                    context.config_store, context=SettingsContext(account_id="student@example.test")
                )
            ).reply_mode
            is ReplyMode.ALL
        )
        assert not ComposerPreferences.from_settings(
            SettingsService(
                context.config_store, context=SettingsContext(account_id="student@example.test")
            )
        ).save_sent_copy
        app.pop_screen()
        await pilot.pause()
        app.ui.selected_reference = "msg-original"
        app.query_one("#message-table", DataTable).focus()
        await pilot.press("r")
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        assert app.screen.kind is ComposeKind.REPLY_ALL
        assert app.screen.query_one("#compose-body", TextArea).text == ""
        assert not app.screen.query_one("#compose-sent-copy", Checkbox).value
        assert [a.email for a in app.screen.draft.to] == [
            "reply@example.test",
            "other@example.test",
        ]
        app.screen.dismiss(None)
        await pilot.pause()
        app.action_forward()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        assert app.screen.kind is ComposeKind.FORWARD
        assert app.screen.query_one("#compose-body", TextArea).text == ""
        assert gateway.calls == [("show", "msg-original", False)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_new_composer_sent_copy_keyboard_and_resumed_choice(tmp_path, size):
    context, settings = configured(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_compose()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        control = composer.query_one("#compose-sent-copy", Checkbox)
        assert not control.value and str(control.label) == "Sent copy: off"
        control.focus()
        await pilot.press("space")
        await pilot.pause()
        assert control.value and str(control.label) == "Sent copy: on"
        assert 0 <= control.region.y < size[1] and control.region.right <= size[0]
        assert composer._payload(action="save")["save_sent_copy"] is True
        assert composer._has_unsaved_changes()
        payload = composer._payload(action="save")
        reconstructed = app._draft_from_payload(payload)
        assert reconstructed.save_sent_copy
        composer.dismiss(None)
        await pilot.pause()
        settings.set("mail.save_sent_copy", False)
        app._open_compose(sendable())
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        assert app.screen.query_one("#compose-sent-copy", Checkbox).value
        assert app.screen.query_one("#compose-body", TextArea).text == "Body"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_long_no_copy_send_review_keeps_actions_visible_and_cancel_preserves_text(
    tmp_path, size
):
    context = _context(tmp_path)
    draft = sendable(
        save_sent_copy=False,
        to=[Address(f"synthetic.{i}@example.test") for i in range(12)],
        subject="Synthetic send review " * 4,
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_compose(draft)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        composer = app.screen
        await wait_for(pilot, lambda: not composer._attempt_pending)
        composer.query_one("#compose-send", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmationScreen)
                and bool(app.screen.query("#cancel"))
                and app.screen.query_one("#cancel", Button).is_mounted
            ),
        )
        await pilot.pause()
        review = app.screen
        assert "Sent Items copy: none" in review.detail
        assert "manual reconciliation" in review.detail
        bounds = review.query_one("#confirm-box").content_region
        for identifier in ("#cancel", "#confirm"):
            button = review.query_one(identifier, Button)
            assert button.content_size.height >= 1
            assert app.screen.region.contains_region(button.region)
            assert bounds.contains_region(button.region), (identifier, bounds, button.region)
        detail = review.query_one("#confirm-detail", VerticalScroll)
        assert detail.max_scroll_y > 0
        detail.scroll_end(animate=False, immediate=True)
        await wait_for(pilot, lambda: detail.scroll_y == detail.max_scroll_y)
        assert bounds.contains_region(review.query_one("#confirm", Button).region)
        assert await pilot.click("#cancel")
        await wait_for(pilot, lambda: app.screen is composer)
        assert composer.query_one("#compose-body", TextArea).text == draft.body_text
        assert not composer.query_one("#compose-sent-copy", Checkbox).value
        assert app.gateway is None and context._gateway is None
