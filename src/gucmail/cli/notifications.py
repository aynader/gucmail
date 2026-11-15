"""New-mail notifications, rules, profiles, destinations and webhooks."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import typer

from gucmail.cli import core
from gucmail.cli.core import NaturalOrderGroup
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
)
from gucmail.models import (
    Address,
    Importance,
    NotificationEvent,
)
from gucmail.notification_events import (
    EventDisclosurePolicy,
    MailEventType,
    NewMailEvent,
    PrivacyMode,
)
from gucmail.notification_policies import MonitoringProfile, ProviderPolicy
from gucmail.notification_rules import (
    BooleanCondition,
    BooleanField,
    NotificationRule,
    RuleAction,
    RuleActionKind,
    TextCondition,
    TextField,
    TextOperator,
    condition_from_data,
    rule_to_data,
)
from gucmail.secret_store import SecretStore
from gucmail.services.messages import MessageService
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
)
from gucmail.services.notification_simulation import NotificationSimulationService
from gucmail.services.notification_switch import NotificationSwitch
from gucmail.services.notifications import (
    ConsoleNotificationProvider,
    DeliveryState,
    DesktopNotificationProvider,
    NotificationDispatcher,
)
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.services.windows_desktop import (
    desktop_notification_provider,
    desktop_setup_service,
)

notifications_app = typer.Typer(
    help="Get told about new mail. Start with: gucmail notifications on"
)


notification_rules_app = typer.Typer(
    help="Choose which mail you're notified about.",
    cls=NaturalOrderGroup,
)


notification_profiles_app = typer.Typer(
    help="(Advanced) Separate notification setups.",
    cls=NaturalOrderGroup,
)


notification_providers_app = typer.Typer(
    help="Where notifications go: desktop, webhooks and more.",
    cls=NaturalOrderGroup,
)


desktop_notifications_app = typer.Typer(help="Set up Windows desktop notifications.")


webhooks_app = typer.Typer(help="(Advanced) Send new-mail events to a web address.")


notifications_app.add_typer(notification_rules_app, name="rules")


notifications_app.add_typer(notification_profiles_app, name="profiles")


notifications_app.add_typer(notification_providers_app, name="providers")


notifications_app.add_typer(desktop_notifications_app, name="desktop")


def _notification_service(ctx: typer.Context) -> NotificationConfigurationService:
    return NotificationConfigurationService(core.cli_state(ctx).context.state)


def _required_notification_profile(
    service: NotificationConfigurationService,
    profile_id: str,
) -> MonitoringProfile:
    profile = service.profiles.get_profile(profile_id)
    if profile is None:
        raise ConfigurationError(f"Monitoring profile {profile_id!r} does not exist.")
    return profile


def _notification_condition(
    *,
    field: str,
    operator: str,
    value: str,
    condition_json: str | None,
) -> Any:
    if condition_json is not None:
        try:
            document = json.loads(condition_json)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("--condition-json must be valid JSON.") from exc
        try:
            return condition_from_data(document)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid notification condition: {exc}") from exc
    normalized = field.casefold().replace("-", "_")
    try:
        boolean_field = BooleanField(normalized)
    except ValueError:
        boolean_field = None
    if boolean_field is not None:
        expected = value.casefold() in {"1", "true", "yes", "on"}
        if value.casefold() not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
            raise ConfigurationError(
                f"Boolean condition {field!r} needs true/false, yes/no, on/off, or 1/0."
            )
        return BooleanCondition(boolean_field, expected)
    try:
        return TextCondition(TextField(normalized), TextOperator(operator), (value,))
    except ValueError as exc:
        raise ConfigurationError(
            "Unsupported rule field/operator. Use a text field with exact, contains, "
            "not_contains, glob, or regex; or a boolean field."
        ) from exc


def _notification_action(action: str, provider: str) -> RuleAction:
    normalized = action.casefold().replace("-", "_")
    aliases = {
        "immediate": RuleActionKind.ROUTE_IMMEDIATE,
        "digest": RuleActionKind.ROUTE_DIGEST,
        "suppress": RuleActionKind.SUPPRESS,
    }
    kind = aliases.get(normalized)
    if kind is None:
        try:
            kind = RuleActionKind(normalized)
        except ValueError as exc:
            raise ConfigurationError("Rule action must be immediate, digest, or suppress.") from exc
    return RuleAction(
        kind,
        provider
        if kind
        in {
            RuleActionKind.ROUTE_IMMEDIATE,
            RuleActionKind.ROUTE_DIGEST,
        }
        else None,
    )


def _ensure_notification_provider_policy(
    service: NotificationConfigurationService,
    profile: MonitoringProfile,
    action: RuleAction,
) -> MonitoringProfile:
    if action.provider_id is None:
        return profile
    if any(
        policy.provider_id.casefold() == action.provider_id.casefold()
        for policy in profile.providers
    ):
        return profile
    policy = ProviderPolicy(
        action.provider_id,
        enabled=True,
        privacy=EventDisclosurePolicy(mode=PrivacyMode.PRIVATE),
    )
    return service.profiles.update_profile(
        profile.profile_id,
        lambda current: replace(current, providers=(*current.providers, policy)),
    )


def _switch(ctx: typer.Context) -> NotificationSwitch:
    return NotificationSwitch(core.cli_state(ctx).context)


def _switch_human(result: dict[str, object]) -> dict[str, object]:
    rows: dict[str, object] = {"": result["summary"]}
    for step in result["steps"]:  # type: ignore[attr-defined]
        rows[step["step"]] = ("✓ " if step["ok"] else "✗ ") + step["detail"]
    return rows


@notifications_app.command("on")
def notifications_on(
    ctx: typer.Context,
    test: bool = typer.Option(True, "--test/--no-test", help="Send a test notification."),
) -> None:
    """Turn on new-mail notifications. Sets up everything, then sends a test.

    Example: gucmail notifications on
    """

    core.run(
        ctx,
        "notifications on",
        lambda: _switch(ctx).turn_on(test=test).as_dict(),
        title="Notifications",
        source="local_system",
        human_data=_switch_human,
    )


@notifications_app.command("off")
def notifications_off(ctx: typer.Context) -> None:
    """Turn off new-mail notifications. Scheduled mail still gets sent.

    Example: gucmail notifications off
    """

    core.run(
        ctx,
        "notifications off",
        lambda: _switch(ctx).turn_off().as_dict(),
        title="Notifications",
        source="local_system",
        human_data=_switch_human,
    )


@notifications_app.command("status")
def notifications_status(ctx: typer.Context) -> None:
    """Show whether notifications are on and the background task is running."""

    def action() -> dict[str, object]:
        service = _notification_service(ctx)
        context = core.cli_state(ctx).context
        return {
            **_switch(ctx).status().as_dict(),
            "active_profile": service.profiles.active_profile(),
            "profiles": service.profiles.list_profiles(),
            "configured_providers": service.providers.list_providers(),
            "watchers": context.state.list_watcher_status(),
        }

    def human(result: dict[str, object]) -> dict[str, object]:
        return {
            "Notifications": result["notifications"],
            "Background task": result["background_task"],
            "Scheduled messages waiting": result["scheduled_messages_waiting"],
            "": result["summary"],
        }

    core.run(
        ctx,
        "notifications status",
        action,
        title="Notifications",
        source="local",
        human_data=human,
    )


@notification_profiles_app.command("list")
def notification_profiles_list(ctx: typer.Context) -> None:
    """List account-scoped monitoring profiles and the active selection."""

    core.run(
        ctx,
        "notifications profiles list",
        lambda: {
            "active": _notification_service(ctx).profiles.active_profile(),
            "profiles": _notification_service(ctx).profiles.list_profiles(),
        },
        title="Monitoring profiles",
        source="local",
    )


@notification_profiles_app.command("show")
def notification_profiles_show(ctx: typer.Context, profile_id: str) -> None:
    """Show one monitoring profile including rules, privacy, quiet hours, and digests."""

    core.run(
        ctx,
        "notifications profiles show",
        lambda: _required_notification_profile(_notification_service(ctx), profile_id),
        title="Monitoring profile",
        source="local",
    )


@notification_profiles_app.command("create")
def notification_profiles_create(
    ctx: typer.Context,
    profile_id: str,
    name: str = typer.Option(..., help="Human-readable profile name."),
    folder: list[str] = typer.Option(["Inbox"], "--folder"),
    poll_interval: float = typer.Option(60.0, min=60.0, max=86_400.0),
    enabled: bool = typer.Option(False, "--enabled/--disabled"),
    notify_existing: bool = typer.Option(False, "--notify-existing/--quiet-baseline"),
    activate: bool = typer.Option(False, help="Make this the active profile after saving."),
) -> None:
    """Create a disabled-by-default profile; this never starts a watcher."""

    def action() -> MonitoringProfile:
        service = _notification_service(ctx)
        if service.profiles.get_profile(profile_id) is not None:
            raise ConfigurationError(f"Monitoring profile {profile_id!r} already exists.")
        profile = MonitoringProfile(
            profile_id,
            name,
            tuple(folder),
            poll_interval_seconds=poll_interval,
            enabled=enabled,
            notify_existing=notify_existing,
        )
        service.profiles.save_profile(profile)
        return service.profiles.activate(profile_id) if activate else profile

    core.run(
        ctx,
        "notifications profiles create",
        action,
        title="Created monitoring profile",
        source="local",
    )


@notification_profiles_app.command("activate")
def notification_profiles_activate(ctx: typer.Context, profile_id: str) -> None:
    """Select an existing profile for subsequent watch and headless commands."""

    core.run(
        ctx,
        "notifications profiles activate",
        lambda: _notification_service(ctx).profiles.activate(profile_id),
        title="Active monitoring profile",
        source="local",
    )


def _set_notification_profile_enabled(
    ctx: typer.Context,
    profile_id: str,
    *,
    enabled: bool,
) -> None:
    core.run(
        ctx,
        f"notifications profiles {'enable' if enabled else 'disable'}",
        lambda: _notification_service(ctx).profiles.set_enabled(profile_id, enabled=enabled),
        title="Monitoring profile",
        source="local",
    )


@notification_profiles_app.command("enable")
def notification_profiles_enable(ctx: typer.Context, profile_id: str) -> None:
    """Enable a profile without starting a background process."""

    _set_notification_profile_enabled(ctx, profile_id, enabled=True)


@notification_profiles_app.command("disable")
def notification_profiles_disable(ctx: typer.Context, profile_id: str) -> None:
    """Disable a profile; running watchers reload and fail closed."""

    _set_notification_profile_enabled(ctx, profile_id, enabled=False)


@notification_profiles_app.command("delete")
def notification_profiles_delete(ctx: typer.Context, profile_id: str) -> None:
    """Delete one local profile and its rule/policy rows after explicit confirmation."""

    def action() -> dict[str, object]:
        service = _notification_service(ctx)
        profile = _required_notification_profile(service, profile_id)
        core.confirm(ctx, f"Delete local monitoring profile {profile.name!r}?", profile.profile_id)
        return {
            "profile_id": profile.profile_id,
            "deleted": service.profiles.delete_profile(profile_id),
        }

    core.run(
        ctx,
        "notifications profiles delete",
        action,
        title="Deleted monitoring profile",
        source="local",
    )


@notification_rules_app.command("list")
def notification_rules_list(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
) -> None:
    """List deterministic rules for one profile."""

    core.run(
        ctx,
        "notifications rules list",
        lambda: _notification_service(ctx).rules(profile).list_rules(),
        title="Notification rules",
        source="local",
    )


@notification_rules_app.command("show")
def notification_rules_show(
    ctx: typer.Context,
    rule_id: str,
    profile: str = typer.Option("default"),
) -> None:
    """Show one rule's complete typed condition/action document."""

    def action() -> dict[str, Any]:
        rule = _notification_service(ctx).rules(profile).get_rule(rule_id)
        if rule is None:
            raise ConfigurationError(f"Notification rule {rule_id!r} does not exist.")
        return rule_to_data(rule)

    core.run(ctx, "notifications rules show", action, title="Notification rule", source="local")


@notification_rules_app.command("create")
def notification_rules_create(
    ctx: typer.Context,
    rule_id: str,
    name: str = typer.Option(...),
    profile: str = typer.Option("default"),
    field: str = typer.Option("subject"),
    operator: str = typer.Option("contains"),
    value: str | None = typer.Option(None),
    condition_json: str | None = typer.Option(
        None,
        help="Complete typed condition JSON; overrides --field/--operator/--value.",
    ),
    action_kind: str = typer.Option("immediate", "--action"),
    provider: str = typer.Option("desktop"),
    enabled: bool = typer.Option(False, "--enabled/--disabled"),
    stop_processing: bool = typer.Option(False),
    priority: int = typer.Option(0, min=-1_000, max=1_000),
) -> None:
    """Create one validated rule; it is disabled unless --enabled is explicit."""

    def action() -> NotificationRule:
        service = _notification_service(ctx)
        profile_value = _required_notification_profile(service, profile)
        repository = service.rules(profile)
        if repository.get_rule(rule_id) is not None:
            raise ConfigurationError(f"Notification rule {rule_id!r} already exists.")
        rule_action = _notification_action(action_kind, provider)
        profile_value = _ensure_notification_provider_policy(service, profile_value, rule_action)
        if value is None and condition_json is None:
            raise ConfigurationError("Provide --value or a complete --condition-json document.")
        rule = NotificationRule(
            rule_id,
            name,
            _notification_condition(
                field=field,
                operator=operator,
                value=value or "",
                condition_json=condition_json,
            ),
            (rule_action,),
            enabled=enabled,
            stop_processing=stop_processing,
            priority=priority,
            order=len(profile_value.rules),
        )
        repository.save_rule(rule)
        return rule

    core.run(
        ctx,
        "notifications rules create",
        action,
        title="Created notification rule",
        source="local",
    )


@notification_rules_app.command("edit")
def notification_rules_edit(
    ctx: typer.Context,
    rule_id: str,
    profile: str = typer.Option("default"),
    name: str | None = typer.Option(None),
    field: str | None = typer.Option(None),
    operator: str = typer.Option("contains"),
    value: str | None = typer.Option(None),
    condition_json: str | None = typer.Option(None),
    action_kind: str | None = typer.Option(None, "--action"),
    provider: str = typer.Option("desktop"),
    priority: int | None = typer.Option(None, min=-1_000, max=1_000),
    stop_processing: bool | None = typer.Option(None, "--stop-processing/--continue"),
) -> None:
    """Edit explicitly supplied fields while preserving every omitted rule property."""

    def action() -> NotificationRule:
        service = _notification_service(ctx)
        profile_value = _required_notification_profile(service, profile)
        repository = service.rules(profile)
        current = repository.get_rule(rule_id)
        if current is None:
            raise ConfigurationError(f"Notification rule {rule_id!r} does not exist.")
        condition = current.condition
        if condition_json is not None or field is not None or value is not None:
            if field is None and condition_json is None:
                raise ConfigurationError("--field is required when changing --value.")
            if value is None and condition_json is None:
                raise ConfigurationError("--value is required when changing --field.")
            condition = _notification_condition(
                field=field or "subject",
                operator=operator,
                value=value or "",
                condition_json=condition_json,
            )
        actions = current.actions
        if action_kind is not None:
            rule_action = _notification_action(action_kind, provider)
            _ensure_notification_provider_policy(service, profile_value, rule_action)
            actions = (rule_action,)
        updated = replace(
            current,
            name=name if name is not None else current.name,
            condition=condition,
            actions=actions,
            priority=priority if priority is not None else current.priority,
            stop_processing=(
                stop_processing if stop_processing is not None else current.stop_processing
            ),
        )
        repository.save_rule(updated)
        return updated

    core.run(
        ctx,
        "notifications rules edit",
        action,
        title="Updated notification rule",
        source="local",
    )


@notification_rules_app.command("duplicate")
def notification_rules_duplicate(
    ctx: typer.Context,
    rule_id: str,
    new_id: str = typer.Option(...),
    new_name: str = typer.Option(...),
    profile: str = typer.Option("default"),
) -> None:
    """Duplicate a rule as disabled so it can be reviewed safely."""

    core.run(
        ctx,
        "notifications rules duplicate",
        lambda: (
            _notification_service(ctx)
            .rules(profile)
            .duplicate_rule(rule_id, new_id=new_id, new_name=new_name)
        ),
        title="Duplicated notification rule",
        source="local",
    )


def _set_notification_rule_enabled(
    ctx: typer.Context,
    rule_id: str,
    *,
    profile: str,
    enabled: bool,
) -> None:
    core.run(
        ctx,
        f"notifications rules {'enable' if enabled else 'disable'}",
        lambda: _notification_service(ctx).rules(profile).set_enabled(rule_id, enabled=enabled),
        title="Notification rule",
        source="local",
    )


@notification_rules_app.command("enable")
def notification_rules_enable(
    ctx: typer.Context,
    rule_id: str,
    profile: str = typer.Option("default"),
) -> None:
    """Enable one reviewed rule."""

    _set_notification_rule_enabled(ctx, rule_id, profile=profile, enabled=True)


@notification_rules_app.command("disable")
def notification_rules_disable(
    ctx: typer.Context,
    rule_id: str,
    profile: str = typer.Option("default"),
) -> None:
    """Disable one rule without deleting it."""

    _set_notification_rule_enabled(ctx, rule_id, profile=profile, enabled=False)


@notification_rules_app.command("delete")
def notification_rules_delete(
    ctx: typer.Context,
    rule_id: str,
    profile: str = typer.Option("default"),
) -> None:
    """Delete one exact local rule after confirmation."""

    def action() -> dict[str, object]:
        repository = _notification_service(ctx).rules(profile)
        rule = repository.get_rule(rule_id)
        if rule is None:
            raise ConfigurationError(f"Notification rule {rule_id!r} does not exist.")
        core.confirm(ctx, f"Delete notification rule {rule.name!r}?", rule.rule_id)
        return {"rule_id": rule.rule_id, "deleted": repository.delete_rule(rule_id)}

    core.run(
        ctx,
        "notifications rules delete",
        action,
        title="Deleted notification rule",
        source="local",
    )


@notification_rules_app.command("test")
def notification_rules_test(
    ctx: typer.Context,
    rule: str | None = typer.Argument(None, help="Preview one rule ID, or all profile rules."),
    profile: str = typer.Option("default"),
    subject: str = typer.Option("Synthetic notification rule test"),
    sender: str = typer.Option("sender@example.test"),
    has_attachments: bool = typer.Option(False),
    recent: bool = typer.Option(False, help="Explicit read-only recent-mail metadata sample."),
    limit: int = typer.Option(50, min=1, max=100, help="Total recent-message request budget."),
    max_folders: int = typer.Option(10, min=1, max=10, help="Maximum profile folders sampled."),
) -> None:
    """Preview runtime routing using synthetic or bounded recent metadata; never deliver."""

    def action() -> Any:
        service = _notification_service(ctx)
        selected = _required_notification_profile(service, profile)
        if rule is not None:
            # Validate the selected rule before resolving credentials or touching EWS.
            try:
                service.simulate(profile, (), rule_id=rule)
            except KeyError as exc:
                raise ConfigurationError(
                    f"Notification rule {rule!r} does not exist in profile {profile!r}."
                ) from exc
        if recent:
            context = core.cli_state(ctx).context
            sample = NotificationSimulationService(
                MessageService(core.gateway(ctx)),
                account_id=context.config.email or "configured-account",
            ).recent(selected, limit=limit, max_folders=max_folders)
            return {
                "mode": "dry_run",
                "source": "recent_metadata",
                "delivery_performed": False,
                "selected_rule": rule,
                "selected_rule_evaluated_even_if_disabled": rule is not None,
                "folders": sample.folders,
                "request_budget": sample.limit,
                "omitted_folder_count": sample.omitted_folder_count,
                "unavailable_fields": ("body", "recipients", "attachment_names"),
                "boundary": (
                    "Profile routing preview; monitoring, delivery and checkpoints are unchanged."
                ),
                "result": service.simulate(profile, sample.events, rule_id=rule, max_examples=20),
            }
        now = datetime.now(UTC)
        event = NewMailEvent(
            event_id="synthetic-notification-rule-test",
            event_type=MailEventType.NEW_MESSAGE,
            occurred_at=now,
            detected_at=now,
            account_id="synthetic-account",
            folder="Inbox",
            message_reference="synthetic-message",
            sender=Address(sender, "Synthetic sender"),
            subject=subject,
            body_preview="Synthetic preview; no mailbox content was read.",
            importance=Importance.NORMAL,
            is_read=False,
            has_attachments=has_attachments,
        )
        preview = service.test_event(profile, event, at=now, rule_id=rule)
        if rule is None:
            return preview
        return {
            "mode": "dry_run",
            "source": "synthetic",
            "delivery_performed": False,
            "selected_rule": rule,
            "selected_rule_evaluated_even_if_disabled": True,
            "decision": preview.decision,
            "routing": preview.routing,
        }

    core.run(
        ctx,
        "notifications rules test",
        action,
        title="Notification rule dry-run",
        source="recent_metadata" if recent else "synthetic",
    )


@notification_providers_app.command("list")
def notification_providers_list(ctx: typer.Context) -> None:
    """List ordinary provider metadata and configured webhook names without secrets."""

    def action() -> dict[str, object]:
        context = core.cli_state(ctx).context
        return {
            "providers": _notification_service(ctx).providers.list_providers(),
            "webhooks": WebhookRegistry(
                context.state,
                secret_store=SecretStore("webhooks"),
            ).list(),
        }

    core.run(
        ctx,
        "notifications providers list",
        action,
        title="Notification providers",
        source="local",
    )


@notification_providers_app.command("test")
def notification_providers_test(
    ctx: typer.Context,
    provider: str = typer.Option("console"),
    deliver: bool = typer.Option(
        False,
        help="Actually invoke the selected provider; default is a configuration-only preview.",
    ),
) -> None:
    """Preview provider health, or explicitly send one synthetic provider event."""

    close_after: WebhookNotificationProvider | None = None

    def action() -> Any:
        nonlocal close_after
        context = core.cli_state(ctx).context
        key = provider.casefold()
        selected: Any
        if key == "console":
            selected = ConsoleNotificationProvider()
        elif key == "desktop":
            selected = desktop_notification_provider(context)
        else:
            definition = next(
                (
                    item
                    for item in WebhookRegistry(
                        context.state,
                        secret_store=SecretStore("webhooks"),
                    ).list()
                    if item.name.casefold() == key or f"webhook:{item.name}".casefold() == key
                ),
                None,
            )
            if definition is None:
                raise ConfigurationError(f"Notification provider {provider!r} does not exist.")
            selected = WebhookNotificationProvider(
                definition,
                state=context.state,
                secret_store=SecretStore("webhooks"),
            )
            close_after = selected
        if not deliver:
            preview: dict[str, object] = {
                "provider": selected.name,
                "configured": True,
                "delivered": False,
                "note": "Use --deliver with explicit confirmation to invoke the provider.",
            }
            if isinstance(selected, DesktopNotificationProvider):
                status = selected.backend_status()
                preview.update(configured=status.available, backend=status)
            return preview
        core.confirm(ctx, f"Send one synthetic test through {selected.name!r}?", selected.name)
        event = NotificationEvent(
            event_id=f"test-local-{selected.name}",
            event_type="test",
            occurred_at=datetime.now(UTC),
            account_id="synthetic",
            subject="Synthetic notification test",
            preview="No mailbox data was used.",
        )
        return NotificationDispatcher([selected], state=context.state, deduplicate=False).dispatch(
            event
        )

    try:
        core.run(
            ctx,
            "notifications providers test",
            action,
            title="Notification provider test",
            source="synthetic",
        )
    finally:
        if close_after is not None:
            close_after.close()


@notifications_app.command("test")
def notifications_test(ctx: typer.Context) -> None:
    """Show a sample notification in this terminal. Nothing is sent anywhere."""

    event = NotificationEvent(
        event_id="test-local-notification",
        event_type="test",
        occurred_at=datetime.now(UTC),
        account_id="synthetic",
        subject="Synthetic notification test",
        preview="No mailbox data was used.",
    )
    core.run(
        ctx,
        "notifications test",
        lambda: NotificationDispatcher(
            [ConsoleNotificationProvider()],
            state=core.cli_state(ctx).context.state,
            deduplicate=False,
        ).dispatch(event),
        title="Notification test",
        source="synthetic",
    )


@desktop_notifications_app.command("status")
def desktop_notifications_status(ctx: typer.Context) -> None:
    """Inspect only this current-user registration; no mailbox access or toast."""
    core.run(
        ctx,
        "notifications desktop status",
        lambda: {
            "setup": desktop_setup_service(core.cli_state(ctx).context).preview().as_dict(),
            "backend": desktop_notification_provider(core.cli_state(ctx).context).backend_status(),
        },
        title="Windows desktop status",
        source="local",
    )


def _desktop_setup_command(ctx: typer.Context, operation: str, apply: bool, yes: bool) -> None:
    def action() -> dict[str, object]:
        service = desktop_setup_service(core.cli_state(ctx).context, operation)
        preview = service.preview(operation)
        if not apply:
            return preview.as_dict()
        if not preview.can_apply:
            raise ConfigurationError(preview.reason)
        core.confirm(
            ctx,
            f"{operation.title()} only this current-user desktop identity and protocol?",
            service.identity.app_id,
            required=not yes,
        )
        return service.apply(preview, confirmed=True)

    core.run(
        ctx,
        f"notifications desktop {operation}",
        action,
        title="Windows desktop setup",
        source="local",
    )


@desktop_notifications_app.command("setup")
def desktop_notifications_setup(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False, help="Register only the reviewed current-user identity and protocol."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm this owned desktop setup in automation."
    ),
) -> None:
    """Preview native identity and exact launch command; --apply requires confirmation."""
    _desktop_setup_command(ctx, "setup", apply, yes)


@desktop_notifications_app.command("remove")
def desktop_notifications_remove(
    ctx: typer.Context,
    apply: bool = typer.Option(False, help="Remove only the reviewed owned desktop registration."),
    yes: bool = typer.Option(False, "--yes", help="Confirm removal of this owned registration."),
) -> None:
    """Preview or remove owned desktop activation; preserve unknown or conflicting keys."""
    _desktop_setup_command(ctx, "remove", apply, yes)


@notifications_app.command("history")
def notifications_history(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=500),
) -> None:
    """Show recent notifications and whether each one was delivered."""

    core.run(
        ctx,
        "notifications history",
        lambda: NotificationDispatcher([], state=core.cli_state(ctx).context.state).history(
            limit=limit
        ),
        title="Notification history",
        source="local",
    )


@webhooks_app.command("list")
def webhooks_list(ctx: typer.Context) -> None:
    """List persisted non-secret webhook definitions."""

    core.run(
        ctx,
        "webhooks list",
        lambda: WebhookRegistry(
            core.cli_state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).load(),
        title="Webhooks",
        source="local",
    )


@webhooks_app.command("add")
def webhooks_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    url_stdin: bool = typer.Option(
        False,
        "--url-stdin",
        help="Read one secret destination URL line from stdin.",
    ),
    destination_reference: str | None = typer.Option(
        None,
        "--destination-reference",
        help="Use an existing namespaced keyring:// destination reference.",
    ),
    secret_keyring_name: str | None = typer.Option(
        None,
        help="Keyring reference or entry name for an existing HMAC signing secret.",
    ),
    enable: bool = typer.Option(False, help="Enable delivery immediately."),
    field: list[str] = typer.Option([], "--field", help="Payload allowlist field."),
    method: str = typer.Option("POST", help="POST, PUT, or PATCH."),
    header: list[str] = typer.Option(
        [],
        "--header",
        help="Header as NAME=keyring://REFERENCE; custom values are treated as secrets.",
    ),
    max_retries: int = typer.Option(3, min=0, max=10),
) -> None:
    """Add an HTTPS webhook, disabled by default, with an optional field allowlist."""

    def action() -> WebhookConfig:
        state = core.cli_state(ctx)
        if url_stdin and destination_reference is not None:
            raise typer.BadParameter("Use either --url-stdin or --destination-reference, not both.")
        if destination_reference is not None:
            url = destination_reference.strip()
        elif url_stdin:
            url = sys.stdin.readline().rstrip("\r\n")
        elif state.non_interactive or not sys.stdin.isatty():
            raise ConfirmationRequiredError(
                "A webhook destination was not supplied. Use a keyring reference or "
                "--url-stdin; raw URLs are not accepted as command arguments."
            )
        else:
            url = typer.prompt(
                "Webhook destination URL",
                hide_input=True,
                confirmation_prompt=False,
            ).strip()
        if not url:
            raise typer.BadParameter("The webhook destination may not be empty.")
        headers: dict[str, str] = {}
        for pair in header:
            header_name, separator, value = pair.partition("=")
            if not separator or not header_name:
                raise typer.BadParameter("Each --header must use NAME=VALUE syntax.")
            if header_name in headers:
                raise typer.BadParameter(f"Duplicate webhook header: {header_name}")
            headers[header_name] = value
        return WebhookRegistry(
            core.cli_state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).add(
            WebhookConfig(
                name=name,
                url=url,
                method=method,
                headers=headers,
                secret_keyring_name=secret_keyring_name,
                enabled=enable,
                field_allowlist=frozenset(field) if field else None,
                max_retries=max_retries,
            )
        )

    core.run(
        ctx,
        "webhooks add",
        action,
        title="Webhook added",
        source="local",
    )


@webhooks_app.command("migrate-secrets")
def webhooks_migrate_secrets(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Copy and verify one legacy plaintext destination before scrubbing SQLite."""

    core.run(
        ctx,
        "webhooks migrate-secrets",
        lambda: WebhookRegistry(
            core.cli_state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).migrate_legacy(name),
        title="Webhook secret migration",
        source="local",
    )


@webhooks_app.command("remove")
def webhooks_remove(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Remove one local webhook definition."""

    def action() -> dict[str, Any]:
        core.confirm(ctx, f"Remove webhook {name!r}?", f"webhook-remove:{name}")
        return {
            "name": name,
            "removed": WebhookRegistry(
                core.cli_state(ctx).context.state,
                secret_store=SecretStore("webhooks"),
            ).remove(name),
        }

    core.run(ctx, "webhooks remove", action, title="Webhook removal", source="local")


def _set_webhook_enabled(ctx: typer.Context, name: str, *, enabled: bool) -> None:
    def action() -> WebhookConfig:
        if enabled:
            core.confirm(
                ctx,
                f"Enable outbound delivery to webhook {name!r}?",
                f"webhook-enable:{name}",
            )
        return WebhookRegistry(
            core.cli_state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).set_enabled(name, enabled=enabled)

    command = "webhooks enable" if enabled else "webhooks disable"
    core.run(ctx, command, action, title=command.title(), source="local")


@webhooks_app.command("enable")
def webhooks_enable(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Enable outbound delivery for one definition after exact confirmation."""

    _set_webhook_enabled(ctx, name, enabled=True)


@webhooks_app.command("disable")
def webhooks_disable(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Stop outbound delivery for one definition; its history remains local."""

    _set_webhook_enabled(ctx, name, enabled=False)


@webhooks_app.command("test")
def webhooks_test(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    deliver: bool = typer.Option(
        False,
        "--deliver",
        help="Send the synthetic event after confirmation; preview is the default.",
    ),
) -> None:
    """Preview a synthetic payload; optionally perform one confirmed delivery."""

    from gucmail.services.notifications import event_payload

    def action() -> dict[str, Any]:
        webhook_secrets = SecretStore("webhooks")
        config = WebhookRegistry(
            core.cli_state(ctx).context.state,
            secret_store=webhook_secrets,
        ).get(name)
        if config is None:
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError(f"Webhook not found: {name}")
        event = NotificationEvent(
            event_id=(
                f"test-webhook-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
                if deliver
                else "test-webhook-preview"
            ),
            event_type="test",
            occurred_at=datetime.now(UTC),
            account_id="synthetic",
            subject="Synthetic webhook preview",
        )
        response: dict[str, Any] = {
            "webhook": config,
            "payload": event_payload(event, redact=True),
            "sent": False,
        }
        if not deliver:
            return response
        core.confirm(
            ctx,
            f"Send one synthetic test event to webhook {config.name!r}?",
            f"webhook-test:{config.name}",
        )
        with WebhookNotificationProvider(
            replace(config, enabled=True),
            state=core.cli_state(ctx).context.state,
            secret_store=webhook_secrets,
        ) as provider:
            result = provider.notify(event)
        response["delivery"] = result
        response["sent"] = result.state is DeliveryState.DELIVERED
        return response

    core.run(
        ctx,
        "webhooks test",
        action,
        title="Webhook delivery test" if deliver else "Webhook preview",
        source="synthetic",
    )
