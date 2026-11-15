from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
    Tabs,
    TextArea,
)

from gucmail.models import Address, Importance, NotificationEvent
from gucmail.notification_events import EventDisclosurePolicy, NewMailEvent, PrivacyMode
from gucmail.notification_policies import (
    DigestCadence,
    DigestSchedule,
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
    AllCondition,
    NotificationRule,
    RuleAction,
    RuleActionKind,
    TextCondition,
    TextField,
    TextOperator,
)
from gucmail.secret_store import InMemorySecretBackend, SecretStore
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    ProviderConfigurationMetadata,
)
from gucmail.services.notifications import DeliveryResult, DeliveryState
from gucmail.services.webhooks import WebhookConfig, WebhookRegistry
from gucmail.state import StateStore
from gucmail.tui.notification_settings_screen import (
    NotificationConfirmationScreen,
    NotificationSettingsScreen,
    ProfileEditorScreen,
    ProviderEditorScreen,
    QuietHoursEditorScreen,
    RuleEditorScreen,
    WebhookSetupScreen,
    preview_provider_metadata_document,
)
from waiting import rounds, wait_until


@pytest.mark.asyncio
async def test_every_notification_tab_has_visible_content(tmp_path: Path) -> None:
    screen = NotificationSettingsScreen(_service(tmp_path / "state.db"))
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        tabs = screen.query_one("#notification-tabs", TabbedContent)
        for name, selector in (
            ("overview", "#notification-overview"),
            ("rules", "#notification-rules-table"),
            ("quiet", "#notification-quiet-table"),
            ("digest", "#notification-digest-table"),
            ("provider", "#notification-provider-table"),
            ("webhook", "#notification-webhook-table"),
            ("routing", "#notification-routing-table"),
            ("transfer", "#notification-transfer-payload"),
            ("diagnostics", "#notification-diagnostics"),
        ):
            tabs.active = f"notification-{name}-tab"
            await pilot.pause()
            widget = screen.query_one(selector)
            assert widget.region.height >= 1, name
            assert screen.region.contains_region(widget.region), name


@pytest.mark.asyncio
async def test_profile_escape_returns_without_saving(tmp_path: Path) -> None:
    screen = NotificationSettingsScreen(_service(tmp_path / "state.db"))
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = ProfileEditorScreen(None)
        app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#notification-profile-name", Input).focus()
        await pilot.press("x", "escape")
        await pilot.pause()
        assert app.screen is screen


def _rule() -> NotificationRule:
    return NotificationRule(
        "exams",
        "Exam notices",
        AllCondition(
            (
                TextCondition(TextField.FOLDER, TextOperator.EXACT, ("Inbox",)),
                TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("exam",)),
            )
        ),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop-main"),),
        enabled=True,
        priority=50,
    )


def _profile() -> MonitoringProfile:
    rule = _rule()
    provider = ProviderPolicy(
        "desktop-main",
        True,
        EventDisclosurePolicy(
            PrivacyMode.SENDER_AND_SUBJECT,
            redact_addresses=True,
        ),
        maximum_notifications_per_burst=4,
    )
    quiet = QuietHoursWindow(
        "night",
        time(23),
        time(8),
        "Africa/Cairo",
        behavior=QuietHoursBehavior.QUEUE,
        bypass_importance=frozenset({Importance.HIGH}),
    )
    digest = DigestSchedule(
        "morning",
        "Morning digest",
        DigestCadence.MORNING,
        "Africa/Cairo",
    )
    return MonitoringProfile(
        "campus",
        "Campus mail",
        ("Inbox", "Inbox/Courses"),
        (rule,),
        (provider,),
        QuietHoursSchedule((quiet,)),
        (digest,),
        RoutingMatrix(
            ("desktop-main",),
            (RoutingCell(rule.rule_id, provider.provider_id, RouteMode.IMMEDIATE),),
        ),
        enabled=True,
    )


def _service(path: Path) -> NotificationConfigurationService:
    service = NotificationConfigurationService(StateStore(path, account_key="settings-test"))
    service.profiles.save_profile(_profile())
    service.profiles.activate("campus")
    service.profiles.set_strategy_preference("campus", "incremental_sync")
    service.providers.save_provider(
        ProviderConfigurationMetadata(
            "desktop-main",
            "desktop",
            "Windows desktop",
            True,
            "keyring://notification/desktop-main",
            {"sound": "default"},
        )
    )
    return service


def _event() -> NewMailEvent:
    now = datetime(2026, 8, 13, 10, tzinfo=UTC)
    return NewMailEvent(
        "synthetic-settings-event",
        now,
        now,
        "synthetic-account",
        folder="Inbox",
        message_reference="synthetic-reference",
        sender=Address("advisor@example.test", "Advisor"),
        subject="Final exam timetable",
        importance=Importance.HIGH,
        is_read=False,
        has_attachments=False,
    )


class NotificationSettingsHarness(App[None]):
    def __init__(self, screen: NotificationSettingsScreen) -> None:
        super().__init__()
        self.settings_screen = screen

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(self.settings_screen)


async def _settle_configuration_read(pilot, screen):
    for _ in rounds(500):
        if not screen._operation_active and screen._pending_configuration_action is None:
            await pilot.pause()
            return
        await pilot.pause(0.02)
    raise AssertionError("Notification metadata did not finish before the next action")


async def _activate_tab_with_keyboard(pilot, screen, target):
    tabs = screen.query_one("#notification-tabs", TabbedContent)
    bar = tabs.query_one(Tabs)
    bar.focus()
    await pilot.pause()
    for _ in range(12):
        if tabs.active == target and screen.app.focused is bar:
            return
        await pilot.press("right")
        await pilot.pause()
    raise AssertionError(f"Keyboard tab activation did not reach {target}")


@pytest.mark.asyncio
async def test_screen_loads_every_configuration_surface_and_redacts_export(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service, simulation_events=(_event(),))
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(160, 48)) as pilot:

        async def acknowledged(condition, phase):
            for _ in rounds(500):
                if condition():
                    return
                await pilot.pause(0.02)
            raise AssertionError(f"Notification workflow did not acknowledge {phase}")

        await acknowledged(
            lambda: (
                screen._profile() is not None
                and not screen._operation_active
                and screen.query_one("#notification-rules-table", DataTable).row_count == 1
            ),
            "loaded configuration",
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.query_one("#notification-rules-table", DataTable).row_count == 1
        assert screen.query_one("#notification-quiet-table", DataTable).row_count == 1
        assert screen.query_one("#notification-digest-table", DataTable).row_count == 1
        assert screen.query_one("#notification-provider-table", DataTable).row_count == 1
        assert screen.query_one("#notification-routing-table", DataTable).row_count == 1
        assert "incremental sync" in str(screen.query_one("#notification-overview", Static).content)

        await _activate_tab_with_keyboard(pilot, screen, "notification-transfer-tab")
        choice = screen.query_one("#notification-transfer-kind", Select)
        await acknowledged(lambda: bool(choice.query("#label")), "mounted transfer choice")
        choice.value = "providers"
        export = screen.query_one("#notification-export", Button)
        export.scroll_visible(animate=False)
        export.focus()
        await pilot.pause()
        await acknowledged(
            lambda: not export.disabled and export.is_on_screen and app.focused is export,
            "export focus",
        )
        await pilot.press("enter")
        await acknowledged(
            lambda: (
                "desktop-main" in screen.query_one("#notification-transfer-payload", TextArea).text
            ),
            "redacted export payload",
        )
        payload = screen.query_one("#notification-transfer-payload", TextArea).text
        assert "desktop-main" in payload
        assert "keyring://notification/desktop-main" not in payload
        assert '"secret_ref": null' in payload

        await _activate_tab_with_keyboard(pilot, screen, "notification-diagnostics-tab")
        run = screen.query_one("#notification-diagnostics-run", Button)
        run.scroll_visible(animate=False)
        run.focus()
        await pilot.pause()
        await acknowledged(
            lambda: not run.disabled and run.is_on_screen and app.focused is run,
            "diagnostic focus",
        )
        await pilot.press("enter")
        await acknowledged(
            lambda: (
                "LOCAL-ONLY DIAGNOSTICS"
                in str(screen.query_one("#notification-diagnostics", Static).content)
            ),
            "local diagnostic report",
        )
        diagnostic = str(screen.query_one("#notification-diagnostics", Static).content)
        assert "LOCAL-ONLY DIAGNOSTICS" in diagnostic
        assert "keyring://" not in diagnostic


@pytest.mark.asyncio
async def test_rule_simulation_and_routing_matrix_mutate_only_local_state(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service, simulation_events=(_event(),))
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(150, 46)) as pilot:

        async def acknowledged(condition, phase):
            for _ in rounds(500):
                if condition():
                    return
                await pilot.pause(0.02)
            raise AssertionError(f"Notification workflow did not acknowledge {phase}")

        await acknowledged(
            lambda: (
                screen._profile() is not None
                and screen.query_one("#notification-routing-table", DataTable).row_count > 0
                and not screen._operation_active
            ),
            "loaded profile and routing rows",
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        await _activate_tab_with_keyboard(pilot, screen, "notification-rules-tab")
        screen._selected_rule_id = "exams"
        screen._selected_route = ("exams", "desktop-main")
        simulate = screen.query_one("#notification-rule-simulate", Button)
        await acknowledged(
            lambda: not simulate.disabled and simulate.is_on_screen, "simulation control"
        )
        simulate.focus()
        await pilot.pause()
        await pilot.press("enter")
        await acknowledged(
            lambda: "DRY RUN" in str(screen.query_one("#notification-rule-detail", Static).content),
            "simulation report",
        )
        detail = str(screen.query_one("#notification-rule-detail", Static).content)
        assert "DRY RUN" in detail
        assert "Planned notification events: 1" in detail

        await _activate_tab_with_keyboard(pilot, screen, "notification-routing-tab")
        screen._selected_route = ("exams", "desktop-main")
        cycle = screen.query_one("#notification-route-cycle", Button)
        cycle.scroll_visible(animate=False)
        cycle.focus()
        await pilot.pause()
        await acknowledged(
            lambda: not cycle.disabled and cycle.is_on_screen and app.focused is cycle,
            "focused route control",
        )
        await pilot.press("enter")
        await acknowledged(
            lambda: (
                service.profiles.get_profile("campus").routing_matrix.mode_for(
                    "exams", "desktop-main"
                )
                is RouteMode.DIGEST
            ),
            "persisted exact route",
        )
        updated = service.profiles.get_profile("campus")
        assert updated is not None
        assert updated.routing_matrix.mode_for("exams", "desktop-main") is RouteMode.DIGEST


@pytest.mark.asyncio
async def test_profile_deletion_requires_explicit_confirmation(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(140, 42)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        screen.query_one("#notification-profile-delete", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, NotificationConfirmationScreen)
        assert service.profiles.get_profile("campus") is not None

        await pilot.press("n")
        await pilot.pause()
        assert service.profiles.get_profile("campus") is not None

        screen.query_one("#notification-profile-delete", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, NotificationConfirmationScreen)
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert service.profiles.get_profile("campus") is None


@pytest.mark.asyncio
async def test_palette_rule_actions_prefill_supported_monitoring_predicates(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    expected = (
        ("sender", "advisor@example.test", "text", "sender"),
        ("subject", "Final exam", "text", "subject"),
        ("importance", None, "choice", "importance"),
        ("has_attachments", None, "boolean", "has_attachments"),
    )
    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        for preset, value, condition_type, field in expected:
            screen.action_create_rule(preset, value)
            await _settle_configuration_read(pilot, screen)
            await pilot.pause()
            assert isinstance(app.screen, RuleEditorScreen)
            editor = app.screen
            condition = json.loads(editor.query_one("#notification-rule-condition", TextArea).text)
            assert condition["type"] == condition_type
            assert condition["field"] == field
            assert editor.query_one("#notification-rule-id", Input).value.startswith("monitor-")
            assert editor.query_one("#notification-rule-name", Input).value
            editor.dismiss(None)
            await pilot.pause()


@pytest.mark.asyncio
async def test_rule_create_edit_and_confirmed_delete_persist_through_the_screen(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )

        screen.action_create_rule()
        await _settle_configuration_read(pilot, screen)
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, RuleEditorScreen) and app.screen.is_mounted,
            interval=0.025,
            pause_first=True,
        )
        assert isinstance(app.screen, RuleEditorScreen)
        editor = app.screen
        editor.query_one("#notification-rule-id", Input).value = "student-affairs"
        editor.query_one("#notification-rule-name", Input).value = "Student Affairs alerts"
        editor.query_one("#notification-rule-enabled", Checkbox).value = True
        editor.query_one("#notification-rule-save", Button).press()
        for _ in rounds(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, NotificationSettingsScreen):
                profile = service.profiles.get_profile("campus")
                if profile is not None and any(
                    item.rule_id == "student-affairs" for item in profile.rules
                ):
                    break

        created_profile = service.profiles.get_profile("campus")
        assert created_profile is not None
        created = next(item for item in created_profile.rules if item.rule_id == "student-affairs")
        assert created.name == "Student Affairs alerts"
        assert created.enabled

        # Persistence is visible before the worker publishes the refreshed UI snapshot.
        await _settle_configuration_read(pilot, screen)
        screen._selected_rule_id = created.rule_id
        screen.action_edit_rule()
        await _settle_configuration_read(pilot, screen)
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, RuleEditorScreen) and app.screen.is_mounted,
            interval=0.025,
            pause_first=True,
        )
        assert isinstance(app.screen, RuleEditorScreen)
        editor = app.screen
        assert editor.query_one("#notification-rule-id", Input).disabled
        editor.query_one("#notification-rule-name", Input).value = "Student Affairs priority"
        editor.query_one("#notification-rule-save", Button).press()
        for _ in rounds(40):
            await pilot.pause(0.025)
            if isinstance(app.screen, NotificationSettingsScreen):
                profile = service.profiles.get_profile("campus")
                updated = (
                    next(
                        (item for item in profile.rules if item.rule_id == "student-affairs"),
                        None,
                    )
                    if profile is not None
                    else None
                )
                if updated is not None and updated.name == "Student Affairs priority":
                    break

        edited_profile = service.profiles.get_profile("campus")
        assert edited_profile is not None
        edited = next(item for item in edited_profile.rules if item.rule_id == "student-affairs")
        assert edited.name == "Student Affairs priority"

        await _settle_configuration_read(pilot, screen)
        screen._selected_rule_id = edited.rule_id
        screen.action_delete_rule()
        await _settle_configuration_read(pilot, screen)
        await wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, NotificationConfirmationScreen)
                and app.screen.is_mounted
            ),
            interval=0.025,
            pause_first=True,
        )
        assert isinstance(app.screen, NotificationConfirmationScreen)
        assert any(item.rule_id == "student-affairs" for item in edited_profile.rules)
        app.screen.query_one("#operational-confirm-yes", Button).press()
        for _ in rounds(40):
            await pilot.pause(0.025)
            profile = service.profiles.get_profile("campus")
            if profile is not None and all(
                item.rule_id != "student-affairs" for item in profile.rules
            ):
                break
        final_profile = service.profiles.get_profile("campus")
        assert final_profile is not None
        assert all(item.rule_id != "student-affairs" for item in final_profile.rules)


@pytest.mark.asyncio
async def test_monitor_folder_palette_action_persists_the_active_watcher_scope(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        screen.action_configure_folder_scope("add", "Inbox/Student Affairs")
        await _settle_configuration_read(pilot, screen)
        updated = service.profiles.active_profile()
        assert updated is not None
        assert updated.folders == ("Inbox", "Inbox/Courses", "Inbox/Student Affairs")

        screen.action_configure_folder_scope("inbox")
        await _settle_configuration_read(pilot, screen)
        updated = service.profiles.active_profile()
        assert updated is not None
        assert updated.folders == ("Inbox",)


@pytest.mark.asyncio
async def test_notification_palette_actions_mutate_the_consumed_profile(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    profile = service.profiles.get_profile("campus")
    assert profile is not None
    service.replace_quiet_hours(
        profile.profile_id,
        QuietHoursSchedule(
            tuple(replace(item, enabled=False) for item in profile.quiet_hours.windows)
        ),
    )
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )

        screen.action_set_desktop_notifications(False)
        await _settle_configuration_read(pilot, screen)
        disabled = service.profiles.active_profile()
        assert disabled is not None
        assert all(not item.enabled for item in disabled.providers)
        assert service.providers.get_provider("desktop-main").enabled is False  # type: ignore[union-attr]

        screen.action_set_desktop_notifications(True)
        await _settle_configuration_read(pilot, screen)
        enabled = service.profiles.active_profile()
        assert enabled is not None
        assert any(item.enabled for item in enabled.providers)
        assert any(
            item.rule_id == "desktop-notifications-all-mail" and item.enabled
            for item in enabled.rules
        )

        for mode in ("private", "sender_only", "full"):
            screen.action_set_notification_privacy(mode)
            await _settle_configuration_read(pilot, screen)
            updated = service.profiles.active_profile()
            assert updated is not None
            assert {item.privacy.mode.value for item in updated.providers} == {mode}

        screen.action_enable_quiet_hours()
        await _settle_configuration_read(pilot, screen)
        quiet = service.profiles.active_profile()
        assert quiet is not None
        assert all(item.enabled for item in quiet.quiet_hours.windows)

        screen.action_mute_notifications()
        await _settle_configuration_read(pilot, screen)
        muted = service.profiles.active_profile()
        assert muted is not None and muted.enabled
        assert all(not item.enabled for item in muted.providers)


def test_monitor_predicate_commands_are_local_offline_configuration() -> None:
    from gucmail.command_catalog import CORE_COMMANDS

    for command_id in (
        "monitoring.monitor_sender",
        "monitoring.monitor_subject",
        "monitoring.monitor_important_messages",
        "monitoring.monitor_messages_with_attachments",
    ):
        command = CORE_COMMANDS.get(command_id)
        assert command.changes_local
        assert not command.changes_server
        assert command.offline
        assert not command.requires_connection


@pytest.mark.asyncio
async def test_secure_webhook_setup_pause_resume_and_remove_are_real_local_lifecycle(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(
        StateStore(tmp_path / "state.db", account_key="settings-test"),
        secret_store=secrets,
    )
    screen = NotificationSettingsScreen(service, webhook_registry=registry)
    app = NotificationSettingsHarness(screen)
    destination = "https://discord.com/api/webhooks/" + "12345/" + ("x" * 20)

    async with app.run_test(size=(160, 48)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        screen.action_configure_webhook("discord")
        await _settle_configuration_read(pilot, screen)
        await pilot.pause()
        assert isinstance(app.screen, WebhookSetupScreen)
        editor = app.screen
        assert editor.query_one("#webhook-url", Input).password
        assert editor.query_one("#webhook-hmac-secret", Input).password
        editor.query_one("#webhook-url", Input).value = destination
        editor.query_one("#webhook-hmac-secret", Input).value = "synthetic-signing-secret"
        editor.query_one("#webhook-setup-save", Button).press()
        await pilot.pause()
        await _settle_configuration_read(pilot, screen)

        stored = registry.get("discord-main")
        assert stored is not None
        assert stored.destination_is_reference
        assert not stored.enabled
        assert secrets.get(stored.url) == destination
        assert stored.secret_keyring_name is not None
        assert secrets.get(stored.secret_keyring_name) == "synthetic-signing-secret"
        metadata = service.providers.get_provider("discord-main")
        assert metadata is not None
        assert metadata.kind == "discord"
        assert metadata.secret_ref == stored.url
        profile = service.profiles.get_profile("campus")
        assert profile is not None
        assert (
            next(item for item in profile.providers if item.provider_id == "discord-main").enabled
            is False
        )

        screen.action_resume_webhook()
        await _settle_configuration_read(pilot, screen)
        await pilot.pause()
        assert isinstance(app.screen, NotificationConfirmationScreen)
        await pilot.press("y")
        await pilot.pause()
        await _settle_configuration_read(pilot, screen)
        assert registry.get("discord-main").enabled  # type: ignore[union-attr]
        assert service.providers.get_provider("discord-main").enabled  # type: ignore[union-attr]

        screen.action_pause_webhook()
        await _settle_configuration_read(pilot, screen)
        await pilot.pause()
        assert registry.get("discord-main").enabled is False  # type: ignore[union-attr]

        screen.action_remove_webhook()
        await _settle_configuration_read(pilot, screen)
        await pilot.pause()
        assert isinstance(app.screen, NotificationConfirmationScreen)
        await pilot.press("y")
        await pilot.pause()
        await _settle_configuration_read(pilot, screen)
        assert registry.get("discord-main") is None
        assert service.providers.get_provider("discord-main") is None
        profile = service.profiles.get_profile("campus")
        assert profile is not None
        assert all(item.provider_id != "discord-main" for item in profile.providers)


@pytest.mark.asyncio
async def test_webhook_provider_variants_persist_disabled_without_network_traffic(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(
        StateStore(tmp_path / "state.db", account_key="settings-test"),
        secret_store=secrets,
    )
    screen = NotificationSettingsScreen(service, webhook_registry=registry)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(160, 48)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        for kind in ("webhook", "slack", "teams"):
            screen.action_configure_webhook(kind)
            await _settle_configuration_read(pilot, screen)
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, WebhookSetupScreen) and app.screen.is_mounted,
                interval=0.025,
                pause_first=True,
            )
            assert isinstance(app.screen, WebhookSetupScreen)
            editor = app.screen
            editor.query_one("#webhook-url", Input).value = f"https://hooks.example.test/{kind}"
            editor.query_one("#webhook-setup-save", Button).press()
            for _ in rounds(40):
                await pilot.pause(0.025)
                metadata = service.providers.get_provider(f"{kind}-main")
                if metadata is not None:
                    break
            metadata = service.providers.get_provider(f"{kind}-main")
            assert metadata is not None
            assert metadata.kind == kind
            stored = registry.get(f"{kind}-main")
            assert stored is not None and not stored.enabled
            assert stored.destination_is_reference


@pytest.mark.asyncio
async def test_confirmed_webhook_test_uses_only_synthetic_data_and_exact_provider(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(
        StateStore(tmp_path / "state.db", account_key="settings-test"),
        secret_store=secrets,
    )
    registry.add(
        WebhookConfig(
            "example",
            "https://hooks.example.test/synthetic",
            enabled=False,
        )
    )
    discord = registry.add(
        WebhookConfig(
            "discord-synthetic",
            "https://discord.com/api/webhooks/12345/" + ("x" * 20),
            enabled=False,
        )
    )
    service.providers.save_provider(
        ProviderConfigurationMetadata(
            discord.name,
            "discord",
            "Discord incoming webhook",
            False,
            discord.url,
        )
    )
    deliveries: list[tuple[str, str, str]] = []

    def deliver(config: WebhookConfig, event: NotificationEvent) -> DeliveryResult:
        assert event.account_id == "synthetic"
        assert event.preview == "No mailbox data was used."
        event_id = event.event_id
        deliveries.append((config.name, event_id, str(event.subject)))
        return DeliveryResult(
            event_id,
            f"webhook:{config.name}",
            DeliveryState.DELIVERED,
            "Synthetic endpoint accepted the event.",
        )

    screen = NotificationSettingsScreen(
        service,
        webhook_registry=registry,
        webhook_delivery=deliver,
    )
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        screen.action_show_webhook_configuration("example")
        await _settle_configuration_read(pilot, screen)
        assert screen._selected_webhook_name == "example"
        screen.action_test_webhook("example", confirmed=True)
        await _settle_configuration_read(pilot, screen)
        await wait_until(
            pilot,
            lambda: deliveries and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )

        assert len(deliveries) == 1
        assert deliveries[0][0] == "example"
        assert deliveries[0][1].startswith("test-webhook-")
        assert deliveries[0][2] == "Synthetic webhook test"

        screen.action_test_webhook(provider_kind="discord", confirmed=True)
        await _settle_configuration_read(pilot, screen)
        await wait_until(
            pilot,
            lambda: len(deliveries) == 2 and not screen._operation_active,
            interval=0.025,
            pause_first=True,
        )
        assert deliveries[1][0] == "discord-synthetic"


@pytest.mark.asyncio
async def test_editors_validate_nested_rules_privacy_and_quiet_hours(tmp_path: Path) -> None:
    del tmp_path

    class EditorHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield Static("host")

    app = EditorHarness()
    async with app.run_test(size=(140, 44)) as pilot:
        rule_editor = RuleEditorScreen(None, next_order=3, provider_id="desktop-main")
        app.push_screen(rule_editor)
        await pilot.pause()
        rule_editor.query_one("#notification-rule-id", Input).value = "nested"
        rule_editor.query_one("#notification-rule-name", Input).value = "Nested"
        rule_editor.query_one("#notification-rule-template-nested", Button).press()
        await pilot.pause()
        rule = rule_editor.build_value()
        assert isinstance(rule.condition, AllCondition)
        assert rule.enabled is False
        assert rule.order == 3
        app.pop_screen()
        await pilot.pause()

        provider_editor = ProviderEditorScreen(None, None, digest_ids=("morning",))
        app.push_screen(provider_editor)
        await pilot.pause()
        provider_editor.query_one("#notification-provider-id", Input).value = "discord-main"
        provider_editor.query_one("#notification-provider-name", Input).value = "Discord"
        provider_editor.query_one("#notification-provider-kind", Select).value = "discord"
        provider_editor.query_one("#notification-provider-privacy", Select).value = "private"
        provider_editor.query_one("#notification-provider-enabled", Checkbox).value = True
        provider = provider_editor.build_value()
        assert provider.policy.privacy.mode is PrivacyMode.PRIVATE
        assert provider.metadata.secret_ref is None
        app.pop_screen()
        await pilot.pause()

        quiet_editor = QuietHoursEditorScreen(None)
        app.push_screen(quiet_editor)
        await pilot.pause()
        quiet_editor.query_one("#notification-quiet-id", Input).value = "weeknights"
        quiet_editor.query_one("#notification-quiet-weekdays", Input).value = "Mon, Wed, 4"
        quiet = quiet_editor.build_value()
        assert quiet.weekdays == frozenset({0, 2, 4})
        assert quiet.release_at(datetime(2026, 8, 14, 22, 30, tzinfo=UTC)) is not None


def test_provider_import_preview_rejects_secret_shaped_or_wrong_documents(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.db")
    exported = service.providers.export_document(include_secret_references=False)
    parsed = json.loads(exported)
    assert parsed["providers"][0]["secret_ref"] is None
    assert "keyring://" not in exported
    assert preview_provider_metadata_document(exported)[0].provider_id == "desktop-main"

    parsed["providers"][0]["secret_ref"] = "keyring://stale/secret"
    with pytest.raises(ValueError, match="cannot contain secret references"):
        preview_provider_metadata_document(json.dumps(parsed))


@pytest.mark.asyncio
async def test_import_must_be_repreviewed_after_payload_changes(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.db")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)

    async with app.run_test(size=(150, 44)) as pilot:
        await wait_until(
            pilot, lambda: screen._profile() is not None, interval=0.025, pause_first=True
        )
        screen.query_one("#notification-tabs", TabbedContent).active = "notification-transfer-tab"
        await pilot.pause()
        screen.query_one("#notification-transfer-kind", Select).value = "profiles"
        screen._export()
        await _settle_configuration_read(pilot, screen)
        screen._preview_import()
        assert screen._import_preview_valid is True

        editor = screen.query_one("#notification-transfer-payload", TextArea)
        editor.load_text(editor.text + " ")
        screen._apply_import()
        assert screen._import_preview_valid is False
        assert "changed after preview" in str(screen.query_one("#area-state", Static).content)
