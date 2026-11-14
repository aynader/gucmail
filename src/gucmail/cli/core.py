"""Shared plumbing for every command: global state, output, confirmation, services."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

import typer

from gucmail.bootstrap import AppContext, build_context
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ExitCode,
    GUCmailError,
)
from gucmail.models import (
    Address,
    DraftData,
    Importance,
    NotificationEvent,
)
from gucmail.notification_policies import MonitoringProfile
from gucmail.output import OutputFormat, OutputWriter
from gucmail.security import html_to_safe_text, redact_text, sanitize_terminal
from gucmail.services.calendar_preferences import CalendarOperationPreferences
from gucmail.services.compose import ComposeKind, ComposePlan
from gucmail.services.compose_preferences import BodyFormat, ComposerPreferences
from gucmail.services.drafts import (
    DraftService,
)
from gucmail.services.monitoring import NotificationEventDispatcher
from gucmail.services.monitoring_preferences import (
    configured_watcher,
    effective_profile,
    validate_direct_preferences,
    watcher_settings,
)
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    WatcherProfileConfiguration,
)
from gucmail.services.notifications import (
    DesktopNotificationProvider,
    NotificationDispatcher,
    NotificationProvider,
    ProviderBinding,
)
from gucmail.services.pim import PimPage
from gucmail.services.profiled_notifications import ProfiledNotificationDispatcher
from gucmail.services.quota import quota_service_from_settings
from gucmail.services.scheduler import SchedulerService
from gucmail.services.webhooks import (
    WebhookNotificationProvider,
)
from gucmail.services.windows_desktop import (
    desktop_notification_provider,
)
from gucmail.settings import SettingsContext, SettingsService

T = TypeVar("T")


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}


class NaturalOrderGroup(typer.core.TyperGroup):
    """Keep the documented command order instead of alphabetizing it."""

    def list_commands(self, ctx: Any) -> list[str]:
        return list(self.commands)


@dataclass(slots=True)
class CLIState:
    config_path: Path | None
    credentials_file: Path | None
    output_format: OutputFormat
    no_color: bool
    quiet: bool
    verbose: bool
    debug: bool
    non_interactive: bool
    assume_yes: bool
    _context: AppContext | None = None

    @property
    def context(self) -> AppContext:
        if self._context is None:
            self._context = build_context(
                config_path=self.config_path,
                credentials_file=self.credentials_file,
            )
        return self._context

    @property
    def writer(self) -> OutputWriter:
        return OutputWriter(
            format=self.output_format,
            quiet=self.quiet,
            no_color=self.no_color,
        )


def cli_state(ctx: typer.Context) -> CLIState:
    value = ctx.obj
    if not isinstance(value, CLIState):
        raise RuntimeError("CLI context was not initialized")
    return value


def iso_datetime(value: str | None, *, parameter: str) -> datetime | None:
    """Parse an ISO-8601 timestamp at the CLI boundary.

    Typer does not support ``datetime.date`` and ``datetime.datetime`` on every
    supported release, so public command parameters stay as strings and are
    converted explicitly. A timestamp must carry an offset to avoid silently
    scheduling or filtering in the wrong zone.
    """

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(
            "expected ISO-8601 date/time, for example 2026-08-05T18:30:00+02:00",
            param_hint=parameter,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter(
            "date/time must include a UTC offset or Z suffix",
            param_hint=parameter,
        )
    return parsed


def iso_date(value: str | None, *, parameter: str) -> date | None:
    """Parse an ISO calendar date at the CLI boundary."""

    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise typer.BadParameter(
            "expected ISO date in YYYY-MM-DD form",
            param_hint=parameter,
        ) from exc


def report_error(ctx: typer.Context, error: BaseException, *, command: str) -> None:
    state = cli_state(ctx)
    if isinstance(error, GUCmailError):
        payload = error.as_dict()
        code = int(error.exit_code)
        message = error.message
    else:
        payload = {
            "error": type(error).__name__,
            "message": "The command could not be completed.",
            "exit_code": int(ExitCode.SERVER),
        }
        code = int(ExitCode.SERVER)
        message = "The command could not be completed."
        if state.debug:
            payload["debug"] = redact_text(error)
    writer = state.writer
    if state.output_format is OutputFormat.HUMAN:
        # markup=False: error text can quote hostile folder names or server text
        # such as "[/b]", which Rich would otherwise swallow or fail to render.
        writer.error_console.print(
            f"Error: {sanitize_terminal(message)}", style="red", markup=False, highlight=False
        )
        suggestion = payload.get("suggestion")
        if suggestion:
            writer.error_console.print(
                sanitize_terminal(suggestion), style="yellow", markup=False, highlight=False
            )
        debug_detail = payload.get("debug")
        if debug_detail:
            # --debug promised redacted detail; it was only in the JSON envelope.
            writer.error_console.print(
                f"Debug ({payload.get('error')}): {sanitize_terminal(debug_detail)}",
                style="dim",
                markup=False,
                highlight=False,
            )
    else:
        writer.emit(payload, command=command)
    raise typer.Exit(code=code)


def run(
    ctx: typer.Context,
    command: str,
    action: Callable[[], T],
    *,
    title: str | Callable[[T], str] | None = None,
    warnings: Sequence[str] = (),
    source: str | None = None,
    human_data: Callable[[T], Any] | None = None,
) -> T | None:
    try:
        result = action()
        resolved_title = title(result) if callable(title) else title
        presentation = (
            human_data(result)
            if human_data is not None and cli_state(ctx).writer.format is OutputFormat.HUMAN
            else result
        )
    except typer.BadParameter:
        raise
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        report_error(ctx, exc, command=command)
        return None
    state = cli_state(ctx)
    resolved_warnings = list(warnings)
    if state._context is not None:
        resolved_warnings.extend(state._context.credential_warnings)
    cli_state(ctx).writer.emit(
        presentation,
        command=command,
        title=resolved_title,
        warnings=list(dict.fromkeys(resolved_warnings)),
        source=source,
    )
    return result


def gateway(ctx: typer.Context, *, interactive: bool | None = None) -> Any:
    state = cli_state(ctx)
    allow_prompt = not state.non_interactive if interactive is None else interactive
    return state.context.gateway(interactive=allow_prompt)


def draft_service(ctx: typer.Context) -> DraftService:
    context = cli_state(ctx).context
    return DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )


def scheduler(ctx: typer.Context) -> SchedulerService:
    context = cli_state(ctx).context

    def sender(draft: DraftData, **_kwargs: Any) -> Any:
        return gateway(ctx).send(draft)

    return SchedulerService(
        context.state,
        draft_service(ctx),
        sender,
        timezone=context.config.timezone,
        retry_attempts=context.config.retry_attempts,
        retry_base_seconds=context.config.retry_base_seconds,
        retry_max_seconds=context.config.retry_max_seconds,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )


def confirm(ctx: typer.Context, summary: str, scope: str, *, required: bool = True) -> str | None:
    if not required:
        return scope
    state = cli_state(ctx)
    if state.assume_yes:
        return scope
    if state.non_interactive or not sys.stdin.isatty():
        raise ConfirmationRequiredError(
            f"Confirmation required for: {summary} Re-run with --yes after reviewing the preview."
        )
    state.writer.console.print(summary, markup=False)
    if not typer.confirm("Continue?", default=False):
        raise typer.Abort()
    return scope


def parse_addresses(values: Iterable[str]) -> list[Address]:
    return [Address(email=value.strip()) for value in values if value.strip()]


def body_value(body: str | None, body_file: Path | None) -> str:
    if body is not None and body_file is not None:
        raise typer.BadParameter("Use either --body or --body-file, not both.")
    if body_file is not None:
        return body_file.expanduser().read_text(encoding="utf-8")
    return body or ""


def _new_draft(
    *,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str,
    body: str,
    attachments: Sequence[Path],
    importance: Importance,
) -> DraftData:
    return DraftData(
        reference="preview:new",
        to=parse_addresses(to),
        cc=parse_addresses(cc),
        bcc=parse_addresses(bcc),
        subject=subject,
        body_text=body,
        attachment_paths=[Path(item) for item in attachments],
        importance=importance,
    )


def new_composition(
    ctx: typer.Context,
    *,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str,
    body: str,
    attachments: Sequence[Path],
    importance: Importance | None,
    body_format: BodyFormat | None,
    read_receipt: bool | None,
    delivery_receipt: bool | None,
    signature_enabled: bool | None,
    save_sent_copy: bool | None = None,
) -> DraftData:
    preferences = ComposerPreferences.from_settings(settings_service(ctx))
    selected_format = body_format or preferences.body_format
    draft = _new_draft(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body=body,
        attachments=attachments,
        importance=importance or preferences.importance,
    )
    if selected_format is BodyFormat.HTML:
        draft.body_html = body
        draft.body_text = html_to_safe_text(body)
    return preferences.seed_new(
        draft,
        body_format=selected_format,
        importance=importance,
        read_receipt=read_receipt,
        delivery_receipt=delivery_receipt,
        signature_enabled=signature_enabled,
        save_sent_copy=save_sent_copy,
    )


def compose_review(plan: ComposePlan) -> str:
    draft = plan.draft
    lines = [
        f"Subject: {sanitize_terminal(draft.subject) or '(empty)'}",
        f"Format: {'HTML' if draft.body_html is not None else 'plain'}; "
        f"importance: {draft.importance.value}",
        f"Read receipt: {'requested' if draft.request_read_receipt else 'off'}; "
        f"delivery receipt: {'requested' if draft.request_delivery_receipt else 'off'}",
        f"Sent Items copy: {'save' if draft.save_sent_copy else 'none'}",
    ]
    for label, addresses in (("To", draft.to), ("Cc", draft.cc), ("Bcc", draft.bcc)):
        if addresses:
            lines.append(label + ": " + ", ".join(sanitize_terminal(a.email) for a in addresses))
    lines.append(
        "Local attachments: "
        + (", ".join(sanitize_terminal(a.path.name) for a in plan.validation.attachments) or "none")
    )
    if plan.kind is ComposeKind.FORWARD and plan.source_has_attachments:
        lines.append(
            "Inherited attachments: retained on Exchange; original-text preferences do not "
            "remove them. No attachment content was downloaded for this preview."
        )
    lines.extend(
        "Warning: " + sanitize_terminal(issue.message) for issue in plan.validation.warnings
    )
    return "\n".join(lines)


def settings_service(ctx: typer.Context) -> SettingsService:
    context = cli_state(ctx).context
    return SettingsService(
        context.config_store,
        context=SettingsContext(
            account_id=context.config.email,
            machine_id=f"{sys.platform}:{platform.node()}",
        ),
    )


def remember_sent_composition(ctx: typer.Context, result: Any) -> None:
    from gucmail.services.recipient_assistance import remember_sent_recipients

    if not result.sent:
        return
    try:
        remember_sent_recipients(
            settings_service(ctx),
            result.plan.draft,
            own_address=cli_state(ctx).context.config.email,
        )
    except Exception:
        # Keep accepted-send JSON/exit status intact; never encourage a resend for local history.
        if not cli_state(ctx).quiet:
            typer.echo(
                "Send accepted; local recipient history could not be saved. Do not resend.",
                err=True,
            )


def local_capacity_paths(ctx: typer.Context) -> dict[str, Path]:
    context = cli_state(ctx).context
    return {
        "data": context.paths.data_dir,
        "cache": context.paths.cache_dir,
        "search_index": context.paths.index_database,
        "backups": context.paths.backup_dir,
        "downloads": context.config.downloads,
    }


def quota_action(ctx: typer.Context) -> Any:
    settings = settings_service(ctx)

    def setting(setting_id: str, fallback: object) -> object:
        try:
            return settings.value(setting_id).effective_value
        except ConfigurationError:
            return fallback

    return quota_service_from_settings(gateway(ctx), setting).report(
        local_paths=local_capacity_paths(ctx)
    )


def watcher_profile(
    ctx: typer.Context,
    profile: str | None,
) -> WatcherProfileConfiguration | None:
    """Resolve a durable profile without creating one or touching credentials."""

    context = cli_state(ctx).context
    return configured_watcher(context, profile)


def profiled_notification_dispatcher(
    context: AppContext,
    bindings: Sequence[ProviderBinding],
    *,
    profile_id: str | None,
) -> NotificationEventDispatcher | None:
    """Use durable rules for a configured profile, otherwise preserve direct delivery."""

    if not bindings:
        return None

    def event_allowed(event: NotificationEvent) -> bool:
        if not event.event_type.startswith("calendar_"):
            return True
        return CalendarOperationPreferences.from_settings(
            watcher_settings(context)
        ).allows_notification(event)

    direct = NotificationDispatcher(bindings, state=context.state, event_allowed=event_allowed)
    if profile_id is None:
        validate_direct_preferences(context)
        return direct
    service = NotificationConfigurationService(context.state)
    profile = service.profiles.get_profile(profile_id)
    if profile is None:
        validate_direct_preferences(context)
        return direct
    profile = effective_profile(context, profile)

    providers: dict[str, NotificationProvider] = {}
    aliases_by_provider: list[tuple[NotificationProvider, set[str]]] = []
    for binding in bindings:
        provider = binding.provider
        aliases = {provider.name.casefold()}
        if isinstance(provider, WebhookNotificationProvider):
            aliases.add(provider.config.name.casefold())
            aliases.add(f"webhook:{provider.config.name}".casefold())
        aliases_by_provider.append((provider, aliases))
        for alias in aliases:
            providers.setdefault(alias, provider)

    # Provider metadata gives profiles stable IDs that need not equal a plugin's
    # display name. Resolve only exact ID/name/kind aliases and never guess between
    # multiple provider instances of the same kind.
    for metadata in service.providers.list_providers():
        if not metadata.enabled:
            continue
        if metadata.kind.casefold() == "desktop" and any(
            isinstance(provider, DesktopNotificationProvider) for provider, _ in aliases_by_provider
        ):
            if not any(
                policy.provider_id.casefold() == metadata.provider_id.casefold()
                for policy in profile.providers
            ):
                continue
            providers[metadata.provider_id.casefold()] = desktop_notification_provider(
                context, provider_id=metadata.provider_id
            )
            continue
        tokens = {
            metadata.provider_id.casefold(),
            metadata.name.casefold(),
            metadata.kind.casefold(),
        }
        matches = [provider for provider, aliases in aliases_by_provider if aliases & tokens]
        unique_matches = tuple(dict.fromkeys(matches))
        if len(unique_matches) == 1:
            providers[metadata.provider_id.casefold()] = unique_matches[0]

    def load_profile() -> MonitoringProfile:
        current = service.profiles.get_profile(profile.profile_id)
        # Removing a running profile must fail closed instead of continuing to
        # disclose events under a stale policy.
        return (
            effective_profile(context, current)
            if current is not None
            else replace(profile, enabled=False)
        )

    return ProfiledNotificationDispatcher(
        profile,
        providers,
        state=context.state,
        profile_loader=load_profile,
        event_allowed=event_allowed,
    )


def pim_page_output(page: PimPage[Any], *, details: bool, warnings: list[str]) -> Any:
    if page.residual:
        warnings.append(page.residual)
    if page.next_offset is not None:
        warnings.append(
            f"More candidates may remain; repeat with --offset {page.next_offset}. "
            "Use --page for structured continuation information."
        )
    return page if details else page.items
