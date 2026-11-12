"""Runtime-faithful, provider-free previews and visible normal-app controls."""

import json
from dataclasses import replace
from datetime import UTC, datetime, time

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static, TabbedContent, Tabs
from typer.testing import CliRunner

import gucmail.cli as cli_module
import gucmail.tui.app as app_module
from gucmail.models import Address
from gucmail.notification_events import EventDisclosurePolicy, NewMailEvent, PrivacyMode
from gucmail.notification_policies import (
    MonitoringProfile,
    ProviderPolicy,
    QuietHoursBehavior,
    QuietHoursSchedule,
    QuietHoursWindow,
    RouteMode,
    RoutingCell,
    RoutingMatrix,
)
from gucmail.notification_rules import (
    NotificationRule,
    RuleAction,
    RuleActionKind,
    TextCondition,
    TextField,
    TextOperator,
)
from gucmail.secret_store import InMemorySecretBackend, SecretStore
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.notification_rule_builder import RuleBuilder
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen, RuleEditorScreen
from test_notification_cli_secondpass import _environment
from test_notification_settings_screen_secondpass import NotificationSettingsHarness, _profile
from test_tui import FakeGateway, context_for

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _event():
    return NewMailEvent(
        event_id="synthetic-preview",
        account_id="owned-account",
        occurred_at=NOW,
        detected_at=NOW,
        folder="Inbox",
        message_reference="message-1",
        subject="Final exam schedule",
        sender=Address("sender@example.test"),
    )


def _routing_profile():
    rule = NotificationRule(
        "exam",
        "Exams",
        TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("exam",)),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),),
        enabled=True,
    )
    return MonitoringProfile(
        "campus",
        "Campus",
        ("Inbox",),
        rules=(rule,),
        providers=(ProviderPolicy("desktop", enabled=True),),
        enabled=True,
    )


@pytest.mark.parametrize("mode", ["disabled", "filter", "matrix", "queue", "digest", "suppress"])
def test_preview_uses_runtime_routes_and_exposure_without_persisting(tmp_path, mode):
    store = StateStore(tmp_path / "state.db", account_key="owned-account")
    service = NotificationConfigurationService(store)
    profile = _routing_profile()
    if mode == "disabled":
        profile = replace(profile, providers=(replace(profile.providers[0], enabled=False),))
    elif mode == "filter":
        profile = replace(
            profile,
            providers=(
                replace(
                    profile.providers[0],
                    filter_condition=TextCondition(
                        TextField.SUBJECT, TextOperator.EXACT, ("News",)
                    ),
                ),
            ),
        )
    elif mode == "matrix":
        profile = replace(
            profile,
            rules=(
                replace(
                    profile.rules[0],
                    actions=(RuleAction(RuleActionKind.SET_URGENCY, value="high"),),
                ),
            ),
            providers=(ProviderPolicy("digest", True, EventDisclosurePolicy(PrivacyMode.PRIVATE)),),
            routing_matrix=RoutingMatrix(
                ("digest",), (RoutingCell("exam", "digest", RouteMode.DIGEST),)
            ),
        )
    else:
        profile = replace(
            profile,
            quiet_hours=QuietHoursSchedule(
                (
                    QuietHoursWindow(
                        "day", time(11), time(13), "UTC", behavior=QuietHoursBehavior(mode)
                    ),
                )
            ),
        )
    service.profiles.save_profile(profile)
    with store.connection() as connection:
        before = tuple(connection.iterdump())
    result = service.simulate("campus", (_event(),), at=NOW)
    runtime = service.test_event("campus", _event(), at=NOW).routing
    example = result.examples[0]
    assert result.matching_events == 1
    assert example.message_reference == "message-1" and example.subject == "Final exam schedule"
    assert example.immediate_providers == runtime.immediate_providers
    assert example.digest_providers == runtime.digest_providers
    assert example.queued_providers == runtime.queued_providers
    assert example.suppressed == runtime.suppressed
    assert example.skipped_providers == runtime.skipped_providers
    assert example.exposed_fields == {
        key: tuple(sorted(payload)) for key, payload in runtime.provider_payloads.items()
    }
    if mode in {"disabled", "filter", "suppress"}:
        assert result.provider_counts == {} and result.notification_events == 0
        assert result.provider_exposed_fields == {}
    elif mode == "matrix":
        assert result.provider_counts == {"digest": 1}
        assert example.digest_providers == ("digest",)
        assert "subject" not in example.exposed_fields["digest"]
    else:
        assert result.provider_counts == {"desktop": 1}
        assert getattr(
            example, mode + "d_providers" if mode == "queue" else "digest_providers"
        ) == ("desktop",)
    with store.connection() as connection:
        assert tuple(connection.iterdump()) == before


def test_selected_rule_preview_is_case_insensitive_and_does_not_change_profile(tmp_path):
    store = StateStore(tmp_path / "state.db", account_key="owned-account")
    service = NotificationConfigurationService(store)
    profile = _routing_profile()
    profile = replace(
        profile, rules=(profile.rules[0], replace(profile.rules[0], rule_id="second"))
    )
    service.profiles.save_profile(profile)
    persisted = service.profiles.get_profile("campus")
    result = service.simulate("campus", (_event(),), rule_id="EXAM", at=NOW)
    assert result.examples[0].matched_rule_ids == ("exam",)
    assert service.test_event("campus", _event(), rule_id="EXAM").decision.matched_rule_ids == (
        "exam",
    )
    with pytest.raises(KeyError):
        service.simulate("campus", (), rule_id="missing")
    assert service.profiles.get_profile("campus") == persisted


def test_disabled_selected_rule_can_be_tested_before_enabling_without_a_write(tmp_path):
    store = StateStore(tmp_path / "state.db", account_key="owned-account")
    service = NotificationConfigurationService(store)
    profile = _routing_profile()
    profile = replace(profile, rules=(replace(profile.rules[0], enabled=False),))
    service.profiles.save_profile(profile)
    with store.connection() as connection:
        before = tuple(connection.iterdump())
    assert service.simulate("campus", (_event(),), at=NOW).matching_events == 0
    result = service.simulate("campus", (_event(),), rule_id="exam", at=NOW)
    assert result.matching_events == 1 and result.provider_counts == {"desktop": 1}
    assert result.examples[0].matched_rule_ids == ("exam",)
    assert service.test_event("campus", _event(), rule_id="exam").routing.immediate_providers == (
        "desktop",
    )
    assert service.profiles.get_profile("campus").rules[0].enabled is False
    with store.connection() as connection:
        assert tuple(connection.iterdump()) == before


def _metadata_gateway(monkeypatch):
    gateway = FakeGateway()
    calls = []
    original = gateway.list_messages

    def listing(**kwargs):
        calls.append(kwargs)
        return [
            replace(
                original(**{**kwargs, "folder": "inbox"})[0],
                reference="synthetic-" + kwargs["folder"].replace("/", "-"),
                folder_path=kwargs["folder"],
                subject="Final exam schedule \x1b[31mexample",
            )
        ]

    monkeypatch.setattr(gateway, "list_messages", listing)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Dry run attempted a mutation, body fetch or attachment download")

    for name in (
        "send",
        "reply",
        "forward",
        "set_read",
        "delete",
        "move",
        "get_message",
        "save_attachment",
    ):
        monkeypatch.setattr(gateway, name, forbidden, raising=False)
    return gateway, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_palette_recent_sample_and_visible_guided_cancel(tmp_path, monkeypatch, size):
    context = context_for(tmp_path)
    service = NotificationConfigurationService(context.state)
    profile = _profile()
    service.profiles.save_profile(
        replace(profile, rules=(replace(profile.rules[0], enabled=False),))
    )
    service.profiles.activate("campus")
    before = service.profiles.get_profile("campus")
    backend = InMemorySecretBackend()
    monkeypatch.setattr(
        app_module, "SecretStore", lambda scope: SecretStore(scope, backend=backend)
    )
    gateway, calls = _metadata_gateway(monkeypatch)
    app = GUCmailApp(context, gateway=gateway)
    monkeypatch.setattr(
        app, "_deliver_webhook_test", lambda *_a, **_k: pytest.fail("Dry run delivered")
    )

    async def wait(pilot, predicate):
        for _ in range(250):
            await pilot.pause(0.02)
            if predicate():
                return
        pytest.fail(f"Normal notification workflow did not settle: {app.screen}; calls={calls}")

    async def focus_visible(pilot, screen, identifier):
        button = screen.query_one(identifier, Button)
        assert not button.disabled
        button.focus()
        pane = next(node for node in button.ancestors if isinstance(node, VerticalScroll))
        await wait(
            pilot,
            lambda: (
                button.has_focus
                and pane.content_region.contains_region(button.region)
                and screen.region.contains_region(button.region)
            ),
        )
        return button

    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.ui.online)
            await pilot.press("ctrl+k")
            await wait(
                pilot, lambda: isinstance(app.screen, PaletteScreen) and app.screen.is_mounted
            )
            app.screen.query_one("#palette-input", Input).value = "Open Notification Settings"
            await pilot.pause()
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: (
                    isinstance(app.screen, NotificationSettingsScreen)
                    and app.screen.is_mounted
                    and app.screen._profile() is not None
                    and not app.screen._operation_active
                ),
            )
            screen = app.screen
            assert screen.recent_simulation_source is not None
            screen.query_one(Tabs).focus()
            await pilot.press("right")
            await wait(
                pilot, lambda: screen.query_one(TabbedContent).active == "notification-rules-tab"
            )
            calls.clear()
            await focus_visible(pilot, screen, "#notification-rule-recent")
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: (
                    not screen._operation_active
                    and "Recent metadata sample"
                    in str(screen.query_one("#notification-rule-detail", Static).content)
                ),
            )
            report = str(screen.query_one("#notification-rule-detail", Static).content)
            assert "DRY RUN" in report and "synthetic-Inbox" in report
            assert "Matching examples" in report and "\x1b" not in report
            assert "evaluated even if disabled" in report
            assert len(calls) == 2 and sum(call["limit"] for call in calls) == 50
            assert service.profiles.get_profile("campus") == before
            await focus_visible(pilot, screen, "#notification-rule-new")
            await pilot.press("enter")
            await wait(
                pilot, lambda: isinstance(app.screen, RuleEditorScreen) and app.screen.is_mounted
            )
            assert app.screen.query_one(RuleBuilder).is_mounted
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is screen)
            assert service.profiles.get_profile("campus") == before and len(calls) == 2
    finally:
        context.close()


@pytest.mark.asyncio
async def test_all_notification_pane_actions_remain_visible_after_resize(tmp_path):
    store = StateStore(tmp_path / "state.db", account_key="owned-account")
    service = NotificationConfigurationService(store)
    service.profiles.save_profile(_profile())
    service.profiles.activate("campus")
    before = service.profiles.get_profile("campus")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    controls = (
        ("notification-rules-tab", "#notification-rule-delete"),
        ("notification-quiet-tab", "#notification-quiet-delete"),
        ("notification-digest-tab", "#notification-digest-delete"),
        ("notification-provider-tab", "#notification-provider-remove"),
        ("notification-webhook-tab", "#notification-webhook-remove"),
        ("notification-routing-tab", "#notification-route-off"),
        ("notification-transfer-tab", "#notification-import-preview"),
        ("notification-diagnostics-tab", "#notification-diagnostics-run"),
    )
    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(250):
            await pilot.pause(0.02)
            if screen._profile() is not None and not screen._operation_active:
                break
        assert screen._profile() is not None
        for width, height in ((120, 35), (70, 20), (80, 24)):
            await pilot.resize_terminal(width, height)
            for tab_id, identifier in controls:
                # Use the ordinary keyboard route. Assigning .active followed by
                # focus() can focus a pane while its queued Hide/Show messages are
                # still in flight; the retained diagnostic records that ordering.
                tabs = screen.query_one(Tabs)
                tabs.focus()
                for _ in range(250):
                    await pilot.pause(0.02)
                    if tabs.has_focus:
                        break
                assert tabs.has_focus
                for _ in range(10):
                    if screen.query_one(TabbedContent).active == tab_id:
                        break
                    await pilot.press("right")
                assert screen.query_one(TabbedContent).active == tab_id
                button = screen.query_one(identifier, Button)
                # Empty webhook configuration intentionally disables Remove. Verify its
                # geometry using the adjacent enabled setup action instead.
                if button.disabled:
                    assert tab_id == "notification-webhook-tab"
                    button = screen.query_one("#notification-webhook-add-teams", Button)
                for _ in range(250):
                    await pilot.pause(0.02)
                    if all(node.display for node in (button, *button.ancestors)):
                        break
                assert all(node.display for node in (button, *button.ancestors))
                button.focus()
                pane = next(node for node in button.ancestors if isinstance(node, VerticalScroll))
                for _ in range(250):
                    await pilot.pause(0.02)
                    if button.has_focus and pane.content_region.contains_region(button.region):
                        break
                assert button.has_focus and pane.content_region.contains_region(button.region), (
                    tab_id,
                    width,
                    height,
                    button.region,
                    pane.content_region,
                )
                assert screen.region.contains_region(button.region)
        assert service.profiles.get_profile("campus") == before


@pytest.mark.parametrize("mode", ["recent", "synthetic", "missing", "invalid_limit"])
def test_cli_rule_preview_is_bounded_and_provider_free(tmp_path, monkeypatch, mode):
    context = context_for(tmp_path)
    service = NotificationConfigurationService(context.state)
    service.profiles.save_profile(_profile())
    before = service.profiles.get_profile("campus")
    gateway, calls = _metadata_gateway(monkeypatch)
    resolutions = []
    monkeypatch.setattr(cli_module, "_notification_service", lambda _ctx: service)
    monkeypatch.setattr(cli_module, "_gateway", lambda _ctx: resolutions.append(True) or gateway)
    args = [
        "--json",
        "--non-interactive",
        "notifications",
        "rules",
        "test",
        "exams",
        "--profile",
        "campus",
    ]
    if mode != "synthetic":
        args.append("--recent")
    if mode == "missing":
        args[5] = "missing"
    if mode == "invalid_limit":
        args.extend(["--limit", "101"])
    try:
        result = CliRunner().invoke(cli_module.app, args, env=_environment(tmp_path / "cli"))
        if mode in {"missing", "invalid_limit"}:
            assert result.exit_code != 0 and not resolutions and not calls
        else:
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)["data"]
            if mode == "synthetic":
                assert "decision" in data and not resolutions and not calls
            else:
                assert data["mode"] == "dry_run" and data["delivery_performed"] is False
                assert data["request_budget"] == 50 and len(calls) == 2
                assert sum(call["limit"] for call in calls) == 50 and len(resolutions) == 1
                assert data["result"]["examples"][0]["message_reference"] == "synthetic-Inbox"
                assert data["unavailable_fields"] == ["body", "recipients", "attachment_names"]
        assert service.profiles.get_profile("campus") == before
    finally:
        context.close()
