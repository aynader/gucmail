from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from gucmail.models import Address, Importance
from gucmail.notification_events import (
    EventDisclosurePolicy,
    NewMailEvent,
    PrivacyMode,
)
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
    NotificationRule,
    RuleAction,
    RuleActionKind,
    TextCondition,
    TextField,
    TextOperator,
)
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    PersistentMonitoringProfileRepository,
    PersistentNotificationRuleRepository,
    PersistentProviderMetadataRepository,
    ProviderConfigurationMetadata,
)
from gucmail.state import StateStore


def _rule(
    rule_id: str = "urgent-exam",
    *,
    order: int = 0,
    enabled: bool = True,
) -> NotificationRule:
    return NotificationRule(
        rule_id,
        f"Rule {rule_id}",
        TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("exam",)),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),),
        enabled=enabled,
        priority=10,
        order=order,
    )


def _profile(
    profile_id: str = "campus",
    *,
    name: str | None = None,
    enabled: bool = True,
) -> MonitoringProfile:
    rule = _rule()
    provider = ProviderPolicy(
        "desktop",
        True,
        EventDisclosurePolicy(
            PrivacyMode.SENDER_AND_SUBJECT,
            redact_addresses=False,
        ),
        maximum_notifications_per_burst=3,
    )
    quiet = QuietHoursWindow(
        "night",
        time(22, 15, 30),
        time(7),
        "UTC",
        frozenset({0, 1, 2, 3, 4}),
        QuietHoursBehavior.QUEUE,
        frozenset({Importance.HIGH}),
        frozenset({"advisor@example.test"}),
        frozenset({"dept.example.test"}),
        frozenset({"urgent-exam"}),
    )
    digest = DigestSchedule(
        "morning",
        "Morning digest",
        DigestCadence.MORNING,
        "UTC",
    )
    return MonitoringProfile(
        profile_id,
        name or f"Profile {profile_id}",
        ("Inbox", "Inbox/Course"),
        (rule,),
        (provider,),
        QuietHoursSchedule((quiet,)),
        (digest,),
        RoutingMatrix(
            ("desktop",),
            (RoutingCell("urgent-exam", "desktop", RouteMode.IMMEDIATE),),
        ),
        60,
        enabled,
        False,
    )


def _event() -> NewMailEvent:
    moment = datetime(2026, 8, 3, 10, tzinfo=UTC)
    return NewMailEvent(
        "synthetic-event",
        moment,
        moment,
        "synthetic-account",
        folder="Inbox",
        message_reference="m-synthetic",
        sender=Address("advisor@example.test", "Advisor"),
        subject="Final exam schedule",
        importance=Importance.HIGH,
        is_read=False,
        has_attachments=False,
    )


def test_rule_repository_is_durable_account_scoped_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = PersistentNotificationRuleRepository(
        StateStore(path, account_key="First@Example.Test"),
        "course",
    )
    second = PersistentNotificationRuleRepository(
        StateStore(path, account_key="second@example.test"),
        "course",
    )
    first.save_rule(_rule("one", order=0, enabled=False))
    first.save_rule(_rule("two", order=1))

    assert [item.rule_id for item in first.list_rules()] == ["one", "two"]
    assert second.list_rules() == ()
    assert first.set_enabled("ONE", enabled=True).enabled is True
    assert [item.order for item in first.reorder(("two", "one"))] == [0, 1]
    before = first.list_rules()
    with pytest.raises(ValueError, match="every rule"):
        first.reorder(("one",))
    assert first.list_rules() == before

    duplicate = first.duplicate_rule("one", new_id="copy", new_name="Copy")
    assert duplicate.enabled is False
    assert first.delete_rule("COPY") is True
    assert first.delete_rule("copy") is False


def test_rule_import_export_and_dry_run_use_persisted_rules(tmp_path: Path) -> None:
    source = PersistentNotificationRuleRepository(
        StateStore(tmp_path / "source.db", account_key="account"),
        "default",
    )
    source.save_rule(_rule())
    document = source.export_document()
    target = PersistentNotificationRuleRepository(
        StateStore(tmp_path / "target.db", account_key="account"),
        "default",
    )
    assert target.import_document(document) == (_rule(),)
    with pytest.raises(ValueError, match="overwrite"):
        target.import_document(document)
    result = target.simulate((_event(),))
    assert result.matching_events == 1
    assert result.provider_counts == {"desktop": 1}


def test_rule_malformed_row_is_isolated_with_safe_diagnostic(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    repository = PersistentNotificationRuleRepository(store, "default")
    repository.save_rule(_rule())
    with store.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE notification_rule_metadata SET definition_json='not-json'
            WHERE account_key=?
            """,
            (store.account_key,),
        )

    snapshot = repository.inspect()
    assert snapshot.items == ()
    assert len(snapshot.diagnostics) == 1
    assert snapshot.diagnostics[0].component == "notification rule"
    assert "not-json" not in snapshot.diagnostics[0].message


def test_profile_round_trip_uses_all_v5_projection_tables(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="Account@Example.Test")
    repository = PersistentMonitoringProfileRepository(store)
    expected = _profile("Campus")
    repository.save_profile(expected)

    assert repository.get_profile("campus") == expected
    assert repository.inspect().diagnostics == ()
    counts: dict[str, int] = {}
    with store.connection() as connection:
        for table in (
            "notification_profiles",
            "notification_rule_metadata",
            "notification_quiet_hours",
            "notification_digest_schedules",
            "notification_routes",
        ):
            counts[table] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_key=?",  # noqa: S608
                    (store.account_key,),
                ).fetchone()[0]
            )
    assert counts == {
        "notification_profiles": 1,
        "notification_rule_metadata": 1,
        "notification_quiet_hours": 5,
        "notification_digest_schedules": 1,
        "notification_routes": 1,
    }


def test_profiles_can_reuse_child_ids_and_activation_is_a_separate_pointer(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    repository = PersistentMonitoringProfileRepository(store)
    enabled = _profile("first", name="First", enabled=True)
    disabled = _profile("second", name="Second", enabled=False)
    repository.save_profile(enabled)
    repository.save_profile(disabled)

    assert len(repository.list_profiles()) == 2
    assert repository.activate("SECOND") == disabled
    assert repository.active_profile() == disabled
    assert repository.get_profile("first") == enabled
    assert repository.set_enabled("second", enabled=True).enabled is True
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_rule_metadata WHERE account_key=?",
                (store.account_key,),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_digest_schedules WHERE account_key=?",
                (store.account_key,),
            ).fetchone()[0]
            == 2
        )

    assert repository.delete_profile("second") is True
    assert repository.active_profile() is None
    assert repository.get_profile("first") == enabled


def test_profile_export_import_and_account_isolation(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = PersistentMonitoringProfileRepository(StateStore(path, account_key="one"))
    second = PersistentMonitoringProfileRepository(StateStore(path, account_key="two"))
    first.save_profile(_profile())
    assert second.list_profiles() == ()
    second.import_document(first.export_document())
    assert second.get_profile("campus") == _profile()
    with pytest.raises(ValueError, match="overwrite"):
        second.import_document(first.export_document())

    replacement = _profile("replacement", name="Replacement")
    temporary = PersistentMonitoringProfileRepository(
        StateStore(tmp_path / "replacement.db", account_key="one")
    )
    temporary.save_profile(replacement)
    second.import_document(temporary.export_document(), replace_existing=True)
    assert second.list_profiles() == (replacement,)


def test_profile_malformed_rows_are_isolated_without_hiding_valid_profiles(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    repository = PersistentMonitoringProfileRepository(store)
    repository.save_profile(_profile("valid", name="Valid"))
    repository.save_profile(_profile("broken", name="Broken"))
    with store.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE notification_profiles SET metadata_json='{"url":"https://secret.test"}'
            WHERE account_key=? AND profile_id='broken'
            """,
            (store.account_key,),
        )

    snapshot = repository.inspect()
    assert [profile.profile_id for profile in snapshot.items] == ["valid"]
    assert any(item.component == "notification profile" for item in snapshot.diagnostics)
    assert all("https://secret.test" not in item.message for item in snapshot.diagnostics)


def test_service_exposes_watcher_settings_mutators_and_pure_event_testing(
    tmp_path: Path,
) -> None:
    service = NotificationConfigurationService(
        StateStore(tmp_path / "state.db", account_key="account")
    )
    service.profiles.save_profile(_profile())
    service.profiles.activate("campus")
    service.profiles.set_strategy_preference("campus", "bounded_poll")

    watcher = service.watcher_configuration()
    assert watcher is not None
    assert watcher.profile_id == "campus"
    assert watcher.poll_interval_seconds == 60
    assert watcher.folders == ("Inbox", "Inbox/Course")
    assert watcher.strategy_preference == "bounded_poll"

    service.replace_quiet_hours("campus", QuietHoursSchedule())
    assert service.profiles.get_profile("campus").quiet_hours == QuietHoursSchedule()  # type: ignore[union-attr]
    simulation = service.simulate("campus", (_event(),))
    assert simulation.matching_events == 1
    test = service.test_event("campus", _event())
    assert test.decision.matched_rule_ids == ("urgent-exam",)
    assert test.routing.immediate_providers == ("desktop",)


def test_profile_updates_preserve_digest_runtime_and_strategy_projection(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    service = NotificationConfigurationService(store)
    service.profiles.save_profile(_profile())
    service.profiles.set_strategy_preference("campus", "incremental_sync")
    with store.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE notification_digest_schedules
            SET last_emitted_at='2026-08-01T00:00:00+00:00',
                next_due_at='2026-08-02T00:00:00+00:00'
            WHERE account_key=?
            """,
            (store.account_key,),
        )

    service.replace_quiet_hours("campus", QuietHoursSchedule())

    assert service.profiles.strategy_preference("campus") == "incremental_sync"
    with store.connection() as connection:
        row = connection.execute(
            """
            SELECT last_emitted_at, next_due_at FROM notification_digest_schedules
            WHERE account_key=?
            """,
            (store.account_key,),
        ).fetchone()
    assert tuple(row) == (
        "2026-08-01T00:00:00+00:00",
        "2026-08-02T00:00:00+00:00",
    )


def test_deleting_profile_rule_prunes_its_canonical_routes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    profiles = PersistentMonitoringProfileRepository(store)
    profiles.save_profile(_profile())
    rules = PersistentNotificationRuleRepository(store, "campus")

    assert rules.delete_rule("urgent-exam") is True
    updated = profiles.get_profile("campus")
    assert updated is not None
    assert updated.rules == ()
    assert updated.routing_matrix.cells == ()
    assert profiles.inspect().diagnostics == ()
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_routes WHERE account_key=?",
                (store.account_key,),
            ).fetchone()[0]
            == 0
        )


def test_provider_metadata_is_durable_account_scoped_and_secret_safe(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = PersistentProviderMetadataRepository(StateStore(path, account_key="one"))
    second = PersistentProviderMetadataRepository(StateStore(path, account_key="two"))
    provider = ProviderConfigurationMetadata(
        "desktop-main",
        "desktop",
        "Windows desktop",
        False,
        "keyring://gucmail/provider/desktop-main",
        {"privacy_mode": "private", "sound_enabled": False},
    )
    first.save_provider(provider)
    assert first.get_provider("DESKTOP-MAIN") == provider
    assert second.list_providers() == ()
    assert first.set_enabled("desktop-main", enabled=True).enabled is True

    redacted_export = json.loads(first.export_document())
    assert redacted_export["providers"][0]["secret_ref"] is None
    portable_export = first.export_document(include_secret_references=True)
    second.import_document(portable_export)
    assert second.get_provider("desktop-main").secret_ref == provider.secret_ref  # type: ignore[union-attr]


def test_provider_metadata_rejects_urls_tokens_and_plaintext_secret_references() -> None:
    with pytest.raises(ValueError, match="URLs"):
        ProviderConfigurationMetadata(
            "webhook",
            "generic",
            "Webhook",
            metadata={"target": "https://x.test"},
        )
    with pytest.raises(ValueError, match="secret-bearing"):
        ProviderConfigurationMetadata(
            "webhook",
            "generic",
            "Webhook",
            metadata={"api_token": "plaintext"},
        )
    with pytest.raises(ValueError, match="secret-bearing"):
        ProviderConfigurationMetadata(
            "webhook",
            "generic",
            "Webhook",
            metadata={"api_token_ref": "plaintext"},
        )
    with pytest.raises(ValueError, match="authorization tokens"):
        ProviderConfigurationMetadata(
            "webhook",
            "generic",
            "Webhook",
            metadata={"value": "Bearer plaintext-token-value"},
        )
    with pytest.raises(ValueError, match="keyring"):
        ProviderConfigurationMetadata(
            "webhook",
            "generic",
            "Webhook",
            secret_ref="plaintext-secret",
        )


def test_rule_and_profile_security_rejection_rolls_back_cleanly(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    rules = PersistentNotificationRuleRepository(store, "default")
    unsafe = replace(
        _rule(),
        condition=TextCondition(
            TextField.SUBJECT,
            TextOperator.CONTAINS,
            ("https://tracking.example.test",),
        ),
    )
    with pytest.raises(ValueError, match="URLs"):
        rules.save_rule(unsafe)
    assert rules.list_rules() == ()

    profiles = PersistentMonitoringProfileRepository(store)
    with pytest.raises(ValueError, match="URLs"):
        profiles.save_profile(replace(_profile(), rules=(unsafe,)))
    assert profiles.list_profiles() == ()


def test_malformed_provider_row_does_not_hide_valid_provider(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="account")
    repository = PersistentProviderMetadataRepository(store)
    repository.save_provider(
        ProviderConfigurationMetadata("valid", "console", "Valid", metadata={"format": "text"})
    )
    repository.save_provider(ProviderConfigurationMetadata("broken", "console", "Broken"))
    with store.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE notification_provider_configs SET secret_ref='plaintext'
            WHERE account_key=? AND provider_id='broken'
            """,
            (store.account_key,),
        )

    snapshot = repository.inspect()
    assert [provider.provider_id for provider in snapshot.items] == ["valid"]
    assert len(snapshot.diagnostics) == 1
    assert "plaintext" not in snapshot.diagnostics[0].message


def test_palette_notification_settings_persist_to_the_runtime_profile(tmp_path: Path) -> None:
    service = NotificationConfigurationService(
        StateStore(tmp_path / "state.db", account_key="notification-palette")
    )

    enabled = service.set_desktop_notifications(enabled=True)
    assert service.profiles.active_profile() == enabled
    metadata = service.providers.get_provider("desktop")
    assert metadata is not None and metadata.enabled and metadata.kind == "desktop"
    desktop = next(item for item in enabled.providers if item.provider_id == "desktop")
    assert desktop.enabled
    assert desktop.privacy.mode is PrivacyMode.SENDER_AND_SUBJECT
    test = service.test_event(enabled.profile_id, _event())
    assert test.decision.immediate_providers == ("desktop",)

    private = service.set_notification_privacy(PrivacyMode.SENDER_ONLY)
    assert {item.privacy.mode for item in private.providers} == {PrivacyMode.SENDER_ONLY}

    quiet = service.enable_quiet_hours()
    assert quiet.quiet_hours.windows
    assert all(item.enabled for item in quiet.quiet_hours.windows)

    muted = service.mute_notifications()
    assert muted.enabled == enabled.enabled
    assert all(not item.enabled for item in muted.providers)

    disabled = service.set_desktop_notifications(enabled=False)
    assert all(not item.enabled for item in disabled.providers)
    managed = next(
        item for item in disabled.rules if item.rule_id == "desktop-notifications-all-mail"
    )
    assert not managed.enabled
    assert service.providers.get_provider("desktop").enabled is False  # type: ignore[union-attr]
