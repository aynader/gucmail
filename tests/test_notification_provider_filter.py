"""Provider edits preserve their disclosure boundary and use the shared condition AST."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Input, Static, TabbedContent, Tabs, TextArea

import gucmail.tui.app as app_module
from gucmail.notification_events import NewMailEvent
from gucmail.notification_policies import MonitoringProfile, ProviderPolicy
from gucmail.notification_rules import (
    AllCondition,
    BooleanCondition,
    BooleanField,
    NotCondition,
    NotificationRule,
    RuleAction,
    RuleActionKind,
    TextCondition,
    TextField,
    TextOperator,
    condition_to_data,
)
from gucmail.secret_store import InMemorySecretBackend, SecretStore
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    ProviderConfigurationMetadata,
)
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.notification_rule_builder import RuleBuilder, RuleValueForm
from gucmail.tui.notification_settings_screen import (
    NotificationSettingsScreen,
    ProviderEditorScreen,
    ProviderFilterEditorScreen,
)
from test_notification_runtime_preview import _metadata_gateway
from test_tui import context_for

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _predicate():
    return TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("Security",))


def _profile(policy):
    rule = NotificationRule(
        "inbox",
        "Inbox",
        TextCondition(TextField.FOLDER, TextOperator.EXACT, ("Inbox",)),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, policy.provider_id),),
        enabled=True,
    )
    return MonitoringProfile(
        "guarded", "Guarded profile", ("Inbox",), rules=(rule,), providers=(policy,), enabled=True
    )


def _event(subject="Unrelated newsletter"):
    return NewMailEvent(
        "synthetic-provider-filter",
        NOW,
        NOW,
        "owned-account",
        folder="Inbox",
        message_reference="synthetic-ref",
        subject=subject,
        is_read=False,
    )


class EditorHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("Owned synthetic provider configuration")


async def _wait(pilot, predicate):
    for _ in range(250):
        await pilot.pause(0.02)
        if predicate():
            return
    pytest.fail("Provider workflow did not settle")


async def _activate(pilot, screen, selector):
    button = screen.query_one(selector, Button)
    assert not button.disabled
    button.focus()
    pane = next(node for node in button.ancestors if isinstance(node, VerticalScroll))
    await _wait(
        pilot,
        lambda: (
            button.has_focus
            and pane.content_region.contains_region(button.region)
            and screen.region.contains_region(button.region)
        ),
    )
    await pilot.press("enter")


@pytest.mark.asyncio
@pytest.mark.parametrize("condition_kind", ["none", "subject", "nested"])
async def test_renaming_provider_preserves_every_policy_field_and_runtime_filter(
    tmp_path, condition_kind
):
    condition = None if condition_kind == "none" else _predicate()
    if condition_kind == "nested":
        condition = AllCondition(
            (_predicate(), NotCondition(BooleanCondition(BooleanField.IS_READ, expected=True)))
        )
    policy = ProviderPolicy(
        "guarded-console", True, filter_condition=condition, maximum_notifications_per_burst=7
    )
    metadata = ProviderConfigurationMetadata(
        policy.provider_id,
        "console",
        "Original name",
        True,
        "keyring://owned/provider",
        {"label": "kept", "nested": {"opaque": [1, False]}},
    )
    store = StateStore(tmp_path / "state.db", account_key="owned-account")
    service = NotificationConfigurationService(store)
    profile = _profile(policy)
    service.profiles.save_profile(profile)
    original = service.simulate(profile.profile_id, (_event(),), at=NOW)
    app = EditorHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        editor = ProviderEditorScreen(metadata, policy, digest_ids=())
        app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#notification-provider-name", Input).value = "Renamed console"
        edited = editor.build_value()
        assert edited.policy == policy
        assert edited.metadata == replace(metadata, name="Renamed console")
        assert editor.query_one(Collapsible).collapsed
        service.profiles.save_profile(replace(profile, providers=(edited.policy,)))
        reopened = NotificationConfigurationService(
            StateStore(tmp_path / "state.db", account_key="owned-account")
        )
        assert reopened.profiles.get_profile("guarded").providers == (policy,)
        after = reopened.simulate("guarded", (_event(),), at=NOW)
        assert after.provider_counts == original.provider_counts
        assert after.notification_events == (1 if condition is None else 0)
        assert after.examples[0].skipped_providers == original.examples[0].skipped_providers


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_provider_guided_filter_save_reopen_and_cancel(tmp_path, monkeypatch, size):
    context = context_for(tmp_path)
    service = NotificationConfigurationService(context.state)
    policy = ProviderPolicy("guarded-console", True, filter_condition=_predicate())
    service.profiles.save_profile(_profile(policy))
    service.profiles.activate("guarded")
    metadata = ProviderConfigurationMetadata(policy.provider_id, "console", "Guarded console", True)
    service.providers.save_provider(metadata)
    backend = InMemorySecretBackend()
    monkeypatch.setattr(
        app_module, "SecretStore", lambda scope: SecretStore(scope, backend=backend)
    )
    gateway, calls = _metadata_gateway(monkeypatch)
    app = GUCmailApp(context, gateway=gateway)
    monkeypatch.setattr(
        app, "_deliver_webhook_test", lambda *_a, **_k: pytest.fail("Filter edit delivered")
    )
    try:
        async with app.run_test(size=size) as pilot:
            await _wait(pilot, lambda: app.ui.online)
            await pilot.press("ctrl+k")
            await _wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            app.screen.query_one("#palette-input", Input).value = "Open Notification Settings"
            await pilot.pause()
            await pilot.press("enter")
            await _wait(
                pilot,
                lambda: (
                    isinstance(app.screen, NotificationSettingsScreen)
                    and app.screen.is_mounted
                    and app.screen._profile() is not None
                    and not app.screen._operation_active
                ),
            )
            screen = app.screen
            screen.query_one(Tabs).focus()
            await pilot.press("right", "right", "right", "right")
            await _wait(
                pilot,
                lambda: screen.query_one(TabbedContent).active == "notification-provider-tab",
            )
            calls.clear()
            await _activate(pilot, screen, "#notification-provider-edit")
            await _wait(pilot, lambda: isinstance(app.screen, ProviderEditorScreen))
            editor = app.screen
            await _activate(pilot, editor, "#notification-provider-edit-filter")
            await _wait(pilot, lambda: isinstance(app.screen, ProviderFilterEditorScreen))
            filter_editor = app.screen
            assert not filter_editor.query_one(".rule-builder-action-controls").display
            assert filter_editor.query_one(RuleBuilder).actions == []
            await _activate(pilot, filter_editor, "#rule-tree-edit")
            await _wait(pilot, lambda: isinstance(app.screen, RuleValueForm))
            value_form = app.screen
            value_form.query_one("#rule-value-values", TextArea).load_text("Final")
            value_form.query_one("#rule-value-save", Button).focus()
            await pilot.press("enter")
            await _wait(pilot, lambda: app.screen is filter_editor)
            await _activate(pilot, filter_editor, "#notification-provider-filter-save")
            await _wait(pilot, lambda: app.screen is editor)
            assert service.profiles.get_profile("guarded").providers == (policy,)
            assert editor.filter_condition != policy.filter_condition
            await _activate(pilot, editor, "#notification-provider-save")
            await _wait(pilot, lambda: app.screen is screen and not screen._operation_active)
            saved = service.profiles.get_profile("guarded").providers[0]
            assert condition_to_data(saved.filter_condition)["values"] == ["Final"]
            assert service.simulate("guarded", (_event("Final schedule"),)).provider_counts == {
                "guarded-console": 1
            }
            assert service.simulate("guarded", (_event("Security alert"),)).provider_counts == {}
            await _activate(pilot, screen, "#notification-provider-edit")
            await _wait(pilot, lambda: isinstance(app.screen, ProviderEditorScreen))
            reopened = app.screen
            assert reopened.filter_condition == saved.filter_condition
            await _activate(pilot, reopened, "#notification-provider-remove-filter")
            assert reopened.filter_condition is None
            await pilot.press("escape")
            await _wait(pilot, lambda: app.screen is screen)
            assert service.profiles.get_profile("guarded").providers == (saved,)
            assert service.providers.get_provider(policy.provider_id) == metadata
            assert calls == []
    finally:
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_filter_modal_cancel_preserves_parent_and_explicit_removal_applies(existing):
    policy = ProviderPolicy("console", True, filter_condition=_predicate() if existing else None)
    metadata = ProviderConfigurationMetadata("console", "console", "Console", True)
    app = EditorHarness()
    async with app.run_test(size=(70, 20)) as pilot:
        editor = ProviderEditorScreen(metadata, policy, digest_ids=())
        app.push_screen(editor)
        await pilot.pause()
        await _activate(pilot, editor, "#notification-provider-edit-filter")
        await _wait(pilot, lambda: isinstance(app.screen, ProviderFilterEditorScreen))
        app.screen.query_one(RuleBuilder).condition = {
            "type": "text",
            "field": "subject",
            "operator": "contains",
            "values": ["changed"],
        }
        await pilot.press("escape")
        await _wait(pilot, lambda: app.screen is editor)
        assert editor.build_value().policy == policy
        await _activate(pilot, editor, "#notification-provider-remove-filter")
        assert editor.build_value().policy == replace(policy, filter_condition=None)
