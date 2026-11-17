"""Exchange server features: capabilities, automatic replies and inbox rules."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.ews.capabilities import DEFAULT_REGISTRY
from gucmail.output import OutputFormat
from gucmail.security import sanitize_terminal
from gucmail.services.automatic_replies import (
    AutomaticReplyService,
    AutomaticReplyState,
    ExternalReplyAudience,
    parse_reply_time,
)
from gucmail.services.delegates import DelegateService, delegate_details_text
from gucmail.services.inbox_rules import (
    InboxRuleOperation,
    InboxRulePlan,
    InboxRuleService,
    SimpleInboxRule,
    validate_simple_rule,
)

ews_app = typer.Typer(help="What the GUC mail server supports.")


automatic_replies_app = typer.Typer(help="Set up an out-of-office reply.")


inbox_rules_app = typer.Typer(
    help="See and edit rules that sort incoming mail."
)


@automatic_replies_app.command("show")
def automatic_replies_show(ctx: typer.Context) -> None:
    """Read the current policy without changing it or enabling replies."""
    core.run(
        ctx,
        "automatic-replies show",
        lambda: AutomaticReplyService(core.gateway(ctx)).get(),
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
        service = AutomaticReplyService(core.gateway(ctx))
        current = service.get()
        plan = service.plan(replace(current, **changes), current=current)
        if dry_run:
            return {"dry_run": True, "plan": plan}
        if core.cli_state(ctx).output_format is OutputFormat.HUMAN:
            core.cli_state(ctx).writer.emit(
                plan, command="automatic-replies preview", title="Review policy"
            )
        core.confirm(
            ctx,
            f"Set automatic replies to {plan.after.state.value}; external audience "
            f"{plan.after.external_audience.value}? Incoming mail may receive these replies.",
            "automatic-replies:" + plan.after.fingerprint,
        )
        return service.apply(plan, confirmed=True)

    core.run(ctx, "automatic-replies set", action, title="Automatic replies")


@inbox_rules_app.command("list")
def inbox_rules_list(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=1000),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Read native policies. EWS returns the whole rule collection in one bounded response."""
    core.run(
        ctx,
        "inbox-rules list",
        lambda: InboxRuleService(core.gateway(ctx)).list(limit=limit, offset=offset),
        title="Exchange inbox rules",
    )


@inbox_rules_app.command("show")
def inbox_rules_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Read conditions, exceptions and actions for one exact rule ID."""
    core.run(ctx, "inbox-rules show", lambda: InboxRuleService(core.gateway(ctx)).get(reference))


def _apply_inbox_rule_plan(
    ctx: typer.Context, service: InboxRuleService, plan: InboxRulePlan, *, dry_run: bool
) -> Any:
    if dry_run:
        return {"dry_run": True, "plan": plan}
    if core.cli_state(ctx).output_format is OutputFormat.HUMAN:
        core.cli_state(ctx).writer.emit(
            plan, command="inbox-rules preview", title="Review exact policy"
        )
    name = plan.creation.name if plan.creation else plan.before.name if plan.before else "rule"
    detail = (
        "New rules start disabled; enable separately after review."
        if plan.operation is InboxRuleOperation.CREATE
        else "Enabled rules can change incoming mail. Outlook rule data will be preserved."
    )
    fingerprint = plan.before.fingerprint if plan.before else plan.inventory_fingerprint
    core.confirm(
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
        service = InboxRuleService(core.gateway(ctx))
        return _apply_inbox_rule_plan(ctx, service, service.plan_create(request), dry_run=dry_run)

    core.run(ctx, "inbox-rules create", action)


def _change_inbox_rule(
    ctx: typer.Context, reference: str, operation: InboxRuleOperation, *, dry_run: bool
) -> None:
    def action() -> Any:
        service = InboxRuleService(core.gateway(ctx))
        return _apply_inbox_rule_plan(
            ctx, service, service.plan(reference, operation), dry_run=dry_run
        )

    core.run(ctx, f"inbox-rules {operation.value}", action)


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


@ews_app.command("capabilities")
@ews_app.command("operations")
def ews_capabilities(
    ctx: typer.Context,
    family: str | None = typer.Option(None),
) -> None:
    """List every represented EWS operation and its independent capability axes."""

    core.run(
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

    core.run(
        ctx,
        "ews delegates",
        lambda: DelegateService(core.gateway(ctx)).list(limit=limit, offset=offset),
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

    core.run(
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

    core.run(ctx, "ews support-matrix", action, title="EWS support matrix")


@ews_app.command("diagnostics")
def ews_diagnostics(ctx: typer.Context, online: bool = typer.Option(False)) -> None:
    """Show registry diagnostics; online probing is one bounded status read only."""

    core.run(
        ctx,
        "ews diagnostics",
        lambda: {
            "registry_operations": len(DEFAULT_REGISTRY.all()),
            "server_status": core.gateway(ctx).status() if online else "not_probed",
            "raw_soap_enabled": False,
            "raw_soap_implementation": "not_implemented",
            "raw_soap_legacy_preference": core.cli_state(
                ctx
            ).context.config.expert_raw_soap_enabled,
        },
    )


@ews_app.command("request-preview")
def ews_request_preview(
    ctx: typer.Context,
    operation: str = typer.Argument(...),
) -> None:
    """Preview operation classification without constructing or sending SOAP."""

    core.run(
        ctx,
        "ews request-preview",
        lambda: {
            "operation": _registry_operation(operation),
            "sent": False,
            "notice": "No XML was constructed and no request was sent.",
        },
        title="EWS request preview",
    )
