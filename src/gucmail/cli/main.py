"""Top-level commands: login, status, read, send, reply, search, watch and the TUI."""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from gucmail import __version__
from gucmail.cli import core
from gucmail.cli.core import CONTEXT_SETTINGS, CLIState, NaturalOrderGroup
from gucmail.credentials import CredentialStore
from gucmail.dates import parse_datetime_expression
from gucmail.doctor import doctor_summary, run_doctor, write_diagnostic_bundle
from gucmail.errors import (
    ConfirmationRequiredError,
    ExitCode,
)
from gucmail.models import (
    Importance,
    SendState,
)
from gucmail.output import OutputFormat
from gucmail.secret_store import SecretStore
from gucmail.services.compose import ComposeKind, ComposeService
from gucmail.services.compose_preferences import BodyFormat, ComposerPreferences
from gucmail.services.mail_preferences import (
    MailResponsePreferences,
    ReplyMode,
)
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.monitoring_preferences import (
    explicit_preference,
    watcher_settings,
)
from gucmail.services.notifications import (
    CommandHookProvider,
    ConsoleNotificationProvider,
    NDJSONNotificationProvider,
    ProviderBinding,
)
from gucmail.services.webhooks import (
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.services.windows_desktop import (
    desktop_notification_provider,
)

app = typer.Typer(
    name="gucmail",
    help="Read and send your GUC email from the terminal. `gucmail tui` opens the full app.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=CONTEXT_SETTINGS,
    cls=NaturalOrderGroup,
)


def _version_callback(value: bool) -> bool:
    if value:
        typer.echo(__version__)
        raise typer.Exit()
    return value


def _tolerate_narrow_console_encoding() -> None:
    """Never crash when redirected Windows output uses a legacy code page (e.g. cp1252)."""

    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").replace("-", "").casefold()
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding and encoding != "utf8" and callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", help="Use this non-secret TOML configuration file."
    ),
    credentials_file: Path | None = typer.Option(
        None,
        "--credentials-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help=(
            "Explicitly use this legacy plaintext credential file for online operations. "
            "Place this global option before the command."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit one stable JSON envelope."),
    ndjson: bool = typer.Option(False, "--ndjson", help="Emit item records followed by a summary."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI colors."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress normal output."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show additional safe detail."),
    debug: bool = typer.Option(False, "--debug", help="Enable redacted debug logging."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Never prompt; fail when confirmation is required."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the exact previewed scope."),
    version_requested: bool = typer.Option(
        False,
        "--version",
        help="Print the installed version and exit.",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    """Global output and safety options apply to every subcommand."""

    _tolerate_narrow_console_encoding()
    if version_requested:
        typer.echo(__version__)
        raise typer.Exit()
    if json_output and ndjson:
        raise typer.BadParameter("Choose either --json or --ndjson, not both.")
    output_format = (
        OutputFormat.JSON if json_output else OutputFormat.NDJSON if ndjson else OutputFormat.HUMAN
    )
    ctx.obj = CLIState(
        config_path=config,
        credentials_file=credentials_file,
        output_format=output_format,
        no_color=no_color,
        quiet=quiet,
        verbose=verbose,
        debug=debug,
        non_interactive=non_interactive,
        assume_yes=yes,
    )
    if debug:
        logging.getLogger("gucmail").setLevel(logging.DEBUG)


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the installed application and output-schema versions."""

    core.run(
        ctx,
        "version",
        lambda: {"version": __version__, "output_schema": "gucmail.output/v1"},
        title="gucmail",
    )


@app.command()
def login(
    ctx: typer.Context,
    email: str | None = typer.Option(None, help="Primary mailbox email address."),
    username: str | None = typer.Option(None, help="Exchange sign-in name; defaults to email."),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help="Read one password line from stdin instead of prompting securely.",
    ),
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help="Validate with one bounded mailbox-status read after storing the credential.",
    ),
) -> None:
    """Store a password in the OS keyring and optionally validate sign-in."""

    def action() -> dict[str, Any]:
        state = core.cli_state(ctx)
        context = state.context
        account_email = (email or context.config.email or "").strip()
        if not account_email:
            if state.non_interactive or not sys.stdin.isatty():
                raise typer.BadParameter("Provide --email on first login.")
            account_email = typer.prompt("GUC email address").strip()
            if not account_email:
                raise typer.BadParameter("The account email may not be empty.")
        if password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        elif state.non_interactive or not sys.stdin.isatty():
            raise ConfirmationRequiredError(
                "A password was not supplied. Use an OS keyring entry or --password-stdin."
            )
        else:
            password = typer.prompt("Exchange password", hide_input=True, confirmation_prompt=False)
        if not password:
            raise typer.BadParameter("The password may not be empty.")
        CredentialStore().set(account_email, password)
        updated = replace(
            context.config,
            email=account_email,
            username=(username or context.config.username or account_email).strip(),
        ).validate()
        context.config_store.save(updated)
        context.config = updated
        result: dict[str, Any] = {
            "credential": "stored_in_os_keyring",
            "email": updated.redacted_dict()["email"],
            "checked": False,
        }
        if check:
            result["status"] = context.gateway(interactive=False).status()
            result["checked"] = True
        return result

    core.run(ctx, "login", action, title="Login")


@app.command()
def logout(
    ctx: typer.Context,
    forget_account: bool = typer.Option(
        False, help="Also remove the non-secret email and username from configuration."
    ),
) -> None:
    """Remove the current mailbox credential from the OS keyring."""

    def action() -> dict[str, Any]:
        context = core.cli_state(ctx).context
        email = context.config.email
        removed = CredentialStore().delete(email) if email else False
        context.close()
        if forget_account:
            updated = replace(context.config, email=None, username=None)
            context.config_store.save(updated)
            context.config = updated
        return {"credential_removed": removed, "account_forgotten": forget_account}

    core.run(ctx, "logout", action, title="Logout")


@app.command()
def status(
    ctx: typer.Context,
    online: bool = typer.Option(
        False, "--online", help="Make one bounded authenticated mailbox-status read."
    ),
) -> None:
    """Show local status; use --online for an intentional EWS check."""

    def action() -> dict[str, Any]:
        context = core.cli_state(ctx).context
        result = context.local_status()
        if online:
            result["exchange"] = context.gateway(
                interactive=not core.cli_state(ctx).non_interactive
            ).status()
            result["credential"] = {
                "source": context.credential_source,
                "plaintext_file_selected": context.credential_source == "legacy_file_explicit",
            }
        else:
            result["exchange"] = "not_probed"
        return result

    core.run(ctx, "status", action, title="Status", source="live_server" if online else "local")


@app.command()
def doctor(
    ctx: typer.Context,
    network: bool = typer.Option(False, help="Test DNS and TLS without authenticating."),
    authenticate: bool = typer.Option(
        False, help="Make one bounded authenticated mailbox-status read."
    ),
) -> None:
    """Check that gucmail is set up correctly. Nothing is sent unless you add --network."""

    core.run(
        ctx,
        "doctor",
        lambda: run_doctor(
            core.cli_state(ctx).context, network=network or authenticate, authenticate=authenticate
        ),
        title="gucmail doctor",
        human_data=doctor_summary,
    )


@app.command()
def diagnostics(
    ctx: typer.Context,
    output: Path = typer.Option(Path("gucmail-diagnostics.json"), help="Sanitized bundle path."),
    network: bool = typer.Option(False, help="Include DNS and TLS checks."),
) -> None:
    """Write a redacted diagnostic bundle with no credentials or mailbox content."""

    def action() -> dict[str, Any]:
        context = core.cli_state(ctx).context
        checks = run_doctor(context, network=network, authenticate=False)
        path = write_diagnostic_bundle(context, output.expanduser(), results=checks)
        return {"path": str(path), "checks": len(checks), "contains_mailbox_data": False}

    core.run(ctx, "diagnostics", action, title="Diagnostics")


@app.command("tui")
def tui_command(
    ctx: typer.Context,
    offline: bool = typer.Option(False, help="Launch without resolving credentials or Exchange."),
) -> None:
    """Launch the responsive Textual interface."""

    try:
        from gucmail.tui import run_tui

        state = core.cli_state(ctx)
        if state.credentials_file is not None and not offline:
            state.writer.warn(
                "An explicitly selected plaintext credential file will be used for this "
                "TUI session. Its values will not be displayed or stored in normal settings."
            )
        run_tui(context=state.context, offline=offline)
    except BaseException as exc:
        core.report_error(ctx, exc, command="tui")


@app.command()
def send(
    ctx: typer.Context,
    to: list[str] = typer.Option([], "--to", help="Recipient; repeat for multiple addresses."),
    cc: list[str] = typer.Option([], "--cc", help="Cc recipient; repeat as needed."),
    bcc: list[str] = typer.Option([], "--bcc", help="Bcc recipient; repeat as needed."),
    subject: str = typer.Option("", help="Message subject."),
    body: str | None = typer.Option(None, help="Body in the selected plain or HTML format."),
    body_file: Path | None = typer.Option(None, help="Read the UTF-8 body from this file."),
    attach: list[Path] = typer.Option([], "--attach", help="Attach a local file; repeatable."),
    importance: Importance | None = typer.Option(
        None, case_sensitive=False, help="Importance; saved composer default when omitted."
    ),
    body_format: BodyFormat | None = typer.Option(
        None, help="Body format; saved default when omitted."
    ),
    read_receipt: bool | None = typer.Option(None, "--read-receipt/--no-read-receipt"),
    delivery_receipt: bool | None = typer.Option(None, "--delivery-receipt/--no-delivery-receipt"),
    signature: bool | None = typer.Option(None, "--signature/--no-signature"),
    save_sent_copy: bool | None = typer.Option(
        None, "--save-sent-copy/--no-sent-copy", help="Override the saved Sent Items default."
    ),
    schedule: str | None = typer.Option(
        None,
        "--schedule",
        "--at",
        "--in",
        help="Store locally for an unambiguous future time such as 'in 2 hours'.",
    ),
    undo_delay: int | None = typer.Option(
        None,
        min=0,
        help="Queue locally for this many seconds; 0 overrides the saved Undo Send delay.",
    ),
    dry_run: bool = typer.Option(False, help="Validate and preview without sending."),
    allow_empty_subject: bool | None = typer.Option(
        None, "--allow-empty-subject/--require-subject"
    ),
    allow_empty_body: bool = typer.Option(False),
) -> None:
    """Preview and send, or durably schedule, a new message."""

    def action() -> Any:
        preferences = ComposerPreferences.from_settings(core.settings_service(ctx))
        draft = core.new_composition(
            ctx,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=core.body_value(body, body_file),
            attachments=attach,
            importance=importance,
            body_format=body_format,
            read_receipt=read_receipt,
            delivery_receipt=delivery_receipt,
            signature_enabled=signature,
            save_sent_copy=save_sent_copy,
        )
        context = core.cli_state(ctx).context
        if schedule and undo_delay:
            raise typer.BadParameter("Use either --schedule or --undo-delay, not both.")
        delay = undo_delay if undo_delay is not None else context.config.undo_send_seconds
        service = ComposeService(
            max_attachment_bytes=context.config.max_attachment_bytes,
            state=context.state,
            preferences=preferences,
        )
        plan = service.plan_send(
            draft,
            allow_empty_subject=allow_empty_subject,
            allow_empty_body=allow_empty_body,
        )
        if schedule or delay:
            now = datetime.now(UTC)
            when = (
                parse_datetime_expression(
                    schedule, timezone=context.config.timezone, now=now, future_required=True
                ).local
                if schedule
                else now + timedelta(seconds=delay)
            )
            if dry_run:
                return {
                    "dry_run": True,
                    "scheduled_for": when,
                    "plan": plan,
                    "scheduler_required": True,
                }
            core.confirm(
                ctx,
                f"Queue {len(plan.draft.to) + len(plan.draft.cc) + len(plan.draft.bcc)} "
                f"recipient(s) for {when.isoformat()}? A scheduler must be running to send.\n"
                + core.compose_review(plan),
                plan.confirmation_scope + ":" + when.isoformat(),
            )
            local = core.draft_service(ctx).create(
                to=plan.draft.to,
                cc=plan.draft.cc,
                bcc=plan.draft.bcc,
                subject=plan.draft.subject,
                body_text=plan.draft.body_text,
                body_html=plan.draft.body_html,
                attachment_paths=plan.draft.attachment_paths,
                importance=plan.draft.importance,
                request_read_receipt=plan.draft.request_read_receipt,
                request_delivery_receipt=plan.draft.request_delivery_receipt,
                save_sent_copy=plan.draft.save_sent_copy,
            )
            return core.scheduler(ctx).schedule_at(
                local.reference,
                when,
                now=now,
                original_expression=schedule or f"Undo Send delay: {delay} seconds",
                state=SendState.SCHEDULED if schedule else SendState.UNDO_DELAY,
                allow_empty_subject=(
                    preferences.allow_empty_subject
                    if allow_empty_subject is None
                    else allow_empty_subject
                ),
                allow_empty_body=allow_empty_body,
            )
        if dry_run:
            return service.execute_send(plan, dry_run=True)
        scope = core.confirm(
            ctx,
            f"Send to {len(plan.draft.to) + len(plan.draft.cc) + len(plan.draft.bcc)} "
            "recipient(s)?\n" + core.compose_review(plan),
            plan.confirmation_scope,
        )
        service.gateway = core.gateway(ctx)
        result = service.execute_send(plan, confirmation_scope=scope)
        core.remember_sent_composition(ctx, result)
        return result

    core.run(ctx, "send", action, title="Send preview" if dry_run else "Send")


def _reply_command(
    ctx: typer.Context,
    *,
    reference: str,
    kind: ComposeKind,
    to: Sequence[str],
    body: str | None,
    body_file: Path | None,
    attach: Sequence[Path],
    dry_run: bool,
    importance: Importance | None,
    body_format: BodyFormat | None,
    read_receipt: bool | None,
    delivery_receipt: bool | None,
    signature: bool | None,
    reply_mode: ReplyMode | None = None,
    include_history: bool | None = None,
    save_sent_copy: bool | None = None,
) -> None:
    def action() -> Any:
        settings = core.settings_service(ctx)
        preferences = ComposerPreferences.from_settings(settings)
        response_preferences = MailResponsePreferences.from_settings(settings)
        service = ComposeService(
            core.gateway(ctx),
            max_attachment_bytes=core.cli_state(ctx).context.config.max_attachment_bytes,
            state=core.cli_state(ctx).context.state,
            preferences=preferences,
            response_preferences=response_preferences,
        )
        if kind is ComposeKind.FORWARD:
            preview = service.preview_forward(
                reference, recipients=core.parse_addresses(to), include_history=include_history
            )
        else:
            configured_email = core.cli_state(ctx).context.config.email
            own: list[str] = [configured_email] if configured_email is not None else []
            preview = service.preview_reply(
                reference,
                reply_all=(
                    True
                    if kind is ComposeKind.REPLY_ALL
                    else None
                    if reply_mode is None
                    else reply_mode is ReplyMode.ALL
                ),
                account_addresses=own,
                include_history=include_history,
            )
        supplied = core.body_value(body, body_file)
        if supplied:
            preview.draft.body_text = supplied + "\n\n" + preview.draft.body_text
        preview.draft.attachment_paths = list(attach)
        preview = replace(
            preview,
            draft=service.preferences.seed_new(
                preview.draft,
                importance=importance,
                body_format=body_format,
                read_receipt=read_receipt,
                delivery_receipt=delivery_receipt,
                signature_enabled=signature,
                save_sent_copy=save_sent_copy,
            ),
        )
        plan = service.plan_send(preview)
        scope = (
            None
            if dry_run
            else core.confirm(
                ctx,
                f"{plan.kind.value.replace('_', ' ').title()} to "
                f"{len(plan.draft.to) + len(plan.draft.cc)} reviewed recipient(s)?\n"
                + core.compose_review(plan),
                plan.confirmation_scope,
            )
        )
        result = service.execute_send(plan, dry_run=dry_run, confirmation_scope=scope)
        core.remember_sent_composition(ctx, result)
        return result

    core.run(ctx, kind.value.replace("_", "-"), action, title=kind.value.replace("_", " ").title())


@app.command()
def reply(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Local message reference."),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    attach: list[Path] = typer.Option([], "--attach"),
    dry_run: bool = typer.Option(False),
    importance: Importance | None = typer.Option(None, case_sensitive=False),
    body_format: BodyFormat | None = typer.Option(None),
    read_receipt: bool | None = typer.Option(None, "--read-receipt/--no-read-receipt"),
    delivery_receipt: bool | None = typer.Option(None, "--delivery-receipt/--no-delivery-receipt"),
    signature: bool | None = typer.Option(None, "--signature/--no-signature"),
    save_sent_copy: bool | None = typer.Option(None, "--save-sent-copy/--no-sent-copy"),
    include_history: bool | None = typer.Option(
        None,
        "--include-history/--no-history",
        help="Override quoted original text for this response.",
    ),
    reply_mode: ReplyMode | None = typer.Option(
        None, help="Sender or all visible recipients; saved default when omitted."
    ),
) -> None:
    """Preview a reply, then send only after exact confirmation."""

    _reply_command(
        ctx,
        reference=reference,
        kind=ComposeKind.REPLY,
        to=(),
        body=body,
        body_file=body_file,
        attach=attach,
        dry_run=dry_run,
        importance=importance,
        body_format=body_format,
        read_receipt=read_receipt,
        delivery_receipt=delivery_receipt,
        signature=signature,
        save_sent_copy=save_sent_copy,
        include_history=include_history,
        reply_mode=reply_mode,
    )


@app.command("reply-all")
def reply_all(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Local message reference."),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    attach: list[Path] = typer.Option([], "--attach"),
    dry_run: bool = typer.Option(False),
    importance: Importance | None = typer.Option(None, case_sensitive=False),
    body_format: BodyFormat | None = typer.Option(None),
    read_receipt: bool | None = typer.Option(None, "--read-receipt/--no-read-receipt"),
    delivery_receipt: bool | None = typer.Option(None, "--delivery-receipt/--no-delivery-receipt"),
    signature: bool | None = typer.Option(None, "--signature/--no-signature"),
    save_sent_copy: bool | None = typer.Option(None, "--save-sent-copy/--no-sent-copy"),
    include_history: bool | None = typer.Option(
        None,
        "--include-history/--no-history",
        help="Override quoted original text for this response.",
    ),
) -> None:
    """Calculate and preview normalized reply-all recipients before sending."""

    _reply_command(
        ctx,
        reference=reference,
        kind=ComposeKind.REPLY_ALL,
        to=(),
        body=body,
        body_file=body_file,
        attach=attach,
        dry_run=dry_run,
        importance=importance,
        body_format=body_format,
        read_receipt=read_receipt,
        delivery_receipt=delivery_receipt,
        signature=signature,
        save_sent_copy=save_sent_copy,
        include_history=include_history,
    )


@app.command()
def forward(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Local message reference."),
    to: list[str] = typer.Option([], "--to", help="Forward recipient; repeatable."),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    attach: list[Path] = typer.Option([], "--attach"),
    dry_run: bool = typer.Option(False),
    importance: Importance | None = typer.Option(None, case_sensitive=False),
    body_format: BodyFormat | None = typer.Option(None),
    read_receipt: bool | None = typer.Option(None, "--read-receipt/--no-read-receipt"),
    delivery_receipt: bool | None = typer.Option(None, "--delivery-receipt/--no-delivery-receipt"),
    signature: bool | None = typer.Option(None, "--signature/--no-signature"),
    save_sent_copy: bool | None = typer.Option(None, "--save-sent-copy/--no-sent-copy"),
    include_history: bool | None = typer.Option(
        None,
        "--include-history/--no-history",
        help="Override quoted original text for this response.",
    ),
) -> None:
    """Review a forward; inherited server attachments are retained and disclosed."""

    _reply_command(
        ctx,
        reference=reference,
        kind=ComposeKind.FORWARD,
        to=to,
        body=body,
        body_file=body_file,
        attach=attach,
        dry_run=dry_run,
        importance=importance,
        body_format=body_format,
        read_receipt=read_receipt,
        delivery_receipt=delivery_receipt,
        signature=signature,
        save_sent_copy=save_sent_copy,
        include_history=include_history,
    )


@app.command("watch")
def watch(
    ctx: typer.Context,
    profile: str | None = typer.Option(
        None,
        help="Use this durable monitoring profile; default uses the active profile when present.",
    ),
    folder: list[str] = typer.Option(
        [],
        "--folder",
        help="Override profile folders; defaults to the profile or Inbox.",
    ),
    forever: bool = typer.Option(False, help="Continue polling until interrupted."),
    notify_existing: bool | None = typer.Option(
        None,
        "--notify-existing/--quiet-baseline",
        help="Override the profile's first-run baseline policy.",
    ),
    console: bool = typer.Option(True, "--console/--no-console"),
    desktop: bool = typer.Option(False, help="Also request optional desktop notifications."),
    event_log: Path | None = typer.Option(
        None, help="Append redacted notification events to this NDJSON file."
    ),
    command_hook: str | None = typer.Option(
        None,
        help="Executable invoked directly for each event; no shell is used.",
    ),
    command_arg: list[str] = typer.Option([], "--command-arg"),
    webhooks: bool = typer.Option(
        True,
        "--webhooks/--no-webhooks",
        help="Dispatch events to explicitly enabled local webhook definitions.",
    ),
    strategy: str | None = typer.Option(
        None, help="Override detection: incremental_sync, incremental_poll, or bounded_poll."
    ),
    reset_checkpoint: bool = typer.Option(
        False, help="Reset only the selected local detection checkpoints; retain delivery history."
    ),
) -> None:
    """Monitor new messages using durable checkpoints and a quiet first baseline."""

    context = core.cli_state(ctx).context
    if strategy not in {None, "incremental_sync", "incremental_poll", "bounded_poll"}:
        raise typer.BadParameter("Unknown monitoring strategy.", param_hint="--strategy")
    configured = core.watcher_profile(ctx, profile)
    if profile is not None and configured is None:
        raise typer.BadParameter(f"Monitoring profile {profile!r} does not exist.")
    if configured is not None and not configured.enabled:
        raise typer.BadParameter(f"Monitoring profile {configured.profile_id!r} is disabled.")
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
    resolved_profile = configured.profile_id if configured is not None else profile or "default"
    bindings: list[ProviderBinding] = []
    outbound_providers: list[WebhookNotificationProvider] = []
    if console:
        stream = (
            sys.stderr
            if core.cli_state(ctx).writer.format is not OutputFormat.HUMAN
            else sys.stdout
        )
        bindings.append(ProviderBinding(ConsoleNotificationProvider(stream=stream)))
    if (
        desktop
        if desktop is not None
        else explicit_preference(watcher_settings(context), "monitoring.desktop_notifications")
    ):
        bindings.append(ProviderBinding(desktop_notification_provider(context)))
    if event_log is not None:
        bindings.append(ProviderBinding(NDJSONNotificationProvider(event_log)))
    if command_hook is not None:
        core.confirm(
            ctx,
            f"Run {command_hook!r} directly for every matching mailbox event?",
            f"command-hook:{command_hook}",
        )
        bindings.append(
            ProviderBinding(
                CommandHookProvider(
                    [command_hook, *command_arg],
                    enabled=True,
                )
            )
        )
    if webhooks:
        webhook_secrets = SecretStore("webhooks")
        for config in WebhookRegistry(
            context.state,
            secret_store=webhook_secrets,
        ).list():
            if not config.enabled:
                continue
            provider = WebhookNotificationProvider(
                config,
                state=context.state,
                secret_store=webhook_secrets,
            )
            outbound_providers.append(provider)
            bindings.append(ProviderBinding(provider))
    if not bindings:
        raise typer.BadParameter(
            "Enable at least one notification destination (console, desktop, event log, "
            "command hook, or webhook)."
        )

    monitor = MailboxMonitor(
        core.gateway(ctx),
        context.state,
        account_id=context.config.email or "configured-mailbox",
        dispatcher=core.profiled_notification_dispatcher(
            context,
            bindings,
            profile_id=configured.profile_id if configured is not None else None,
        ),
        folders=resolved_folders,
        poll_interval_seconds=poll_interval,
        limit=context.config.page_size,
        notify_existing=baseline_policy,
        profile_id=resolved_profile,
        strategy_preference=strategy or (configured.strategy_preference if configured else None),
    )

    try:
        if reset_checkpoint:
            monitor.reset_checkpoints()
        if forever:
            for result in monitor.iter_foreground():
                core.cli_state(ctx).writer.emit(result, command="watch", title="Mailbox watch")
        else:
            result = monitor.run_once()
            core.cli_state(ctx).writer.emit(result, command="watch", title="Mailbox watch")
            if not result.fully_succeeded:
                raise typer.Exit(code=int(ExitCode.PARTIAL))
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        core.report_error(ctx, exc, command="watch")
    finally:
        for provider in outbound_providers:
            provider.close()
