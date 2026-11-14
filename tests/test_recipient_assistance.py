from __future__ import annotations

import json
from dataclasses import replace

import pytest
from textual.widgets import Button, Input, Select
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import Address, DraftData, MessageDetail, MessageSummary
from gucmail.services.drafts import DraftService
from gucmail.services.recipient_assistance import (
    RECIPIENT_HISTORY_SETTING,
    RECIPIENT_SETTINGS,
    RecipientPreferences,
    recent_addresses,
    remember_sent_recipients,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_mail_services import ComposeFake
from test_settings_secondpass import _context
from test_tui import _ready_composer_for_editing


async def activate(pilot, screen, selector):
    button = screen.query_one(selector, Button)
    await wait_for(
        pilot,
        lambda: (
            not button.disabled and not button.has_class("-active") and not screen._operation_active
        ),
    )
    button.focus()
    await pilot.pause()
    await pilot.press("enter")


def sent_draft():
    return DraftData(
        "synthetic:sent",
        to=[Address("Ada@example.test"), Address("student@example.test")],
        cc=[Address("ADA@EXAMPLE.test"), Address("cc@example.test")],
        bcc=[Address("private@example.test")],
        subject="Synthetic",
        body_text="Offline fixture",
    )


def test_policy_stage_cancel_apply_reopen_scope_transfer_reset_undo_and_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = settings_for(context, revisions=InMemoryRevisionStore())
    initial = context.paths.config_file.read_bytes()
    session = service.begin()
    for key, value in zip(RECIPIENT_SETTINGS, (False, False, True), strict=True):
        session.set(key, value)
    session.cancel()
    assert context.paths.config_file.read_bytes() == initial
    session = service.begin()
    for key, value in zip(RECIPIENT_SETTINGS, (False, False, True), strict=True):
        session.set(key, value)
    session.apply()
    expected = RecipientPreferences(False, False, True)
    assert RecipientPreferences.from_settings(settings_for(context)) == expected
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert RecipientPreferences.from_settings(other) == RecipientPreferences()
    transfer = settings_for(_context(tmp_path / "transfer"))
    exported = service.export(setting_ids=RECIPIENT_SETTINGS)
    transfer.apply_import(transfer.plan_import(exported))
    assert RecipientPreferences.from_settings(transfer) == expected
    for key in RECIPIENT_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert RecipientPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__AUTOCOMPLETE", "true")
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__RECENT_RECIPIENTS", "true")
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__REMEMBER_BCC", "false")
    assert RecipientPreferences.from_settings(service) == RecipientPreferences()
    assert context.paths.config_file.read_bytes() == before
    context.close()


@pytest.mark.parametrize("key", RECIPIENT_SETTINGS)
def test_invalid_boolean_preserves_saved_bytes(tmp_path, key):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, "sometimes")
    assert context.paths.config_file.read_bytes() == before
    context.close()


def test_recent_history_validation_bounds_deduplication_and_account_isolation(tmp_path):
    context = _context(tmp_path)
    service = settings_for(context)
    values = ("not an address", "Ada@example.test", "ADA@EXAMPLE.test", "bad\x1b@example.test")
    assert recent_addresses(values) == ("Ada@example.test",)
    service.set(RECIPIENT_HISTORY_SETTING, values[:3])
    remember_sent_recipients(service, sent_draft(), own_address=context.config.email)
    assert RecipientPreferences.from_settings(settings_for(context)).recent == (
        "Ada@example.test",
        "cc@example.test",
    )
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert RecipientPreferences.from_settings(other).recent == ()
    remember_sent_recipients(
        service, replace(sent_draft(), to=[Address(f"p{i}@example.test") for i in range(50)])
    )
    assert len(RecipientPreferences.from_settings(service).recent) == 20
    assert recent_addresses("invalid" for _ in range(10000)) == ()
    exported = service.export()
    assert RECIPIENT_HISTORY_SETTING not in exported["settings"]
    assert "Ada@example.test" not in json.dumps(exported)
    context.close()


def test_concurrent_history_edit_is_preserved_instead_of_overwritten(tmp_path, monkeypatch):
    context = _context(tmp_path)
    service = settings_for(context)
    snapshot = service.snapshot

    def concurrent(keys):
        result = snapshot(keys)
        settings_for(context).set(RECIPIENT_HISTORY_SETTING, ("newer@example.test",))
        return result

    monkeypatch.setattr(service, "snapshot", concurrent)
    with pytest.raises(ConfigurationError, match="changed"):
        remember_sent_recipients(service, sent_draft())
    assert RecipientPreferences.from_settings(settings_for(context)).history == (
        "newer@example.test",
    )
    context.close()


@pytest.mark.parametrize(
    "autocomplete,recent,bcc", [(False, True, False), (True, False, True), (True, True, True)]
)
def test_recording_policy_is_independent_of_completion_and_bcc_opt_in(
    tmp_path, autocomplete, recent, bcc
):
    context = _context(tmp_path)
    service = settings_for(context)
    session = service.begin()
    for key, value in zip(RECIPIENT_SETTINGS, (autocomplete, recent, bcc), strict=True):
        session.set(key, value)
    session.apply()
    before = context.paths.config_file.read_bytes()
    remember_sent_recipients(service, sent_draft(), own_address=context.config.email)
    policy = RecipientPreferences.from_settings(settings_for(context))
    if not recent:
        assert context.paths.config_file.read_bytes() == before
        assert policy.recent == ()
    else:
        assert policy.recent == ("Ada@example.test", "cc@example.test") + (
            ("private@example.test",) if bcc else ()
        )
        assert (policy.suggest("Ad") is not None) == autocomplete
    context.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("AD", "ADa@example.test"),
        ("  ad", "  ada@example.test"),
        ("prior@example.test, ad", "prior@example.test, ada@example.test"),
        (
            '"Prior, Person" <prior@example.test>, AD',
            '"Prior, Person" <prior@example.test>, ADa@example.test',
        ),
        ("Ada@example.test, ad", None),
        ("Ada@example.test", None),
        ("prior@example.test, ", None),
        ("invalid, ad", None),
        ('"unclosed, ad', None),
        ("Prior <prior@example.test, ad", None),
        ("ad\x1b[31m", None),
        ("ad\n", None),
        ("a" * 8193, None),
        ("unknown", None),
    ],
)
def test_completion_preserves_case_quotes_and_prior_recipients_without_duplicate_or_hostile_input(
    value, expected
):
    assert RecipientPreferences(history=("Ada@example.test",)).suggest(value) == expected


def invoke(tmp_path, arguments):
    return CliRunner().invoke(
        cli.app, ["--json", "--non-interactive", "--yes", *arguments], env=_environment(tmp_path)
    )


@pytest.mark.parametrize("kind", ["send", "reply", "reply-all", "forward", "draft"])
def test_cli_accepted_send_records_only_exact_successful_recipients(tmp_path, monkeypatch, kind):
    context = _context(tmp_path)
    gateway = ComposeFake(
        MessageDetail(
            MessageSummary(
                "synthetic:original",
                sender=Address("Ada@example.test"),
                subject="Original",
                received_at=None,
            ),
            to=(Address("student@example.test"),),
            body_text="Synthetic original",
        )
    )
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    if kind == "draft":
        local = DraftService(context.state, context.paths.recovery_dir).create(sent_draft())
        args = ["drafts", "send", local.reference]
    elif kind == "send":
        args = [
            "send",
            "--to",
            "Ada@example.test",
            "--bcc",
            "private@example.test",
            "--subject",
            "Synthetic",
            "--body",
            "Offline fixture",
        ]
    else:
        args = [kind, "synthetic:original", "--body", "Offline fixture"]
        if kind == "forward":
            args += ["--to", "Ada@example.test"]
    result = invoke(tmp_path, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["sent"] is True
    assert len([call for call in gateway.calls if call[0] != "show"]) == 1
    policy = RecipientPreferences.from_settings(settings_for(context))
    assert policy.recent[0] == "Ada@example.test" and "private@example.test" not in policy.recent
    assert context._gateway is None
    context.close()


@pytest.mark.parametrize(
    "mode", ["dry-run", "schedule", "rejected", "uncertain", "disabled", "history-write-error"]
)
def test_cli_preview_queue_failure_opt_out_and_local_error_do_not_confuse_send_outcome(
    tmp_path, monkeypatch, mode
):
    context = _context(tmp_path)
    service = settings_for(context)
    if mode == "disabled":
        service.set("contacts.recent_recipients", False)
    gateway = ComposeFake()
    if mode in {"rejected", "uncertain"}:

        def send(draft):
            gateway.calls.append(("send", draft))
            if mode == "rejected":
                return {"accepted": False}
            raise TimeoutError("Synthetic outcome unknown")

        gateway.send = send
    if mode == "history-write-error":
        import gucmail.services.recipient_assistance as module

        def broken(*args, **kwargs):
            raise OSError("Synthetic private path that must not be printed")

        monkeypatch.setattr(module, "remember_sent_recipients", broken)
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    args = [
        "send",
        "--to",
        "Ada@example.test",
        "--subject",
        "Synthetic",
        "--body",
        "Offline fixture",
    ]
    if mode == "dry-run":
        args += ["--dry-run"]
    elif mode == "schedule":
        args += ["--schedule", "2099-01-05T12:00:00+02:00"]
    result = invoke(tmp_path, args)
    assert (result.exit_code == 0) == (mode not in {"rejected", "uncertain"}), result.output
    assert RecipientPreferences.from_settings(service).history == ()
    if mode in {"dry-run", "schedule"}:
        assert gateway.calls == []
    else:
        assert len(gateway.calls) == 1
    if mode == "history-write-error":
        assert json.loads(result.stdout)["data"]["sent"] is True
        assert "Do not resend" in result.stderr
        assert "Synthetic private path" not in result.output
    context.close()


@pytest.mark.asyncio
async def test_recipient_completion_preserves_prior_address_and_typed_case(tmp_path):
    context = _context(tmp_path)
    settings_for(context).set("palette.recent_people", ("Ada@example.test",))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(DraftData("synthetic:draft", subject="Synthetic", body_text="Local"))
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen)
            and app.screen._status_checked and not app.screen._attempt_pending,
        )
        field = app.screen.query_one("#compose-to", Input)
        assert not field.disabled
        field.focus()
        field.value = '"Prior, Person" <prior@example.test>, '
        field.cursor_position = len(field.value)
        await pilot.pause()
        field.cursor_position = len(field.value)
        await pilot.press("A", "D")
        await wait_for(pilot, lambda: bool(field._suggestion))
        await pilot.press("right")
        assert field.value == '"Prior, Person" <prior@example.test>, ADa@example.test'
        assert context._gateway is None and app.gateway is None
    context.close()


@pytest.mark.asyncio
async def test_successful_send_history_excludes_bcc_by_default(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._remember_contacted_people(
            DraftData(
                "synthetic:sent",
                to=[Address("visible@example.test")],
                bcc=[Address("private@example.test")],
            )
        )
        await pilot.pause()
        assert settings_for(context).value("palette.recent_people").effective_value == (
            "visible@example.test",
        )
        assert context._gateway is None and app.gateway is None
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("name", ["to", "cc", "bcc"])
async def test_normal_composer_keyboard_completes_each_recipient_field_at_three_sizes(
    tmp_path, size, name
):
    context = _context(tmp_path)
    settings_for(context).set(RECIPIENT_HISTORY_SETTING, ("Ada@example.test",))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_compose(sent_draft())
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        target = composer.query_one(f"#compose-{name}", Input)
        composer.query_one("#compose-to", Input).focus()
        for _ in range(20):
            if composer.focused is target:
                break
            await pilot.press("tab")
        assert composer.focused is target
        await pilot.pause()
        assert target.content_region.intersection(composer.region) == target.content_region
        target.value = "prior@example.test, AD"
        target.cursor_position = len(target.value)
        await wait_for(pilot, lambda: bool(target._suggestion))
        await pilot.press("right")
        assert target.value == "prior@example.test, ADa@example.test"
        recipients = composer._payload(action="save")[name]
        assert recipients == target.value
        assert context._gateway is None and app.gateway is None
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_cancel_apply_resume_and_clear_preserve_composer_and_private_history(
    tmp_path, size
):
    context = _context(tmp_path)
    service = settings_for(context)
    service.set(RECIPIENT_HISTORY_SETTING, ("Ada@example.test",))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_compose(sent_draft())
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        field = composer.query_one("#compose-to", Input)
        field.value = "AD"
        await wait_for(pilot, lambda: bool(field._suggestion))
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen

        async def stage(key, value):
            await wait_for(pilot, lambda: not screen._operation_active)
            assert screen.action_select_setting(key)
            await pilot.pause()
            if key == RECIPIENT_HISTORY_SETTING:
                screen.query_one("#settings-editor", Input).value = value
            else:
                screen.query_one("#settings-choice", Select).value = "on" if value else "off"
            await activate(pilot, screen, "#settings-stage")
            await wait_for(pilot, lambda: key in screen._staged and not screen._operation_active)

        await stage("contacts.recent_recipients", False)
        await activate(pilot, screen, "#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        assert context.paths.config_file.read_bytes() == before
        await stage("contacts.recent_recipients", False)
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(pilot, lambda: app.screen is composer and field.suggester is None)
        assert field.value == "AD" and field._suggestion == ""
        retained = RecipientPreferences.from_settings(service)
        assert retained.history == ("Ada@example.test",) and retained.recent == ()
        before = context.paths.config_file.read_bytes()
        assert app._remember_contacted_people(sent_draft())
        assert context.paths.config_file.read_bytes() == before
        assert not any(
            row.invocation.command_id == "compose.new"
            for row in app._palette_supplemental_suggestions(app._palette_context())
        )
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        await stage("contacts.recent_recipients", True)
        await stage("contacts.remember_bcc", True)
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(pilot, lambda: app.screen is composer and bool(field._suggestion))
        assert app._remember_contacted_people(sent_draft())
        assert "private@example.test" in RecipientPreferences.from_settings(service).recent
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        await stage(RECIPIENT_HISTORY_SETTING, "")
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(pilot, lambda: app.screen is composer and not field._suggestion)
        assert field.value == "AD"
        assert RecipientPreferences.from_settings(settings_for(context)).history == ()
        assert context._gateway is None and app.gateway is None
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["accepted", "rejected", "uncertain", "queued"])
async def test_normal_tui_review_send_records_history_only_after_accepted_send(
    tmp_path, monkeypatch, mode
):
    context = _context(tmp_path)
    if mode == "queued":
        settings_for(context).set("composer.undo_send_seconds", 30)
    gateway = ComposeFake()
    if mode in {"rejected", "uncertain"}:

        def send(draft):
            gateway.calls.append(("send", draft))
            if mode == "rejected":
                return {"accepted": False}
            raise TimeoutError("Synthetic uncertain send")

        gateway.send = send
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    statuses = []
    monkeypatch.setattr(app, "_set_status", statuses.append)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_compose(sent_draft(), new=True)
        await wait_for(
            pilot, lambda: isinstance(app.screen, ComposeScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        assert RecipientPreferences.from_settings(settings_for(context)).history == ()
        composer.query_one("#compose-send", Button).press()
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert "private@example.test" in app.screen.detail
        assert RecipientPreferences.from_settings(settings_for(context)).history == ()
        await pilot.pause()
        app.screen.query_one("#confirm", Button).press()
        await wait_for(
            pilot,
            lambda: (
                bool(app._scheduler_service().list()) if mode == "queued" else bool(gateway.calls)
            ),
        )
        await app.workers.wait_for_complete()
        history = RecipientPreferences.from_settings(settings_for(context)).history
        if mode == "accepted":
            assert history == ("Ada@example.test", "cc@example.test")
            assert any("Send accepted" in status for status in statuses)
        else:
            assert history == ()
        assert len(gateway.calls) == (0 if mode == "queued" else 1)
        assert context._gateway is None and app.gateway is None
    context.close()
