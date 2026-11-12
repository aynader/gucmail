from __future__ import annotations

from dataclasses import replace
from datetime import time
from pathlib import Path

import pytest

from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.notification_events import PrivacyMode
from gucmail.notification_policies import (
    DigestCadence,
    DigestSchedule,
    QuietHoursSchedule,
    QuietHoursWindow,
)
from gucmail.services.monitoring import MailboxMonitor, MonitorStrategy
from gucmail.services.monitoring_preferences import (
    configured_watcher,
    effective_profile,
    validate_direct_preferences,
    watcher_settings,
)
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.notifications import DeliveryResult, DeliveryState
from gucmail.services.profiled_notifications import ProfiledNotificationDispatcher
from gucmail.state import StateStore
from test_profiled_notifications import _event, _profile
from test_settings_secondpass import _context


def test_watcher_settings_survive_reopen_without_overwriting_profile(tmp_path: Path) -> None:
    context = _context(tmp_path)
    service = NotificationConfigurationService(context.state)
    original = _profile()
    service.profiles.save_profile(original)
    watcher_settings(context).set("monitoring.profile", "study")
    settings = watcher_settings(context, "study")
    settings.set("monitoring.watched_folders", ["Inbox", "Projects"])
    settings.set("monitoring.strategy", "polling")
    # Reopen settings and state, as a new headless process would.
    context.state = StateStore(context.paths.state_database)
    resolved = configured_watcher(context)
    assert resolved is not None
    assert resolved.profile_id == "study"
    assert resolved.folders == ("Inbox", "Projects")
    assert resolved.strategy_preference == "bounded_poll"
    assert NotificationConfigurationService(context.state).profiles.get_profile("study") == original
    # Explicit CLI profile choice takes precedence over the preference.
    other = replace(original, profile_id="other", name="Other profile")
    service.profiles.save_profile(other)
    assert configured_watcher(context, "other").folders == ("Inbox",)


def test_privacy_override_applies_to_actual_dispatch_and_reloads(tmp_path: Path) -> None:
    context = _context(tmp_path)
    profile = _profile(privacy=PrivacyMode.FULL)
    captured = []

    class Provider:
        name = "capture"

        def notify(self, event):
            captured.append(event)
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    dispatcher = ProfiledNotificationDispatcher(
        profile,
        {"capture": Provider()},
        state=context.state,
        profile_loader=lambda: effective_profile(context, profile),
    )
    assert effective_profile(context, profile) == profile  # unset default does not override policy
    watcher_settings(context, "study").set("integrations.notification_privacy", "private")
    dispatcher.dispatch(_event("one"))
    assert captured[0].subject is None
    assert captured[0].sender is None
    watcher_settings(context, "study").set("integrations.notification_privacy", "sender_only")
    dispatcher.dispatch(_event("two"))
    assert captured[1].subject is None
    assert captured[1].sender is not None
    assert profile.providers[0].privacy.mode is PrivacyMode.FULL


def test_quiet_digest_controls_preserve_schedule_and_require_configuration(tmp_path: Path) -> None:
    context = _context(tmp_path)
    profile = _profile(
        quiet_hours=QuietHoursSchedule((QuietHoursWindow("night", time(22), time(7), "UTC"),)),
        digest_schedules=(
            DigestSchedule("daily", "Daily", DigestCadence.DAILY, "UTC", delivery_time=time(9)),
        ),
    )
    settings = watcher_settings(context, "study")
    settings.set("monitoring.quiet_hours_enabled", False)
    settings.set("monitoring.digest_enabled", False)
    disabled = effective_profile(context, profile)
    assert not disabled.quiet_hours.windows[0].enabled
    assert not disabled.digest_schedules[0].enabled
    settings.set("monitoring.quiet_hours_enabled", True)
    settings.set("monitoring.digest_enabled", True)
    assert effective_profile(context, profile) == profile
    with pytest.raises(ConfigurationError, match="quiet hours"):
        effective_profile(context, _profile())
    assert profile.quiet_hours.windows[0].enabled


def test_legacy_direct_dispatch_cannot_bypass_explicit_privacy(tmp_path: Path) -> None:
    context = _context(tmp_path)
    validate_direct_preferences(context)
    watcher_settings(context).set("integrations.notification_privacy", "private")
    with pytest.raises(ConfigurationError, match="configured monitoring profile"):
        validate_direct_preferences(context)


def test_forced_polling_does_not_reuse_an_opaque_incremental_checkpoint(tmp_path: Path) -> None:
    calls = []

    class Gateway:
        def poll_new_messages(self, folder, since, limit):
            calls.append(("incremental", since))
            return ([], "opaque-server-token")

        def list_messages(self, folder, **kwargs):
            calls.append(("list", kwargs["offset"]))
            return []

    gateway = Gateway()
    state = StateStore(tmp_path / "state.sqlite")
    automatic = MailboxMonitor(gateway, state, account_id="test")
    assert automatic.run_once().succeeded
    forced = MailboxMonitor(gateway, state, account_id="test", strategy_preference="bounded_poll")
    assert forced.run_once().succeeded
    assert forced.status().strategy is MonitorStrategy.POLLING
    assert automatic.run_once().succeeded
    assert calls == [("incremental", None), ("list", 0), ("incremental", "opaque-server-token")]


@pytest.mark.parametrize("strategy", ["ews_streaming", "ews_pull"])
def test_unimplemented_push_transport_fails_before_any_mailbox_request(
    tmp_path: Path, strategy: str
) -> None:
    with pytest.raises(UnsupportedFeatureError, match="no active"):
        MailboxMonitor(
            object(),
            StateStore(tmp_path / "unused"),
            account_id="test",
            strategy_preference=strategy,
        )
    assert not (tmp_path / "unused").exists()
