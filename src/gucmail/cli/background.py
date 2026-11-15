"""Running the watcher without the TUI, and starting it with Windows."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.cli.main import app
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.headless import (
    HeadlessCycleResult,
    HeadlessWatcherRuntime,
    WatcherStatusStore,
    evaluate_watcher_health,
    run_headless_cycles,
    validate_headless_configuration,
)
from gucmail.secret_store import SecretStore
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.monitoring_preferences import (
    effective_profile,
)
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
)
from gucmail.services.notifications import (
    ConsoleNotificationProvider,
    DeliveryState,
    NDJSONNotificationProvider,
    ProviderBinding,
)
from gucmail.services.startup_runtime import (
    StartupPreview,
    apply_startup_preview,
    context_watcher_process,
    inspect_startup_snapshot,
)
from gucmail.services.webhooks import (
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.services.windows_desktop import (
    desktop_notification_provider,
)
from gucmail.startup import (
    InstalledStartupSnapshot,
    StartupInspection,
    StartupOperationPlan,
    StartupPlan,
    assert_safe_startup_plan,
    generate_startup_plan,
    inspect_startup,
    plan_disable_startup,
    plan_enable_startup,
    plan_repair_startup,
    plan_uninstall_startup,
)

headless_app = typer.Typer(help="Run the new-mail watcher without the full-screen app.")


startup_app = typer.Typer(help="Start the new-mail watcher automatically with Windows.")


def _headless_account_id(ctx: typer.Context) -> str:
    account_id = (core.cli_state(ctx).context.config.email or "").strip()
    if not account_id:
        raise ConfigurationError(
            "Headless watcher state needs a configured account email. Run 'gucmail login' "
            "or set GUCMAIL_EMAIL before starting it."
        )
    return account_id


def _headless_process_spec(ctx: typer.Context, *, profile: str) -> Any:
    return context_watcher_process(core.cli_state(ctx).context, profile=profile)


def _headless_status_store(ctx: typer.Context, *, profile: str) -> WatcherStatusStore:
    context = core.cli_state(ctx).context
    spec = _headless_process_spec(ctx, profile=profile)
    return WatcherStatusStore((context.paths.data_dir / "watcher").resolve(), spec.scope_id)


def _monitor_cycle_result(result: Any, *, poll_interval_seconds: int) -> HeadlessCycleResult:
    strategies = tuple(dict.fromkeys(folder.strategy.value for folder in result.folders))
    errors = tuple(folder.error for folder in result.folders if folder.error)
    delivered = sum(
        1 for delivery in result.deliveries if delivery.state is DeliveryState.DELIVERED
    )
    failures = len(result.deliveries) - delivered
    delay = result.retry_after or (0.0 if result.succeeded else float(poll_interval_seconds))
    return HeadlessCycleResult(
        connection_status="connected" if result.succeeded else "reconnecting",
        strategy=",".join(strategies) or "unknown",
        folders=tuple(folder.folder for folder in result.folders),
        successful=result.succeeded,
        events_detected=len(result.events),
        notifications_delivered=delivered,
        notification_failures=failures,
        next_poll_at=datetime.now(UTC) + timedelta(seconds=max(delay, poll_interval_seconds)),
        backoff_seconds=delay,
        error="; ".join(errors) if errors else None,
    )


@headless_app.command("validate")
def headless_validate(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
) -> None:
    """Validate a non-interactive deployment without opening a mailbox session."""

    def action() -> Any:
        state = core.cli_state(ctx)
        context = state.context
        account_id = context.config.email
        provider_names = tuple(
            definition.name
            for definition in WebhookRegistry(
                context.state,
                secret_store=SecretStore("webhooks"),
            ).list()
            if definition.enabled
        )
        # Desktop and other ordinary providers are attached to the profile, not to the
        # webhook registry; count them so validation doesn't claim nothing will notify.
        try:
            configuration = NotificationConfigurationService(context.state)
            stored = configuration.profiles.get_profile(profile)
            enabled_ids = {
                item.provider_id.casefold()
                for item in configuration.providers.list_providers()
                if item.enabled
            }
            provider_names += tuple(
                policy.provider_id
                for policy in (stored.providers if stored is not None else ())
                if policy.enabled
                and policy.provider_id.casefold() in enabled_ids
                and policy.provider_id not in provider_names
            )
        except Exception as exc:  # validation must still report its other checks
            logging.getLogger(__name__).debug("profile provider lookup failed: %s", type(exc))
        credential_source_declared = bool(
            state.credentials_file or os.environ.get("GUCMAIL_PASSWORD") or account_id
        )
        return validate_headless_configuration(
            account_id=account_id,
            profile=profile,
            poll_interval_seconds=context.config.poll_interval_seconds,
            executable=Path(sys.executable).resolve(),
            state_directory=(context.paths.data_dir / "watcher").resolve(),
            log_directory=context.paths.log_dir.resolve(),
            credentials_available=credential_source_declared,
            provider_names=provider_names,
        )

    core.run(
        ctx,
        "headless validate",
        action,
        title="Headless validation",
        source="local",
    )


@headless_app.command("status")
def headless_status(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
) -> None:
    """Inspect the persisted watcher heartbeat without contacting Exchange."""

    core.run(
        ctx,
        "headless status",
        lambda: (
            _headless_status_store(ctx, profile=profile).read()
            or {"state": "never_started", "profile": profile}
        ),
        title="Headless watcher status",
        source="local",
    )


@headless_app.command("health")
@app.command("health")
def headless_health(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    heartbeat_timeout: int = typer.Option(180, min=30, max=86_400),
) -> None:
    """Return mailbox-content-free watcher health suitable for automation."""

    core.run(
        ctx,
        "health" if ctx.command.name == "health" else "headless health",
        lambda: evaluate_watcher_health(
            _headless_status_store(ctx, profile=profile),
            heartbeat_timeout_seconds=heartbeat_timeout,
        ),
        title="Watcher health",
        source="local",
    )


def _headless_control(ctx: typer.Context, *, profile: str, action: str) -> None:
    def execute() -> dict[str, object]:
        store = _headless_status_store(ctx, profile=profile)
        status = store.read()
        if status is None:
            raise ConfigurationError("No watcher instance has published status for this profile.")
        path = store.request(action)
        return {
            "action": action,
            "requested": True,
            "profile": profile,
            "control_file": path.name,
            "target_pid": status.pid,
        }

    core.run(ctx, f"headless {action}", execute, title="Watcher control", source="local")


@headless_app.command("stop")
def headless_stop(ctx: typer.Context, profile: str = typer.Option("default")) -> None:
    """Request a cooperative stop; never force-terminate a process."""

    _headless_control(ctx, profile=profile, action="stop")


@headless_app.command("pause")
def headless_pause(ctx: typer.Context, profile: str = typer.Option("default")) -> None:
    """Request a cooperative pause while preserving checkpoints."""

    _headless_control(ctx, profile=profile, action="pause")


@headless_app.command("resume")
def headless_resume(ctx: typer.Context, profile: str = typer.Option("default")) -> None:
    """Resume a cooperatively paused watcher."""

    _headless_control(ctx, profile=profile, action="resume")


@headless_app.command("run")
def headless_run(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    folder: list[str] = typer.Option([], "--folder"),
    forever: bool = typer.Option(
        False,
        "--forever",
        help="Continue until a signal or cooperative stop request; default is one cycle.",
    ),
    notify_existing: bool | None = typer.Option(
        None,
        "--notify-existing/--quiet-baseline",
        help="Override the profile's first-run baseline policy.",
    ),
    console: bool = typer.Option(False, "--console/--no-console"),
    event_log: Path | None = typer.Option(None, help="Append redacted events as NDJSON."),
    webhooks: bool = typer.Option(True, "--webhooks/--no-webhooks"),
    send_scheduled: bool = typer.Option(
        True,
        "--send-scheduled/--no-send-scheduled",
        help="Also send scheduled messages when they're due (checked every 30 seconds).",
    ),
) -> None:
    """Check for new mail in the background and send scheduled messages."""

    providers: list[ProviderBinding] = []
    close_after: list[WebhookNotificationProvider] = []

    def action() -> dict[str, object]:
        context = core.cli_state(ctx).context
        configured = core.watcher_profile(ctx, profile)
        if configured is None and profile != "default":
            raise ConfigurationError(f"Monitoring profile {profile!r} does not exist.")
        if configured is not None and not configured.enabled:
            raise ConfigurationError(f"Monitoring profile {configured.profile_id!r} is disabled.")
        resolved_profile = configured.profile_id if configured is not None else profile
        resolved_folders = tuple(folder) or (
            configured.folders if configured is not None else ("inbox",)
        )
        poll_interval = (
            configured.poll_interval_seconds
            if configured is not None
            else context.config.poll_interval_seconds
        )
        baseline_policy = (
            notify_existing
            if notify_existing is not None
            else configured.notify_existing
            if configured is not None
            else False
        )
        if configured is not None:
            notification_service = NotificationConfigurationService(context.state)
            configured_profile = notification_service.profiles.get_profile(configured.profile_id)
            if configured_profile is not None:
                configured_profile = effective_profile(context, configured_profile)
            desktop_ids = {
                item.provider_id.casefold()
                for item in notification_service.providers.list_providers()
                if item.enabled and item.kind.casefold() == "desktop"
            }
            if configured_profile is not None and any(
                policy.enabled and policy.provider_id.casefold() in desktop_ids
                for policy in configured_profile.providers
            ):
                providers.append(ProviderBinding(desktop_notification_provider(context)))
        if console:
            providers.append(ProviderBinding(ConsoleNotificationProvider()))
        if event_log is not None:
            providers.append(ProviderBinding(NDJSONNotificationProvider(event_log)))
        if webhooks:
            webhook_secrets = SecretStore("webhooks")
            for definition in WebhookRegistry(
                context.state,
                secret_store=webhook_secrets,
            ).list():
                if not definition.enabled:
                    continue
                provider = WebhookNotificationProvider(
                    definition,
                    state=context.state,
                    secret_store=webhook_secrets,
                )
                close_after.append(provider)
                providers.append(ProviderBinding(provider))
        dispatcher = core.profiled_notification_dispatcher(
            context,
            providers,
            profile_id=configured.profile_id if configured is not None else None,
        )
        monitor = MailboxMonitor(
            core.gateway(ctx, interactive=False),
            context.state,
            account_id=_headless_account_id(ctx),
            dispatcher=dispatcher,
            folders=resolved_folders,
            poll_interval_seconds=poll_interval,
            limit=context.config.page_size,
            notify_existing=baseline_policy,
            profile_id=resolved_profile,
            strategy_preference=configured.strategy_preference if configured else None,
        )
        spec = _headless_process_spec(ctx, profile=profile)
        store = _headless_status_store(ctx, profile=profile)
        runtime = HeadlessWatcherRuntime(spec, store)

        def cycle() -> HeadlessCycleResult:
            return _monitor_cycle_result(
                monitor.run_once(),
                poll_interval_seconds=int(poll_interval),
            )

        tick = _scheduled_sender(ctx) if send_scheduled else None
        cycles = run_headless_cycles(runtime, cycle, max_cycles=None if forever else 1, tick=tick)
        return {
            "cycles": cycles,
            "profile": profile,
            "folders": resolved_folders,
            "final_status": store.read(),
            "tui_initialized": False,
        }

    try:
        core.run(ctx, "headless run", action, title="Headless watcher")
    finally:
        for provider in close_after:
            provider.close()


def _scheduled_sender(ctx: typer.Context) -> Any:
    """Send due scheduled messages; a failure is logged and never stops mail checks."""

    scheduler = core.scheduler(ctx)

    def tick() -> None:
        try:
            scheduler.run_once(limit=20)
        except Exception:  # keep checking mail; the job keeps its state for the next try
            logging.getLogger(__name__).warning("Sending scheduled mail failed.", exc_info=True)

    return tick


def _startup_plan(
    ctx: typer.Context,
    *,
    profile: str,
    platform_name: str | None,
    system_service: bool,
    output_directory: Path | None,
    startup_delay: int,
    restart_delay: int,
    restart_attempts: int,
    service_user: str | None,
) -> StartupPlan:
    process = _headless_process_spec(ctx, profile=profile)
    return assert_safe_startup_plan(
        generate_startup_plan(
            process,
            platform_name=platform_name,
            system_service=system_service,
            output_directory=(
                output_directory.expanduser().resolve(strict=False)
                if output_directory is not None
                else None
            ),
            startup_delay_seconds=startup_delay,
            restart_delay_seconds=restart_delay,
            restart_attempts=restart_attempts,
            service_user=service_user,
        )
    )


def _inspect_startup_snapshot(plan: StartupPlan) -> InstalledStartupSnapshot:
    return inspect_startup_snapshot(plan)


def _apply_startup_operation(
    ctx: typer.Context,
    plan: StartupPlan,
    operation: StartupOperationPlan,
    inspection: StartupInspection,
) -> dict[str, object]:
    if not operation.can_apply:
        raise ConfigurationError(operation.reason)
    if not operation.no_changes:
        core.confirm(
            ctx,
            f"{operation.reason} Managed entry: {operation.service_name}",
            f"startup:{operation.operation.value}:{operation.service_name}",
        )
    return apply_startup_preview(StartupPreview(plan, inspection, operation), confirmed=True)


def _startup_command_plan(
    ctx: typer.Context,
    *,
    operation_name: str,
    profile: str,
    platform_name: str | None,
    system_service: bool,
    output_directory: Path | None,
    startup_delay: int,
    restart_delay: int,
    restart_attempts: int,
    service_user: str | None,
    apply: bool,
) -> None:
    def action() -> object:
        plan = _startup_plan(
            ctx,
            profile=profile,
            platform_name=platform_name,
            system_service=system_service,
            output_directory=output_directory,
            startup_delay=startup_delay,
            restart_delay=restart_delay,
            restart_attempts=restart_attempts,
            service_user=service_user,
        )
        inspection = inspect_startup(plan, _inspect_startup_snapshot(plan))
        planners = {
            "enable": plan_enable_startup,
            "repair": plan_repair_startup,
            "disable": plan_disable_startup,
            "uninstall": plan_uninstall_startup,
        }
        operation = planners[operation_name](plan, inspection)
        if not apply:
            return {
                "inspection": inspection,
                "operation": operation,
                "applied": False,
            }
        return _apply_startup_operation(ctx, plan, operation, inspection)

    core.run(
        ctx,
        f"startup {operation_name}",
        action,
        title="Watcher startup integration",
        source="local_system",
    )


@startup_app.command("plan")
def startup_plan(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    startup_delay: int = typer.Option(30, min=0, max=3600),
    restart_delay: int = typer.Option(60, min=1, max=3600),
    restart_attempts: int = typer.Option(3, min=1, max=999),
    service_user: str | None = typer.Option(None),
) -> None:
    """Generate a deterministic, secret-free Task Scheduler or systemd plan."""

    core.run(
        ctx,
        "startup plan",
        lambda: _startup_plan(
            ctx,
            profile=profile,
            platform_name=platform_name,
            system_service=system_service,
            output_directory=output_directory,
            startup_delay=startup_delay,
            restart_delay=restart_delay,
            restart_attempts=restart_attempts,
            service_user=service_user,
        ).as_dict(include_content=True),
        title="Watcher startup plan",
        source="local_preview",
    )


@startup_app.command("status")
def startup_status(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    service_user: str | None = typer.Option(None),
) -> None:
    """Compare actual startup state with the expected managed definition."""

    def action() -> object:
        plan = _startup_plan(
            ctx,
            profile=profile,
            platform_name=platform_name,
            system_service=system_service,
            output_directory=output_directory,
            startup_delay=30,
            restart_delay=60,
            restart_attempts=3,
            service_user=service_user,
        )
        return inspect_startup(plan, _inspect_startup_snapshot(plan))

    core.run(ctx, "startup status", action, title="Startup status", source="local_system")


def _startup_mutation_options(
    ctx: typer.Context,
    operation_name: str,
    profile: str,
    platform_name: str | None,
    system_service: bool,
    output_directory: Path | None,
    startup_delay: int,
    restart_delay: int,
    restart_attempts: int,
    service_user: str | None,
    apply: bool,
) -> None:
    _startup_command_plan(
        ctx,
        operation_name=operation_name,
        profile=profile,
        platform_name=platform_name,
        system_service=system_service,
        output_directory=output_directory,
        startup_delay=startup_delay,
        restart_delay=restart_delay,
        restart_attempts=restart_attempts,
        service_user=service_user,
        apply=apply,
    )


@startup_app.command("install")
@startup_app.command("enable")
def startup_enable(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    startup_delay: int = typer.Option(30, min=0, max=3600),
    restart_delay: int = typer.Option(60, min=1, max=3600),
    restart_attempts: int = typer.Option(3, min=1, max=999),
    service_user: str | None = typer.Option(None),
    apply: bool = typer.Option(False, "--apply", help="Apply after exact confirmation."),
) -> None:
    """Preview or explicitly install and enable watcher startup."""

    _startup_mutation_options(
        ctx,
        "enable",
        profile,
        platform_name,
        system_service,
        output_directory,
        startup_delay,
        restart_delay,
        restart_attempts,
        service_user,
        apply,
    )


@startup_app.command("repair")
def startup_repair(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    startup_delay: int = typer.Option(30, min=0, max=3600),
    restart_delay: int = typer.Option(60, min=1, max=3600),
    restart_attempts: int = typer.Option(3, min=1, max=999),
    service_user: str | None = typer.Option(None),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or replace a drifted managed startup definition."""

    _startup_mutation_options(
        ctx,
        "repair",
        profile,
        platform_name,
        system_service,
        output_directory,
        startup_delay,
        restart_delay,
        restart_attempts,
        service_user,
        apply,
    )


@startup_app.command("disable")
def startup_disable(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    service_user: str | None = typer.Option(None),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or explicitly disable startup while retaining its definition."""

    _startup_mutation_options(
        ctx,
        "disable",
        profile,
        platform_name,
        system_service,
        output_directory,
        30,
        60,
        3,
        service_user,
        apply,
    )


@startup_app.command("remove")
@startup_app.command("uninstall")
def startup_uninstall(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
    platform_name: str | None = typer.Option(None, "--platform"),
    system_service: bool = typer.Option(False),
    output_directory: Path | None = typer.Option(None),
    service_user: str | None = typer.Option(None),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or explicitly remove the managed startup entry and artifact."""

    _startup_mutation_options(
        ctx,
        "uninstall",
        profile,
        platform_name,
        system_service,
        output_directory,
        30,
        60,
        3,
        service_user,
        apply,
    )
