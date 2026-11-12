"""Apply explicit user preferences to durable watcher profiles without rewriting them."""

from __future__ import annotations

import platform
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from gucmail.errors import ConfigurationError
from gucmail.notification_events import PrivacyMode
from gucmail.notification_policies import MonitoringProfile, QuietHoursSchedule
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    WatcherProfileConfiguration,
)
from gucmail.settings import SettingsContext, SettingsService, ValueSource

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext


def watcher_settings(context: AppContext, profile: str = "default") -> SettingsService:
    return SettingsService(
        context.config_store,
        context=SettingsContext(
            account_id=context.config.email,
            machine_id=f"{sys.platform}:{platform.node()}",
            profile=profile,
        ),
    )


def explicit_preference(settings: SettingsService, key: str) -> Any:
    value = settings.value(key)
    return None if value.source is ValueSource.DEFAULT else value.effective_value


def configured_watcher(
    context: AppContext, profile: str | None = None
) -> WatcherProfileConfiguration | None:
    settings = watcher_settings(context)
    selected = profile or explicit_preference(settings, "monitoring.profile")
    service = NotificationConfigurationService(context.state)
    configured = service.watcher_configuration(selected)
    if configured is None and selected == "default":
        # "default" is the pre-filled name in Startup setup and CLI options. Unless a
        # profile is literally called "default", it means the active profile; otherwise
        # a background watcher would run with no providers and never notify.
        configured = service.watcher_configuration(None)
    if selected and selected != "default" and configured is None:
        raise ConfigurationError(
            "The selected monitoring profile does not exist. "
            "Configure it in Notifications & Monitoring."
        )
    settings = watcher_settings(context, configured.profile_id if configured else "default")
    folders = explicit_preference(settings, "monitoring.watched_folders")
    strategy = explicit_preference(settings, "monitoring.strategy")
    if configured is None and (folders is not None or strategy is not None):
        configured = WatcherProfileConfiguration(
            "default",
            (context.config.default_folder,),
            context.config.poll_interval_seconds,
            None,
            True,
            False,
        )
    if configured is not None:
        if folders is not None:
            if not folders or any(not str(folder).strip() for folder in folders):
                raise ConfigurationError("At least one nonempty monitored folder is required.")
            configured = replace(configured, folders=tuple(folders))
        if strategy is not None:
            configured = replace(
                configured,
                strategy_preference={
                    "automatic": None,
                    "polling": "bounded_poll",
                    "incremental": "incremental_poll",
                    "streaming": "ews_streaming",
                    "pull": "ews_pull",
                }[strategy],
            )
    return configured


def effective_profile(context: AppContext, profile: MonitoringProfile) -> MonitoringProfile:
    """Unset Settings values preserve profile policies, including privacy restrictions.

    Enabling a master control requires a real configured schedule. Disabling digest
    schedules suppresses their delivery; it never reroutes private mail immediately.
    """
    settings = watcher_settings(context, profile.profile_id)
    quiet = explicit_preference(settings, "monitoring.quiet_hours_enabled")
    digest = explicit_preference(settings, "monitoring.digest_enabled")
    privacy = explicit_preference(settings, "integrations.notification_privacy")
    desktop = explicit_preference(settings, "monitoring.desktop_notifications")
    if quiet is True and not profile.quiet_hours.windows:
        raise ConfigurationError(
            "Configure quiet hours in Notifications & Monitoring before enabling them."
        )
    if digest is True and not profile.digest_schedules:
        raise ConfigurationError(
            "Configure a digest schedule and routing in Notifications & Monitoring "
            "before enabling digests."
        )
    if quiet is not None:
        profile = replace(
            profile,
            quiet_hours=QuietHoursSchedule(
                tuple(replace(window, enabled=quiet) for window in profile.quiet_hours.windows)
            ),
        )
    if digest is not None:
        profile = replace(
            profile,
            digest_schedules=tuple(
                replace(schedule, enabled=digest) for schedule in profile.digest_schedules
            ),
        )
    policies = profile.providers
    if privacy is not None:
        policies = tuple(
            replace(policy, privacy=replace(policy.privacy, mode=PrivacyMode(privacy)))
            for policy in policies
        )
    if desktop is not None:
        desktop_ids = {
            item.provider_id.casefold()
            for item in NotificationConfigurationService(context.state).providers.list_providers()
            if item.kind.casefold() == "desktop" and item.enabled
        }
        policies = tuple(
            replace(policy, enabled=desktop)
            if policy.provider_id.casefold() in desktop_ids
            else policy
            for policy in policies
        )
        if desktop and not any(policy.provider_id.casefold() in desktop_ids for policy in policies):
            raise ConfigurationError(
                "Configure a desktop provider and routing in Notifications & Monitoring first."
            )
    return replace(profile, providers=policies)


def validate_direct_preferences(context: AppContext) -> None:
    """Do not silently bypass an explicit privacy or scheduling policy in legacy watch."""
    settings = watcher_settings(context)
    for key in (
        "monitoring.quiet_hours_enabled",
        "monitoring.digest_enabled",
        "integrations.notification_privacy",
    ):
        value = explicit_preference(settings, key)
        if value is not None and value is not False:
            raise ConfigurationError(
                "Select a configured monitoring profile to apply notification privacy, "
                "quiet hours, and digest policies."
            )
