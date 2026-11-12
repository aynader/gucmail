"""Scriptable Typer command surface for GUC Mail CLI.

Importing this module is offline and side-effect free. Commands construct an application
context only when invoked, and only commands that explicitly need Exchange resolve a gateway.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import platform
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.markdown import Markdown

from gucmail import __version__
from gucmail.bootstrap import AppContext, build_context
from gucmail.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from gucmail.credentials import CredentialStore
from gucmail.dates import parse_datetime_expression
from gucmail.doctor import run_doctor, write_diagnostic_bundle
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ExitCode,
    GUCmailError,
    NetworkError,
)
from gucmail.ews.capabilities import DEFAULT_REGISTRY
from gucmail.headless import (
    HeadlessCycleResult,
    HeadlessWatcherRuntime,
    WatcherStatusStore,
    evaluate_watcher_health,
    run_headless_cycles,
    validate_headless_configuration,
)
from gucmail.help import HelpRepository, HelpRuntimeContext
from gucmail.help.cli_syntax import CliCommandSyntax
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    DraftData,
    Importance,
    NotificationEvent,
    SearchQuery,
    SendState,
    to_primitive,
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
from gucmail.output import OutputFormat, OutputWriter
from gucmail.palette import universal_palette_registry
from gucmail.secret_store import SecretStore
from gucmail.security import html_to_safe_text, redact_text, sanitize_terminal
from gucmail.services.attachment_indexing import MailboxIndexProducer, attachment_index_preferences
from gucmail.services.attachment_preferences import AttachmentPreferences
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.automatic_replies import (
    AutomaticReplyService,
    AutomaticReplyState,
    ExternalReplyAudience,
    parse_reply_time,
)
from gucmail.services.backup import (
    BackupKind,
    BackupOptions,
    BackupRequest,
    BackupSelection,
    BackupService,
    PrunePlan,
    RestorePlan,
    RestoreResult,
)
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    CalendarViewKind,
    InvitationPolicy,
    RecurrenceScope,
    calendar_event_search_text,
    detect_conflicts,
)
from gucmail.services.calendar_display import (
    CalendarDisplayPreferences,
    calendar_view_rows,
    event_display_data,
    load_calendar_view,
)
from gucmail.services.calendar_history import CalendarSeriesHistory, history_text
from gucmail.services.calendar_preferences import CalendarOperationPreferences
from gucmail.services.calendar_working_time import (
    CalendarWorkingTime,
    find_common_free_time,
    validate_free_time_request,
)
from gucmail.services.cleanup import CleanupPlanner
from gucmail.services.clusters import cluster_messages
from gucmail.services.compose import ComposeKind, ComposePlan, ComposeService, normalize_recipient
from gucmail.services.compose_preferences import BodyFormat, ComposerPreferences
from gucmail.services.contact_groups import (
    ContactGroupPlan,
    ContactGroupService,
    GroupSource,
    LocalContactGroups,
    validate_group,
)
from gucmail.services.contact_history import ContactHistoryIndex
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import (
    ContactPatch,
    ContactQuery,
    ContactsService,
)
from gucmail.services.contacts_display import ContactsDisplayPreferences
from gucmail.services.delegates import DelegateService, delegate_details_text
from gucmail.services.drafts import (
    DraftService,
    DraftSyncCoordinator,
    ServerDraftSnapshot,
)
from gucmail.services.folders import FolderService
from gucmail.services.gateway import DeferredGateway
from gucmail.services.inbox_rules import (
    InboxRuleOperation,
    InboxRulePlan,
    InboxRuleService,
    SimpleInboxRule,
    validate_simple_rule,
)
from gucmail.services.index import SearchIndex
from gucmail.services.insights import (
    analyze_mailbox,
    duplicate_insights,
    sender_insights,
    storage_insights,
)
from gucmail.services.layout_presets import LayoutPresetService, validate_name
from gucmail.services.mail_preferences import (
    DeleteBehavior,
    MailDeletionPreferences,
    MailResponsePreferences,
    MailSortPreferences,
    ReplyMode,
)
from gucmail.services.message_scan import scan_message_summaries
from gucmail.services.messages import MessageService
from gucmail.services.monitoring import MailboxMonitor, NotificationEventDispatcher
from gucmail.services.monitoring_preferences import (
    configured_watcher,
    effective_profile,
    explicit_preference,
    validate_direct_preferences,
    watcher_settings,
)
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    WatcherProfileConfiguration,
)
from gucmail.services.notification_simulation import NotificationSimulationService
from gucmail.services.notifications import (
    CommandHookProvider,
    ConsoleNotificationProvider,
    DeliveryState,
    DesktopNotificationProvider,
    NDJSONNotificationProvider,
    NotificationDispatcher,
    NotificationProvider,
    ProviderBinding,
)
from gucmail.services.ocr import OcrService
from gucmail.services.pim import PimPage
from gucmail.services.pim_preferences import (
    UNSET as PIM_UNSET,
)
from gucmail.services.pim_preferences import CalendarCreationPreferences, TaskCreationPreferences
from gucmail.services.profiled_notifications import ProfiledNotificationDispatcher
from gucmail.services.quota import QuotaService, quota_service_from_settings
from gucmail.services.recurring import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurringService,
    RecurringState,
)
from gucmail.services.scheduler import SchedulerService
from gucmail.services.search import fuzzy_rerank, parse_query
from gucmail.services.semantic import SemanticStore, account_identity
from gucmail.services.semantic_search import SemanticSearchService
from gucmail.services.send_attempts import SendAttemptStore
from gucmail.services.startup_runtime import (
    StartupPreview,
    apply_startup_preview,
    context_watcher_process,
    inspect_startup_snapshot,
)
from gucmail.services.task_recurrence import TaskRepeatKind, TaskRepeatPreferences
from gucmail.services.tasks import TaskPatch, TaskQuery, TaskService, TaskStatus
from gucmail.services.tasks_display import TaskDisplayPreferences, TaskSortOrder, TaskView
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.services.windows_desktop import (
    desktop_notification_provider,
    desktop_setup_service,
)
from gucmail.settings import SettingLevel, SettingsContext, SettingsService
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

T = TypeVar("T")

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}


class NaturalOrderGroup(typer.core.TyperGroup):
    """Keep the documented command order instead of alphabetizing it."""

    def list_commands(self, ctx: Any) -> list[str]:
        return list(self.commands)


app = typer.Typer(
    name="gucmail",
    help="Independent, security-focused terminal client for GUC Exchange mailboxes.",
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


folders_app = typer.Typer(help="Browse and safely manage Exchange folders.", cls=NaturalOrderGroup)
messages_app = typer.Typer(help="List, read, search, and organize messages.", cls=NaturalOrderGroup)
message_operations_app = typer.Typer(help="Review durable local message-operation outcomes.")
messages_app.add_typer(message_operations_app, name="operations")
drafts_app = typer.Typer(help="Create and recover durable local drafts.", cls=NaturalOrderGroup)
attachments_app = typer.Typer(help="List and safely download attachments.", cls=NaturalOrderGroup)
saved_searches_app = typer.Typer(
    help="Manage named local search definitions.", cls=NaturalOrderGroup
)
notifications_app = typer.Typer(help="Inspect or test local notification providers.")
notification_rules_app = typer.Typer(
    help="Create, inspect, test, and order deterministic notification rules.",
    cls=NaturalOrderGroup,
)
notification_profiles_app = typer.Typer(
    help="Manage named, account-scoped monitoring profiles.",
    cls=NaturalOrderGroup,
)
notification_providers_app = typer.Typer(
    help="Inspect and explicitly test configured notification destinations.",
    cls=NaturalOrderGroup,
)
desktop_notifications_app = typer.Typer(
    help="Inspect, preview and configure owned Windows desktop activation."
)
webhooks_app = typer.Typer(help="Manage disabled-by-default signed HTTPS webhooks.")
config_app = typer.Typer(help="Inspect or change non-secret configuration.")
settings_app = typer.Typer(help="Search, edit, import, export, and undo settings.")
layouts_app = typer.Typer(help="Save, preview, load, and remove named local layouts.")
help_app = typer.Typer(help="Browse the complete packaged offline Help center.")
cache_app = typer.Typer(help="Inspect or clear disposable local cache state.")
quota_app = typer.Typer(help="Show exact server quota when available and separate local capacity.")
ews_app = typer.Typer(help="Inspect the typed EWS capability registry.")
calendar_app = typer.Typer(help="Calendar views, events, invitations, and interchange.")
contacts_app = typer.Typer(help="Mailbox contacts, duplicates, and vCard interchange.")
automatic_replies_app = typer.Typer(help="Inspect and review Exchange automatic replies.")
inbox_rules_app = typer.Typer(
    help="Inspect and review Exchange inbox rules; preserve Outlook rules."
)
contact_groups_app = typer.Typer(help="Account-local and personal mailbox contact groups.")
directory_app = typer.Typer(help="Search the Exchange directory when account access permits it.")
recipients_app = typer.Typer(help="Inspect privacy-local recent and frequent correspondents.")
tasks_app = typer.Typer(help="Exchange task lifecycle and export.")
scheduled_app = typer.Typer(help="Durable local scheduled-send queue.")
scheduler_app = typer.Typer(help="Control and diagnose the durable local scheduler worker.")
headless_app = typer.Typer(help="Validate and inspect TUI-free watcher operation.")
startup_app = typer.Typer(help="Preview safe watcher startup and service-manager integration.")
recurring_app = typer.Typer(help="Opt-in recurring message automation.")
backup_app = typer.Typer(help="Versioned, verifiable mailbox backup and offline browsing.")
index_app = typer.Typer(help="Local full-text index management.")
ocr_app = typer.Typer(help="Optional local image OCR with installed Windows languages.")
semantic_app = typer.Typer(help="Inspect optional semantic-search availability and privacy status.")
clusters_app = typer.Typer(help="Deterministic mailbox cluster exploration.")
insights_app = typer.Typer(help="Read-only storage and sender analysis.")
cleanup_app = typer.Typer(help="Preview-first, recoverable mailbox cleanup workflows.")

for name, group in (
    ("folders", folders_app),
    ("messages", messages_app),
    ("drafts", drafts_app),
    ("attachments", attachments_app),
    ("saved-searches", saved_searches_app),
    ("notifications", notifications_app),
    ("webhooks", webhooks_app),
    ("config", config_app),
    ("settings", settings_app),
    ("layouts", layouts_app),
    ("help", help_app),
    ("cache", cache_app),
    ("quota", quota_app),
    ("ews", ews_app),
    ("calendar", calendar_app),
    ("contacts", contacts_app),
    ("automatic-replies", automatic_replies_app),
    ("inbox-rules", inbox_rules_app),
    ("contact-groups", contact_groups_app),
    ("directory", directory_app),
    ("recipients", recipients_app),
    ("tasks", tasks_app),
    ("scheduled", scheduled_app),
    ("scheduler", scheduler_app),
    ("headless", headless_app),
    ("startup", startup_app),
    ("recurring", recurring_app),
    ("backup", backup_app),
    ("index", index_app),
    ("ocr", ocr_app),
    ("semantic", semantic_app),
    ("clusters", clusters_app),
    ("insights", insights_app),
    ("cleanup", cleanup_app),
):
    app.add_typer(group, name=name)

notifications_app.add_typer(notification_rules_app, name="rules")
notifications_app.add_typer(notification_profiles_app, name="profiles")
notifications_app.add_typer(notification_providers_app, name="providers")
notifications_app.add_typer(desktop_notifications_app, name="desktop")


@automatic_replies_app.command("show")
def automatic_replies_show(ctx: typer.Context) -> None:
    """Read the current policy without changing it or enabling replies."""
    _run(
        ctx,
        "automatic-replies show",
        lambda: AutomaticReplyService(_gateway(ctx)).get(),
        title="Automatic replies",
    )


@automatic_replies_app.command("set")
def automatic_replies_set(
    ctx: typer.Context,
    state: AutomaticReplyState = typer.Option(..., "--state"),
    external_audience: ExternalReplyAudience | None = typer.Option(None),
    internal_reply_file: Path | None = typer.Option(None, exists=True, dir_okay=False),
    external_reply_file: Path | None = typer.Option(None, exists=True, dir_okay=False),
    start: str | None = typer.Option(None, help="Scheduled ISO start with explicit UTC offset."),
    end: str | None = typer.Option(None, help="Scheduled ISO end with explicit UTC offset."),
    dry_run: bool = typer.Option(False, help="Show the exact policy without applying it."),
) -> None:
    """Change only supplied policy fields; preview and confirmation are required."""

    def action() -> Any:
        changes: dict[str, Any] = {"state": state}
        for name, path in (
            ("internal_reply", internal_reply_file),
            ("external_reply", external_reply_file),
        ):
            if path is not None:
                with path.open("rb") as stream:
                    data = stream.read(128 * 1024 + 1)
                if len(data) > 128 * 1024:
                    raise ConfigurationError("Each automatic reply is limited to 128 KiB.")
                try:
                    changes[name] = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ConfigurationError("Automatic-reply files must use UTF-8.") from exc
        for name, value in (
            ("external_audience", external_audience),
            ("start", parse_reply_time(start)),
            ("end", parse_reply_time(end)),
        ):
            if value is not None:
                changes[name] = value
        service = AutomaticReplyService(_gateway(ctx))
        current = service.get()
        plan = service.plan(replace(current, **changes), current=current)
        if dry_run:
            return {"dry_run": True, "plan": plan}
        if _state(ctx).output_format is OutputFormat.HUMAN:
            _state(ctx).writer.emit(
                plan, command="automatic-replies preview", title="Review policy"
            )
        _confirm(
            ctx,
            f"Set automatic replies to {plan.after.state.value}; external audience "
            f"{plan.after.external_audience.value}? Incoming mail may receive these replies.",
            "automatic-replies:" + plan.after.fingerprint,
        )
        return service.apply(plan, confirmed=True)

    _run(ctx, "automatic-replies set", action, title="Automatic replies")


@inbox_rules_app.command("list")
def inbox_rules_list(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=1000),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Read native policies. EWS returns the whole rule collection in one bounded response."""
    _run(
        ctx,
        "inbox-rules list",
        lambda: InboxRuleService(_gateway(ctx)).list(limit=limit, offset=offset),
        title="Exchange inbox rules",
    )


@inbox_rules_app.command("show")
def inbox_rules_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Read conditions, exceptions and actions for one exact rule ID."""
    _run(ctx, "inbox-rules show", lambda: InboxRuleService(_gateway(ctx)).get(reference))


def _apply_inbox_rule_plan(
    ctx: typer.Context, service: InboxRuleService, plan: InboxRulePlan, *, dry_run: bool
) -> Any:
    if dry_run:
        return {"dry_run": True, "plan": plan}
    if _state(ctx).output_format is OutputFormat.HUMAN:
        _state(ctx).writer.emit(plan, command="inbox-rules preview", title="Review exact policy")
    name = plan.creation.name if plan.creation else plan.before.name if plan.before else "rule"
    detail = (
        "New rules start disabled; enable separately after review."
        if plan.operation is InboxRuleOperation.CREATE
        else "Enabled rules can change incoming mail. Outlook rule data will be preserved."
    )
    fingerprint = plan.before.fingerprint if plan.before else plan.inventory_fingerprint
    _confirm(
        ctx,
        f"{plan.operation.value.title()} Exchange rule {sanitize_terminal(name)}? {detail}",
        f"inbox-rule:{plan.operation.value}:{fingerprint}",
    )
    result = service.apply(plan, confirmed=True)
    return (
        result
        if result is not None
        else {"deleted": plan.before.reference if plan.before else None}
    )


@inbox_rules_app.command("create")
def inbox_rules_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    priority: int = typer.Option(0, min=0, max=10000),
    sender_contains: list[str] | None = typer.Option(None),
    subject_contains: list[str] | None = typer.Option(None),
    has_attachments: bool | None = typer.Option(None, "--has-attachments/--no-attachments"),
    category: list[str] | None = typer.Option(None),
    mark_read: bool = typer.Option(False),
    move_to_folder: str | None = typer.Option(None),
    stop_processing: bool = typer.Option(False),
    dry_run: bool = typer.Option(False),
) -> None:
    """Create a disabled simple rule. Values within a condition are OR; conditions are AND."""

    def action() -> Any:
        request = validate_simple_rule(
            SimpleInboxRule(
                name,
                priority,
                tuple(sender_contains or ()),
                tuple(subject_contains or ()),
                has_attachments,
                tuple(category or ()),
                mark_read,
                move_to_folder,
                stop_processing,
            )
        )
        service = InboxRuleService(_gateway(ctx))
        return _apply_inbox_rule_plan(ctx, service, service.plan_create(request), dry_run=dry_run)

    _run(ctx, "inbox-rules create", action)


def _change_inbox_rule(
    ctx: typer.Context, reference: str, operation: InboxRuleOperation, *, dry_run: bool
) -> None:
    def action() -> Any:
        service = InboxRuleService(_gateway(ctx))
        return _apply_inbox_rule_plan(
            ctx, service, service.plan(reference, operation), dry_run=dry_run
        )

    _run(ctx, f"inbox-rules {operation.value}", action)


@inbox_rules_app.command("enable")
def inbox_rules_enable(
    ctx: typer.Context, reference: str = typer.Argument(...), dry_run: bool = typer.Option(False)
) -> None:
    """Enable one reviewed policy without changing its conditions or actions."""
    _change_inbox_rule(ctx, reference, InboxRuleOperation.ENABLE, dry_run=dry_run)


@inbox_rules_app.command("disable")
def inbox_rules_disable(
    ctx: typer.Context, reference: str = typer.Argument(...), dry_run: bool = typer.Option(False)
) -> None:
    """Disable one reviewed policy without changing its conditions or actions."""
    _change_inbox_rule(ctx, reference, InboxRuleOperation.DISABLE, dry_run=dry_run)


@inbox_rules_app.command("delete")
def inbox_rules_delete(
    ctx: typer.Context, reference: str = typer.Argument(...), dry_run: bool = typer.Option(False)
) -> None:
    """Delete exactly one rule. Never temporarily enable a disabled rule to delete it."""
    _change_inbox_rule(ctx, reference, InboxRuleOperation.DELETE, dry_run=dry_run)


def _contact_group_service(ctx: typer.Context, source: GroupSource) -> ContactGroupService:
    return ContactGroupService(
        LocalContactGroups(_state(ctx).context.state)
        if source is GroupSource.LOCAL
        else _gateway(ctx)
    )


@contact_groups_app.command("list")
def contact_groups_list(
    ctx: typer.Context,
    source: GroupSource = typer.Option(GroupSource.LOCAL),
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List a bounded group page. Default local groups require no mailbox connection."""
    _run(
        ctx,
        "contact-groups list",
        lambda: _contact_group_service(ctx, source).list(limit=limit, offset=offset),
    )


@contact_groups_app.command("show")
def contact_groups_show(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
) -> None:
    """Read group identity, recipients and unsupported-member status."""
    _run(ctx, "contact-groups show", lambda: _contact_group_service(ctx, source).get(reference))


@contact_groups_app.command("recipients")
def contact_groups_recipients(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
) -> None:
    """Expand reviewed SMTP recipients for scripting. Does not compose or send mail."""
    _run(
        ctx,
        "contact-groups recipients",
        lambda: _contact_group_service(ctx, source).recipients(reference),
    )


def _apply_contact_group_plan(
    ctx: typer.Context, service: ContactGroupService, plan: ContactGroupPlan, *, dry_run: bool
) -> Any:
    if dry_run:
        return {"dry_run": True, "plan": plan}
    if _state(ctx).output_format is OutputFormat.HUMAN:
        _state(ctx).writer.emit(
            plan, command="contact-groups preview", title="Review group and recipients"
        )
    _confirm(
        ctx,
        f"{'Delete' if plan.delete else 'Save'} this exact contact group and membership?",
        "contact-group:" + (plan.before.fingerprint if plan.before else plan.name),
    )
    result = service.apply(plan, confirmed=True)
    return (
        result
        if result is not None
        else {"deleted": plan.before.reference if plan.before else None}
    )


@contact_groups_app.command("create")
def contact_groups_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    member: list[str] | None = typer.Option(None),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
    dry_run: bool = typer.Option(False),
) -> None:
    """Create a group of explicit SMTP members. No directory lookup or email is sent."""

    def action() -> Any:
        checked_name, members = validate_group(
            name, tuple(normalize_recipient(value) for value in member or ())
        )
        service = _contact_group_service(ctx, source)
        return _apply_contact_group_plan(
            ctx, service, service.plan(checked_name, members), dry_run=dry_run
        )

    _run(ctx, "contact-groups create", action)


@contact_groups_app.command("edit")
def contact_groups_edit(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    name: str | None = typer.Option(None),
    member: list[str] | None = typer.Option(
        None, help="Replace the complete membership with these explicit addresses."
    ),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preserve omitted fields; --member replaces recipients and must be reviewed."""

    def action() -> Any:
        members = (
            tuple(normalize_recipient(value) for value in member) if member is not None else None
        )
        if name is None and members is None:
            raise ConfigurationError("Supply a name or explicit replacement members.")
        if name is not None:
            validate_group(name, members or ())
        service = _contact_group_service(ctx, source)
        before = service.get(reference)
        plan = service.plan(
            name if name is not None else before.name,
            members if members is not None else before.members,
            before=before,
        )
        return _apply_contact_group_plan(ctx, service, plan, dry_run=dry_run)

    _run(ctx, "contact-groups edit", action)


@contact_groups_app.command("delete")
def contact_groups_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
    dry_run: bool = typer.Option(False),
) -> None:
    """Delete local membership, or move a personal mailbox group to Deleted Items."""

    def action() -> Any:
        service = _contact_group_service(ctx, source)
        return _apply_contact_group_plan(
            ctx, service, service.plan_delete(service.get(reference)), dry_run=dry_run
        )

    _run(ctx, "contact-groups delete", action)


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


def _state(ctx: typer.Context) -> CLIState:
    value = ctx.obj
    if not isinstance(value, CLIState):
        raise RuntimeError("CLI context was not initialized")
    return value


def _iso_datetime(value: str | None, *, parameter: str) -> datetime | None:
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


def _search_bound(ctx: typer.Context, value: str | None, *, parameter: str) -> datetime | None:
    """Parse a search date bound; a bare YYYY-MM-DD means local midnight.

    The documented ``gucmail search --after 2026-01-01`` form names a calendar day,
    which is unambiguous once anchored to the configured display time zone. Full
    timestamps keep the strict offset requirement of :func:`_iso_datetime`.
    """

    if value is not None and len(value.strip()) == 10:
        try:
            day = date.fromisoformat(value.strip())
        except ValueError:
            day = None
        if day is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            zone_name = _state(ctx).context.config.timezone
            try:
                zone = ZoneInfo(zone_name)
            except ZoneInfoNotFoundError as exc:
                raise typer.BadParameter(
                    f"configured time zone {zone_name!r} is unknown", param_hint=parameter
                ) from exc
            return datetime(day.year, day.month, day.day, tzinfo=zone)
    return _iso_datetime(value, parameter=parameter)


def _iso_date(value: str | None, *, parameter: str) -> date | None:
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


def _error(ctx: typer.Context, error: BaseException, *, command: str) -> None:
    state = _state(ctx)
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


def _run(
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
            if human_data is not None and _state(ctx).writer.format is OutputFormat.HUMAN
            else result
        )
    except typer.BadParameter:
        raise
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        _error(ctx, exc, command=command)
        return None
    state = _state(ctx)
    resolved_warnings = list(warnings)
    if state._context is not None:
        resolved_warnings.extend(state._context.credential_warnings)
    _state(ctx).writer.emit(
        presentation,
        command=command,
        title=resolved_title,
        warnings=list(dict.fromkeys(resolved_warnings)),
        source=source,
    )
    return result


def _gateway(ctx: typer.Context, *, interactive: bool | None = None) -> Any:
    state = _state(ctx)
    allow_prompt = not state.non_interactive if interactive is None else interactive
    return state.context.gateway(interactive=allow_prompt)


def _draft_service(ctx: typer.Context) -> DraftService:
    context = _state(ctx).context
    return DraftService(
        context.state,
        context.paths.recovery_dir,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )


def _scheduler(ctx: typer.Context) -> SchedulerService:
    context = _state(ctx).context

    def sender(draft: DraftData, **_kwargs: Any) -> Any:
        return _gateway(ctx).send(draft)

    return SchedulerService(
        context.state,
        _draft_service(ctx),
        sender,
        timezone=context.config.timezone,
        retry_attempts=context.config.retry_attempts,
        retry_base_seconds=context.config.retry_base_seconds,
        retry_max_seconds=context.config.retry_max_seconds,
        maximum_attachment_bytes=context.config.max_attachment_bytes,
    )


def _confirm(ctx: typer.Context, summary: str, scope: str, *, required: bool = True) -> str | None:
    if not required:
        return scope
    state = _state(ctx)
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


def _addresses(values: Iterable[str]) -> list[Address]:
    return [Address(email=value.strip()) for value in values if value.strip()]


def _labeled_options(values: Iterable[str], *, parameter: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for candidate in values:
        if "=" not in candidate:
            raise typer.BadParameter("expected label=value", param_hint=parameter)
        label, value = (part.strip() for part in candidate.split("=", 1))
        key = label.casefold()
        if not key or not value or key in result:
            raise typer.BadParameter(
                "labels and values must be non-empty and labels unique",
                param_hint=parameter,
            )
        result[key] = value
    return result


def _body_value(body: str | None, body_file: Path | None) -> str:
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
        to=_addresses(to),
        cc=_addresses(cc),
        bcc=_addresses(bcc),
        subject=subject,
        body_text=body,
        attachment_paths=[Path(item) for item in attachments],
        importance=importance,
    )


def _new_composition(
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
    preferences = ComposerPreferences.from_settings(_settings_service(ctx))
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


def _compose_review(plan: ComposePlan) -> str:
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


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the installed application and output-schema versions."""

    _run(
        ctx,
        "version",
        lambda: {"version": __version__, "output_schema": "gucmail.output/v1"},
        title="GUC Mail CLI",
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
        state = _state(ctx)
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

    _run(ctx, "login", action, title="Login")


@app.command()
def logout(
    ctx: typer.Context,
    forget_account: bool = typer.Option(
        False, help="Also remove the non-secret email and username from configuration."
    ),
) -> None:
    """Remove the current mailbox credential from the OS keyring."""

    def action() -> dict[str, Any]:
        context = _state(ctx).context
        email = context.config.email
        removed = CredentialStore().delete(email) if email else False
        context.close()
        if forget_account:
            updated = replace(context.config, email=None, username=None)
            context.config_store.save(updated)
            context.config = updated
        return {"credential_removed": removed, "account_forgotten": forget_account}

    _run(ctx, "logout", action, title="Logout")


@app.command()
def status(
    ctx: typer.Context,
    online: bool = typer.Option(
        False, "--online", help="Make one bounded authenticated mailbox-status read."
    ),
) -> None:
    """Show local status; use --online for an intentional EWS check."""

    def action() -> dict[str, Any]:
        context = _state(ctx).context
        result = context.local_status()
        if online:
            result["exchange"] = context.gateway(
                interactive=not _state(ctx).non_interactive
            ).status()
            result["credential"] = {
                "source": context.credential_source,
                "plaintext_file_selected": context.credential_source == "legacy_file_explicit",
            }
        else:
            result["exchange"] = "not_probed"
        return result

    _run(ctx, "status", action, title="Status", source="live_server" if online else "local")


@app.command()
def doctor(
    ctx: typer.Context,
    network: bool = typer.Option(False, help="Test DNS and TLS without authenticating."),
    authenticate: bool = typer.Option(
        False, help="Make one bounded authenticated mailbox-status read."
    ),
) -> None:
    """Run safe local diagnostics; network and authentication are explicit opt-ins."""

    _run(
        ctx,
        "doctor",
        lambda: run_doctor(
            _state(ctx).context, network=network or authenticate, authenticate=authenticate
        ),
        title="Doctor",
    )


@app.command()
def diagnostics(
    ctx: typer.Context,
    output: Path = typer.Option(Path("gucmail-diagnostics.json"), help="Sanitized bundle path."),
    network: bool = typer.Option(False, help="Include DNS and TLS checks."),
) -> None:
    """Write a redacted diagnostic bundle with no credentials or mailbox content."""

    def action() -> dict[str, Any]:
        context = _state(ctx).context
        checks = run_doctor(context, network=network, authenticate=False)
        path = write_diagnostic_bundle(context, output.expanduser(), results=checks)
        return {"path": str(path), "checks": len(checks), "contains_mailbox_data": False}

    _run(ctx, "diagnostics", action, title="Diagnostics")


@app.command("tui")
def tui_command(
    ctx: typer.Context,
    offline: bool = typer.Option(False, help="Launch without resolving credentials or Exchange."),
) -> None:
    """Launch the responsive Textual interface."""

    try:
        from gucmail.tui import run_tui

        state = _state(ctx)
        if state.credentials_file is not None and not offline:
            state.writer.warn(
                "An explicitly selected plaintext credential file will be used for this "
                "TUI session. Its values will not be displayed or stored in normal settings."
            )
        run_tui(context=state.context, offline=offline)
    except BaseException as exc:
        _error(ctx, exc, command="tui")


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
        preferences = ComposerPreferences.from_settings(_settings_service(ctx))
        draft = _new_composition(
            ctx,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=_body_value(body, body_file),
            attachments=attach,
            importance=importance,
            body_format=body_format,
            read_receipt=read_receipt,
            delivery_receipt=delivery_receipt,
            signature_enabled=signature,
            save_sent_copy=save_sent_copy,
        )
        context = _state(ctx).context
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
            _confirm(
                ctx,
                f"Queue {len(plan.draft.to) + len(plan.draft.cc) + len(plan.draft.bcc)} "
                f"recipient(s) for {when.isoformat()}? A scheduler must be running to send.\n"
                + _compose_review(plan),
                plan.confirmation_scope + ":" + when.isoformat(),
            )
            local = _draft_service(ctx).create(
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
            return _scheduler(ctx).schedule_at(
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
        scope = _confirm(
            ctx,
            f"Send to {len(plan.draft.to) + len(plan.draft.cc) + len(plan.draft.bcc)} "
            "recipient(s)?\n" + _compose_review(plan),
            plan.confirmation_scope,
        )
        service.gateway = _gateway(ctx)
        result = service.execute_send(plan, confirmation_scope=scope)
        _remember_sent_composition(ctx, result)
        return result

    _run(ctx, "send", action, title="Send preview" if dry_run else "Send")


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
        settings = _settings_service(ctx)
        preferences = ComposerPreferences.from_settings(settings)
        response_preferences = MailResponsePreferences.from_settings(settings)
        service = ComposeService(
            _gateway(ctx),
            max_attachment_bytes=_state(ctx).context.config.max_attachment_bytes,
            state=_state(ctx).context.state,
            preferences=preferences,
            response_preferences=response_preferences,
        )
        if kind is ComposeKind.FORWARD:
            preview = service.preview_forward(
                reference, recipients=_addresses(to), include_history=include_history
            )
        else:
            configured_email = _state(ctx).context.config.email
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
        supplied = _body_value(body, body_file)
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
            else _confirm(
                ctx,
                f"{plan.kind.value.replace('_', ' ').title()} to "
                f"{len(plan.draft.to) + len(plan.draft.cc)} reviewed recipient(s)?\n"
                + _compose_review(plan),
                plan.confirmation_scope,
            )
        )
        result = service.execute_send(plan, dry_run=dry_run, confirmation_scope=scope)
        _remember_sent_composition(ctx, result)
        return result

    _run(ctx, kind.value.replace("_", "-"), action, title=kind.value.replace("_", " ").title())


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


@folders_app.command("list")
def folders_list(
    ctx: typer.Context,
    parent: str | None = typer.Option(None, help="Parent path or local folder reference."),
    limit: int = typer.Option(25, min=1, max=500),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List one bounded page of child folders."""

    _run(
        ctx,
        "folders list",
        lambda: FolderService(_gateway(ctx)).list_folders(
            parent_reference=parent, limit=limit, offset=offset
        ),
        title="Folders",
        source="live_server",
    )


@folders_app.command("tree")
def folders_tree(
    ctx: typer.Context,
    root: str | None = typer.Option(None, help="Start at this local folder reference."),
    depth: int = typer.Option(3, min=1, max=20),
    limit: int = typer.Option(500, min=1, max=2000, help="Maximum total nodes."),
) -> None:
    """Render a bounded folder hierarchy without forcing a full mailbox scan."""

    def action() -> list[dict[str, Any]]:
        service = FolderService(_gateway(ctx))
        rows: list[dict[str, Any]] = []

        def visit(parent: str | None, level: int) -> None:
            if level >= depth or len(rows) >= limit:
                return
            offset = 0
            while len(rows) < limit:
                page = service.list_folders(
                    parent_reference=parent,
                    limit=min(MAX_PAGE_SIZE, limit - len(rows)),
                    offset=offset,
                )
                for folder in page.items:
                    rows.append(
                        {
                            "depth": level,
                            "name": folder.name,
                            "path": folder.path,
                            "reference": folder.reference,
                            "unread": folder.unread_count,
                            "items": folder.total_count,
                        }
                    )
                    if len(rows) >= limit:
                        break
                    if folder.child_count is None or folder.child_count > 0:
                        visit(folder.reference, level + 1)
                if not page.has_more or page.next_offset is None or not page.items:
                    break
                offset = page.next_offset

        visit(root, 0)
        return rows

    _run(ctx, "folders tree", action, title="Folder tree", source="live_server")


@folders_app.command("create")
def folders_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New folder display name."),
    parent: str | None = typer.Option(None, help="Parent folder path or reference."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create one folder."""

    def action() -> Any:
        service = FolderService(_gateway(ctx))
        plan = service.plan_create(name, parent=parent)
        scope = (
            None
            if dry_run
            else _confirm(ctx, plan.summary, plan.confirmation_scope, required=False)
        )
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    _run(ctx, "folders create", action, title="Folder creation")


def _folder_mutation(
    ctx: typer.Context,
    command: str,
    planner: Callable[[FolderService], Any],
    *,
    dry_run: bool,
) -> None:
    def action() -> Any:
        service = FolderService(_gateway(ctx))
        plan = planner(service)
        scope = None if dry_run else _confirm(ctx, plan.summary, plan.confirmation_scope)
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    _run(ctx, command, action, title=command.title())


@folders_app.command("rename")
def folders_rename(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    name: str = typer.Argument(..., help="New display name."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and rename one folder using an explicit update field."""

    _folder_mutation(
        ctx,
        "folders rename",
        lambda service: service.plan_rename(reference, name),
        dry_run=dry_run,
    )


@folders_app.command("move")
def folders_move(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Folder path or local reference to move."),
    destination: str = typer.Argument(
        ...,
        help="Destination parent folder path or local reference.",
    ),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and move one folder below another folder."""

    _folder_mutation(
        ctx,
        "folders move",
        lambda service: service.plan_move(reference, destination),
        dry_run=dry_run,
    )


@folders_app.command("delete")
def folders_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(False, help="Irreversible hard deletion; expert use only."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move a folder to Deleted Items, or explicitly request permanent deletion."""

    _folder_mutation(
        ctx,
        "folders delete",
        lambda service: service.plan_delete(reference, permanent=permanent),
        dry_run=dry_run,
    )


@folders_app.command("empty")
def folders_empty(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(False, help="Irreversibly remove the folder contents."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview the exact folder scope before emptying it."""

    _folder_mutation(
        ctx,
        "folders empty",
        lambda service: service.plan_empty(reference, permanent=permanent),
        dry_run=dry_run,
    )


@messages_app.command("list")
def messages_list(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(25, min=1, max=MAX_PAGE_SIZE),
    offset: int = typer.Option(0, min=0),
    order_by: str | None = typer.Option(None, help="Override the saved message sort order."),
) -> None:
    """List a bounded page of lightweight message summaries."""

    def action() -> Any:
        order = (
            order_by
            if order_by is not None
            else MailSortPreferences.from_settings(_settings_service(ctx)).order_by
        )
        # Reject an invalid explicit order before resolving credentials.
        MessageService.validate_order(order)
        return MessageService(_gateway(ctx)).list_messages(
            folder, limit=limit, offset=offset, order_by=order
        )

    _run(
        ctx,
        "messages list",
        action,
        title=f"Messages: {folder}",
        source="live_server",
    )


@messages_app.command("show")
def messages_show(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    mark_read: bool = typer.Option(False, help="Explicitly mark read after fetching."),
) -> None:
    """Read one message as sanitized text; remote content is never loaded."""

    _run(
        ctx,
        "messages show",
        lambda: MessageService(_gateway(ctx)).show_message(reference, mark_read=mark_read),
        title="Message",
        source="live_server",
        human_data=_message_detail_human,
    )


def _message_detail_human(detail: Any) -> dict[str, Any]:
    """Flatten a message for the human reader; --json keeps the full nested record."""

    primitive = to_primitive(detail)
    summary = primitive.pop("summary", None) or {}
    flattened: dict[str, Any] = {
        "reference": summary.get("reference"),
        "subject": summary.get("subject"),
        "from": summary.get("sender"),
        "received_at": summary.get("received_at"),
        "sent_at": summary.get("sent_at"),
        "folder": summary.get("folder_path"),
        "is_read": summary.get("is_read"),
        "flagged": summary.get("flagged"),
        "importance": summary.get("importance"),
        "categories": summary.get("categories"),
        "size": summary.get("size"),
        "conversation_id": summary.get("conversation_id"),
    }
    if primitive.get("body_text"):
        primitive.pop("body_html", None)
    flattened.update(primitive)
    return flattened


@messages_app.command("headers")
def messages_headers(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show bounded parsed Internet headers for one message."""

    _run(
        ctx,
        "messages headers",
        lambda: MessageService(_gateway(ctx)).show_message(reference).raw_headers,
        title="Message headers",
    )


@messages_app.command("raw")
def messages_raw(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    output: Path = typer.Option(..., help="Destination .eml file; never printed to the terminal."),
    overwrite: bool = typer.Option(False),
) -> None:
    """Export server-provided MIME bytes to a local file."""

    def action() -> dict[str, Any]:
        target = output.expanduser().resolve(strict=False)
        if target.exists() and not overwrite:
            raise ConfirmationRequiredError("The output exists; use --overwrite and --yes.")
        if target.exists():
            _confirm(ctx, f"Overwrite {target}?", f"raw:{target}")
        data = _gateway(ctx).raw_message(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": str(target), "bytes": len(data), "remote_content_loaded": False}

    _run(ctx, "messages raw", action, title="Raw message export")


def _search_query(
    *,
    text: str | None,
    sender: str | None,
    recipient: str | None,
    subject: str | None,
    after: datetime | None,
    before: datetime | None,
    has_attachments: bool | None,
    is_read: bool | None,
    flagged: bool | None,
    folder: str | None,
    mailbox_scope: bool = False,
) -> SearchQuery:
    return SearchQuery(
        text=text,
        sender=sender,
        recipient=recipient,
        subject=subject,
        after=after,
        before=before,
        has_attachments=has_attachments,
        is_read=is_read,
        flagged=flagged,
        folder=folder,
        mailbox_scope=mailbox_scope,
    )


@messages_app.command("search")
@app.command("search")
def messages_search(
    ctx: typer.Context,
    text: str | None = typer.Argument(None, help="Simple text criterion."),
    sender: str | None = typer.Option(None),
    recipient: str | None = typer.Option(None),
    subject: str | None = typer.Option(None),
    after: str | None = typer.Option(
        None, help="YYYY-MM-DD (local midnight) or an ISO-8601 timestamp with an offset."
    ),
    before: str | None = typer.Option(
        None, help="YYYY-MM-DD (local midnight) or an ISO-8601 timestamp with an offset."
    ),
    has_attachments: bool | None = typer.Option(None, "--has-attachments/--no-attachments"),
    is_read: bool | None = typer.Option(None, "--read/--unread"),
    flagged: bool | None = typer.Option(None, "--flagged/--unflagged"),
    folder: str | None = typer.Option(None),
    all_folders: bool = typer.Option(
        False,
        "--all-folders",
        help="Search all mail folders; up to 50 descendants and a 200-result window.",
    ),
    local: bool = typer.Option(False, help="Search the disposable local full-text index."),
    fuzzy: bool = typer.Option(False, help="Use bounded local typo-tolerant ranking."),
    fuzzy_threshold: int = typer.Option(82, min=0, max=100),
    limit: int = typer.Option(25, min=1, max=500),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Run a bounded server search; use the local index for deep/full-text queries."""

    if all_folders and (folder is not None or local or fuzzy):
        raise typer.BadParameter(
            "--all-folders cannot be combined with --folder, --local, or --fuzzy."
        )
    query = _search_query(
        text=text,
        sender=sender,
        recipient=recipient,
        subject=subject,
        after=_search_bound(ctx, after, parameter="--after"),
        before=_search_bound(ctx, before, parameter="--before"),
        has_attachments=has_attachments,
        is_read=is_read,
        flagged=flagged,
        folder=folder,
        mailbox_scope=all_folders,
    )
    if local or fuzzy:
        if not text:
            raise typer.BadParameter(
                "local and fuzzy search require a text query",
                param_hint="text",
            )
        _run(
            ctx,
            "messages search",
            lambda: SearchIndex(_state(ctx).context.paths.index_database).search(
                text,
                folder=folder,
                fuzzy=fuzzy,
                fuzzy_threshold=fuzzy_threshold,
                limit=limit,
                offset=offset,
            ),
            title="Local search results",
            source="search_index",
        )
        return
    _run(
        ctx,
        "messages search",
        lambda: MessageService(_gateway(ctx)).search_messages(query, limit=limit, offset=offset),
        title="Search results",
        source="live_server",
    )


def _message_mutation(
    ctx: typer.Context,
    command: str,
    planner: Callable[[MessageService], Any],
    *,
    dry_run: bool,
) -> None:
    def action() -> Any:
        service = MessageService(
            DeferredGateway(lambda: _gateway(ctx)), state=_state(ctx).context.state
        )
        plan = planner(service)
        scope = (
            None
            if dry_run
            else _confirm(
                ctx, plan.summary, plan.confirmation_scope, required=plan.requires_confirmation
            )
        )
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    _run(ctx, command, action, title=command.title())


@message_operations_app.command("list")
def message_operations_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List local operation records without connecting to Exchange."""
    _run(
        ctx,
        "messages operations list",
        lambda: _state(ctx).context.state.list_message_operations(limit=limit, offset=offset),
        title="Message operation history",
        source="local_state",
    )


@message_operations_app.command("show")
def message_operations_show(ctx: typer.Context, operation_id: str = typer.Argument(...)) -> None:
    """Show known/uncertain per-target effects; never replay a write."""

    def action() -> Any:
        result = _state(ctx).context.state.get_message_operation(operation_id)
        if result is None:
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError("The local message operation was not found.")
        return result

    _run(
        ctx,
        "messages operations show",
        action,
        title="Message operation outcomes",
        source="local_state",
    )


@messages_app.command("move")
def messages_move(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    destination: str = typer.Option(..., "--to"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and move exactly the selected messages."""

    _message_mutation(
        ctx,
        "messages move",
        lambda service: service.plan_move(references, destination),
        dry_run=dry_run,
    )


@messages_app.command("copy")
def messages_copy(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    destination: str = typer.Option(..., "--to"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and copy exactly the selected messages."""

    _message_mutation(
        ctx,
        "messages copy",
        lambda service: service.plan_copy(references, destination),
        dry_run=dry_run,
    )


@messages_app.command("delete")
def messages_delete(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    permanent: bool = typer.Option(False, help="Irreversible hard deletion."),
    behavior: DeleteBehavior | None = typer.Option(
        None, "--behavior", help="Override the saved ordinary Delete destination behavior."
    ),
    destination: str | None = typer.Option(None, "--to", help="Move to this folder instead."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Review the saved Delete destination; permanent deletion must be explicit."""

    def planner(service: MessageService) -> Any:
        if permanent:
            if behavior is not None or destination is not None:
                raise ConfigurationError("Permanent deletion cannot override a move destination.")
            return service.plan_delete(references, permanent=True)
        preference = MailDeletionPreferences.from_settings(_settings_service(ctx))
        return service.plan_delete(
            references,
            destination=preference.destination_for(behavior=behavior, folder=destination),
        )

    _message_mutation(
        ctx,
        "messages delete",
        planner,
        dry_run=dry_run,
    )


@messages_app.command("restore")
def messages_restore(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    destination: str = typer.Option("inbox", "--to"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Restore selected Deleted Items to an explicit destination."""

    _message_mutation(
        ctx,
        "messages restore",
        lambda service: service.plan_move(references, destination),
        dry_run=dry_run,
    )


def _read_flag_command(
    ctx: typer.Context,
    command: str,
    references: Sequence[str],
    *,
    read: bool | None = None,
    flagged: bool | None = None,
    dry_run: bool,
) -> None:
    if read is not None:

        def planner(service: MessageService) -> Any:
            return service.plan_mark_read(references, read=read)
    else:

        def planner(service: MessageService) -> Any:
            return service.plan_flag(references, flagged=bool(flagged))

    _message_mutation(ctx, command, planner, dry_run=dry_run)


@messages_app.command("mark-read")
def messages_mark_read(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Mark selected messages read using an explicit single-field update."""

    _read_flag_command(ctx, "messages mark-read", references, read=True, dry_run=dry_run)


@messages_app.command("mark-unread")
def messages_mark_unread(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Mark selected messages unread using an explicit single-field update."""

    _read_flag_command(ctx, "messages mark-unread", references, read=False, dry_run=dry_run)


@messages_app.command("flag")
def messages_flag(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Flag selected messages."""

    _read_flag_command(ctx, "messages flag", references, flagged=True, dry_run=dry_run)


@messages_app.command("unflag")
def messages_unflag(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Remove follow-up flags from selected messages."""

    _read_flag_command(ctx, "messages unflag", references, flagged=False, dry_run=dry_run)


@messages_app.command("categorize")
def messages_categorize(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    category: list[str] = typer.Option([], "--category"),
    mode: str = typer.Option("set", help="set, add, or remove"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Set, add, or remove categories on selected messages."""

    _message_mutation(
        ctx,
        "messages categorize",
        lambda service: service.plan_categorize(references, category, mode=mode),
        dry_run=dry_run,
    )


@messages_app.command("export")
def messages_export(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    output: Path = typer.Option(..., help="Destination .eml path."),
) -> None:
    """Export one message as server-provided RFC 822 MIME."""

    messages_raw(ctx, reference=reference, output=output, overwrite=False)


@drafts_app.command("list")
def drafts_list(
    ctx: typer.Context,
    query: str | None = typer.Option(None, help="Optional local subject/body/address search."),
    limit: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=MAX_PAGE_SIZE, help="Drafts per page."),
    offset: int = typer.Option(0, min=0, help="Number of matching drafts to skip."),
) -> None:
    """List a bounded local draft page; use --offset to continue. No Exchange connection."""

    service = _draft_service(ctx)
    _run(
        ctx,
        "drafts list",
        lambda: service.list_page(query=query or "", limit=limit, offset=offset).items,
        title="Local drafts",
        source="local",
    )


@drafts_app.command("show")
def drafts_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one durable local draft and its revision."""

    _run(
        ctx,
        "drafts show",
        lambda: _draft_service(ctx).get(reference),
        title="Draft",
        source="local",
    )


@drafts_app.command("create")
def drafts_create(
    ctx: typer.Context,
    to: list[str] = typer.Option([], "--to"),
    cc: list[str] = typer.Option([], "--cc"),
    bcc: list[str] = typer.Option([], "--bcc"),
    subject: str = typer.Option(""),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    attach: list[Path] = typer.Option([], "--attach"),
    importance: Importance | None = typer.Option(None, case_sensitive=False),
    body_format: BodyFormat | None = typer.Option(None),
    read_receipt: bool | None = typer.Option(None, "--read-receipt/--no-read-receipt"),
    delivery_receipt: bool | None = typer.Option(None, "--delivery-receipt/--no-delivery-receipt"),
    signature: bool | None = typer.Option(None, "--signature/--no-signature"),
    save_sent_copy: bool | None = typer.Option(None, "--save-sent-copy/--no-sent-copy"),
) -> None:
    """Create a recoverable local draft; recipients are optional."""

    def action() -> DraftData:
        draft = _new_composition(
            ctx,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=_body_value(body, body_file),
            attachments=attach,
            importance=importance,
            body_format=body_format,
            read_receipt=read_receipt,
            delivery_receipt=delivery_receipt,
            signature_enabled=signature,
            save_sent_copy=save_sent_copy,
        )
        return _draft_service(ctx).create(
            to=draft.to,
            cc=draft.cc,
            bcc=draft.bcc,
            subject=draft.subject,
            body_text=draft.body_text,
            body_html=draft.body_html,
            attachment_paths=draft.attachment_paths,
            importance=draft.importance,
            request_read_receipt=draft.request_read_receipt,
            request_delivery_receipt=draft.request_delivery_receipt,
            save_sent_copy=draft.save_sent_copy,
        )

    _run(
        ctx,
        "drafts create",
        action,
        title="Draft created",
        source="local",
    )


@drafts_app.command("edit")
def drafts_edit(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    to: list[str] | None = typer.Option(None, "--to"),
    cc: list[str] | None = typer.Option(None, "--cc"),
    bcc: list[str] | None = typer.Option(None, "--bcc"),
    subject: str | None = typer.Option(None),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    attach: list[Path] | None = typer.Option(None, "--attach"),
    expected_revision: int | None = typer.Option(None, min=1),
    save_sent_copy: bool | None = typer.Option(None, "--save-sent-copy/--no-sent-copy"),
) -> None:
    """Edit supplied fields using optimistic revision checks."""

    def action() -> DraftData:
        changes: dict[str, Any] = {}
        if save_sent_copy is not None:
            changes["save_sent_copy"] = save_sent_copy
        for name, value in (("to", to), ("cc", cc), ("bcc", bcc)):
            if value is not None:
                changes[name] = _addresses(value)
        if subject is not None:
            changes["subject"] = subject
        if body is not None or body_file is not None:
            changes["body_text"] = _body_value(body, body_file)
        if attach is not None:
            changes["attachment_paths"] = attach
        return _draft_service(ctx).edit(reference, expected_revision=expected_revision, **changes)

    _run(ctx, "drafts edit", action, title="Draft updated", source="local")


@drafts_app.command("send")
def drafts_send(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    schedule: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and send a local draft, or place it in the durable schedule."""

    def action() -> Any:
        draft = _draft_service(ctx).get(reference)
        service = ComposeService(
            max_attachment_bytes=_state(ctx).context.config.max_attachment_bytes,
            state=_state(ctx).context.state,
            preferences=ComposerPreferences.from_settings(_settings_service(ctx)),
        )
        plan = service.plan_send(draft)
        if schedule:
            now = datetime.now(UTC)
            when = parse_datetime_expression(
                schedule,
                timezone=_state(ctx).context.config.timezone,
                now=now,
                future_required=True,
            ).local
            if dry_run:
                return {"dry_run": True, "scheduled_for": when, "plan": plan}
            _confirm(
                ctx,
                f"Queue this draft for {when.isoformat()}? A scheduler must be running to send.\n"
                + _compose_review(plan),
                plan.confirmation_scope + ":" + when.isoformat(),
            )
            return _scheduler(ctx).schedule_at(
                reference,
                when,
                now=now,
                original_expression=schedule,
                allow_empty_subject=service.preferences.allow_empty_subject,
            )
        scope = (
            None
            if dry_run
            else _confirm(
                ctx, "Send this reviewed draft?\n" + _compose_review(plan), plan.confirmation_scope
            )
        )
        if not dry_run:
            service.gateway = _gateway(ctx)
        result = service.execute_send(plan, dry_run=dry_run, confirmation_scope=scope)
        _remember_sent_composition(ctx, result)
        if result.sent:
            _draft_service(ctx).finish_sent(draft.reference, expected_revision=draft.revision)
        return result

    _run(ctx, "drafts send", action, title="Draft send")


@drafts_app.command("send-status")
def drafts_send_status(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Inspect a draft's durable send outcome without contacting Exchange."""

    def action() -> Any:
        attempt = SendAttemptStore(_state(ctx).context.state).get(reference)
        return attempt or {"draft_reference": reference, "state": "no_recorded_attempt"}

    _run(ctx, "drafts send-status", action, title="Send outcome", source="local")


@drafts_app.command("delete")
def drafts_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Keep the Exchange copy of a synchronized draft; delete only the local draft.",
    ),
) -> None:
    """Delete a draft; its Exchange copy, if any, moves to Deleted Items first."""

    def action() -> dict[str, Any]:
        service = _draft_service(ctx)
        server_id = None if local_only else service.get(reference).server_id
        prompt = (
            f"Delete draft {reference} and move its Exchange copy to Deleted Items?"
            if server_id
            else f"Delete local draft {reference}?"
        )
        _confirm(ctx, prompt, f"draft-delete:{reference}")
        # Same order as the TUI discard: never leave a local draft pointing at a
        # server copy that was already removed, and never orphan the server copy.
        if server_id:
            _gateway(ctx).delete_message(server_id, permanent=False)
        return {
            "reference": reference,
            "deleted": service.delete(reference),
            "server_copy_moved_to_deleted_items": bool(server_id),
        }

    _run(ctx, "drafts delete", action, title="Draft deletion", source="local")


@drafts_app.command("duplicate")
def drafts_duplicate(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Create an independent local copy of a draft."""

    _run(
        ctx,
        "drafts duplicate",
        lambda: _draft_service(ctx).duplicate(reference),
        title="Draft duplicated",
        source="local",
    )


@drafts_app.command("schedule")
def drafts_schedule(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    when: str = typer.Option(..., "--at", "--in", help="Future time or relative expression."),
    final_confirmation: bool = typer.Option(False),
) -> None:
    """Place a local draft in the durable scheduled-send queue."""

    _run(
        ctx,
        "drafts schedule",
        lambda: _scheduler(ctx).schedule(
            reference,
            when,
            require_final_confirmation=final_confirmation,
        ),
        title="Draft scheduled",
        source="local",
    )


@drafts_app.command("recover")
def drafts_recover(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    overwrite: bool = typer.Option(False),
) -> None:
    """Recover an autosave journal, with explicit overwrite for divergent drafts."""

    def action() -> DraftData:
        if overwrite:
            _confirm(
                ctx,
                f"Replace stored draft {reference} with its recovery copy?",
                f"draft-recover:{reference}",
            )
        return _draft_service(ctx).recover(reference, overwrite=overwrite)

    _run(ctx, "drafts recover", action, title="Draft recovered", source="local")


@drafts_app.command("conflicts")
def drafts_conflicts(ctx: typer.Context) -> None:
    """List drafts that need a manual conflict decision."""

    _run(
        ctx,
        "drafts conflicts",
        lambda: _draft_service(ctx).conflicts(),
        title="Draft conflicts",
        source="local",
    )


@drafts_app.command("save-server")
def drafts_save_server(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview without changing Exchange."),
) -> None:
    """Create or update the corresponding Exchange draft without changing its local identity."""

    def action() -> Any:
        drafts = _draft_service(ctx)
        local = drafts.get(reference)
        metadata = DraftSyncCoordinator(drafts, _gateway(ctx)).metadata(reference)
        if dry_run:
            return {
                "reference": reference,
                "server_id_present": bool(metadata.server_id or local.server_id),
                "sync_state": metadata.state.value,
                "would_create": not bool(metadata.server_id or local.server_id),
                "dry_run": True,
            }
        verb = "Update" if metadata.server_id or local.server_id else "Create"
        _confirm(
            ctx,
            f"{verb} the reviewed Exchange draft for {reference}?",
            f"draft-server-save:{reference}",
        )
        return DraftSyncCoordinator(drafts, _gateway(ctx)).sync_to_server(reference)

    _run(ctx, "drafts save-server", action, title="Server draft synchronization")


@drafts_app.command("resolve-conflict")
def drafts_resolve_conflict(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    use: str = typer.Option(..., help="Choose 'local' or 'server'."),
    push: bool = typer.Option(False, help="After keeping local, save it back to Exchange."),
) -> None:
    """Resolve a recorded local/server draft conflict with an explicit winner."""

    def action() -> DraftData:
        choice = use.casefold().strip()
        if choice not in {"local", "server"}:
            raise ConfigurationError("Draft conflict choice must be 'local' or 'server'.")
        drafts = _draft_service(ctx)
        coordinator = DraftSyncCoordinator(drafts, _gateway(ctx))
        _confirm(
            ctx,
            f"Resolve {reference} using the {choice} copy?",
            f"draft-conflict:{reference}:{choice}",
        )
        resolved = coordinator.resolve_conflict(reference, use=choice)
        if push:
            if choice != "local":
                raise ConfigurationError("--push is only valid when keeping the local copy.")
            _confirm(
                ctx,
                f"Save the selected local copy of {reference} back to Exchange?",
                f"draft-conflict-push:{reference}",
            )
            return coordinator.sync_to_server(reference)
        return resolved

    _run(ctx, "drafts resolve-conflict", action, title="Draft conflict resolution")


@drafts_app.command("sync")
def drafts_sync(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=500),
    apply: bool = typer.Option(False, help="Import missing server drafts into local state."),
) -> None:
    """Compare bounded server drafts and optionally import missing editable copies."""

    def action() -> dict[str, Any]:
        gateway = _gateway(ctx)
        summaries = gateway.list_messages(folder="drafts", limit=limit)
        service = _draft_service(ctx)
        coordinator = DraftSyncCoordinator(service, gateway)
        imported: list[str] = []
        existing: list[str] = []
        existing_by_server_id = {
            draft.server_id: draft.reference for draft in service.list() if draft.server_id
        }
        if apply:
            _confirm(ctx, f"Import up to {len(summaries)} server draft(s)?", "draft-sync-import")
        for summary in summaries:
            if summary.reference in existing_by_server_id:
                existing.append(existing_by_server_id[summary.reference])
                continue
            if not apply:
                continue
            remote = gateway.get_server_draft(summary.reference)
            imported_draft = coordinator.import_server_draft(
                ServerDraftSnapshot(
                    draft=remote,
                    server_id=remote.server_id or summary.reference,
                    server_change_key=remote.server_change_key,
                    modified_at=remote.updated_at,
                )
            )
            imported.append(imported_draft.reference)
        return {
            "server_drafts": len(summaries),
            "imported": imported,
            "already_local": existing,
            "applied": apply,
            "bounded_limit": limit,
        }

    _run(ctx, "drafts sync", action, title="Draft synchronization")


@attachments_app.command("list")
def attachments_list(
    ctx: typer.Context,
    message: str = typer.Argument(...),
    limit: int = typer.Option(100, min=1, max=200),
) -> None:
    """List attachment metadata without downloading content."""

    _run(
        ctx,
        "attachments list",
        lambda: AttachmentService(_gateway(ctx)).list_attachments(message, limit=limit),
        title="Attachments",
        source="live_server",
    )


@attachments_app.command("save")
def attachments_save(
    ctx: typer.Context,
    message: str = typer.Argument(...),
    attachment: str = typer.Argument(...),
    directory: Path | None = typer.Option(None, help="Destination directory."),
    filename: str | None = typer.Option(None),
    collision: CollisionPolicy | None = typer.Option(
        None,
        case_sensitive=False,
        help="Override the saved attachment collision behavior: rename, error, or overwrite.",
    ),
    sha256: bool = typer.Option(False, help="Calculate and report SHA-256."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Stream one attachment through size and traversal safeguards."""

    def action() -> Any:
        context = _state(ctx).context
        preferences = AttachmentPreferences.from_settings(_settings_service(ctx))
        service = AttachmentService(
            _gateway(ctx),
            max_download_bytes=context.config.max_attachment_bytes,
            collision_policy=preferences.collision,
        )
        plan = service.plan_save(
            message,
            attachment,
            directory or context.config.downloads,
            filename=filename,
            collision=collision,
        )
        scope = (
            None
            if dry_run
            else _confirm(
                ctx,
                f"Save {plan.attachment.name!r} to {plan.target}?",
                plan.confirmation_scope,
                required=plan.requires_confirmation,
            )
        )
        return service.execute_save(
            plan,
            dry_run=dry_run,
            confirmation_scope=scope,
            calculate_sha256=sha256,
        )

    _run(ctx, "attachments save", action, title="Attachment save")


@attachments_app.command("save-all")
def attachments_save_all(
    ctx: typer.Context,
    message: str = typer.Argument(...),
    directory: Path | None = typer.Option(None),
    collision: CollisionPolicy | None = typer.Option(
        None,
        case_sensitive=False,
        help="Override the saved attachment collision behavior: rename, error, or overwrite.",
    ),
    limit: int = typer.Option(100, min=1, max=200, help="Bounded attachment metadata limit."),
    sha256: bool = typer.Option(False, help="Calculate and report SHA-256 for each file."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and safely save every attachment on one message."""

    def action() -> list[Any]:
        context = _state(ctx).context
        preferences = AttachmentPreferences.from_settings(_settings_service(ctx))
        service = AttachmentService(
            _gateway(ctx),
            max_download_bytes=context.config.max_attachment_bytes,
            collision_policy=preferences.collision,
        )
        plans = service.plan_save_all(
            message,
            directory or context.config.downloads,
            limit=limit,
            collision=collision,
        )
        # Resolve every overwrite confirmation before the first file is downloaded.
        scopes = [
            None
            if dry_run
            else _confirm(
                ctx,
                f"Save {plan.attachment.name!r} to {plan.target}?",
                plan.confirmation_scope,
                required=plan.requires_confirmation,
            )
            for plan in plans
        ]
        return [
            service.execute_save(
                plan, dry_run=dry_run, confirmation_scope=scope, calculate_sha256=sha256
            )
            for plan, scope in zip(plans, scopes, strict=True)
        ]

    _run(ctx, "attachments save-all", action, title="Attachments saved")


@saved_searches_app.command("list")
def saved_searches_list(ctx: typer.Context) -> None:
    """List named local search definitions."""

    _run(
        ctx,
        "saved-searches list",
        lambda: _state(ctx).context.state.list_searches(),
        title="Saved searches",
        source="local",
    )


@saved_searches_app.command("add")
def saved_searches_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    query: str = typer.Argument(..., help="Advanced query expression."),
) -> None:
    """Validate and store a named advanced query."""

    def action() -> dict[str, Any]:
        parsed = parse_query(query, timezone=_state(ctx).context.config.timezone)
        _state(ctx).context.state.save_search(name, {"query": query, "ast": parsed.to_dict()})
        return {"name": name, "query": query}

    _run(ctx, "saved-searches add", action, title="Saved search", source="local")


@saved_searches_app.command("remove")
def saved_searches_remove(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Remove one local saved search."""

    _run(
        ctx,
        "saved-searches remove",
        lambda: {"name": name, "removed": _state(ctx).context.state.remove_search(name)},
        source="local",
    )


@saved_searches_app.command("run")
def saved_searches_run(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    limit: int = typer.Option(25, min=1, max=500),
) -> None:
    """Plan a saved advanced query and run its bounded server-compatible portion."""

    def action() -> Any:
        row = _state(ctx).context.state.get_search(name)
        if row is None:
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError(f"Saved search not found: {name}")
        payload = row.get("query")
        if isinstance(payload, dict):
            query_text = str(payload.get("query", ""))
        else:
            query_text = str(payload or "")
        context = _state(ctx).context
        results = MessageService(_gateway(ctx)).search_expression(
            query_text,
            timezone=context.config.timezone,
            limit=limit,
        )
        context.state.record_search_run(
            query=row,
            result_count=len(results.items),
            source="cli",
            name=name,
        )
        return {"plan": results.plan, "results": results}

    _run(ctx, "saved-searches run", action, title="Saved search results")


def _help_repository() -> HelpRepository:
    """Load and validate the package-resident corpus without touching the network."""

    return HelpRepository.load_default()


def _installed_cli_help_syntax() -> CliCommandSyntax:
    """Copy the installed parser grammar without invoking commands or converters."""

    from typer.core import TyperCommand, TyperGroup
    from typer.main import get_command

    from gucmail.help.cli_syntax import syntax_from_command

    command = get_command(app)
    if not isinstance(command, (TyperCommand, TyperGroup)):
        raise ValueError("Help grammar requires a Typer command tree.")
    return syntax_from_command(command, eager_exit_parameters=frozenset({"version_requested"}))


def _installed_cli_help_references() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Return installed leaf and invokable-group paths and their options."""

    from typer.core import TyperGroup
    from typer.main import get_command

    command_ids: set[str] = set()
    cli_paths: set[str] = set()
    cli_options: dict[str, set[str]] = {}
    pending: list[tuple[tuple[str, ...], object]] = [((), get_command(app))]
    while pending:
        prefix, command = pending.pop()
        if prefix and (not isinstance(command, TyperGroup) or command.invoke_without_command):
            command_ids.add(".".join(prefix))
            shell_path = " ".join(prefix)
            cli_paths.add(shell_path)
            options: set[str] = set()
            for parameter in getattr(command, "params", ()):
                options.update(str(value) for value in getattr(parameter, "opts", ()))
                options.update(str(value) for value in getattr(parameter, "secondary_opts", ()))
            cli_options[shell_path] = options
        if not isinstance(command, TyperGroup):
            continue
        for name, child in command.commands.items():
            path = (*prefix, name)
            command_ids.add(".".join(path))
            pending.append((path, child))
    return command_ids, cli_paths, cli_options


def _help_runtime_context(
    ctx: typer.Context,
    *,
    active_view: str | None = None,
) -> HelpRuntimeContext:
    """Build an offline Help context without exposing private setting values."""

    settings: dict[str, object] = {}
    for value in _settings_service(ctx).values():
        if value.spec.sensitive:
            # Generated Help needs to distinguish configured from absent, never the value.
            settings[value.spec.setting_id] = bool(value.secret_configured)
        else:
            settings[value.spec.setting_id] = value.effective_value
    return HelpRuntimeContext(active_view=active_view, settings=settings)


def _show_help_page(ctx: typer.Context, page_id: str, *, command: str) -> None:
    try:
        rendered = _help_repository().render(page_id, context=_help_runtime_context(ctx))
    except BaseException as exc:
        _error(ctx, exc, command=command)
        return
    state = _state(ctx)
    if state.output_format is OutputFormat.HUMAN:
        if state.quiet:
            return
        state.writer.console.rule(sanitize_terminal(rendered.page.title))
        state.writer.console.print(Markdown(rendered.markdown))
        return
    state.writer.emit(rendered, command=command, source="packaged_offline_help")


@help_app.callback(invoke_without_command=True)
def help_callback(ctx: typer.Context) -> None:
    """Open the packaged Help home page when no subcommand is supplied."""

    if ctx.invoked_subcommand is None:
        repository = _help_repository()
        _show_help_page(ctx, repository.home_page_id, command="help")


@help_app.command("show")
def help_show(
    ctx: typer.Context,
    page: str = typer.Argument(..., help="Help page ID, alias, or legacy topic ID."),
) -> None:
    """Render one complete Help article entirely offline."""

    _show_help_page(ctx, page, command="help show")


@help_app.command("list")
def help_list(
    ctx: typer.Context,
    category: str | None = typer.Option(None, help="Limit pages to this exact category."),
) -> None:
    """List available packaged Help pages and their stable IDs."""

    def action() -> tuple[dict[str, object], ...]:
        repository = _help_repository()
        pages = repository.pages_in_category(category) if category else repository.pages
        if category and not pages:
            raise ConfigurationError(f"Unknown or empty Help category: {category}")
        return tuple(
            {
                "id": page.page_id,
                "title": page.title,
                "category": page.category,
                "level": page.skill_level.value,
                "summary": page.summary,
            }
            for page in pages
        )

    _run(ctx, "help list", action, title="Help pages", source="packaged_offline_help")


@help_app.command("categories")
def help_categories(ctx: typer.Context) -> None:
    """List the Help hierarchy's top-level categories."""

    def action() -> tuple[dict[str, object], ...]:
        repository = _help_repository()
        return tuple(
            {
                "category": category,
                "pages": len(repository.pages_in_category(category)),
            }
            for category in repository.categories
        )

    _run(
        ctx,
        "help categories",
        action,
        title="Help categories",
        source="packaged_offline_help",
    )


@help_app.command("toc")
def help_toc(
    ctx: typer.Context,
    headings: bool = typer.Option(
        False,
        "--headings/--no-headings",
        help="Include article heading anchors below every page.",
    ),
) -> None:
    """Show the complete hierarchical table of contents."""

    def action() -> dict[str, object]:
        repository = _help_repository()
        toc = repository.table_of_contents(
            platform=_help_runtime_context(ctx).platform,
            include_headings=headings,
        )
        return {
            "title": toc.title,
            "categories": tuple(
                {
                    "id": category.node_id,
                    "title": category.title,
                    "pages": tuple(
                        {
                            "id": page.page_id,
                            "title": page.title,
                            "headings": tuple(
                                {
                                    "title": heading.title,
                                    "anchor": heading.anchor,
                                }
                                for heading in page.children
                            ),
                        }
                        for page in category.children
                    ),
                }
                for category in toc.children
            ),
        }

    _run(
        ctx,
        "help toc",
        action,
        title="Help table of contents",
        source="packaged_offline_help",
    )


@help_app.command("search")
def help_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Question, keywords, or misspelled topic."),
    limit: int = typer.Option(10, min=1, max=50),
) -> None:
    """Run typo-tolerant, natural-language search over the offline Help corpus."""

    if not query.strip():
        raise typer.BadParameter("Help search needs a non-empty question.")
    _run(
        ctx,
        "help search",
        lambda: _help_repository().search(
            query,
            limit=limit,
            context=_help_runtime_context(ctx),
        ),
        title="Help search results",
        source="packaged_offline_help",
    )


@help_app.command("error")
def help_error(
    ctx: typer.Context,
    message: str = typer.Argument(
        ...,
        help="Sanitized error name or message; do not paste credentials or private mail.",
    ),
    limit: int = typer.Option(3, min=1, max=10),
) -> None:
    """Map a known error or user-visible symptom to recovery articles."""

    if not message.strip():
        raise typer.BadParameter("Error-aware Help needs a non-empty description.")
    _run(
        ctx,
        "help error",
        lambda: _help_repository().for_error(
            message,
            limit=limit,
            context=_help_runtime_context(ctx, active_view="error"),
        ),
        title="Error-aware Help",
        source="packaged_offline_help",
    )


def _show_named_help_page(ctx: typer.Context, page_id: str, command: str) -> None:
    _show_help_page(ctx, page_id, command=command)


@help_app.command("commands")
def help_commands(ctx: typer.Context) -> None:
    """Show the generated reference for every canonical command."""

    _show_named_help_page(ctx, "reference.commands", "help commands")


@help_app.command("shortcuts")
def help_shortcuts(ctx: typer.Context) -> None:
    """Show keyboard navigation and current default shortcuts."""

    _show_named_help_page(ctx, "reference.shortcuts", "help shortcuts")


@help_app.command("glossary")
def help_glossary(ctx: typer.Context) -> None:
    """Show the offline GUC Mail and Exchange glossary."""

    _show_named_help_page(ctx, "glossary", "help glossary")


@help_app.command("validate")
def help_validate(ctx: typer.Context) -> None:
    """Validate packaged pages, metadata, and internal links offline."""

    def action() -> dict[str, object]:
        repository = _help_repository()
        registry = universal_palette_registry()
        from gucmail.help.reference import DOCUMENTED_NLP_EXAMPLES, shortcut_records
        from gucmail.nlp import load_command_corpus

        cli_command_ids, cli_paths, cli_options = _installed_cli_help_references()
        known_commands = {command.command_id for command in registry.all()} | cli_command_ids
        known_commands.update(
            " ".join(command.cli_path) for command in registry.all() if command.cli_path is not None
        )
        setting_specs = tuple(_settings_service(ctx).registry)
        known_settings = {spec.setting_id for spec in setting_specs}
        known_settings.update(spec.attr_name for spec in setting_specs if spec.attr_name)
        corpus = load_command_corpus()
        report = repository.validate(
            known_command_ids=known_commands,
            known_setting_ids=known_settings,
            known_shortcut_ids={record.shortcut_id for record in shortcut_records()},
            known_nlp_examples={
                *(phrase for group in corpus.groups for phrase in group.phrases),
                *(example.text for example in DOCUMENTED_NLP_EXAMPLES),
            },
            known_cli_paths=cli_paths,
            known_cli_options=cli_options,
            known_cli_syntax=_installed_cli_help_syntax(),
        )
        return {
            "valid": report.ok,
            "pages": len(repository.pages),
            "categories": len(repository.categories),
            "errors": report.errors,
            "warnings": report.warnings,
        }

    _run(
        ctx,
        "help validate",
        action,
        title="Help validation",
        source="packaged_offline_help",
    )


def _settings_service(ctx: typer.Context) -> SettingsService:
    context = _state(ctx).context
    return SettingsService(
        context.config_store,
        context=SettingsContext(
            account_id=context.config.email,
            machine_id=f"{sys.platform}:{platform.node()}",
        ),
    )


def _remember_sent_composition(ctx: typer.Context, result: Any) -> None:
    from gucmail.services.recipient_assistance import remember_sent_recipients

    if not result.sent:
        return
    try:
        remember_sent_recipients(
            _settings_service(ctx), result.plan.draft, own_address=_state(ctx).context.config.email
        )
    except Exception:
        # Keep accepted-send JSON/exit status intact; never encourage a resend for local history.
        if not _state(ctx).quiet:
            typer.echo(
                "Send accepted; local recipient history could not be saved. Do not resend.",
                err=True,
            )


def _resolve_setting_id(service: SettingsService, value: str) -> str:
    try:
        return service.registry.get(value).setting_id
    except ConfigurationError as original:
        try:
            return service.registry.for_app_config_field(value).setting_id
        except ConfigurationError:
            raise original from None


def _setting_record(value: Any) -> dict[str, object]:
    configured = value.configured_value
    effective = value.effective_value
    if value.spec.attr_name in {"email", "username"}:
        configured = "<configured>" if configured else None
        effective = "<configured>" if effective else None
    return {
        "id": value.spec.setting_id,
        "label": value.spec.label,
        "category": value.spec.category,
        "configured": configured,
        "effective": effective,
        "source": value.source.value,
        "overridden": value.overridden,
        "environment_variable": value.environment_variable,
        "level": value.spec.level.value,
        "apply": value.spec.apply_semantics.value,
        "help": value.spec.help_id,
    }


@layouts_app.command("list")
def layouts_list(ctx: typer.Context) -> None:
    """List local saved layout names and display preferences; never connect to Exchange."""
    _run(
        ctx,
        "layouts list",
        lambda: LayoutPresetService(_settings_service(ctx)).catalog().presets,
        title="Saved layouts",
        source="local",
    )


@layouts_app.command("save")
def layouts_save(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    replace_existing: bool = typer.Option(
        False, "--replace", help="Explicitly replace an existing name."
    ),
    dry_run: bool = typer.Option(False, help="Preview without saving."),
) -> None:
    """Save the configured layout. TUI Save captures its displayed arrangement."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(_settings_service(ctx))
        catalog = service.catalog()
        selected = validate_name(name)
        exists = any(key.casefold() == selected.casefold() for key in catalog.presets)
        if exists and not replace_existing:
            raise ConfirmationRequiredError("This layout name exists. Use --replace to replace it.")
        if not dry_run:
            service.save(
                selected, catalog.current, catalog=catalog, replace_existing=replace_existing
            )
        return {
            "name": selected,
            "settings": catalog.current,
            "dry_run": dry_run,
            "replaced": exists,
        }

    _run(
        ctx,
        "layouts save",
        action,
        title="Layout preview" if dry_run else "Layout saved",
        source="local",
    )


@layouts_app.command("load")
def layouts_load(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview the saved choices without applying them."),
) -> None:
    """Atomically apply one named layout through Settings; mailbox data is unaffected."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(_settings_service(ctx))
        catalog = service.catalog()
        preview = service.preview(name, catalog=catalog)
        if not dry_run:
            service.load(name, catalog=catalog)
            _state(ctx).context.reload_config()
        return {**preview, "dry_run": dry_run}

    _run(
        ctx,
        "layouts load",
        action,
        title="Layout preview" if dry_run else "Layout loaded",
        source="local",
    )


@layouts_app.command("delete")
def layouts_delete(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview removal without deleting the preset."),
) -> None:
    """Remove a saved layout with confirmation; keep the active arrangement."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(_settings_service(ctx))
        catalog = service.catalog()
        preview = service.preview(name, catalog=catalog)
        if not dry_run:
            _confirm(
                ctx, f"Remove saved layout {preview['name']}?", "layout-delete:" + preview["name"]
            )
            service.delete(name, catalog=catalog)
        return {"name": preview["name"], "dry_run": dry_run, "removed": not dry_run}

    _run(ctx, "layouts delete", action, title="Layout removal", source="local")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show non-secret configuration with account identifiers redacted."""

    _run(
        ctx,
        "config show",
        lambda: _state(ctx).context.config.redacted_dict(),
        title="Configuration",
        source="local",
    )


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    value: str = typer.Argument(...),
) -> None:
    """Set one supported non-secret configuration value."""

    def action() -> dict[str, object]:
        context = _state(ctx).context
        service = _settings_service(ctx)
        setting_id = _resolve_setting_id(service, key)
        revision = service.set(setting_id, value, confirm_external=False)
        context.reload_config()
        return {
            "setting": _setting_record(service.value(setting_id)),
            "revision": revision.revision_id if revision is not None else None,
        }

    _run(ctx, "config set", action, title="Configuration updated", source="local")


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Show platform-specific configuration and state paths."""

    def action() -> dict[str, str]:
        paths = _state(ctx).context.paths
        return {
            "config": str(paths.config_file),
            "state": str(paths.state_database),
            "index": str(paths.index_database),
            "backups": str(paths.backup_dir),
            "logs": str(paths.log_dir),
        }

    _run(ctx, "config path", action, title="Paths", source="local")


@settings_app.command("list")
def settings_list(
    ctx: typer.Context,
    category: str | None = typer.Option(None, help="Exact settings category."),
    level: SettingLevel = typer.Option(SettingLevel.EXPERT, case_sensitive=False),
    limit: int = typer.Option(500, min=1, max=1_000),
) -> None:
    """List effective settings, their source, and override state."""

    def action() -> tuple[dict[str, object], ...]:
        service = _settings_service(ctx)
        values = service.values(category=category)
        if category and not values:
            raise ConfigurationError(f"Unknown settings category: {category}")
        ranks = {
            SettingLevel.BASIC: 0,
            SettingLevel.ADVANCED: 1,
            SettingLevel.EXPERT: 2,
        }
        return tuple(
            _setting_record(value) for value in values if ranks[value.spec.level] <= ranks[level]
        )[:limit]

    _run(ctx, "settings list", action, title="Settings", source="local")


@settings_app.command("categories")
def settings_categories(ctx: typer.Context) -> None:
    """List settings categories and entry counts."""

    def action() -> tuple[dict[str, object], ...]:
        service = _settings_service(ctx)
        categories = tuple(dict.fromkeys(spec.category for spec in service.registry))
        return tuple(
            {
                "category": category,
                "settings": len(service.registry.by_category(category)),
            }
            for category in categories
        )

    _run(ctx, "settings categories", action, title="Settings categories", source="local")


@settings_app.command("get")
def settings_get(ctx: typer.Context, setting: str = typer.Argument(...)) -> None:
    """Show one setting with configured/effective value and precedence source."""

    def action() -> dict[str, object]:
        service = _settings_service(ctx)
        return _setting_record(service.value(_resolve_setting_id(service, setting)))

    _run(ctx, "settings get", action, title="Setting", source="local")


@settings_app.command("search")
def settings_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(20, min=1, max=100),
    level: SettingLevel = typer.Option(SettingLevel.EXPERT, case_sensitive=False),
) -> None:
    """Search labels, IDs, descriptions, categories, and aliases."""

    if not query.strip():
        raise typer.BadParameter("Settings search needs non-empty text.")

    def action() -> tuple[dict[str, object], ...]:
        service = _settings_service(ctx)
        return tuple(
            {
                "id": result.spec.setting_id,
                "label": result.spec.label,
                "category": result.spec.category,
                "description": result.spec.description,
                "score": round(result.score, 2),
                "matched": result.matched_text,
                "help": result.spec.help_id,
            }
            for result in service.search(query, limit=limit, maximum_level=level)
        )

    _run(ctx, "settings search", action, title="Settings search", source="local")


def _edit_setting(
    ctx: typer.Context,
    *,
    setting: str,
    value: str | None,
    reset: bool,
    dry_run: bool,
) -> None:
    def action() -> dict[str, object]:
        context = _state(ctx).context
        service = _settings_service(ctx)
        setting_id = _resolve_setting_id(service, setting)
        session = service.begin()
        if reset:
            session.reset(setting_id)
        else:
            assert value is not None
            session.set(setting_id, value)
        preview = session.preview()
        if dry_run or not preview:
            session.cancel()
            return {"dry_run": dry_run, "changes": preview, "applied": False}
        external = any(change.external_effect for change in preview)
        if external:
            _confirm(
                ctx,
                "Apply setting changes that affect an external integration?",
                "settings-external:" + ",".join(change.setting_id for change in preview),
            )
        revision = session.apply(
            reason=f"CLI {'reset' if reset else 'update'}: {setting_id}",
            confirm_external=external,
        )
        context.reload_config()
        return {
            "dry_run": False,
            "changes": preview,
            "applied": revision is not None,
            "revision": revision.revision_id if revision is not None else None,
            "setting": _setting_record(service.value(setting_id)),
        }

    command = "settings reset" if reset else "settings set"
    _run(ctx, command, action, title=command.title(), source="local")


@settings_app.command("set")
def settings_set(
    ctx: typer.Context,
    setting: str = typer.Argument(...),
    value: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Validate and preview without saving."),
) -> None:
    """Validate, preview, and apply one non-secret setting."""

    _edit_setting(ctx, setting=setting, value=value, reset=False, dry_run=dry_run)


@settings_app.command("reset")
def settings_reset(
    ctx: typer.Context,
    setting: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview the default restoration."),
) -> None:
    """Reset one setting to its built-in default."""

    _edit_setting(ctx, setting=setting, value=None, reset=True, dry_run=dry_run)


@settings_app.command("export")
def settings_export(
    ctx: typer.Context,
    output: Path | None = typer.Option(None, help="Optional JSON destination."),
    category: list[str] = typer.Option([], "--category"),
    setting: list[str] = typer.Option([], "--setting"),
    overwrite: bool = typer.Option(False),
) -> None:
    """Export portable non-secret settings; secret values are always excluded."""

    def action() -> dict[str, object]:
        payload = _settings_service(ctx).export(
            categories=category or None,
            setting_ids=setting or None,
        )
        if output is None:
            return payload
        target = output.expanduser().resolve(strict=False)
        if target.exists() and not overwrite:
            raise ConfirmationRequiredError(
                "The settings export destination exists; use --overwrite and --yes."
            )
        if target.exists():
            _confirm(ctx, f"Overwrite settings export {target}?", f"settings-export:{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "path": str(target),
            "settings": len(payload["settings"]),
            "excluded_secrets": payload["excluded_secrets"],
        }

    _run(ctx, "settings export", action, title="Settings export", source="local")


@settings_app.command("import")
def settings_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, file_okay=True, readable=True),
    apply: bool = typer.Option(False, help="Apply the previewed changes after confirmation."),
) -> None:
    """Preview a JSON settings import; applying is explicit and revisioned."""

    def action() -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Could not read settings import: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("Settings import must be a JSON object.")
        context = _state(ctx).context
        service = _settings_service(ctx)
        plan = service.plan_import(payload)
        if not apply or not plan.changes:
            return {"plan": plan, "applied": False}
        _confirm(
            ctx,
            f"Apply exactly {len(plan.changes)} imported setting change(s)?",
            "settings-import:" + ",".join(change.setting_id for change in plan.changes),
        )
        revision = service.apply_import(plan, confirm_external=True)
        context.reload_config()
        return {
            "plan": plan,
            "applied": revision is not None,
            "revision": revision.revision_id if revision is not None else None,
        }

    _run(ctx, "settings import", action, title="Settings import", source="local")


@settings_app.command("history")
def settings_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, min=1, max=100),
) -> None:
    """List bounded non-secret settings revisions."""

    _run(
        ctx,
        "settings history",
        lambda: _settings_service(ctx).revisions.list()[:limit],
        title="Settings history",
        source="local",
    )


@settings_app.command("undo")
def settings_undo(
    ctx: typer.Context,
    revision: str | None = typer.Argument(None, help="Revision ID; defaults to latest."),
) -> None:
    """Restore values from one previous revision after confirmation."""

    def action() -> dict[str, object]:
        context = _state(ctx).context
        _confirm(
            ctx,
            "Restore settings from the selected revision?",
            f"settings-undo:{revision or 'latest'}",
        )
        restored = _settings_service(ctx).undo(revision)
        context.reload_config()
        return {
            "restored": restored is not None,
            "revision": restored.revision_id if restored is not None else None,
        }

    _run(ctx, "settings undo", action, title="Settings restored", source="local")


@cache_app.command("status")
def cache_status(ctx: typer.Context) -> None:
    """Show cache database health and the configured cache directory."""

    _run(
        ctx,
        "cache status",
        lambda: {
            "enabled": _state(ctx).context.config.cache_enabled,
            "directory": str(_state(ctx).context.paths.cache_dir),
            "state": _state(ctx).context.state.health(),
        },
        source="local",
    )


@cache_app.command("clear")
def cache_clear(ctx: typer.Context, dry_run: bool = typer.Option(False)) -> None:
    """Clear disposable cached references and checkpoints, never mailbox or backup data."""

    def action() -> Any:
        if dry_run:
            return {"dry_run": True, "scope": "disposable cache tables only"}
        _confirm(ctx, "Clear disposable local cache tables?", "cache-clear:v1")
        return _state(ctx).context.state.clear_cache()

    _run(ctx, "cache clear", action, title="Cache clear", source="local")


def _local_capacity_paths(ctx: typer.Context) -> dict[str, Path]:
    context = _state(ctx).context
    return {
        "data": context.paths.data_dir,
        "cache": context.paths.cache_dir,
        "search_index": context.paths.index_database,
        "backups": context.paths.backup_dir,
        "downloads": context.config.downloads,
    }


def _quota_action(ctx: typer.Context) -> Any:
    settings = _settings_service(ctx)

    def setting(setting_id: str, fallback: object) -> object:
        try:
            return settings.value(setting_id).effective_value
        except ConfigurationError:
            return fallback

    return quota_service_from_settings(_gateway(ctx), setting).report(
        local_paths=_local_capacity_paths(ctx)
    )


@quota_app.callback(invoke_without_command=True)
def quota_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit the quota report as JSON."),
) -> None:
    """Show server quota, configured evidence, and clearly labelled GUC fallback data."""

    if json_output:
        _state(ctx).output_format = OutputFormat.JSON
    if ctx.invoked_subcommand is None:
        _run(ctx, "quota", lambda: _quota_action(ctx), title="Mailbox and local capacity")


@quota_app.command("show")
def quota_show(ctx: typer.Context) -> None:
    """Show Exchange quota evidence and separate local capacity."""

    _run(ctx, "quota show", lambda: _quota_action(ctx), title="Mailbox and local capacity")


@quota_app.command("folders")
def quota_folders(
    ctx: typer.Context,
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Show folder item counts; never mislabel them as byte usage or quota."""

    def action() -> dict[str, Any]:
        # The EWS gateway accepts at most MAX_PAGE_SIZE folders per request; the
        # default --limit 500 used to fail live with "limit must be between 1 and 200".
        gateway = _gateway(ctx)
        folders = list(gateway.list_folders(limit=min(limit, MAX_PAGE_SIZE)))
        exhausted = len(folders) < min(limit, MAX_PAGE_SIZE)
        while not exhausted and len(folders) < limit:
            request = min(MAX_PAGE_SIZE, limit - len(folders))
            page = list(gateway.list_folders(limit=request, offset=len(folders)))[:request]
            folders.extend(page)
            exhausted = len(page) < request
        return {
            "folders": tuple(folders),
            "notice": (
                "Standard EWS folder metadata exposes item counts here, not exact folder bytes. "
                "These values are not mailbox quota."
            ),
            "exact_folder_sizes_available": False,
        }

    _run(ctx, "quota folders", action, title="Folder capacity metadata")


@quota_app.command("local")
def quota_local(ctx: typer.Context) -> None:
    """Show local filesystem capacity without opening an Exchange session."""

    _run(
        ctx,
        "quota local",
        lambda: QuotaService.local_disk_usage(_local_capacity_paths(ctx)),
        title="Local capacity",
        source="local",
    )


@ews_app.command("capabilities")
@ews_app.command("operations")
def ews_capabilities(
    ctx: typer.Context,
    family: str | None = typer.Option(None),
) -> None:
    """List every represented EWS operation and its independent capability axes."""

    _run(
        ctx,
        "ews capabilities",
        lambda: DEFAULT_REGISTRY.by_family(family) if family else DEFAULT_REGISTRY.all(),
        title="EWS capability registry",
        source="static_registry",
    )


@ews_app.command("delegates")
def ews_delegates(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=1000),
    offset: int = typer.Option(0, min=0, max=1000),
) -> None:
    """Read this mailbox's delegates. Limits slice one unpaged EWS collection locally."""

    _run(
        ctx,
        "ews delegates",
        lambda: DelegateService(_gateway(ctx)).list(limit=limit, offset=offset),
        title=lambda page: f"Mailbox delegates · {len(page.entries)} of {page.total}",
        human_data=lambda page: (
            {
                f"Delegate {page.offset + index + 1}": delegate_details_text(delegate)
                for index, delegate in enumerate(page.entries)
            }
            or {"Status": "Exchange returned no delegates in this local page."}
        ),
        warnings=[
            "EWS returns one collection (maximum 1000 delegates); pagination is local. "
            "This does not list all folder sharing or effective mailbox access."
        ],
    )


def _registry_operation(operation: str) -> Any:
    try:
        return DEFAULT_REGISTRY.get(operation)
    except KeyError:
        # An unknown name is a not-found input, not an unexpected internal error.
        from gucmail.errors import ItemNotFoundError

        raise ItemNotFoundError(
            f"Unknown EWS operation: {operation}. Run 'gucmail ews operations' to list names."
        ) from None


@ews_app.command("describe")
def ews_describe(ctx: typer.Context, operation: str = typer.Argument(...)) -> None:
    """Describe one typed, fallback, or registry-only EWS operation."""

    _run(
        ctx,
        "ews describe",
        lambda: _registry_operation(operation),
        title=operation,
        source="static_registry",
    )


@ews_app.command("support-matrix")
def ews_support_matrix(ctx: typer.Context) -> None:
    """Summarize capability counts without claiming unprobed server support."""

    def action() -> dict[str, Any]:
        items = DEFAULT_REGISTRY.all()
        counts: dict[str, int] = {}
        for item in items:
            key = f"{item.implementation_path.value}:{item.implementation_status.value}"
            counts[key] = counts.get(key, 0) + 1
        return {"total": len(items), "counts": counts, "server": "not_probed"}

    _run(ctx, "ews support-matrix", action, title="EWS support matrix")


@ews_app.command("diagnostics")
def ews_diagnostics(ctx: typer.Context, online: bool = typer.Option(False)) -> None:
    """Show registry diagnostics; online probing is one bounded status read only."""

    _run(
        ctx,
        "ews diagnostics",
        lambda: {
            "registry_operations": len(DEFAULT_REGISTRY.all()),
            "server_status": _gateway(ctx).status() if online else "not_probed",
            "raw_soap_enabled": False,
            "raw_soap_implementation": "not_implemented",
            "raw_soap_legacy_preference": _state(ctx).context.config.expert_raw_soap_enabled,
        },
    )


@ews_app.command("request-preview")
def ews_request_preview(
    ctx: typer.Context,
    operation: str = typer.Argument(...),
) -> None:
    """Preview operation classification without constructing or sending SOAP."""

    _run(
        ctx,
        "ews request-preview",
        lambda: {
            "operation": _registry_operation(operation),
            "sent": False,
            "notice": "No XML was constructed and no request was sent.",
        },
        title="EWS request preview",
    )


@scheduled_app.command("list")
def scheduled_list(
    ctx: typer.Context,
    state: list[SendState] = typer.Option([], "--state", case_sensitive=False),
    limit: int = typer.Option(200, min=1, max=10_000),
) -> None:
    """List the durable local send queue and uncertain/manual-review states."""

    def present(records: Any) -> list[dict[str, Any]]:
        # Human output only; JSON keeps the complete records. Without this the
        # outbox rendered each nested job as a raw JSON blob.
        import contextlib
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        rows: list[dict[str, Any]] = []
        for record in records:
            message = record.message
            due = message.scheduled_for
            with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
                due = due.astimezone(ZoneInfo(message.timezone))
            rows.append(
                {
                    "reference": message.reference,
                    "state": str(getattr(message.state, "value", message.state)),
                    "scheduled_for": due.isoformat(sep=" ", timespec="minutes"),
                    "attempts": message.attempts,
                    "next_attempt_at": message.next_attempt_at,
                    "last_error": message.last_error or "",
                    "final_confirmation": message.require_final_confirmation,
                }
            )
        return rows

    _run(
        ctx,
        "scheduled list",
        lambda: _scheduler(ctx).list(states=state or None, limit=limit),
        title="Scheduled messages",
        source="local",
        human_data=present,
    )


@scheduled_app.command("show")
def scheduled_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one scheduled job, its draft snapshot, and attachment manifest."""

    _run(
        ctx,
        "scheduled show",
        lambda: _scheduler(ctx).get(reference),
        title="Scheduled message",
        source="local",
    )


@scheduled_app.command("edit")
def scheduled_edit(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    to: list[str] | None = typer.Option(None, "--to"),
    cc: list[str] | None = typer.Option(None, "--cc"),
    bcc: list[str] | None = typer.Option(None, "--bcc"),
    subject: str | None = typer.Option(None),
    body: str | None = typer.Option(None),
    body_file: Path | None = typer.Option(None),
    final_confirmation: bool | None = typer.Option(None),
) -> None:
    """Edit the snapshotted draft used by an editable scheduled job."""

    def action() -> Any:
        changes: dict[str, Any] = {}
        for name, value in (("to", to), ("cc", cc), ("bcc", bcc)):
            if value is not None:
                changes[name] = _addresses(value)
        if subject is not None:
            changes["subject"] = subject
        if body is not None or body_file is not None:
            changes["body_text"] = _body_value(body, body_file)
        if not changes and final_confirmation is None:
            raise typer.BadParameter("Supply at least one draft or confirmation change.")
        return _scheduler(ctx).edit(
            reference,
            draft_changes=changes or None,
            require_final_confirmation=final_confirmation,
        )

    _run(ctx, "scheduled edit", action, title="Scheduled message updated", source="local")


@scheduled_app.command("history")
def scheduled_history(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show the append-only local transition history for one job."""

    _run(
        ctx,
        "scheduled history",
        lambda: _scheduler(ctx).history(reference),
        title="Schedule history",
        source="local",
    )


@scheduled_app.command("reschedule")
def scheduled_reschedule(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    when: str = typer.Option(
        ..., "--at", "--in", help="Unambiguous future or relative expression."
    ),
) -> None:
    """Resolve and replace a future send time."""

    _run(
        ctx,
        "scheduled reschedule",
        lambda: _scheduler(ctx).reschedule(reference, when),
        title="Rescheduled",
        source="local",
    )


@scheduled_app.command("cancel")
def scheduled_cancel(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Cancel an editable local scheduled or undo-delay job."""

    def action() -> Any:
        _confirm(ctx, f"Cancel scheduled message {reference}?", f"schedule-cancel:{reference}")
        return _scheduler(ctx).cancel(reference)

    _run(ctx, "scheduled cancel", action, title="Scheduled message cancelled")


@scheduled_app.command("undo")
def scheduled_undo(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Cancel a message while it remains in the local Undo Send delay."""

    _run(
        ctx,
        "scheduled undo",
        lambda: _scheduler(ctx).undo(reference),
        title="Send undone",
        source="local",
    )


@scheduled_app.command("send-now")
def scheduled_send_now(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Send one editable job immediately after confirmation."""

    def action() -> Any:
        _confirm(ctx, f"Send scheduled message {reference} now?", f"send-now:{reference}")
        return _scheduler(ctx).send_now(reference, confirmed=True)

    _run(ctx, "scheduled send-now", action, title="Send now")


@scheduled_app.command("run-once")
def scheduled_run_once(
    ctx: typer.Context,
    max_jobs: int = typer.Option(20, min=1, max=100),
) -> None:
    """Process one bounded batch of due jobs; safe for OS schedulers."""

    _run(
        ctx,
        "scheduled run-once",
        lambda: _scheduler(ctx).run_once(limit=max_jobs),
        title="Scheduler run",
    )


@scheduled_app.command("run")
def scheduled_run(
    ctx: typer.Context,
    interval: float = typer.Option(30.0, min=5.0, max=3600.0),
) -> None:
    """Run the local scheduler in the foreground until interrupted."""

    try:
        result = _scheduler(ctx).run_foreground(poll_interval=interval)
        _state(ctx).writer.emit(result, command="scheduled run", title="Scheduler stopped")
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        _error(ctx, exc, command="scheduled run")


@scheduled_app.command("install")
def scheduled_install(
    ctx: typer.Context,
    interval: int = typer.Option(60, min=60, max=86400),
    apply: bool = typer.Option(False, "--apply", help="Write artifacts and run native commands."),
) -> None:
    """Preview or explicitly install Task Scheduler/systemd/launchd integration."""

    from gucmail.platform_scheduler import (
        bound_scheduler_arguments,
        generate_scheduler_plan,
        install_scheduler,
    )

    def action() -> Any:
        plan = generate_scheduler_plan(
            interval_seconds=interval,
            arguments=bound_scheduler_arguments(_state(ctx).context.paths),
        )
        if not apply:
            return plan.as_dict()
        _confirm(ctx, f"Install {plan.platform.value} scheduler integration?", "scheduler-install")
        return install_scheduler(plan, dry_run=False, confirmed=True)

    _run(ctx, "scheduled install", action, title="Scheduler integration")


@scheduled_app.command("uninstall")
def scheduled_uninstall(
    ctx: typer.Context,
    interval: int = typer.Option(60, min=60, max=86400),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or explicitly remove native scheduler integration."""

    from gucmail.platform_scheduler import (
        bound_scheduler_arguments,
        generate_scheduler_plan,
        uninstall_scheduler,
    )

    def action() -> Any:
        plan = generate_scheduler_plan(
            interval_seconds=interval,
            arguments=bound_scheduler_arguments(_state(ctx).context.paths),
        )
        if not apply:
            return {"plan": plan.as_dict(), "uninstall": True, "dry_run": True}
        _confirm(ctx, f"Remove {plan.platform.value} scheduler integration?", "scheduler-uninstall")
        return uninstall_scheduler(plan, dry_run=False, confirmed=True)

    _run(ctx, "scheduled uninstall", action, title="Scheduler integration")


@scheduler_app.command("status")
def scheduler_status(ctx: typer.Context) -> None:
    """Show durable queue and worker pause status without contacting Exchange."""

    _run(
        ctx,
        "scheduler status",
        lambda: _scheduler(ctx).status(),
        title="Scheduler status",
        source="local",
    )


@scheduler_app.command("run")
def scheduler_run(
    ctx: typer.Context,
    interval: float = typer.Option(30.0, min=5.0, max=3600.0),
) -> None:
    """Run the local scheduler in the foreground until interrupted."""

    try:
        result = _scheduler(ctx).run_foreground(poll_interval=interval)
        _state(ctx).writer.emit(result, command="scheduler run", title="Scheduler stopped")
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        _error(ctx, exc, command="scheduler run")


@scheduler_app.command("run-once")
def scheduler_run_once(
    ctx: typer.Context,
    max_jobs: int = typer.Option(20, min=1, max=100),
) -> None:
    """Process one bounded due-job batch for an OS scheduler."""

    _run(
        ctx,
        "scheduler run-once",
        lambda: _scheduler(ctx).run_once(limit=max_jobs),
        title="Scheduler run",
    )


@scheduler_app.command("pause")
def scheduler_pause(ctx: typer.Context) -> None:
    """Pause worker processing while retaining every queued job."""

    def action() -> dict[str, Any]:
        service = _scheduler(ctx)
        service.pause()
        return service.status()

    _run(ctx, "scheduler pause", action, title="Scheduler paused", source="local")


@scheduler_app.command("resume")
def scheduler_resume(ctx: typer.Context) -> None:
    """Resume due-job processing; overdue jobs follow the documented retry policy."""

    def action() -> dict[str, Any]:
        service = _scheduler(ctx)
        service.resume()
        return service.status()

    _run(ctx, "scheduler resume", action, title="Scheduler resumed", source="local")


@scheduler_app.command("doctor")
def scheduler_doctor(ctx: typer.Context) -> None:
    """Inspect queue health and preview the native integration for this platform."""

    from gucmail.platform_scheduler import bound_scheduler_arguments, generate_scheduler_plan

    def action() -> dict[str, Any]:
        context = _state(ctx).context
        return {
            "queue": _scheduler(ctx).status(),
            "state": context.state.health(),
            "native_integration": generate_scheduler_plan(
                arguments=bound_scheduler_arguments(context.paths)
            ).as_dict(),
            "network_request_performed": False,
        }

    _run(ctx, "scheduler doctor", action, title="Scheduler diagnostics", source="local")


@scheduler_app.command("install")
def scheduler_install(
    ctx: typer.Context,
    interval: int = typer.Option(60, min=60, max=86400),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or explicitly install native scheduler integration."""

    scheduled_install(ctx, interval=interval, apply=apply)


@scheduler_app.command("uninstall")
def scheduler_uninstall(
    ctx: typer.Context,
    interval: int = typer.Option(60, min=60, max=86400),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview or explicitly remove native scheduler integration."""

    scheduled_uninstall(ctx, interval=interval, apply=apply)


def _headless_account_id(ctx: typer.Context) -> str:
    account_id = (_state(ctx).context.config.email or "").strip()
    if not account_id:
        raise ConfigurationError(
            "Headless watcher state needs a configured account email. Run 'gucmail login' "
            "or set GUCMAIL_EMAIL before starting it."
        )
    return account_id


def _headless_process_spec(ctx: typer.Context, *, profile: str) -> Any:
    return context_watcher_process(_state(ctx).context, profile=profile)


def _headless_status_store(ctx: typer.Context, *, profile: str) -> WatcherStatusStore:
    context = _state(ctx).context
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


def _watcher_profile(
    ctx: typer.Context,
    profile: str | None,
) -> WatcherProfileConfiguration | None:
    """Resolve a durable profile without creating one or touching credentials."""

    context = _state(ctx).context
    return configured_watcher(context, profile)


def _profiled_notification_dispatcher(
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


@headless_app.command("validate")
def headless_validate(
    ctx: typer.Context,
    profile: str = typer.Option("default"),
) -> None:
    """Validate a non-interactive deployment without opening a mailbox session."""

    def action() -> Any:
        state = _state(ctx)
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
                if policy.enabled and policy.provider_id.casefold() in enabled_ids
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

    _run(
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

    _run(
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

    _run(
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

    _run(ctx, f"headless {action}", execute, title="Watcher control", source="local")


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
) -> None:
    """Run a single-instance, TUI-free watcher with durable health and control state."""

    providers: list[ProviderBinding] = []
    close_after: list[WebhookNotificationProvider] = []

    def action() -> dict[str, object]:
        context = _state(ctx).context
        configured = _watcher_profile(ctx, profile)
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
        dispatcher = _profiled_notification_dispatcher(
            context,
            providers,
            profile_id=configured.profile_id if configured is not None else None,
        )
        monitor = MailboxMonitor(
            _gateway(ctx, interactive=False),
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

        cycles = run_headless_cycles(runtime, cycle, max_cycles=None if forever else 1)
        return {
            "cycles": cycles,
            "profile": profile,
            "folders": resolved_folders,
            "final_status": store.read(),
            "tui_initialized": False,
        }

    try:
        _run(ctx, "headless run", action, title="Headless watcher")
    finally:
        for provider in close_after:
            provider.close()


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
        _confirm(
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

    _run(
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

    _run(
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

    _run(ctx, "startup status", action, title="Startup status", source="local_system")


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


@recurring_app.command("list")
def recurring_list(
    ctx: typer.Context,
    state: RecurringState | None = typer.Option(None, case_sensitive=False),
) -> None:
    """List paused, active, and completed recurring rules."""

    _run(
        ctx,
        "recurring list",
        lambda: RecurringService(
            _state(ctx).context.state, _draft_service(ctx), _scheduler(ctx)
        ).list(state=state),
        title="Recurring messages",
        source="local",
    )


@recurring_app.command("show")
def recurring_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one recurring rule and its next bounded occurrences."""

    def action() -> dict[str, Any]:
        context = _state(ctx).context
        service = RecurringService(
            context.state,
            _draft_service(ctx),
            _scheduler(ctx),
            automation_enabled=False,
        )
        return {
            "rule": service.get(reference),
            "next_occurrences": service.preview(reference, limit=10),
        }

    _run(ctx, "recurring show", action, title="Recurring rule", source="local")


@recurring_app.command("create")
def recurring_create(
    ctx: typer.Context,
    draft: str = typer.Argument(..., help="Template local draft reference."),
    frequency: RecurrenceFrequency = typer.Option(..., case_sensitive=False),
    start: str = typer.Option(..., help="ISO-8601 first occurrence time with an offset."),
    interval: int = typer.Option(1, min=1, max=1000),
    activate: bool = typer.Option(False, help="Activate immediately (expert opt-in)."),
    count: int | None = typer.Option(None, min=1),
) -> None:
    """Create a paused recurring rule; activation is explicit."""

    def action() -> Any:
        context = _state(ctx).context
        parsed_start = _iso_datetime(start, parameter="--start")
        assert parsed_start is not None
        rule = RecurrenceRule(
            frequency=frequency,
            interval=interval,
            timezone=context.config.timezone,
            start_at=parsed_start,
            count=count,
        )
        service = RecurringService(
            context.state,
            _draft_service(ctx),
            _scheduler(ctx),
            automation_enabled=activate and _state(ctx).assume_yes,
        )
        if activate:
            _confirm(ctx, "Activate automatic recurring sends?", "recurring-activate")
        return service.create(draft, rule, activate=activate)

    _run(ctx, "recurring create", action, title="Recurring rule")


def _recurring_action(ctx: typer.Context, command: str, reference: str, method: str) -> None:
    def action() -> Any:
        context = _state(ctx).context
        service = RecurringService(
            context.state,
            _draft_service(ctx),
            _scheduler(ctx),
            automation_enabled=True,
        )
        if method in {"resume", "delete"}:
            _confirm(
                ctx,
                f"{method.title()} recurring rule {reference}?",
                f"recurring:{method}:{reference}",
            )
        result = getattr(service, method)(reference)
        if method == "delete" and result is False:
            # delete() reports a missing rule as False; printing it with exit 0
            # looked like a successful deletion.
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError(f"Recurring rule not found: {reference}")
        return result

    _run(ctx, command, action, title=command.title(), source="local")


@recurring_app.command("pause")
def recurring_pause(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Pause future materialization without deleting history."""

    _recurring_action(ctx, "recurring pause", reference, "pause")


@recurring_app.command("resume")
def recurring_resume(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Resume an existing recurring rule after confirmation."""

    _recurring_action(ctx, "recurring resume", reference, "resume")


@recurring_app.command("skip")
def recurring_skip(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Skip exactly the next occurrence."""

    _recurring_action(ctx, "recurring skip", reference, "skip")


@recurring_app.command("delete")
def recurring_delete(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Delete one local rule; already-created scheduled jobs remain auditable."""

    _recurring_action(ctx, "recurring delete", reference, "delete")


@recurring_app.command("materialize")
def recurring_materialize(
    ctx: typer.Context,
    through: str = typer.Option(..., help="ISO-8601 horizon with an offset."),
    max_occurrences: int = typer.Option(50, min=1, max=500),
) -> None:
    """Materialize a bounded set of active recurring occurrences."""

    def action() -> Any:
        context = _state(ctx).context
        service = RecurringService(
            context.state,
            _draft_service(ctx),
            _scheduler(ctx),
            automation_enabled=True,
        )
        horizon = _iso_datetime(through, parameter="--through")
        assert horizon is not None
        return service.materialize_until(horizon, limit=max_occurrences)

    _run(ctx, "recurring materialize", action, title="Recurring occurrences")


def _calendar_view_command(
    ctx: typer.Context,
    command: str,
    *,
    kind: CalendarViewKind | None = None,
    anchor: date | None = None,
    agenda_days: int = 14,
    limit: int = 200,
) -> None:
    def action() -> Any:
        preferences = CalendarDisplayPreferences.from_settings(_settings_service(ctx))
        selected_kind = kind or preferences.default_view
        working_days = (
            CalendarWorkingTime.from_settings(_settings_service(ctx)).weekdays
            if selected_kind is CalendarViewKind.WORK_WEEK
            else (0, 1, 2, 3, 4)
        )
        return load_calendar_view(
            CalendarService(
                DeferredGateway(lambda: _gateway(ctx)), display_timezone=preferences.timezone
            ),
            kind=selected_kind,
            anchor=anchor,
            first_weekday=preferences.first_weekday,
            working_days=working_days,
            agenda_days=agenda_days,
            limit=limit,
        )

    _run(
        ctx,
        command,
        action,
        title=lambda view: (
            f"Calendar {view.kind.value} · {view.timezone} · "
            f"{view.start.date().isoformat()} to {view.end.date().isoformat()} (exclusive)"
        ),
        source="live_server",
        human_data=calendar_view_rows,
    )


@calendar_app.callback(invoke_without_command=True)
def calendar_callback(ctx: typer.Context) -> None:
    """Open the saved Calendar view, week start and display zone; one bounded query."""

    if ctx.invoked_subcommand is None:
        _calendar_view_command(ctx, "calendar")


@calendar_app.command("list")
@calendar_app.command("agenda")
def calendar_agenda(
    ctx: typer.Context,
    days: int = typer.Option(14, min=1, max=366),
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Show a bounded agenda in the configured display time zone."""

    _calendar_view_command(
        ctx, "calendar agenda", kind=CalendarViewKind.AGENDA, agenda_days=days, limit=limit
    )


@calendar_app.command("day")
def calendar_day(
    ctx: typer.Context,
    day: str | None = typer.Argument(None, help="ISO date (YYYY-MM-DD)."),
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Show one day; omitted dates use the configured Calendar zone."""

    _calendar_view_command(
        ctx,
        "calendar day",
        kind=CalendarViewKind.DAY,
        anchor=_iso_date(day, parameter="day"),
        limit=limit,
    )


@calendar_app.command("week")
def calendar_week(
    ctx: typer.Context,
    day: str | None = typer.Argument(None, help="ISO date (YYYY-MM-DD)."),
    work_week: bool = typer.Option(False),
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Show a week or work week, aligned to the saved first weekday."""

    _calendar_view_command(
        ctx,
        "calendar week",
        kind=CalendarViewKind.WORK_WEEK if work_week else CalendarViewKind.WEEK,
        anchor=_iso_date(day, parameter="day"),
        limit=limit,
    )


@calendar_app.command("month")
def calendar_month(
    ctx: typer.Context,
    day: str | None = typer.Argument(None, help="ISO date (YYYY-MM-DD)."),
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Show aligned complete weeks, including adjacent month dates; one bounded query."""

    _calendar_view_command(
        ctx,
        "calendar month",
        kind=CalendarViewKind.MONTH,
        anchor=_iso_date(day, parameter="day"),
        limit=limit,
    )


@calendar_app.command("search")
def calendar_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    days: int = typer.Option(365, min=1, max=3650),
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Search a bounded calendar window locally after one EWS calendar view."""

    needle = query.casefold().strip()
    if not needle:
        raise typer.BadParameter("Calendar search needs non-empty text.")

    preferences: CalendarDisplayPreferences | None = None

    def action() -> tuple[CalendarEvent, ...]:
        nonlocal preferences
        preferences = CalendarDisplayPreferences.from_settings(_settings_service(ctx))
        view = CalendarService(
            DeferredGateway(lambda: _gateway(ctx)), display_timezone=preferences.timezone
        ).agenda(days=days, limit=limit)
        return tuple(
            event for event in view.events if needle in calendar_event_search_text(event).casefold()
        )

    def present(events: tuple[CalendarEvent, ...]) -> Any:
        assert preferences is not None
        return [event_display_data(event, preferences.timezone) for event in events]

    _run(ctx, "calendar search", action, title="Calendar search results", human_data=present)


@calendar_app.command("show")
def calendar_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one event in the display zone; JSON retains original event metadata."""

    preferences: CalendarDisplayPreferences | None = None

    def action() -> CalendarEvent:
        nonlocal preferences
        preferences = CalendarDisplayPreferences.from_settings(_settings_service(ctx))
        return CalendarService(
            DeferredGateway(lambda: _gateway(ctx)), display_timezone=preferences.timezone
        ).get(reference)

    def present(event: CalendarEvent) -> Any:
        assert preferences is not None
        return event_display_data(event, preferences.timezone)

    _run(ctx, "calendar show", action, title="Calendar event", human_data=present)


@calendar_app.command("history")
def calendar_history(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    limit: int = typer.Option(100, min=1, max=200, help="Maximum entries per exception list."),
) -> None:
    """Read a series' current modified/deleted occurrence metadata; no audit-trail claim."""
    preferences: CalendarDisplayPreferences | None = None

    def action() -> CalendarSeriesHistory:
        nonlocal preferences
        preferences = CalendarDisplayPreferences.from_settings(_settings_service(ctx))
        return CalendarService(DeferredGateway(lambda: _gateway(ctx))).history(
            reference, limit=limit
        )

    def present(result: CalendarSeriesHistory) -> str:
        assert preferences is not None
        return history_text(result, preferences.timezone)

    _run(
        ctx,
        "calendar history",
        action,
        title="Calendar occurrence history",
        human_data=present,
    )


@calendar_app.command("create")
def calendar_create(
    ctx: typer.Context,
    subject: str = typer.Option(...),
    start: str = typer.Option(..., help="ISO-8601 start with an explicit offset."),
    end: str | None = typer.Option(
        None, help="ISO-8601 end with an explicit offset; overrides the saved duration."
    ),
    duration_minutes: int | None = typer.Option(
        None,
        min=1,
        max=10080,
        help="Override the saved duration; conflicts with --end and --all-day.",
    ),
    reminder_minutes: int | None = typer.Option(
        None, min=0, max=525600, help="Minutes before start; overrides the saved reminder."
    ),
    no_reminder: bool = typer.Option(
        False, "--no-reminder", help="Disable the saved reminder for this event."
    ),
    availability: str | None = typer.Option(
        None,
        help="Override saved availability: free, working_elsewhere, tentative, "
        "busy, out_of_office.",
    ),
    location: str | None = typer.Option(None),
    body: str = typer.Option(""),
    required: list[str] = typer.Option([], "--required"),
    optional: list[str] = typer.Option([], "--optional"),
    attachment: list[Path] = typer.Option([], "--attach", exists=True, dir_okay=False),
    all_day: bool = typer.Option(False),
    send_invitations: bool = typer.Option(True, "--send-invitations/--no-invitations"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create an event with explicit invitation policy."""

    def action() -> Any:
        parsed_start = _iso_datetime(start, parameter="--start")
        parsed_end = _iso_datetime(end, parameter="--end")
        assert parsed_start is not None
        if no_reminder and reminder_minutes is not None:
            raise ConfigurationError("Choose either --reminder-minutes or --no-reminder.")
        preferences = CalendarCreationPreferences.from_settings(_settings_service(ctx))
        service = CalendarService(
            DeferredGateway(lambda: _gateway(ctx)),
            display_timezone=_state(ctx).context.config.timezone,
        )
        event = replace(
            preferences.new_event(
                subject=subject,
                start=parsed_start,
                end=parsed_end,
                timezone=_state(ctx).context.config.timezone,
                duration_minutes=duration_minutes,
                reminder_minutes=None
                if no_reminder
                else reminder_minutes
                if reminder_minutes is not None
                else PIM_UNSET,
                availability=availability,
                all_day=all_day,
            ),
            location=location,
            body=body,
            required_attendees=tuple(_addresses(required)),
            optional_attendees=tuple(_addresses(optional)),
            is_all_day=all_day,
            is_meeting=bool(required or optional),
        )
        plan = service.plan_create(
            event,
            invitations=InvitationPolicy.SEND if send_invitations else InvitationPolicy.SEND_NONE,
            attachment_paths=attachment,
        )
        if dry_run:
            return plan
        _confirm(ctx, f"Create calendar event {subject!r}?", f"event-create:{subject}")
        return service.gateway.create_event(plan)

    _run(ctx, "calendar create", action, title="Calendar event")


@calendar_app.command("edit")
@calendar_app.command("update")
def calendar_update(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    subject: str | None = typer.Option(None),
    start: str | None = typer.Option(None, help="ISO-8601 timestamp with an offset."),
    end: str | None = typer.Option(None, help="ISO-8601 timestamp with an offset."),
    location: str | None = typer.Option(None),
    body: str | None = typer.Option(None),
    attachment: list[Path] = typer.Option([], "--attach", exists=True, dir_okay=False),
    remove_attachment: list[str] = typer.Option([], "--remove-attachment"),
    scope: RecurrenceScope = typer.Option(RecurrenceScope.OCCURRENCE, case_sensitive=False),
    dry_run: bool = typer.Option(False),
) -> None:
    """Update only explicitly supplied event fields."""

    def action() -> Any:
        supplied = {
            name: value
            for name, value in (
                ("subject", subject),
                ("start", _iso_datetime(start, parameter="--start")),
                ("end", _iso_datetime(end, parameter="--end")),
                ("location", location),
                ("body", body),
            )
            if value is not None
        }
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        plan = service.plan_update(
            reference,
            CalendarPatch(**supplied),
            scope=scope,
            attachment_paths=attachment,
            remove_attachment_references=remove_attachment,
        )
        if dry_run:
            return plan
        _confirm(
            ctx,
            f"Update {len(plan.changes)} field(s) on event {reference}?",
            f"event-update:{reference}",
        )
        return service.gateway.update_event(plan)

    _run(ctx, "calendar update", action, title="Calendar update")


@calendar_app.command("delete")
def calendar_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    send_cancellation: bool = typer.Option(False),
    reason: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move an event to Deleted Items or send a reviewed cancellation."""

    def action() -> Any:
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        plan = service.plan_delete(reference, send_cancellation=send_cancellation, reason=reason)
        if dry_run:
            return plan
        _confirm(ctx, f"Delete calendar event {reference}?", f"event-delete:{reference}")
        service.delete(plan)
        return {"reference": reference, "deleted": True}

    _run(ctx, "calendar delete", action, title="Calendar deletion")


@calendar_app.command("cancel")
def calendar_cancel(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    reason: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
) -> None:
    """Cancel a meeting and request attendee notifications after preview."""

    def action() -> Any:
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        current = service.gateway.get_event(reference)
        if not current.is_meeting:
            # Matches the TUI: an appointment has no attendees to notify, so a
            # "cancellation" would only delete it while reporting notifications.
            raise ConfigurationError(
                "The selected item is an appointment, not a meeting. "
                "Use 'gucmail calendar delete' instead."
            )
        plan = service.plan_delete(
            reference, current=current, send_cancellation=True, reason=reason
        )
        if dry_run:
            return plan
        _confirm(
            ctx, f"Cancel meeting {reference} and notify attendees?", f"event-cancel:{reference}"
        )
        service.delete(plan)
        return {"reference": reference, "cancelled": True, "attendees_notified": True}

    _run(ctx, "calendar cancel", action, title="Meeting cancellation")


@calendar_app.command("respond")
def calendar_respond(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    response: CalendarResponse | None = typer.Argument(
        None, case_sensitive=False, help="Explicit response; omitted uses the saved initial choice."
    ),
    comment: str | None = typer.Option(None),
    proposed_start: str | None = typer.Option(
        None, help="ISO-8601 proposed start with an explicit offset."
    ),
    proposed_end: str | None = typer.Option(
        None, help="ISO-8601 proposed end with an explicit offset."
    ),
    send_response: bool = typer.Option(True, "--send-response/--save-only"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Accept, tentatively accept, or decline a meeting."""

    def action() -> Any:
        chosen_response = (
            response
            if response is not None
            else CalendarOperationPreferences.from_settings(
                _settings_service(ctx)
            ).invitation_response
        )
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        event = service.gateway.get_event(reference)
        plan = service.plan_response(
            event,
            chosen_response,
            send_response=send_response,
            comment=comment,
            proposed_start=_iso_datetime(proposed_start, parameter="--proposed-start"),
            proposed_end=_iso_datetime(proposed_end, parameter="--proposed-end"),
        )
        if dry_run:
            return plan
        _confirm(
            ctx,
            f"{chosen_response.value.title()} meeting {reference}?",
            f"meeting:{chosen_response}:{reference}",
        )
        return service.respond(plan)

    _run(ctx, "calendar respond", action, title="Meeting response")


@calendar_app.command("availability")
def calendar_availability(
    ctx: typer.Context,
    attendee: list[str] = typer.Option(..., "--attendee"),
    start: str = typer.Option(..., help="ISO-8601 start with an offset."),
    end: str = typer.Option(..., help="ISO-8601 end with an offset."),
    interval: int = typer.Option(30, min=5, max=1440),
) -> None:
    """Read attendee free/busy data through EWS GetUserAvailability."""

    parsed_start = _iso_datetime(start, parameter="--start")
    parsed_end = _iso_datetime(end, parameter="--end")
    assert parsed_start is not None and parsed_end is not None
    if parsed_end <= parsed_start:
        raise typer.BadParameter("--end must be later than --start.")
    _run(
        ctx,
        "calendar availability",
        lambda: _gateway(ctx).get_availability(
            _addresses(attendee),
            start=parsed_start,
            end=parsed_end,
            interval_minutes=interval,
        ),
        title="Calendar availability",
    )


@calendar_app.command("free-time")
def calendar_free_time(
    ctx: typer.Context,
    attendee: list[str] = typer.Option(..., "--attendee"),
    start: str = typer.Option(..., help="ISO-8601 start with an offset."),
    end: str = typer.Option(..., help="ISO-8601 end with an offset; at most 31 elapsed days."),
    interval: int = typer.Option(30, min=5, max=1440),
    duration_minutes: int = typer.Option(60, min=5, max=1440),
    all_hours: bool = typer.Option(False, help="Ignore saved working days/hours for this search."),
) -> None:
    """Find the first confirmed common slot within saved local working days/hours."""

    def action() -> Any:
        parsed_start = _iso_datetime(start, parameter="--start")
        parsed_end = _iso_datetime(end, parameter="--end")
        assert parsed_start is not None and parsed_end is not None
        attendees = tuple(_addresses(attendee))
        validate_free_time_request(attendees, parsed_start, parsed_end, interval, duration_minutes)
        timezone = CalendarDisplayPreferences.from_settings(_settings_service(ctx)).timezone
        working_time = (
            None if all_hours else CalendarWorkingTime.from_settings(_settings_service(ctx))
        )
        if working_time is not None:
            working_time.windows(parsed_start, parsed_end, timezone)
        values = _gateway(ctx).get_availability(
            attendees, start=parsed_start, end=parsed_end, interval_minutes=interval
        )
        return find_common_free_time(
            values,
            attendees=attendees,
            start=parsed_start,
            end=parsed_end,
            interval_minutes=interval,
            duration_minutes=duration_minutes,
            timezone=timezone,
            working_time=working_time,
        )

    _run(ctx, "calendar free-time", action, title="Calendar free-time suggestion")


@calendar_app.command("conflicts")
def calendar_conflicts(
    ctx: typer.Context,
    days: int = typer.Option(30, min=1, max=366),
    limit: int = typer.Option(200, min=2, max=200),
) -> None:
    """Detect overlapping busy events in a bounded local calendar view."""

    def action() -> Any:
        view = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        ).agenda(days=days, limit=limit)
        return {
            "window": {"start": view.start, "end": view.end},
            "conflicts": detect_conflicts(view.events),
        }

    _run(ctx, "calendar conflicts", action, title="Calendar conflicts")


@calendar_app.command("import")
def calendar_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, readable=True),
    apply: bool = typer.Option(False, help="Create events after preview."),
) -> None:
    """Parse an ICS file locally; creating events requires --apply and confirmation."""

    def action() -> Any:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ConfigurationError("Calendar ICS imports are limited to 5 MiB.")
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        plan = service.import_ics(path.read_text(encoding="utf-8"))
        if not apply:
            return plan
        _confirm(ctx, f"Create {len(plan.events)} imported event(s)?", "calendar-import")
        return service.apply_import(plan)

    _run(ctx, "calendar import", action, title="Calendar import")


@calendar_app.command("export")
def calendar_export(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    output: Path = typer.Option(...),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing ICS file."),
) -> None:
    """Export one event to an RFC 5545 ICS file."""

    def action() -> dict[str, Any]:
        service = CalendarService(
            _gateway(ctx), display_timezone=_state(ctx).context.config.timezone
        )
        content = service.export_event(reference)
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            _confirm(ctx, f"Overwrite existing ICS file {output}?", f"calendar-export:{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "events": 1}

    _run(ctx, "calendar export", action, title="Calendar export")


@calendar_app.command("watch")
def calendar_watch(
    ctx: typer.Context,
    forever: bool = typer.Option(False, help="Continue until interrupted."),
    notify_existing: bool = typer.Option(False, help="Also report the initial stored events."),
    reset_checkpoint: bool = typer.Option(False, help="Rebuild this local calendar baseline."),
) -> None:
    """Report calendar creations, updates and deletions using bounded EWS sync pages."""

    def action() -> object:
        context = _state(ctx).context
        monitor = MailboxMonitor(
            _gateway(ctx),
            context.state,
            account_id=context.config.email or "configured-mailbox",
            folders=("calendar",),
            poll_interval_seconds=context.config.poll_interval_seconds,
            limit=context.config.page_size,
            notify_existing=notify_existing,
            profile_id="calendar",
            strategy_preference="incremental_sync",
        )
        if reset_checkpoint:
            monitor.reset_checkpoints()
        if not forever:
            result = monitor.run_once()
            if not result.succeeded:
                raise NetworkError(result.folders[0].error or "Calendar monitoring failed.")
            return result
        for result in monitor.iter_foreground():
            _state(ctx).writer.emit(result, command="calendar watch", title="Calendar changes")
        return None

    _run(ctx, "calendar watch", action, title="Calendar watch")


def _contacts_service(
    ctx: typer.Context, *, index_warning: Callable[[str], None] | None = None
) -> ContactsService:
    gateway = DeferredGateway(lambda: _gateway(ctx))
    return ContactsService(
        gateway,
        correspondence_gateway=gateway,
        operation_preferences=lambda: ContactOperationPreferences.from_settings(
            _settings_service(ctx)
        ),
        history_index=ContactHistoryIndex(_state(ctx).context.state),
        index_warning=index_warning,
    )


def _contacts_read_command(
    ctx: typer.Context,
    command: str,
    action: Callable[[ContactsService, ContactsDisplayPreferences], Any],
    *,
    title: str,
    warnings: Sequence[str] = (),
    source: str | None = None,
    threshold_override: int | None = None,
) -> None:
    display = ContactsDisplayPreferences()

    def execute() -> Any:
        nonlocal display
        display = ContactsDisplayPreferences.from_settings(
            _settings_service(ctx), threshold_override=threshold_override
        )
        return action(_contacts_service(ctx), display)

    _run(
        ctx,
        command,
        execute,
        title=title,
        warnings=warnings,
        source=source,
        human_data=lambda result: display.human_data(result),
    )


@contacts_app.callback(invoke_without_command=True)
def contacts_callback(ctx: typer.Context) -> None:
    """List a bounded contact view when no contacts subcommand is supplied."""

    if ctx.invoked_subcommand is None:
        _contacts_read_command(
            ctx,
            "contacts",
            lambda service, display: display.sort(service.list(limit=200)),
            title="Contacts",
            source="live_server",
            warnings=("Sorting covers this bounded contact page.",),
        )


def _pim_page_output(page: PimPage[Any], *, details: bool, warnings: list[str]) -> Any:
    if page.residual:
        warnings.append(page.residual)
    if page.next_offset is not None:
        warnings.append(
            f"More candidates may remain; repeat with --offset {page.next_offset}. "
            "Use --page for structured continuation information."
        )
    return page if details else page.items


@contacts_app.command("list")
def contacts_list(
    ctx: typer.Context,
    limit: int = typer.Option(200, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """List a bounded contact page using the saved sorting preference."""

    warnings: list[str] = ["Sorting covers this bounded contact page."]
    _contacts_read_command(
        ctx,
        "contacts list",
        lambda service, display: _pim_page_output(
            display.page(service.page(limit=limit, offset=offset)),
            details=page,
            warnings=warnings,
        ),
        title="Contacts",
        source="live_server",
        warnings=warnings,
    )


@contacts_app.command("search")
def contacts_search(
    ctx: typer.Context,
    text: str | None = typer.Argument(None),
    email: str | None = typer.Option(None),
    company: str | None = typer.Option(None),
    category: str | None = typer.Option(None),
    fuzzy: bool = typer.Option(False, help="Use bounded local fuzzy ranking."),
    threshold: int = typer.Option(82, min=0, max=100),
    limit: int = typer.Option(100, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """Search a bounded contact candidate set."""

    query = ContactQuery(text=text, email=email, company=company, category=category)
    warnings: list[str] = []

    def action(service: ContactsService, display: ContactsDisplayPreferences) -> Any:
        if not any((text, email, company, category)):
            raise ConfigurationError("Contact search needs at least one criterion.")
        if not fuzzy:
            return _pim_page_output(
                display.page(service.page(query, limit=limit, offset=offset)),
                details=page,
                warnings=warnings,
            )
        if not text:
            raise typer.BadParameter("--fuzzy requires search text.")
        candidates = service.page(replace(query, text=None), limit=limit, offset=offset)
        ranked = fuzzy_rerank(
            candidates.items,
            text,
            threshold=threshold,
            limit=limit,
            max_candidates=limit,
            fields=("display_name", "given_name", "family_name", "emails", "company", "categories"),
        )
        return _pim_page_output(
            PimPage(
                tuple(ranked),
                candidates.offset,
                candidates.scanned,
                candidates.next_offset,
                "Fuzzy ranking covers this candidate page; explicit filters apply before ranking.",
            ),
            details=page,
            warnings=warnings,
        )

    _contacts_read_command(
        ctx,
        "contacts search",
        action,
        title="Contact results",
        warnings=warnings,
    )


@contacts_app.command("show")
def contacts_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one mailbox contact."""

    _contacts_read_command(
        ctx,
        "contacts show",
        lambda service, display: service.get(reference),
        title="Contact",
    )


@contacts_app.command("create")
def contacts_create(
    ctx: typer.Context,
    display_name: str = typer.Option(...),
    given_name: str | None = typer.Option(None),
    middle_name: str | None = typer.Option(None),
    family_name: str | None = typer.Option(None),
    nickname: str | None = typer.Option(None),
    email: list[str] = typer.Option([], "--email"),
    phone: list[str] = typer.Option([], "--phone", help="Repeatable label=value phone."),
    address: list[str] = typer.Option([], "--address", help="Repeatable label=value address."),
    company: str | None = typer.Option(None),
    department: str | None = typer.Option(None),
    job_title: str | None = typer.Option(None),
    birthday: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    notes: str | None = typer.Option(None),
    category: list[str] = typer.Option([], "--category"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create a mailbox contact."""

    def action() -> Any:
        service = ContactsService(_gateway(ctx))
        contact = ContactInfo(
            reference="preview:contact",
            display_name=display_name,
            given_name=given_name,
            middle_name=middle_name,
            family_name=family_name,
            nickname=nickname,
            emails=tuple(_addresses(email)),
            phones=_labeled_options(phone, parameter="--phone"),
            addresses=_labeled_options(address, parameter="--address"),
            company=company,
            department=department,
            job_title=job_title,
            birthday=_iso_date(birthday, parameter="--birthday"),
            notes=notes,
            categories=tuple(category),
        )
        plan = service.plan_create(contact)
        if dry_run:
            return plan
        _confirm(ctx, f"Create contact {display_name!r}?", f"contact-create:{display_name}")
        return service.gateway.create_contact(plan)

    _run(ctx, "contacts create", action, title="Contact creation")


@contacts_app.command("edit")
@contacts_app.command("update")
def contacts_update(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    display_name: str | None = typer.Option(None),
    given_name: str | None = typer.Option(None),
    middle_name: str | None = typer.Option(None),
    family_name: str | None = typer.Option(None),
    nickname: str | None = typer.Option(None),
    email: list[str] = typer.Option([], "--email"),
    phone: list[str] = typer.Option([], "--phone", help="Repeatable label=value phone."),
    address: list[str] = typer.Option([], "--address", help="Repeatable label=value address."),
    company: str | None = typer.Option(None),
    department: str | None = typer.Option(None),
    job_title: str | None = typer.Option(None),
    birthday: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    notes: str | None = typer.Option(None),
    category: list[str] = typer.Option([], "--category"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Update only supplied contact fields."""

    def action() -> Any:
        changes: dict[str, Any] = {
            name: value
            for name, value in (
                ("display_name", display_name),
                ("given_name", given_name),
                ("middle_name", middle_name),
                ("family_name", family_name),
                ("nickname", nickname),
                ("company", company),
                ("department", department),
                ("job_title", job_title),
                ("birthday", _iso_date(birthday, parameter="--birthday")),
                ("notes", notes),
            )
            if value is not None
        }
        if email:
            changes["emails"] = tuple(_addresses(email))
        if phone:
            changes["phones"] = _labeled_options(phone, parameter="--phone")
        if address:
            changes["addresses"] = _labeled_options(address, parameter="--address")
        if category:
            changes["categories"] = tuple(category)
        service = ContactsService(_gateway(ctx))
        plan = service.plan_update(reference, ContactPatch(**changes))
        if dry_run:
            return plan
        _confirm(ctx, f"Update contact {reference}?", f"contact-update:{reference}")
        return service.gateway.update_contact(plan)

    _run(ctx, "contacts update", action, title="Contact update")


@contacts_app.command("delete")
def contacts_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(False),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move a contact to Deleted Items; permanent deletion is explicit."""

    def action() -> Any:
        service = ContactsService(_gateway(ctx))
        plan = service.plan_delete(reference, hard_delete=permanent)
        if dry_run:
            return plan
        _confirm(ctx, f"Delete contact {reference}?", f"contact-delete:{reference}")
        service.delete(plan)
        return {"reference": reference, "deleted": True, "permanent": permanent}

    _run(ctx, "contacts delete", action, title="Contact deletion")


@contacts_app.command("duplicates")
def contacts_duplicates(
    ctx: typer.Context,
    threshold: int | None = typer.Option(
        None, min=1, max=100, help="Override the saved duplicate threshold for this invocation."
    ),
    limit: int = typer.Option(200, min=2, max=200),
) -> None:
    """Analyze possible duplicates without changing contacts."""

    _contacts_read_command(
        ctx,
        "contacts duplicates",
        lambda service, display: service.duplicates(
            threshold=display.duplicate_threshold, limit=limit
        ),
        title="Possible contact duplicates",
        threshold_override=threshold,
    )


@contacts_app.command("merge")
def contacts_merge(
    ctx: typer.Context,
    references: list[str] = typer.Argument(...),
    primary: str | None = typer.Option(None),
    apply: bool = typer.Option(
        False, help="Update the primary and move other contacts to Deleted Items."
    ),
) -> None:
    """Preview an explainable contact merge; execution is recoverable by default."""

    if len(references) < 2:
        raise typer.BadParameter("Contact merge needs at least two distinct references.")
    if len(set(references)) != len(references):
        raise typer.BadParameter("A contact reference may appear only once.")

    def action() -> Any:
        service = ContactsService(_gateway(ctx))
        preview = service.merge_preview(
            (service.get(reference) for reference in references),
            primary_reference=primary,
        )
        plan = service.plan_merge(preview)
        if not apply:
            return plan
        _confirm(
            ctx,
            f"Merge {len(references)} contacts and move {len(plan.deletes)} "
            "duplicate(s) to Deleted Items?",
            f"contacts-merge:{preview.primary_reference}:{len(plan.deletes)}",
        )
        return service.apply_merge(plan)

    _run(ctx, "contacts merge", action, title="Contact merge")


@contacts_app.command("groups")
def contacts_groups(
    ctx: typer.Context,
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Group a bounded contact view by Exchange contact category."""

    def action() -> dict[str, Any]:
        contacts = ContactsService(_gateway(ctx)).list(limit=limit)
        grouped: dict[str, list[ContactInfo]] = {}
        for contact in contacts:
            labels = contact.categories or ("(uncategorized)",)
            for label in labels:
                grouped.setdefault(label, []).append(contact)
        return {
            "groups": grouped,
            "notice": (
                "These are contact categories. Exchange distribution-list mutation is not "
                "exposed by this command."
            ),
        }

    _run(ctx, "contacts groups", action, title="Contact groups")


@contacts_app.command("export")
def contacts_export(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    output: Path = typer.Option(...),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Export one contact as vCard 4.0."""

    def action() -> dict[str, Any]:
        content = ContactsService(_gateway(ctx)).export_contact(reference)
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            _confirm(ctx, f"Overwrite existing vCard file {output}?", f"contacts-export:{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "contacts": 1}

    _run(ctx, "contacts export", action, title="Contact export")


@contacts_app.command("import")
def contacts_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, readable=True),
    apply: bool = typer.Option(False),
    allow_duplicates: bool = typer.Option(False),
) -> None:
    """Parse vCards locally; creation requires --apply and confirmation."""

    warnings: list[str] = []

    def action() -> Any:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ConfigurationError("vCard imports are limited to 5 MiB.")
        service = ContactsService(_gateway(ctx))
        plan = service.import_vcards(
            path.read_text(encoding="utf-8"), allow_duplicates=allow_duplicates
        )
        if not apply:
            return plan
        _confirm(ctx, f"Create up to {len(plan.contacts)} contact(s)?", "contacts-import")
        created = service.apply_import(plan)
        skipped = len(plan.contacts) - len(created)
        if skipped:
            # Without this the human output said only "No matching items." and the
            # skipped duplicates were invisible to the user.
            warnings.append(
                f"Skipped {skipped} imported contact(s) that look like existing contacts. "
                "Review 'gucmail contacts import' without --apply, or re-run with "
                "--allow-duplicates to create them anyway."
            )
        return created

    _run(ctx, "contacts import", action, title="Contact import", warnings=warnings)


@contacts_app.command("correspondence")
def contacts_correspondence(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    limit: int = typer.Option(100, min=1, max=200),
) -> None:
    """Show a body-free bounded inbox/sent history for one mailbox contact."""

    def action() -> Any:
        service = _contacts_service(ctx)
        service.operation_preferences().require("history")
        contact = service.get(reference)
        return service.correspondence_history(contact.emails, limit=limit)

    _run(ctx, "contacts correspondence", action, title="Contact correspondence")


@directory_app.command("search")
def directory_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(25, min=1, max=100),
) -> None:
    """Resolve a partial name against the Exchange directory when permitted."""

    value = query.strip()
    if not value:
        raise typer.BadParameter("Directory search needs non-empty text.")
    _run(
        ctx,
        "directory search",
        lambda: _contacts_service(ctx).search_directory(value, limit=limit),
        title="Directory results",
        source="live_server",
        human_data=lambda rows: rows
        or (
            "No directory matches. The GUC directory may not be searchable from this "
            "account; search your own contacts with `gucmail contacts search`."
        ),
    )


@directory_app.command("resolve")
def directory_resolve(ctx: typer.Context, query: str = typer.Argument(...)) -> None:
    """Require one unambiguous Exchange directory result."""

    _run(
        ctx,
        "directory resolve",
        lambda: _contacts_service(ctx).resolve_directory(query),
        title="Resolved directory address",
        source="live_server",
    )


@contacts_app.command("history-index")
def contacts_history_index(
    ctx: typer.Context,
    show: bool = typer.Option(False, help="Show retained local address observations."),
    clear: bool = typer.Option(False, help="Clear this account's local index after confirmation."),
    address: str | None = typer.Option(None, help="Filter --show by one exact email address."),
    limit: int = typer.Option(100, min=1, max=200),
) -> None:
    """Inspect or clear the opt-in local observation index without connecting to Exchange."""

    if show and clear:
        raise typer.BadParameter("Choose --show or --clear, not both.")
    if address is not None and not show:
        raise typer.BadParameter("--address requires --show.")

    def action() -> Any:
        preferences = ContactOperationPreferences.from_settings(_settings_service(ctx))
        index = ContactHistoryIndex(_state(ctx).context.state)
        if clear:
            _confirm(
                ctx, "Clear this account's local contact history index?", "contact-history-clear"
            )
            return {"cleared": index.clear()}
        if show:
            if not preferences.history_indexing:
                raise ConfigurationError(
                    "Contact history indexing is disabled in Contacts settings."
                )
            return index.query(
                address, limit=limit, retention_days=preferences.history_retention_days
            )
        return {
            **index.status(retention_days=preferences.history_retention_days),
            "indexing_enabled": preferences.history_indexing,
            "mailbox_history_enabled": preferences.correspondence_history,
        }

    _run(ctx, "contacts history-index", action, title="Local contact history", source="local_state")


@recipients_app.command("recent")
def recipients_recent(
    ctx: typer.Context,
    since: str | None = typer.Option(None, help="Optional ISO-8601 lower bound with offset."),
    limit: int = typer.Option(20, min=1, max=100),
    scan: int = typer.Option(
        50,
        min=1,
        max=200,
        help="Maximum metadata observations; each folder reads at most this many items.",
    ),
) -> None:
    """Summarize recent correspondents from a bounded mailbox scan."""

    parsed_since = _iso_datetime(since, parameter="--since")
    warnings = [f"Bounded Inbox/Sent metadata: at most {scan} observations; no message bodies."]

    def action() -> Any:
        service = _contacts_service(ctx, index_warning=warnings.append)
        service.operation_preferences().require("history")
        email_value = _state(ctx).context.config.email
        excluded: tuple[str, ...] = (email_value,) if email_value else ()
        return service.recent_correspondents(
            service.read_correspondence(since=parsed_since, limit=scan),
            excluded_addresses=excluded,
            since=parsed_since,
            limit=limit,
        )

    _run(ctx, "recipients recent", action, title="Recent correspondents", warnings=warnings)


@recipients_app.command("frequent")
def recipients_frequent(
    ctx: typer.Context,
    since: str | None = typer.Option(None, help="Optional ISO-8601 lower bound with offset."),
    minimum_count: int = typer.Option(2, min=1),
    limit: int = typer.Option(20, min=1, max=100),
    scan: int = typer.Option(
        100,
        min=1,
        max=200,
        help="Maximum metadata observations; each folder reads at most this many items.",
    ),
) -> None:
    """Rank frequent correspondents from a bounded privacy-local mailbox scan."""

    parsed_since = _iso_datetime(since, parameter="--since")
    warnings = [f"Bounded Inbox/Sent metadata: at most {scan} observations; no message bodies."]

    def action() -> Any:
        service = _contacts_service(ctx, index_warning=warnings.append)
        service.operation_preferences().require("frequent")
        email_value = _state(ctx).context.config.email
        excluded: tuple[str, ...] = (email_value,) if email_value else ()
        return service.frequent_correspondents(
            service.read_correspondence(since=parsed_since, limit=scan),
            excluded_addresses=excluded,
            since=parsed_since,
            minimum_count=minimum_count,
            limit=limit,
        )

    _run(ctx, "recipients frequent", action, title="Frequent correspondents", warnings=warnings)


@tasks_app.command("list")
def tasks_list(
    ctx: typer.Context,
    view: TaskView | None = typer.Option(None, case_sensitive=False),
    completed: bool | None = typer.Option(
        None, "--completed/--no-completed", help="Override ordinary completed-task visibility."
    ),
    sort: TaskSortOrder | None = typer.Option(None, case_sensitive=False),
    limit: int = typer.Option(200, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """List one bounded task page using saved view, visibility and sorting."""

    warnings: list[str] = []

    def action() -> Any:
        display = TaskDisplayPreferences.from_settings(
            _settings_service(ctx), view=view, show_completed=completed, sort_order=sort
        )
        query = display.query()
        result = TaskService(DeferredGateway(lambda: _gateway(ctx))).page(
            query, limit=limit, offset=offset
        )
        return _pim_page_output(display.page(result, query), details=page, warnings=warnings)

    _run(
        ctx,
        "tasks list",
        action,
        title="Tasks",
        source="live_server",
        warnings=warnings,
    )


@tasks_app.command("search")
def tasks_search(
    ctx: typer.Context,
    text: str | None = typer.Argument(None),
    status: TaskStatus | None = typer.Option(None, case_sensitive=False),
    due_before: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    overdue: bool = typer.Option(False),
    completed: bool | None = typer.Option(
        None, "--completed/--no-completed", help="Explicit status takes precedence."
    ),
    sort: TaskSortOrder | None = typer.Option(None, case_sensitive=False),
    limit: int = typer.Option(200, min=1, max=200),
    offset: int = typer.Option(0, min=0, help="Candidate offset from the preceding page."),
    page: bool = typer.Option(False, help="Include candidate counts and next_offset in results."),
) -> None:
    """Search tasks by text, status, or due date."""

    query = TaskQuery(
        text=text,
        status=status,
        due_before=_iso_date(due_before, parameter="--due-before"),
    )
    warnings: list[str] = []

    def action() -> Any:
        if not any((query.text, query.status, query.due_before, overdue)):
            raise ConfigurationError("Task search needs at least one criterion.")
        display = TaskDisplayPreferences.from_settings(
            _settings_service(ctx), show_completed=completed, sort_order=sort
        )
        criteria = display.search_query(
            replace(query, overdue_at=display.today() if overdue else None)
        )
        result = TaskService(DeferredGateway(lambda: _gateway(ctx))).page(
            criteria, limit=limit, offset=offset
        )
        return _pim_page_output(
            display.page(result, criteria),
            details=page,
            warnings=warnings,
        )

    _run(
        ctx,
        "tasks search",
        action,
        title="Task results",
        warnings=warnings,
    )


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one task."""

    _run(ctx, "tasks show", lambda: TaskService(_gateway(ctx)).get(reference), title="Task")


@tasks_app.command("history")
def tasks_history(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    limit: int = typer.Option(20, min=1, max=200),
    before: int | None = typer.Option(None, min=1, help="Continue before the returned sequence."),
) -> None:
    """Read bounded local accepted-update history without contacting Exchange."""
    from gucmail.services.task_history import TaskHistory, history_reference, history_text

    def read() -> Any:
        checked = history_reference(reference)
        return TaskHistory(_state(ctx).context.state).page(checked, limit=limit, before=before)

    _run(
        ctx,
        "tasks history",
        read,
        title="Local Task history",
        source="local",
        human_data=history_text,
    )


@tasks_app.command("create")
def tasks_create(
    ctx: typer.Context,
    subject: str = typer.Option(...),
    due: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    no_due: bool = typer.Option(
        False, "--no-due", help="Leave the due date unset instead of using the saved default."
    ),
    start: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    reminder_at: str | None = typer.Option(
        None,
        help="ISO timestamp with UTC offset, for example 2026-08-24T09:00:00+02:00.",
    ),
    no_reminder: bool = typer.Option(
        False, "--no-reminder", help="Disable the saved reminder for this task."
    ),
    body: str = typer.Option(""),
    importance: Importance | None = typer.Option(
        None, case_sensitive=False, help="Override the saved importance."
    ),
    repeat: TaskRepeatKind | None = typer.Option(
        None,
        case_sensitive=False,
        help="Override the saved repeat pattern; after_completion_* uses task regeneration; "
        "none disables repetition.",
    ),
    repeat_interval: int | None = typer.Option(
        None,
        help="Repeat every N periods; fixed-date yearly requires 1; after-completion uses 1-999.",
    ),
    repeat_count: int | None = typer.Option(
        None, help="Occurrence limit, including the first (1-999)."
    ),
    repeat_forever: bool = typer.Option(
        False, help="Clear the saved occurrence limit for this task."
    ),
    repeat_until: str | None = typer.Option(
        None, help="Last repeating date (YYYY-MM-DD); overrides the saved count."
    ),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create an Exchange task."""

    def action() -> Any:
        if no_due and due is not None:
            raise ConfigurationError("Choose either --due or --no-due.")
        if no_reminder and reminder_at is not None:
            raise ConfigurationError("Choose either --reminder-at or --no-reminder.")
        settings = _settings_service(ctx)
        preferences = TaskCreationPreferences.from_settings(settings)
        if sum((repeat_count is not None, repeat_forever, repeat_until is not None)) > 1:
            raise ConfigurationError("Choose --repeat-count, --repeat-forever or --repeat-until.")
        repeat_policy = TaskRepeatPreferences(
            repeat if repeat is not None else preferences.repeat.kind,
            repeat_interval if repeat_interval is not None else preferences.repeat.interval,
            repeat_count
            if repeat_count is not None
            else None
            if repeat_forever or repeat_until is not None
            else preferences.repeat.count,
        )
        if repeat_policy.kind is TaskRepeatKind.NONE and any(
            (
                repeat_interval is not None,
                repeat_count is not None,
                repeat_forever,
                repeat_until is not None,
            )
        ):
            raise ConfigurationError("Choose a repeat pattern before supplying repeat options.")
        service = TaskService(DeferredGateway(lambda: _gateway(ctx)))
        task = replace(
            preferences.new_task(
                subject=subject,
                timezone=str(settings.value("calendar.timezone").effective_value),
                start_date=_iso_date(start, parameter="--start"),
                due_date=None
                if no_due
                else _iso_date(due, parameter="--due")
                if due is not None
                else PIM_UNSET,
                reminder_at=None
                if no_reminder
                else _iso_datetime(reminder_at, parameter="--reminder-at")
                if reminder_at is not None
                else PIM_UNSET,
                importance=importance,
                repeat=repeat_policy,
                repeat_until=_iso_date(repeat_until, parameter="--repeat-until"),
            ),
            body=body,
        )
        plan = service.plan_create(task)
        if dry_run:
            return plan
        _confirm(ctx, f"Create task {subject!r}?", f"task-create:{subject}")
        return service.gateway.create_task(plan)

    _run(ctx, "tasks create", action, title="Task creation")


@tasks_app.command("edit")
@tasks_app.command("update")
def tasks_update(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    subject: str | None = typer.Option(None),
    due: str | None = typer.Option(None, help="ISO date (YYYY-MM-DD)."),
    reminder_at: str | None = typer.Option(
        None,
        help="ISO timestamp with UTC offset.",
    ),
    clear_reminder: bool = typer.Option(False, help="Disable the task reminder."),
    body: str | None = typer.Option(None),
    percent: int | None = typer.Option(None, min=0, max=100),
    dry_run: bool = typer.Option(False),
) -> None:
    """Update only supplied task fields."""

    if reminder_at is not None and clear_reminder:
        raise typer.BadParameter(
            "use either --reminder-at or --clear-reminder, not both",
            param_hint="--reminder-at",
        )

    def action() -> Any:
        changes: dict[str, Any] = {
            name: value
            for name, value in (
                ("subject", subject),
                ("due_date", _iso_date(due, parameter="--due")),
                ("body", body),
                ("percent_complete", percent),
            )
            if value is not None
        }
        if reminder_at is not None:
            changes["reminder_at"] = _iso_datetime(reminder_at, parameter="--reminder-at")
        elif clear_reminder:
            changes["reminder_at"] = None
        service = TaskService(_gateway(ctx))
        current = None
        if percent is not None:
            # --percent is the only progress control here, so it must move the
            # lifecycle state with it instead of tripping the status invariants.
            if percent == 100:
                raise ConfigurationError(
                    "Use 'gucmail tasks complete' to mark a task 100 percent complete."
                )
            current = service.get(reference)
            if current.status == TaskStatus.COMPLETED.value:
                raise ConfigurationError(
                    "This task is completed. Use 'gucmail tasks reopen' before changing progress."
                )
            if percent > 0 and current.status == TaskStatus.NOT_STARTED.value:
                changes["status"] = TaskStatus.IN_PROGRESS.value
        plan = service.plan_update(reference, TaskPatch(**changes), current=current)
        if dry_run:
            return plan
        _confirm(ctx, f"Update task {reference}?", f"task-update:{reference}")
        return service.gateway.update_task(plan)

    _run(ctx, "tasks update", action, title="Task update")


@tasks_app.command("complete")
def tasks_complete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Complete a task using explicit status and percentage fields."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_complete(reference)
        if dry_run:
            return plan
        _confirm(ctx, f"Complete task {reference}?", f"task-complete:{reference}")
        return _gateway(ctx).update_task(plan)

    _run(ctx, "tasks complete", action, title="Task complete")


@tasks_app.command("reopen")
def tasks_reopen(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    dry_run: bool = typer.Option(False),
) -> None:
    """Reopen a completed task."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_reopen(reference)
        if dry_run:
            return plan
        _confirm(ctx, f"Reopen task {reference}?", f"task-reopen:{reference}")
        return _gateway(ctx).update_task(plan)

    _run(ctx, "tasks reopen", action, title="Task reopen")


@tasks_app.command("delete")
def tasks_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(
        False, help="Permanently delete this task occurrence; otherwise move the stored task."
    ),
    series: bool = typer.Option(
        False, help="With --permanent, delete the entire recurring task series."
    ),
    confirm_series: str | None = typer.Option(
        None, help="For actual series deletion, repeat the exact task reference after reviewing."
    ),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move a task to Deleted Items; permanent deletion is explicit."""

    def action() -> Any:
        service = TaskService()
        plan = service.plan_delete(reference, hard_delete=permanent, delete_series=series)
        if confirm_series is not None and (not series or confirm_series != reference):
            raise ConfigurationError(
                "--confirm-series must match the task reference with --series."
            )
        if dry_run:
            return plan
        if series and confirm_series != reference:
            raise ConfirmationRequiredError(
                "Permanent series deletion requires --confirm-series with the exact task reference "
                "as well as ordinary confirmation. Review --dry-run first."
            )
        scope = (
            "whole recurring series"
            if series
            else "this occurrence"
            if permanent
            else "stored task"
        )
        _confirm(
            ctx, f"Delete {scope} {reference}? Permanent: {permanent}.", f"task-delete:{reference}"
        )
        TaskService(_gateway(ctx)).delete(plan)
        return {"reference": reference, "deleted": True, "permanent": permanent, "series": series}

    _run(ctx, "tasks delete", action, title="Task deletion")


@tasks_app.command("export")
def tasks_export(
    ctx: typer.Context,
    output: Path = typer.Option(...),
    format: str = typer.Option("json", help="json or csv"),
    limit: int = typer.Option(200, min=1, max=200),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing export file."),
) -> None:
    """Export a bounded task list to JSON or CSV."""

    def action() -> dict[str, Any]:
        service = TaskService(_gateway(ctx))
        tasks = service.list(limit=limit)
        content = service.export(tasks, format=format)
        # Same overwrite policy as calendar/contacts export: never silently
        # replace an existing user file.
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            _confirm(ctx, f"Overwrite existing task export {output}?", f"tasks-export:{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "tasks": len(tasks), "format": format}

    _run(ctx, "tasks export", action, title="Task export")


def _passphrase(ctx: typer.Context, *, stdin: bool, required: bool) -> str | None:
    if not required:
        return None
    if stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    elif _state(ctx).non_interactive or not sys.stdin.isatty():
        raise ConfirmationRequiredError(
            "An encrypted backup passphrase is required; use --passphrase-stdin."
        )
    else:
        value = getpass("Backup passphrase: ")
    if len(value) < 12:
        raise typer.BadParameter("Backup passphrases must contain at least 12 characters.")
    return value


def _backup_service(ctx: typer.Context) -> BackupService:
    context = _state(ctx).context
    return BackupService(context.paths.backup_dir, context.state)


@backup_app.command("list")
def backup_list(
    ctx: typer.Context,
    include_incomplete: bool = typer.Option(False),
    limit: int = typer.Option(200, min=1, max=10_000),
) -> None:
    """List local complete and optionally resumable incomplete backups."""

    _run(
        ctx,
        "backup list",
        lambda: _backup_service(ctx).list(
            include_incomplete=include_incomplete,
            limit=limit,
            inspect_corrupt_sizes=False,
        ),
        title="Backups",
        source="backup",
    )


@backup_app.command("create")
def backup_create(
    ctx: typer.Context,
    folder: list[str] = typer.Option([], "--folder", help="Limit to selected folder(s)."),
    message: list[str] = typer.Option([], "--message", help="Limit to selected message(s)."),
    item_kind: list[str] = typer.Option([], "--item-kind"),
    exclude: list[str] = typer.Option([], "--exclude", help="Local fnmatch exclusion."),
    encrypt: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
    no_compression: bool = typer.Option(False, "--no-compression"),
    estimate_only: bool = typer.Option(False),
) -> None:
    """Create a content-addressed full or selected mailbox backup."""

    def action() -> Any:
        if folder and message:
            raise typer.BadParameter("Choose folders or messages, not both.")
        kind = (
            BackupKind.SELECTED_FOLDERS
            if folder
            else BackupKind.SELECTED_MESSAGES
            if message
            else BackupKind.FULL
        )
        selection = BackupSelection(
            folders=tuple(folder), messages=tuple(message), item_kinds=tuple(item_kind)
        )
        request = BackupRequest(
            kind=kind,
            selection=selection,
            exclusions=tuple(exclude),
            options=BackupOptions(compression=not no_compression),
        )
        service = _backup_service(ctx)
        gateway = _gateway(ctx)
        if estimate_only:
            return service.estimate(gateway, request, encrypted=encrypt)
        passphrase = _passphrase(ctx, stdin=passphrase_stdin, required=encrypt or passphrase_stdin)
        return service.create(gateway, request, passphrase=passphrase)

    _run(ctx, "backup create", action, title="Mailbox backup")


@backup_app.command("incremental")
def backup_incremental(
    ctx: typer.Context,
    parent: str = typer.Argument(..., help="Parent backup ID."),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Rescan the parent's saved scope and store changes; selected references stay fixed."""

    def action() -> Any:
        passphrase = _passphrase(
            ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
        )
        return _backup_service(ctx).incremental(
            _gateway(ctx),
            parent,
            sync_state={
                "strategy": "bounded_rescan",
                "captured_at": datetime.now(UTC).isoformat(),
            },
            passphrase=passphrase,
        )

    _run(ctx, "backup incremental", action, title="Incremental backup")


@backup_app.command("resume")
def backup_resume(
    ctx: typer.Context,
    identifier: str = typer.Argument(..., help="Incomplete backup ID or checkpoint path."),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Resume an atomic incomplete backup from its durable checkpoint."""

    def action() -> Any:
        return _backup_service(ctx).resume(
            identifier,
            _gateway(ctx),
            passphrase=_passphrase(
                ctx,
                stdin=passphrase_stdin,
                required=encrypted or passphrase_stdin,
            ),
        )

    _run(ctx, "backup resume", action, title="Resumed backup")


@backup_app.command("estimate")
def backup_estimate(
    ctx: typer.Context,
    folder: list[str] = typer.Option([], "--folder"),
    message: list[str] = typer.Option([], "--message"),
    item_kind: list[str] = typer.Option([], "--item-kind"),
    exclude: list[str] = typer.Option([], "--exclude"),
    encrypted: bool = typer.Option(False),
    no_compression: bool = typer.Option(False, "--no-compression"),
) -> None:
    """Estimate bounded source counts and bytes before writing a backup."""

    if folder and message:
        raise typer.BadParameter("Choose folders or messages, not both.")
    kind = (
        BackupKind.SELECTED_FOLDERS
        if folder
        else BackupKind.SELECTED_MESSAGES
        if message
        else BackupKind.FULL
    )
    request = BackupRequest(
        kind=kind,
        selection=BackupSelection(
            folders=tuple(folder),
            messages=tuple(message),
            item_kinds=tuple(item_kind),
        ),
        exclusions=tuple(exclude),
        options=BackupOptions(compression=not no_compression),
    )
    _run(
        ctx,
        "backup estimate",
        lambda: _backup_service(ctx).estimate(_gateway(ctx), request, encrypted=encrypted),
        title="Backup estimate",
    )


@backup_app.command("inspect")
def backup_inspect(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Inspect one manifest without connecting to Exchange."""

    _run(
        ctx,
        "backup inspect",
        lambda: _backup_service(ctx).inspect(
            identifier,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
        ),
        title="Backup manifest",
        source="backup",
    )


@backup_app.command("verify")
def backup_verify(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    quick: bool = typer.Option(False, help="Check stored hashes without decoding every object."),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Verify manifest, object hashes, sizes, and optionally decoded content."""

    report = _run(
        ctx,
        "backup verify",
        lambda: _backup_service(ctx).verify(
            identifier,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
            full=not quick,
        ),
        title="Backup verification",
        source="backup",
    )
    # A report with valid=False (corruption, wrong passphrase, missing objects)
    # used to exit 0, so scripts treated a failed verification as success.
    if report is not None and not getattr(report, "valid", True):
        raise typer.Exit(code=int(ExitCode.LOCAL_STATE))


@backup_app.command("browse")
def backup_browse(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    kind: str | None = typer.Option(None),
    folder: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1, max=500),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Browse a verified backup entirely offline."""

    def action() -> Any:
        archive = _backup_service(ctx).open_offline(
            identifier,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
        )
        return archive.list_items(kind=kind, folder=folder, limit=limit)

    _run(ctx, "backup browse", action, title="Offline backup", source="backup")


@backup_app.command("search")
def backup_search(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    query: str = typer.Argument(...),
    fuzzy: bool = typer.Option(False),
    limit: int = typer.Option(50, min=1, max=500),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Search one verified backup without Exchange access."""

    def action() -> Any:
        archive = _backup_service(ctx).open_offline(
            identifier,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
        )
        return archive.search(query, fuzzy=fuzzy, limit=limit)

    _run(ctx, "backup search", action, title="Offline search", source="backup")


@backup_app.command("export")
def backup_export(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    output: Path = typer.Option(...),
    overwrite: bool = typer.Option(False),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Export a verified backup chain to a portable archive."""

    def action() -> dict[str, Any]:
        if overwrite:
            _confirm(ctx, f"Overwrite backup export {output}?", f"backup-export:{output}")
        path = _backup_service(ctx).export(
            identifier,
            output,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
            overwrite=overwrite,
        )
        return {"path": str(path)}

    _run(ctx, "backup export", action, title="Backup export", source="backup")


@backup_app.command("restore-plan")
def backup_restore_plan(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    destination: str = typer.Option("Restored by GUC Mail CLI"),
    selected: list[str] = typer.Option([], "--selected"),
    another_copy: bool = typer.Option(
        False, "--another-copy",
        help="Review another copy of completed restores; unconfirmed items stay blocked.",
    ),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Build a no-mutation restore plan with destinations and duplicate policy."""

    _run(
        ctx,
        "backup restore-plan",
        lambda: _backup_service(ctx).plan_restore(
            identifier,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
            destination_root=destination,
            selected_references=selected or None,
            allow_completed_duplicates=another_copy,
        ),
        title="Restore plan",
        source="backup",
    )


@backup_app.command("restore")
def backup_restore(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    destination: str = typer.Option("Restored by GUC Mail CLI"),
    selected: list[str] = typer.Option([], "--selected"),
    another_copy: bool = typer.Option(
        False, "--another-copy",
        help="Review another copy of completed restores; unconfirmed items stay blocked.",
    ),
    apply: bool = typer.Option(False, help="Attempt the provider-specific restore after preview."),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Plan restore by default; provider execution is explicit and capability-gated."""

    warnings: list[str] = []

    def action() -> Any:
        service = _backup_service(ctx)
        passphrase = _passphrase(
            ctx,
            stdin=passphrase_stdin,
            required=encrypted or passphrase_stdin,
        )
        plan = service.plan_restore(
            identifier,
            passphrase=passphrase,
            destination_root=destination,
            selected_references=selected or None,
            allow_completed_duplicates=another_copy,
        )
        if apply and not plan.create_count:
            # --apply used to print an unchanged plan with exit 0, which read as a
            # completed restore even when --selected matched nothing.
            warnings.append(
                "Nothing was restored: the plan has no items to create."
                + (
                    " No backup item matched --selected."
                    if selected and not plan.entries
                    else ""
                )
            )
        if not apply or not plan.create_count:
            return plan
        _confirm(
            ctx,
            f"Restore {plan.create_count} item(s) into {destination!r}?"
            + (
                " This deliberately creates another copy of recorded completed restores."
                if another_copy else ""
            )
            + (f" {plan.blocked_count} unconfirmed item(s) will not be repeated."
               if plan.blocked_count else ""),
            plan.confirmation_token,
        )
        return service.restore(
            identifier,
            passphrase=passphrase,
            confirmed=True,
            target=_gateway(ctx),
            confirmation_token=plan.confirmation_token,
            destination_root=destination,
            selected_references=selected or None,
            allow_completed_duplicates=another_copy,
        )

    result = _run(ctx, "backup restore", action, title="Mailbox restore", warnings=warnings)
    if isinstance(result, RestoreResult) and (result.interrupted or result.cancelled):
        raise typer.Exit(code=int(ExitCode.INTERRUPTED))
    if isinstance(result, RestoreResult) and (
        result.failed_count or result.blocked_count or result.recovery_write_failed
    ):
        raise typer.Exit(code=int(ExitCode.PARTIAL))
    if apply and isinstance(result, RestorePlan) and result.blocked_count:
        raise typer.Exit(code=int(ExitCode.PARTIAL))


@backup_app.command("restore-status")
def backup_restore_status(
    ctx: typer.Context,
    operation_id: str | None = typer.Argument(None),
    limit: int = typer.Option(25, min=1, max=200),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Inspect paged local restore history without connecting or replaying a write."""

    def action() -> Any:
        from gucmail.errors import ItemNotFoundError
        from gucmail.services.restore_journal import RestoreJournal

        journal = RestoreJournal(_state(ctx).context.state)
        if operation_id is None:
            return journal.operations(limit=limit, offset=offset)
        result = journal.operation(operation_id, limit=limit, offset=offset)
        if result is None:
            raise ItemNotFoundError("The local restore operation was not found.")
        return result

    _run(ctx, "backup restore-status", action, title="Local restore recovery", source="local_state")


@backup_app.command("prune")
def backup_prune(
    ctx: typer.Context,
    keep_last: int = typer.Option(3, min=1),
    apply: bool = typer.Option(False),
) -> None:
    """Preview retention; --apply moves candidates to recoverable local trash."""

    def action() -> Any:
        service = _backup_service(ctx)
        planned = service.prune(keep_last=keep_last)
        if not isinstance(planned, PrunePlan):
            raise RuntimeError("Backup prune unexpectedly executed while planning.")
        if not apply:
            return planned
        _confirm(
            ctx,
            f"Move {len(planned.candidates)} backup(s) to local trash?",
            planned.confirmation_token,
        )
        return service.execute_prune(planned, confirmation_token=planned.confirmation_token)

    _run(ctx, "backup prune", action, title="Backup retention", source="backup")


@index_app.command("status")
def index_status(ctx: typer.Context) -> None:
    """Show local FTS document count, size, and last update."""

    _run(
        ctx,
        "index status",
        lambda: SearchIndex(_state(ctx).context.paths.index_database).stats(),
        title="Search index",
        source="search_index",
    )


@index_app.command("update")
@index_app.command("build")
def index_build(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
    include_bodies: bool = typer.Option(False, help="Fetch and index safe text bodies."),
) -> None:
    """Build or refresh a bounded local index page from Exchange."""

    def action() -> dict[str, Any]:
        preferences = attachment_index_preferences(_settings_service(ctx))
        if preferences.enabled:
            _confirm(
                ctx,
                f"Index one bounded page from {folder!r} (limit {limit}). " + preferences.review(),
                f"index-attachments:{folder}:{limit}:{include_bodies}:{preferences.fingerprint}",
            )
        if attachment_index_preferences(_settings_service(ctx)) != preferences:
            raise ConflictError(
                "Attachment indexing settings changed after review; retry with the current policy."
            )
        gateway = _gateway(ctx)
        producer = MailboxIndexProducer(gateway, preferences)
        index = SearchIndex(_state(ctx).context.paths.index_database)
        count = index.upsert_many(
            producer.documents(folder=folder, limit=limit, include_bodies=include_bodies)
        )
        return {
            "indexed": count,
            "attachment_processing": producer.report.summary(),
            "folder": folder,
            "bodies": include_bodies,
            "stats": index.stats(),
        }

    _run(ctx, "index build", action, title="Index build", source="search_index")


@index_app.command("rebuild")
def index_rebuild(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
    include_bodies: bool = typer.Option(False),
) -> None:
    """Replace the disposable local index with one bounded mailbox view."""

    def action() -> dict[str, Any]:
        index = SearchIndex(_state(ctx).context.paths.index_database)
        revision = index.revision()
        preferences = attachment_index_preferences(_settings_service(ctx))
        scope = hashlib.sha256(
            json.dumps(
                [
                    folder,
                    limit,
                    include_bodies,
                    preferences.fingerprint,
                    revision.documents,
                    revision.updated_at,
                    revision.revision_id,
                ],
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        _confirm(
            ctx,
            f"Replace {revision.documents} disposable index rows from "
            f"{sanitize_terminal(folder, max_length=200)!r}, at most {limit} messages, "
            f"bodies={include_bodies}, up to 32 MiB prepared data? "
            "The prior index remains until the atomic commit. " + preferences.review(),
            "index-rebuild:v2:" + scope,
        )
        if attachment_index_preferences(_settings_service(ctx)) != preferences:
            raise ConflictError(
                "Attachment indexing settings changed after review; retry with the current policy."
            )
        gateway = _gateway(ctx)
        producer = MailboxIndexProducer(gateway, preferences)
        replaced = index.replace_many(
            producer.documents(folder=folder, limit=limit, include_bodies=include_bodies),
            limit=limit,
            expected_revision=revision,
        )
        return {
            "removed": replaced.removed,
            "indexed": replaced.indexed,
            "attachment_processing": producer.report.summary(),
            "stats": index.stats(),
        }

    _run(ctx, "index rebuild", action, title="Index rebuild", source="search_index")


@index_app.command("verify")
def index_verify(ctx: typer.Context) -> None:
    """Run SQLite integrity, schema, and FTS row-count checks."""

    _run(
        ctx,
        "index verify",
        lambda: SearchIndex(_state(ctx).context.paths.index_database).verify(),
        title="Index verification",
        source="search_index",
    )


@index_app.command("optimize")
def index_optimize(ctx: typer.Context) -> None:
    """Compact local FTS segments and refresh SQLite planner statistics."""

    _run(
        ctx,
        "index optimize",
        lambda: SearchIndex(_state(ctx).context.paths.index_database).optimize(),
        title="Index optimized",
        source="search_index",
    )


@index_app.command("search")
def index_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    folder: str | None = typer.Option(None),
    fuzzy: bool = typer.Option(False),
    threshold: int = typer.Option(75, min=0, max=100),
    limit: int = typer.Option(50, min=1, max=500),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Search locally with FTS5 or deterministic bounded fuzzy ranking."""

    _run(
        ctx,
        "index search",
        lambda: SearchIndex(_state(ctx).context.paths.index_database).search(
            query,
            folder=folder,
            fuzzy=fuzzy,
            fuzzy_threshold=threshold,
            limit=limit,
            offset=offset,
        ),
        title="Local search results",
        source="search_index",
    )


@index_app.command("clear")
def index_clear(ctx: typer.Context, dry_run: bool = typer.Option(False)) -> None:
    """Clear only the disposable local search index."""

    def action() -> Any:
        index = SearchIndex(_state(ctx).context.paths.index_database)
        if dry_run:
            return {"dry_run": True, "documents": index.stats().documents}
        _confirm(ctx, "Clear the disposable local full-text index?", "index-clear:v1")
        return {"cleared": index.clear()}

    _run(ctx, "index clear", action, title="Index clear", source="search_index")


def _semantic_service(ctx: typer.Context) -> SemanticSearchService:
    context = _state(ctx).context
    enabled = bool(_settings_service(ctx).value("search.semantic_enabled").effective_value)
    return SemanticSearchService(
        SearchIndex(context.paths.index_database),
        SemanticStore(
            context.paths.data_dir / "semantic.sqlite3",
            identity=account_identity(context.config.email or "", context.config.endpoint),
            enabled=enabled,
        ),
    )


@ocr_app.command("status")
def ocr_status(ctx: typer.Context) -> None:
    """Inspect installed local recognizer languages; never install or recognize an image."""
    _run(ctx, "ocr status", lambda: OcrService().status(), title="Local OCR", source="local")


@ocr_app.command("extract")
def ocr_extract(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="One already saved local PNG or JPEG."),
    language: str = typer.Option(..., help="Exact installed tag from ocr status, such as en-US."),
    apply: bool = typer.Option(False, help="Opt in to recognition after reviewing the preview."),
    timeout: float = typer.Option(8, min=1, max=15, help="Owned recognition worker time limit."),
    max_chars: int = typer.Option(50000, min=1, max=100000, help="Maximum recognized output."),
) -> None:
    """Preview a bounded local image; --apply and confirmation recognize it locally."""

    def action() -> Any:
        service = OcrService()
        plan = service.preview(
            path, language=language, timeout_seconds=timeout, max_output_chars=max_chars
        )
        if not apply:
            return plan.as_dict()
        _confirm(
            ctx,
            str(plan.as_dict()["privacy"]) + " " + str(plan.as_dict()["accuracy"]),
            "ocr:" + plan.sha256 + ":" + plan.language,
        )
        result = service.extract(plan, enabled=True)
        if result.status != "success":
            raise ConfigurationError(result.error or "Local OCR is unavailable. Run ocr status.")
        return result.as_dict()

    _run(ctx, "ocr extract", action, title="Local OCR", source="local")


@semantic_app.command("status")
def semantic_status(ctx: typer.Context) -> None:
    """Show semantic-search consent and implementation status without network access."""

    _run(
        ctx,
        "semantic status",
        lambda: _semantic_service(ctx).status(),
        title="Semantic search",
        source="local",
    )


@semantic_app.command("enable")
def semantic_enable(ctx: typer.Context) -> None:
    """Consent to a private corpus-trained model; does not download or build it."""

    def action() -> Any:
        _confirm(
            ctx,
            "Enable optional local topic search. Building saves vocabulary, title metadata and "
            "embeddings locally; bodies require a separate build opt-in. No remote model is used.",
            "local-semantic-consent",
        )
        result = _settings_service(ctx).set("search.semantic_enabled", True)
        _state(ctx).context.reload_config()
        return {
            "enabled": _semantic_service(ctx).store.enabled,
            "revision": result.revision_id if result is not None else None,
        }

    _run(ctx, "semantic enable", action, title="Semantic consent", source="local")


@semantic_app.command("disable")
def semantic_disable(ctx: typer.Context) -> None:
    """Disable semantic processing while retaining the model until explicitly cleared."""

    def action() -> Any:
        result = _settings_service(ctx).set("search.semantic_enabled", False)
        _state(ctx).context.reload_config()
        return {
            "enabled": _semantic_service(ctx).store.enabled,
            "revision": result.revision_id if result is not None else None,
        }

    _run(ctx, "semantic disable", action, title="Semantic consent", source="local")


@semantic_app.command("build")
@semantic_app.command("update")
def semantic_build(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Apply the reviewed local build."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Retrain the local corpus-topic model."),
    limit: int | None = typer.Option(
        None, min=2, max=2000, help="Latest local records; initially 500."
    ),
    dimensions: int | None = typer.Option(None, min=1, max=96, help="Initial maximum 32 topics."),
    include_body: bool | None = typer.Option(
        None, "--include-body/--subjects-only", help="Explicitly select indexed body chunks."
    ),
) -> None:
    """Preview/build/update from existing local text. No mailbox fetch or model download."""

    def action() -> Any:
        service = _semantic_service(ctx)
        options: dict[str, Any] = dict(
            limit=limit, dimensions=dimensions, include_body=include_body, rebuild=rebuild
        )
        preview = service.preview(**options)
        if not apply:
            return preview
        _confirm(
            ctx,
            f"Build local-lsa-v1 from {preview['selected_documents']} local records; "
            f"body chunks: {preview['include_body']}; retrain: {rebuild}. "
            "Save vocabulary, title/folder metadata and embeddings in local semantic storage.",
            "local-semantic-build",
        )
        return service.build(
            confirmed=True, expected_source_digest=preview["source_digest"], **options
        )

    _run(ctx, "semantic build", action, title="Local topic model", source="local")


@semantic_app.command("search")
def semantic_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(50, min=1, max=500),
    semantic_weight: float = typer.Option(0.8, min=0.0, max=1.0),
) -> None:
    """Rank local topic projections and lexical overlap; reject stale indexed text."""

    if not query.strip():
        raise typer.BadParameter("Semantic search needs non-empty text.")
    _run(
        ctx,
        "semantic search",
        lambda: _semantic_service(ctx).search(query, limit=limit, semantic_weight=semantic_weight),
        title="Local topic matches",
        source="local",
    )


@semantic_app.command("clear")
def semantic_clear(ctx: typer.Context) -> None:
    """Confirm removal of the local topic model and embeddings; retain the lexical index."""

    def action() -> Any:
        _confirm(
            ctx, "Remove the local semantic model, vocabulary and embeddings.", "semantic-clear"
        )
        return _semantic_service(ctx).clear(confirmed=True)

    _run(
        ctx,
        "semantic clear",
        action,
        title="Semantic index clear",
        source="local",
    )


def _clusters(
    ctx: typer.Context,
    *,
    folder: str,
    by: str,
    limit: int,
) -> Any:
    try:
        return cluster_messages(
            scan_message_summaries(_gateway(ctx), folder=folder, limit=limit),
            by=by,
            max_items=limit,
        )
    except (TypeError, ValueError) as exc:
        # An unsupported --by value surfaced as a generic internal error (exit 12).
        raise ConfigurationError(str(exc)) from exc


@clusters_app.command("build")
@clusters_app.command("rebuild")
@clusters_app.command("list")
def clusters_list(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    by: str = typer.Option(
        "topic", help="topic, sender, domain, thread, month, or attachment_type"
    ),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Build deterministic explainable clusters from a bounded mailbox view."""

    _run(
        ctx,
        "clusters list",
        lambda: _clusters(ctx, folder=folder, by=by, limit=limit),
        title="Mailbox clusters",
    )


@clusters_app.command("show")
def clusters_show(
    ctx: typer.Context,
    cluster_id: str = typer.Argument(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Rebuild a bounded view and show one deterministic cluster ID."""

    def action() -> Any:
        for cluster in _clusters(ctx, folder=folder, by=by, limit=limit):
            if cluster.cluster_id == cluster_id:
                return cluster
        raise typer.BadParameter("Cluster ID was not present in the selected bounded view.")

    _run(ctx, "clusters show", action, title="Mailbox cluster")


@clusters_app.command("search")
def clusters_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Filter deterministic cluster labels, explanations, and common terms locally."""

    needle = query.casefold().strip()
    if not needle:
        raise typer.BadParameter("Cluster search needs non-empty text.")

    def action() -> tuple[Any, ...]:
        return tuple(
            cluster
            for cluster in _clusters(ctx, folder=folder, by=by, limit=limit)
            if needle
            in " ".join(
                (
                    cluster.label,
                    cluster.explanation,
                    *(term for term, _count in cluster.common_terms),
                )
            ).casefold()
        )

    _run(ctx, "clusters search", action, title="Cluster results")


@clusters_app.command("export")
def clusters_export(
    ctx: typer.Context,
    output: Path = typer.Option(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
    overwrite: bool = typer.Option(False),
) -> None:
    """Export deterministic cluster summaries as canonical JSON."""

    def action() -> dict[str, Any]:
        if output.exists() and not overwrite:
            raise typer.BadParameter("Output exists; use --overwrite after reviewing the path.")
        if output.exists():
            _confirm(ctx, f"Overwrite cluster export {output}?", f"clusters-export:{output}")
        clusters = _clusters(ctx, folder=folder, by=by, limit=limit)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps([cluster.as_dict() for cluster in clusters], indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        return {"path": str(output), "clusters": len(clusters), "by": by}

    _run(ctx, "clusters export", action, title="Cluster export")


def _insight_source(ctx: typer.Context, *, folder: str, limit: int) -> Any:
    return scan_message_summaries(_gateway(ctx), folder=folder, limit=limit)


@insights_app.command("overview")
@insights_app.command("analyze")
def insights_analyze(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze storage, senders, and duplicates without changing messages."""

    _run(
        ctx,
        "insights analyze",
        lambda: analyze_mailbox(
            scan_message_summaries(_gateway(ctx), folder=folder, limit=limit), max_items=limit
        ),
        title="Mailbox insights",
    )


@insights_app.command("storage")
def insights_storage(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze bounded storage metadata by folder, sender, time, and attachment type."""

    _run(
        ctx,
        "insights storage",
        lambda: storage_insights(_insight_source(ctx, folder=folder, limit=limit), max_items=limit),
        title="Storage insights",
    )


@insights_app.command("senders")
def insights_senders(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze bounded sender frequency and estimated storage."""

    _run(
        ctx,
        "insights senders",
        lambda: sender_insights(
            _insight_source(ctx, folder=folder, limit=limit),
            limit=limit,
            max_items=limit,
        ),
        title="Sender insights",
    )


@insights_app.command("duplicates")
def insights_duplicates(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    near: bool = typer.Option(False, help="Also perform bounded near-duplicate comparison."),
    threshold: int = typer.Option(92, min=0, max=100),
    limit: int = typer.Option(500, min=2, max=500),
) -> None:
    """Find exact and optionally bounded near-duplicate messages locally."""

    _run(
        ctx,
        "insights duplicates",
        lambda: duplicate_insights(
            _insight_source(ctx, folder=folder, limit=limit),
            include_near=near,
            similarity_threshold=threshold,
            max_items=limit,
        ),
        title="Duplicate insights",
    )


@insights_app.command("unanswered")
def insights_unanswered(
    ctx: typer.Context,
    self_address: list[str] = typer.Option(
        [], "--self-address", help="Your sending addresses/aliases."
    ),
    folder: list[str] = typer.Option(["inbox"], "--folder", help="Incoming candidate folders."),
    sent_folder: list[str] = typer.Option(
        ["sent"], "--sent-folder", help="Folders containing sent mail."
    ),
    limit: int = typer.Option(100, min=1, max=1000, help="Metadata records per folder."),
) -> None:
    """Review conversations with no later self message in the bounded folder scope."""

    def action() -> Any:
        from gucmail.services.unanswered import UnansweredService, normalize_self_addresses

        primary = _state(ctx).context.config.email
        addresses = normalize_self_addresses(([primary] if primary else []) + self_address)
        return UnansweredService(_gateway(ctx)).analyze(
            self_addresses=addresses,
            folders=folder,
            sent_folders=sent_folder,
            limit=limit,
        )

    _run(ctx, "insights unanswered", action, title="Unanswered messages")


@insights_app.command("quota")
def insights_quota(ctx: typer.Context) -> None:
    """Show mailbox quota evidence and local disk capacity as separate measurements."""

    _run(ctx, "insights quota", lambda: _quota_action(ctx), title="Storage capacity")


def _cleanup_plan(
    ctx: typer.Context,
    *,
    folder: str,
    limit: int,
    include_old_unread: bool,
    old_unread_days: int,
    exclusions: list[str],
) -> Any:
    messages = scan_message_summaries(_gateway(ctx), folder=folder, limit=limit)
    return CleanupPlanner().analyze(
        messages,
        include_duplicates=True,
        include_old_unread=include_old_unread,
        old_unread_days=old_unread_days,
        exclusions=exclusions,
        max_items=limit,
    )


@cleanup_app.command("analyze")
@cleanup_app.command("plan")
def cleanup_analyze(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    old_unread: bool = typer.Option(False, help="Also include old unread messages."),
    old_unread_days: int = typer.Option(365, min=1),
    exclude: list[str] = typer.Option([], "--exclude"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Create an immutable, exact-candidate cleanup preview without mutations."""

    _run(
        ctx,
        "cleanup analyze",
        lambda: _cleanup_plan(
            ctx,
            folder=folder,
            limit=limit,
            include_old_unread=old_unread,
            old_unread_days=old_unread_days,
            exclusions=exclude,
        ),
        title="Cleanup preview",
    )


@cleanup_app.command("execute")
def cleanup_execute(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    old_unread: bool = typer.Option(False),
    old_unread_days: int = typer.Option(365, min=1),
    exclude: list[str] = typer.Option([], "--exclude"),
    limit: int = typer.Option(500, min=1, max=500),
    apply: bool = typer.Option(False, help="Move the exact previewed candidates to Deleted Items."),
    continue_on_error: bool = typer.Option(False),
) -> None:
    """Preview by default; --apply uses recoverable deletion after exact confirmation."""

    def action() -> Any:
        plan = _cleanup_plan(
            ctx,
            folder=folder,
            limit=limit,
            include_old_unread=old_unread,
            old_unread_days=old_unread_days,
            exclusions=exclude,
        )
        if not apply:
            return plan
        _confirm(
            ctx,
            f"Move {plan.item_count} exact cleanup candidate(s) to Deleted Items?",
            plan.confirmation_token,
        )
        gateway = _gateway(ctx)
        return CleanupPlanner().execute(
            plan,
            lambda candidate: gateway.delete_message(candidate.reference, permanent=False),
            confirmed=True,
            continue_on_error=continue_on_error,
        )

    _run(ctx, "cleanup execute", action, title="Cleanup execution")


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

    context = _state(ctx).context
    if strategy not in {None, "incremental_sync", "incremental_poll", "bounded_poll"}:
        raise typer.BadParameter("Unknown monitoring strategy.", param_hint="--strategy")
    configured = _watcher_profile(ctx, profile)
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
        stream = sys.stderr if _state(ctx).writer.format is not OutputFormat.HUMAN else sys.stdout
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
        _confirm(
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
        _gateway(ctx),
        context.state,
        account_id=context.config.email or "configured-mailbox",
        dispatcher=_profiled_notification_dispatcher(
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
                _state(ctx).writer.emit(result, command="watch", title="Mailbox watch")
        else:
            result = monitor.run_once()
            _state(ctx).writer.emit(result, command="watch", title="Mailbox watch")
            if not result.fully_succeeded:
                raise typer.Exit(code=int(ExitCode.PARTIAL))
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        _error(ctx, exc, command="watch")
    finally:
        for provider in outbound_providers:
            provider.close()


def _notification_service(ctx: typer.Context) -> NotificationConfigurationService:
    return NotificationConfigurationService(_state(ctx).context.state)


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


@notifications_app.command("status")
def notifications_status(ctx: typer.Context) -> None:
    """Inspect active monitoring configuration and watcher state without Exchange access."""

    def action() -> dict[str, object]:
        service = _notification_service(ctx)
        active = service.profiles.active_profile()
        context = _state(ctx).context
        watcher_rows = context.state.list_watcher_status()
        return {
            "active_profile": active,
            "profiles": service.profiles.list_profiles(),
            "configured_providers": service.providers.list_providers(),
            "watchers": watcher_rows,
        }

    _run(ctx, "notifications status", action, title="Notification status", source="local")


@notification_profiles_app.command("list")
def notification_profiles_list(ctx: typer.Context) -> None:
    """List account-scoped monitoring profiles and the active selection."""

    _run(
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

    _run(
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

    _run(
        ctx,
        "notifications profiles create",
        action,
        title="Created monitoring profile",
        source="local",
    )


@notification_profiles_app.command("activate")
def notification_profiles_activate(ctx: typer.Context, profile_id: str) -> None:
    """Select an existing profile for subsequent watch and headless commands."""

    _run(
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
    _run(
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
        _confirm(ctx, f"Delete local monitoring profile {profile.name!r}?", profile.profile_id)
        return {
            "profile_id": profile.profile_id,
            "deleted": service.profiles.delete_profile(profile_id),
        }

    _run(
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

    _run(
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

    _run(ctx, "notifications rules show", action, title="Notification rule", source="local")


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

    _run(
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

    _run(
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

    _run(
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
    _run(
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
        _confirm(ctx, f"Delete notification rule {rule.name!r}?", rule.rule_id)
        return {"rule_id": rule.rule_id, "deleted": repository.delete_rule(rule_id)}

    _run(
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
            context = _state(ctx).context
            sample = NotificationSimulationService(
                MessageService(_gateway(ctx)),
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

    _run(
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
        context = _state(ctx).context
        return {
            "providers": _notification_service(ctx).providers.list_providers(),
            "webhooks": WebhookRegistry(
                context.state,
                secret_store=SecretStore("webhooks"),
            ).list(),
        }

    _run(
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
        context = _state(ctx).context
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
        _confirm(ctx, f"Send one synthetic test through {selected.name!r}?", selected.name)
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
        _run(
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
    """Emit one synthetic redacted local notification; no mailbox or webhook call."""

    event = NotificationEvent(
        event_id="test-local-notification",
        event_type="test",
        occurred_at=datetime.now(UTC),
        account_id="synthetic",
        subject="Synthetic notification test",
        preview="No mailbox data was used.",
    )
    _run(
        ctx,
        "notifications test",
        lambda: NotificationDispatcher(
            [ConsoleNotificationProvider()], state=_state(ctx).context.state, deduplicate=False
        ).dispatch(event),
        title="Notification test",
        source="synthetic",
    )


@desktop_notifications_app.command("status")
def desktop_notifications_status(ctx: typer.Context) -> None:
    """Inspect only this current-user registration; no mailbox access or toast."""
    _run(
        ctx,
        "notifications desktop status",
        lambda: {
            "setup": desktop_setup_service(_state(ctx).context).preview().as_dict(),
            "backend": desktop_notification_provider(_state(ctx).context).backend_status(),
        },
        title="Windows desktop status",
        source="local",
    )


def _desktop_setup_command(ctx: typer.Context, operation: str, apply: bool, yes: bool) -> None:
    def action() -> dict[str, object]:
        service = desktop_setup_service(_state(ctx).context, operation)
        preview = service.preview(operation)
        if not apply:
            return preview.as_dict()
        if not preview.can_apply:
            raise ConfigurationError(preview.reason)
        _confirm(
            ctx,
            f"{operation.title()} only this current-user desktop identity and protocol?",
            service.identity.app_id,
            required=not yes,
        )
        return service.apply(preview, confirmed=True)

    _run(
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
    """Show bounded, sanitized local provider delivery history."""

    _run(
        ctx,
        "notifications history",
        lambda: NotificationDispatcher([], state=_state(ctx).context.state).history(limit=limit),
        title="Notification history",
        source="local",
    )


@webhooks_app.command("list")
def webhooks_list(ctx: typer.Context) -> None:
    """List persisted non-secret webhook definitions."""

    _run(
        ctx,
        "webhooks list",
        lambda: WebhookRegistry(
            _state(ctx).context.state,
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
        state = _state(ctx)
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
            _state(ctx).context.state,
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

    _run(
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

    _run(
        ctx,
        "webhooks migrate-secrets",
        lambda: WebhookRegistry(
            _state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).migrate_legacy(name),
        title="Webhook secret migration",
        source="local",
    )


@webhooks_app.command("remove")
def webhooks_remove(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Remove one local webhook definition."""

    def action() -> dict[str, Any]:
        _confirm(ctx, f"Remove webhook {name!r}?", f"webhook-remove:{name}")
        return {
            "name": name,
            "removed": WebhookRegistry(
                _state(ctx).context.state,
                secret_store=SecretStore("webhooks"),
            ).remove(name),
        }

    _run(ctx, "webhooks remove", action, title="Webhook removal", source="local")


def _set_webhook_enabled(ctx: typer.Context, name: str, *, enabled: bool) -> None:
    def action() -> WebhookConfig:
        if enabled:
            _confirm(
                ctx,
                f"Enable outbound delivery to webhook {name!r}?",
                f"webhook-enable:{name}",
            )
        return WebhookRegistry(
            _state(ctx).context.state,
            secret_store=SecretStore("webhooks"),
        ).set_enabled(name, enabled=enabled)

    command = "webhooks enable" if enabled else "webhooks disable"
    _run(ctx, command, action, title=command.title(), source="local")


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
            _state(ctx).context.state,
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
        _confirm(
            ctx,
            f"Send one synthetic test event to webhook {config.name!r}?",
            f"webhook-test:{config.name}",
        )
        with WebhookNotificationProvider(
            replace(config, enabled=True),
            state=_state(ctx).context.state,
            secret_store=webhook_secrets,
        ) as provider:
            result = provider.notify(event)
        response["delivery"] = result
        response["sent"] = result.state is DeliveryState.DELIVERED
        return response

    _run(
        ctx,
        "webhooks test",
        action,
        title="Webhook delivery test" if deliver else "Webhook preview",
        source="synthetic",
    )


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point
    app()
