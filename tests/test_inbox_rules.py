"""Native policy safety, real EWS payloads, uncertainty and mounted product workflows."""

import asyncio
import copy
import json
from threading import Event
from types import SimpleNamespace

import pytest
from exchangelib.properties import Actions, Conditions, Exceptions, Rule
from exchangelib.services import CreateInboxRule, DeleteInboxRule, GetInboxRules, SetInboxRule
from exchangelib.version import Build, Version
from textual.app import App
from textual.widgets import Button, Checkbox, Input, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.config import AppConfig
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    UnsupportedFeatureError,
)
from gucmail.ews.adapter import EWSGateway
from gucmail.services.inbox_rules import (
    InboxRuleOperation,
    InboxRuleService,
    InboxRuleVerificationError,
    SimpleInboxRule,
)
from gucmail.services.notification_configuration import PersistentMonitoringProfileRepository
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.inbox_rules_screen import InboxRulesScreen, SimpleInboxRuleEditor
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from test_cli import _environment
from test_settings_secondpass import _context
from test_tui import FakeGateway
from waiting import wait_until


class RuleServer:
    def __init__(self, monkeypatch):
        self.version = Version(build=Build(15, 1, 2375, 7))
        self.protocol = SimpleNamespace(version=self.version)
        self.rules = [
            Rule(
                id="native-rule-1",
                display_name="Synthetic rule",
                priority=0,
                is_enabled=False,
                conditions=Conditions(contains_subject_strings=["Review"], has_attachments=False),
                exceptions=Exceptions(contains_sender_strings=["excluded.example"]),
                actions=Actions(assign_categories=["Work"], mark_as_read=True),
            )
        ]
        self.writes = []
        self.reads = 0
        self.readback_fails = False
        self.ignore_write = False
        self.response_lost = False
        self.next_id = 2

        def read(service, **kwargs):
            self.reads += 1
            if self.writes and self.readback_fails:
                raise TimeoutError("synthetic readback failure")
            return iter(copy.deepcopy(self.rules))

        def write(service, *, rule, remove_outlook_rule_blob, **kwargs):
            assert remove_outlook_rule_blob is False
            wire = service.get_payload(rule=rule, remove_outlook_rule_blob=remove_outlook_rule_blob)
            assert wire.find("{*}RemoveOutlookRuleBlob").text in {"false", "0"}
            self.writes.append((type(service), copy.copy(rule), wire))
            if self.ignore_write:
                return None
            if isinstance(service, CreateInboxRule):
                result = copy.deepcopy(rule)
                result.id = f"native-rule-{self.next_id}"
                self.next_id += 1
                self.rules.append(result)
            elif isinstance(service, DeleteInboxRule):
                self.rules = [value for value in self.rules if value.id != rule.id]
            elif isinstance(service, SetInboxRule):
                self.rules = [
                    copy.deepcopy(rule) if value.id == rule.id else value for value in self.rules
                ]
            if self.response_lost:
                raise TimeoutError("synthetic lost mutation response")
            return None

        monkeypatch.setattr(GetInboxRules, "call", read)
        for service in (CreateInboxRule, SetInboxRule, DeleteInboxRule):
            monkeypatch.setattr(service, "get", write)
        self.gateway = EWSGateway(AppConfig(), account_factory=lambda: self)
        self.service = InboxRuleService(self.gateway)


@pytest.fixture
def server(monkeypatch):
    return RuleServer(monkeypatch)


def test_enable_preserves_exact_native_policy_and_checks_fresh_preview(server):
    before = server.service.get("native-rule-1")
    assert before.exceptions == {"contains_sender_strings": ["excluded.example"]}
    plan = server.service.plan(before.reference, InboxRuleOperation.ENABLE, current=before)
    with pytest.raises(ConfirmationRequiredError):
        server.service.apply(plan)
    assert server.reads == 1 and not server.writes
    result = server.service.apply(plan, confirmed=True)
    assert result.enabled and result.conditions == before.conditions
    assert result.exceptions == before.exceptions and result.actions == before.actions
    assert len(server.writes) == 1 and server.writes[0][0] is SetInboxRule
    # A second mutation based on the old preview must not overwrite this change.
    with pytest.raises(ConflictError):
        server.service.apply(plan, confirmed=True)
    assert len(server.writes) == 1


def test_disabled_delete_is_one_exact_id_operation_without_temporarily_enabling(server):
    plan = server.service.plan("native-rule-1", InboxRuleOperation.DELETE)
    assert server.service.apply(plan, confirmed=True) is None
    assert len(server.writes) == 1
    kind, original, wire = server.writes[0]
    assert kind is DeleteInboxRule and original.is_enabled is False
    assert wire.find("{*}Operations/{*}DeleteRuleOperation/{*}RuleId").text == "native-rule-1"
    assert server.rules == []


def test_simple_creation_real_serialization_starts_disabled_and_preserves_other_rules(server):
    request = SimpleInboxRule(
        "New synthetic rule",
        3,
        ("sender.example",),
        ("Notice", "Lecture"),
        False,
        ("Course",),
        True,
        stop_processing=True,
    )
    plan = server.service.plan_create(request)
    result = server.service.apply(plan, confirmed=True)
    assert result.reference == "native-rule-2" and result.enabled is False
    assert result.conditions["has_attachments"] is False
    assert result.actions == {
        "assign_categories": ["Course"],
        "mark_as_read": True,
        "stop_processing_rules": True,
    }
    assert len(server.writes) == 1 and len(server.rules) == 2
    assert server.rules[0].id == "native-rule-1" and server.rules[0].is_enabled is False
    assert server.writes[0][2].find(
        "{*}Operations/{*}CreateRuleOperation/{*}Rule/{*}IsEnabled"
    ).text in {"false", "0"}


def test_policy_change_blocks_creation_before_mutation(server):
    plan = server.service.plan_create(
        SimpleInboxRule("New", subject_contains=("x",), mark_read=True)
    )
    server.rules[0].display_name = "Renamed elsewhere"
    with pytest.raises(ConflictError):
        server.service.apply(plan, confirmed=True)
    assert not server.writes


@pytest.mark.parametrize("failure", ["readback", "mismatch", "response_lost"])
def test_uncertain_rule_mutations_do_not_replay(server, failure):
    plan = server.service.plan("native-rule-1", InboxRuleOperation.ENABLE)
    server.readback_fails = failure == "readback"
    server.ignore_write = failure == "mismatch"
    server.response_lost = failure == "response_lost"
    with pytest.raises(InboxRuleVerificationError) as error:
        server.service.apply(plan, confirmed=True)
    assert len(server.writes) == 1
    assert error.value.context.mutation_uncertain and error.value.context.retry_safe is False


@pytest.mark.parametrize("flag", ["is_not_supported", "is_in_error"])
def test_unsupported_rule_can_be_inspected_but_cannot_be_rewritten(server, flag):
    setattr(server.rules[0], flag, True)
    assert server.service.get("native-rule-1").name == "Synthetic rule"
    with pytest.raises(UnsupportedFeatureError):
        server.service.plan("native-rule-1", InboxRuleOperation.ENABLE)
    assert not server.writes


def test_oversized_or_duplicate_policy_fails_closed(server):
    server.rules.append(copy.deepcopy(server.rules[0]))
    with pytest.raises(UnsupportedFeatureError, match="duplicate"):
        server.service.list()
    server.rules = [
        Rule(
            id=str(i),
            display_name="Too many",
            priority=i,
            is_enabled=False,
            actions=Actions(mark_as_read=True),
        )
        for i in range(1001)
    ]
    with pytest.raises(UnsupportedFeatureError, match="1000-rule"):
        server.service.list()
    assert not server.writes


@pytest.mark.parametrize(
    "candidate",
    [
        SimpleInboxRule("No condition", mark_read=True),
        SimpleInboxRule("No action", subject_contains=("x",)),
        SimpleInboxRule("Unsafe\x1bName", subject_contains=("x",), mark_read=True),
        SimpleInboxRule("Huge", subject_contains=("x" * 256,), mark_read=True),
    ],
)
def test_invalid_creation_rejected_without_reads(candidate, server):
    with pytest.raises(ConfigurationError):
        server.service.plan_create(candidate)
    assert server.reads == 0 and not server.writes


def test_cli_help_invalid_local_input_dry_run_confirmation_and_json(server, tmp_path, monkeypatch):
    runner = CliRunner()
    env = _environment(tmp_path)
    for command in ("list", "show", "create", "enable", "disable", "delete"):
        result = runner.invoke(cli.app, ["inbox-rules", command, "--help"], env=env)
        assert result.exit_code == 0, result.output
    monkeypatch.setattr(
        cli.core, "gateway",
        lambda *args, **kwargs: pytest.fail("Invalid local input resolved credentials"),
    )
    bad = runner.invoke(cli.app, ["inbox-rules", "create", "--name", "Invalid"], env=env)
    assert bad.exit_code == 3 and server.reads == 0
    monkeypatch.setattr(cli.core, "gateway", lambda *args, **kwargs: server.gateway)
    base = ["inbox-rules", "enable", "native-rule-1"]
    dry = runner.invoke(cli.app, ["--json", *base, "--dry-run"], env=env)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["data"]["plan"]["before"]["reference"] == "native-rule-1"
    denied = runner.invoke(cli.app, ["--non-interactive", *base], env=env)
    assert denied.exit_code == 9 and not server.writes
    applied = runner.invoke(cli.app, ["--json", "--yes", *base], env=env)
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["data"]["enabled"] is True
    assert len(server.writes) == 1


class _Harness(App):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def on_mount(self):
        self.push_screen(InboxRulesScreen(self.service))


@pytest.mark.asyncio
async def test_mounted_uncertain_write_explains_outcome_and_offers_no_write_retry(server):
    app = _Harness(server.service)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(pilot, lambda: app.screen.rules, interval=0.05, pause_first=True)
        screen = app.screen
        server.readback_fails = True
        screen.action_toggle_rule()
        await pilot.pause()
        await pilot.press("y")
        await wait_until(
            pilot, lambda: screen.view_state.value == "error", interval=0.05, pause_first=True
        )
        from textual.widgets import Static

        text = str(screen.query_one("#area-state", Static).content)
        assert "may have gone through" in text and "Refresh" in text
        assert not screen.query_one("#area-retry", Button).display
        screen.action_retry()
        assert len(server.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_guided_native_rule_workflow_and_cancel_at_three_sizes(server, size):
    app = _Harness(server.service)
    async with app.run_test(size=size) as pilot:
        await wait_until(pilot, lambda: app.screen.rules, interval=0.05, pause_first=True)
        screen = app.screen
        screen.action_toggle_rule()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "native-rule-1" in app.screen.detail_text
        await pilot.press("escape")
        assert not server.writes
        screen.action_create()
        await pilot.pause()
        assert isinstance(app.screen, SimpleInboxRuleEditor)
        app.screen.query_one("#inbox-rule-name", Input).value = "Guided synthetic"
        app.screen.query_one("#inbox-rule-subjects", TextArea).load_text("Lecture\nTutorial")
        app.screen.query_one("#inbox-rule-mark-read", Checkbox).value = True
        await pilot.press("ctrl+s")
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ConfirmationScreen),
            interval=0.05,
            pause_first=True,
        )
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Lecture" in app.screen.detail_text and not server.writes
        await pilot.press("y")
        await wait_until(pilot, lambda: len(screen.rules) == 2, interval=0.05, pause_first=True)
        assert len(screen.rules) == 2 and len(server.writes) == 1
        assert not server.rules[-1].is_enabled


@pytest.mark.asyncio
async def test_actual_mail_app_notification_button_opens_native_rules_without_mutation(
    server, tmp_path
):
    class Gateway(FakeGateway):
        def list_inbox_rules(self):
            return server.gateway.list_inbox_rules()

    app = GUCmailApp(context=_context(tmp_path), gateway=Gateway())
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert server.reads == 0
        app._open_area("notifications")
        await pilot.pause()
        assert isinstance(app.screen, NotificationSettingsScreen)
        screen = app.screen
        async with asyncio.timeout(15):
            while (
                not screen.is_mounted or screen._operation_active
                or screen.view_state.value in {"idle", "loading"}
            ):
                await asyncio.sleep(0.005)
        await pilot.pause()
        button = screen.query_one("#notification-inbox-rules", Button)
        assert not button.disabled
        button.press()
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, InboxRulesScreen) and app.screen.rules,
            interval=0.05,
            pause_first=True,
        )
        assert isinstance(app.screen, InboxRulesScreen)
        assert len(app.screen.rules) == 1 and not server.writes


@pytest.mark.asyncio
async def test_inbox_rule_route_observes_actual_notification_readiness(
    server, tmp_path, monkeypatch
):
    started, release = Event(), Event()
    inspect = PersistentMonitoringProfileRepository.inspect

    def held(repository):
        started.set()
        if not release.wait(20):
            raise TimeoutError("Synthetic local notification profile read hold expired")
        return inspect(repository)

    monkeypatch.setattr(PersistentMonitoringProfileRepository, "inspect", held)

    class Gateway(FakeGateway):
        def list_inbox_rules(self):
            return server.gateway.list_inbox_rules()

    context = _context(tmp_path)
    app = GUCmailApp(context=context, gateway=Gateway())
    try:
        async with app.run_test(size=(100, 35)) as pilot:
            app._open_area("notifications")
            async with asyncio.timeout(15):
                while not (isinstance(app.screen, NotificationSettingsScreen) and started.is_set()):
                    await asyncio.sleep(0.005)
            screen = app.screen
            button = screen.query_one("#notification-inbox-rules", Button)
            assert screen._operation_active and button.disabled
            assert server.reads == 0 and not server.writes
            button.press()
            await pilot.pause()
            assert app.screen is screen and server.reads == 0 and not server.writes
            release.set()
            async with asyncio.timeout(15):
                while (screen._operation_active or button.disabled
                       or screen.view_state.value in {"idle", "loading"}):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert app.screen is screen and server.reads == 0 and not server.writes
            button.focus()
            await pilot.press("enter")
            async with asyncio.timeout(15):
                while not (isinstance(app.screen, InboxRulesScreen) and app.screen.rules):
                    await asyncio.sleep(0.005)
            assert len(app.screen.rules) == 1
            assert server.reads == 1 and not server.writes
            assert len(server.rules) == 1 and not server.rules[0].is_enabled
    finally:
        release.set()
        context.close()
