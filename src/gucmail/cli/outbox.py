"""Drafts, scheduled sending, the scheduler and recurring messages."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer

from gucmail.cli import core
from gucmail.cli.core import NaturalOrderGroup
from gucmail.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from gucmail.dates import parse_datetime_expression
from gucmail.errors import (
    ConfigurationError,
    ExitCode,
)
from gucmail.models import (
    DraftData,
    Importance,
    SendState,
)
from gucmail.services.compose import ComposeService
from gucmail.services.compose_preferences import BodyFormat, ComposerPreferences
from gucmail.services.drafts import (
    DraftSyncCoordinator,
    ServerDraftSnapshot,
)
from gucmail.services.recurring import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurringService,
    RecurringState,
)
from gucmail.services.send_attempts import SendAttemptStore

drafts_app = typer.Typer(help="Write, edit and recover drafts.", cls=NaturalOrderGroup)


scheduled_app = typer.Typer(help="Messages waiting to be sent later.")


scheduler_app = typer.Typer(help="The background sender for scheduled messages.")


recurring_app = typer.Typer(help="Send the same message on a schedule.")


@drafts_app.command("list")
def drafts_list(
    ctx: typer.Context,
    query: str | None = typer.Option(None, help="Optional local subject/body/address search."),
    limit: int = typer.Option(DEFAULT_PAGE_SIZE, min=1, max=MAX_PAGE_SIZE, help="Drafts per page."),
    offset: int = typer.Option(0, min=0, help="Number of matching drafts to skip."),
) -> None:
    """List a bounded local draft page; use --offset to continue. No Exchange connection."""

    service = core.draft_service(ctx)
    core.run(
        ctx,
        "drafts list",
        lambda: service.list_page(query=query or "", limit=limit, offset=offset).items,
        title="Local drafts",
        source="local",
    )


@drafts_app.command("show")
def drafts_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one durable local draft and its revision."""

    core.run(
        ctx,
        "drafts show",
        lambda: core.draft_service(ctx).get(reference),
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
        return core.draft_service(ctx).create(
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

    core.run(
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
                changes[name] = core.parse_addresses(value)
        if subject is not None:
            changes["subject"] = subject
        if body is not None or body_file is not None:
            changes["body_text"] = core.body_value(body, body_file)
        if attach is not None:
            changes["attachment_paths"] = attach
        return core.draft_service(ctx).edit(
            reference, expected_revision=expected_revision, **changes
        )

    core.run(ctx, "drafts edit", action, title="Draft updated", source="local")


@drafts_app.command("send")
def drafts_send(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    schedule: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and send a local draft, or place it in the durable schedule."""

    def action() -> Any:
        draft = core.draft_service(ctx).get(reference)
        service = ComposeService(
            max_attachment_bytes=core.cli_state(ctx).context.config.max_attachment_bytes,
            state=core.cli_state(ctx).context.state,
            preferences=ComposerPreferences.from_settings(core.settings_service(ctx)),
        )
        plan = service.plan_send(draft)
        if schedule:
            now = datetime.now(UTC)
            when = parse_datetime_expression(
                schedule,
                timezone=core.cli_state(ctx).context.config.timezone,
                now=now,
                future_required=True,
            ).local
            if dry_run:
                return {"dry_run": True, "scheduled_for": when, "plan": plan}
            core.confirm(
                ctx,
                f"Queue this draft for {when.isoformat()}? A scheduler must be running to send.\n"
                + core.compose_review(plan),
                plan.confirmation_scope + ":" + when.isoformat(),
            )
            return core.scheduler(ctx).schedule_at(
                reference,
                when,
                now=now,
                original_expression=schedule,
                allow_empty_subject=service.preferences.allow_empty_subject,
            )
        scope = (
            None
            if dry_run
            else core.confirm(
                ctx,
                "Send this reviewed draft?\n" + core.compose_review(plan),
                plan.confirmation_scope,
            )
        )
        if not dry_run:
            service.gateway = core.gateway(ctx)
        result = service.execute_send(plan, dry_run=dry_run, confirmation_scope=scope)
        core.remember_sent_composition(ctx, result)
        if result.sent:
            core.draft_service(ctx).finish_sent(draft.reference, expected_revision=draft.revision)
        return result

    core.run(ctx, "drafts send", action, title="Draft send")


@drafts_app.command("send-status")
def drafts_send_status(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Inspect a draft's durable send outcome without contacting Exchange."""

    def action() -> Any:
        attempt = SendAttemptStore(core.cli_state(ctx).context.state).get(reference)
        return attempt or {"draft_reference": reference, "state": "no_recorded_attempt"}

    core.run(ctx, "drafts send-status", action, title="Send outcome", source="local")


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
        service = core.draft_service(ctx)
        server_id = None if local_only else service.get(reference).server_id
        prompt = (
            f"Delete draft {reference} and move its Exchange copy to Deleted Items?"
            if server_id
            else f"Delete local draft {reference}?"
        )
        core.confirm(ctx, prompt, f"draft-delete:{reference}")
        # Same order as the TUI discard: never leave a local draft pointing at a
        # server copy that was already removed, and never orphan the server copy.
        if server_id:
            core.gateway(ctx).delete_message(server_id, permanent=False)
        return {
            "reference": reference,
            "deleted": service.delete(reference),
            "server_copy_moved_to_deleted_items": bool(server_id),
        }

    core.run(ctx, "drafts delete", action, title="Draft deletion", source="local")


@drafts_app.command("duplicate")
def drafts_duplicate(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Create an independent local copy of a draft."""

    core.run(
        ctx,
        "drafts duplicate",
        lambda: core.draft_service(ctx).duplicate(reference),
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

    core.run(
        ctx,
        "drafts schedule",
        lambda: core.scheduler(ctx).schedule(
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
            core.confirm(
                ctx,
                f"Replace stored draft {reference} with its recovery copy?",
                f"draft-recover:{reference}",
            )
        return core.draft_service(ctx).recover(reference, overwrite=overwrite)

    core.run(ctx, "drafts recover", action, title="Draft recovered", source="local")


@drafts_app.command("conflicts")
def drafts_conflicts(ctx: typer.Context) -> None:
    """List drafts that need a manual conflict decision."""

    core.run(
        ctx,
        "drafts conflicts",
        lambda: core.draft_service(ctx).conflicts(),
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
        drafts = core.draft_service(ctx)
        local = drafts.get(reference)
        metadata = DraftSyncCoordinator(drafts, core.gateway(ctx)).metadata(reference)
        if dry_run:
            return {
                "reference": reference,
                "server_id_present": bool(metadata.server_id or local.server_id),
                "sync_state": metadata.state.value,
                "would_create": not bool(metadata.server_id or local.server_id),
                "dry_run": True,
            }
        verb = "Update" if metadata.server_id or local.server_id else "Create"
        core.confirm(
            ctx,
            f"{verb} the reviewed Exchange draft for {reference}?",
            f"draft-server-save:{reference}",
        )
        return DraftSyncCoordinator(drafts, core.gateway(ctx)).sync_to_server(reference)

    core.run(ctx, "drafts save-server", action, title="Server draft synchronization")


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
        drafts = core.draft_service(ctx)
        coordinator = DraftSyncCoordinator(drafts, core.gateway(ctx))
        core.confirm(
            ctx,
            f"Resolve {reference} using the {choice} copy?",
            f"draft-conflict:{reference}:{choice}",
        )
        resolved = coordinator.resolve_conflict(reference, use=choice)
        if push:
            if choice != "local":
                raise ConfigurationError("--push is only valid when keeping the local copy.")
            core.confirm(
                ctx,
                f"Save the selected local copy of {reference} back to Exchange?",
                f"draft-conflict-push:{reference}",
            )
            return coordinator.sync_to_server(reference)
        return resolved

    core.run(ctx, "drafts resolve-conflict", action, title="Draft conflict resolution")


@drafts_app.command("sync")
def drafts_sync(
    ctx: typer.Context,
    limit: int = typer.Option(100, min=1, max=500),
    apply: bool = typer.Option(False, help="Import missing server drafts into local state."),
) -> None:
    """Compare bounded server drafts and optionally import missing editable copies."""

    def action() -> dict[str, Any]:
        gateway = core.gateway(ctx)
        summaries = gateway.list_messages(folder="drafts", limit=limit)
        service = core.draft_service(ctx)
        coordinator = DraftSyncCoordinator(service, gateway)
        imported: list[str] = []
        existing: list[str] = []
        existing_by_server_id = {
            draft.server_id: draft.reference for draft in service.list() if draft.server_id
        }
        if apply:
            core.confirm(
                ctx, f"Import up to {len(summaries)} server draft(s)?", "draft-sync-import"
            )
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

    core.run(ctx, "drafts sync", action, title="Draft synchronization")


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

    core.run(
        ctx,
        "scheduled list",
        lambda: core.scheduler(ctx).list(states=state or None, limit=limit),
        title="Scheduled messages",
        source="local",
        human_data=present,
    )


@scheduled_app.command("show")
def scheduled_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one scheduled job, its draft snapshot, and attachment manifest."""

    core.run(
        ctx,
        "scheduled show",
        lambda: core.scheduler(ctx).get(reference),
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
                changes[name] = core.parse_addresses(value)
        if subject is not None:
            changes["subject"] = subject
        if body is not None or body_file is not None:
            changes["body_text"] = core.body_value(body, body_file)
        if not changes and final_confirmation is None:
            raise typer.BadParameter("Supply at least one draft or confirmation change.")
        return core.scheduler(ctx).edit(
            reference,
            draft_changes=changes or None,
            require_final_confirmation=final_confirmation,
        )

    core.run(ctx, "scheduled edit", action, title="Scheduled message updated", source="local")


@scheduled_app.command("history")
def scheduled_history(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show the append-only local transition history for one job."""

    core.run(
        ctx,
        "scheduled history",
        lambda: core.scheduler(ctx).history(reference),
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

    core.run(
        ctx,
        "scheduled reschedule",
        lambda: core.scheduler(ctx).reschedule(reference, when),
        title="Rescheduled",
        source="local",
    )


@scheduled_app.command("cancel")
def scheduled_cancel(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Cancel an editable local scheduled or undo-delay job."""

    def action() -> Any:
        core.confirm(ctx, f"Cancel scheduled message {reference}?", f"schedule-cancel:{reference}")
        return core.scheduler(ctx).cancel(reference)

    core.run(ctx, "scheduled cancel", action, title="Scheduled message cancelled")


@scheduled_app.command("undo")
def scheduled_undo(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Cancel a message while it remains in the local Undo Send delay."""

    core.run(
        ctx,
        "scheduled undo",
        lambda: core.scheduler(ctx).undo(reference),
        title="Send undone",
        source="local",
    )


@scheduled_app.command("send-now")
def scheduled_send_now(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Send one editable job immediately after confirmation."""

    def action() -> Any:
        core.confirm(ctx, f"Send scheduled message {reference} now?", f"send-now:{reference}")
        return core.scheduler(ctx).send_now(reference, confirmed=True)

    core.run(ctx, "scheduled send-now", action, title="Send now")


@scheduled_app.command("run-once")
def scheduled_run_once(
    ctx: typer.Context,
    max_jobs: int = typer.Option(20, min=1, max=100),
) -> None:
    """Process one bounded batch of due jobs; safe for OS schedulers."""

    core.run(
        ctx,
        "scheduled run-once",
        lambda: core.scheduler(ctx).run_once(limit=max_jobs),
        title="Scheduler run",
    )


@scheduled_app.command("run")
def scheduled_run(
    ctx: typer.Context,
    interval: float = typer.Option(30.0, min=5.0, max=3600.0),
) -> None:
    """Run the local scheduler in the foreground until interrupted."""

    try:
        result = core.scheduler(ctx).run_foreground(poll_interval=interval)
        core.cli_state(ctx).writer.emit(result, command="scheduled run", title="Scheduler stopped")
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        core.report_error(ctx, exc, command="scheduled run")


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
            arguments=bound_scheduler_arguments(core.cli_state(ctx).context.paths),
        )
        if not apply:
            return plan.as_dict()
        core.confirm(
            ctx, f"Install {plan.platform.value} scheduler integration?", "scheduler-install"
        )
        return install_scheduler(plan, dry_run=False, confirmed=True)

    core.run(ctx, "scheduled install", action, title="Scheduler integration")


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
            arguments=bound_scheduler_arguments(core.cli_state(ctx).context.paths),
        )
        if not apply:
            return {"plan": plan.as_dict(), "uninstall": True, "dry_run": True}
        core.confirm(
            ctx, f"Remove {plan.platform.value} scheduler integration?", "scheduler-uninstall"
        )
        return uninstall_scheduler(plan, dry_run=False, confirmed=True)

    core.run(ctx, "scheduled uninstall", action, title="Scheduler integration")


@scheduler_app.command("status")
def scheduler_status(ctx: typer.Context) -> None:
    """Show durable queue and worker pause status without contacting Exchange."""

    core.run(
        ctx,
        "scheduler status",
        lambda: core.scheduler(ctx).status(),
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
        result = core.scheduler(ctx).run_foreground(poll_interval=interval)
        core.cli_state(ctx).writer.emit(result, command="scheduler run", title="Scheduler stopped")
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except BaseException as exc:
        core.report_error(ctx, exc, command="scheduler run")


@scheduler_app.command("run-once")
def scheduler_run_once(
    ctx: typer.Context,
    max_jobs: int = typer.Option(20, min=1, max=100),
) -> None:
    """Process one bounded due-job batch for an OS scheduler."""

    core.run(
        ctx,
        "scheduler run-once",
        lambda: core.scheduler(ctx).run_once(limit=max_jobs),
        title="Scheduler run",
    )


@scheduler_app.command("pause")
def scheduler_pause(ctx: typer.Context) -> None:
    """Pause worker processing while retaining every queued job."""

    def action() -> dict[str, Any]:
        service = core.scheduler(ctx)
        service.pause()
        return service.status()

    core.run(ctx, "scheduler pause", action, title="Scheduler paused", source="local")


@scheduler_app.command("resume")
def scheduler_resume(ctx: typer.Context) -> None:
    """Resume due-job processing; overdue jobs follow the documented retry policy."""

    def action() -> dict[str, Any]:
        service = core.scheduler(ctx)
        service.resume()
        return service.status()

    core.run(ctx, "scheduler resume", action, title="Scheduler resumed", source="local")


@scheduler_app.command("doctor")
def scheduler_doctor(ctx: typer.Context) -> None:
    """Inspect queue health and preview the native integration for this platform."""

    from gucmail.platform_scheduler import bound_scheduler_arguments, generate_scheduler_plan

    def action() -> dict[str, Any]:
        context = core.cli_state(ctx).context
        return {
            "queue": core.scheduler(ctx).status(),
            "state": context.state.health(),
            "native_integration": generate_scheduler_plan(
                arguments=bound_scheduler_arguments(context.paths)
            ).as_dict(),
            "network_request_performed": False,
        }

    core.run(ctx, "scheduler doctor", action, title="Scheduler diagnostics", source="local")


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


@recurring_app.command("list")
def recurring_list(
    ctx: typer.Context,
    state: RecurringState | None = typer.Option(None, case_sensitive=False),
) -> None:
    """List paused, active, and completed recurring rules."""

    core.run(
        ctx,
        "recurring list",
        lambda: RecurringService(
            core.cli_state(ctx).context.state, core.draft_service(ctx), core.scheduler(ctx)
        ).list(state=state),
        title="Recurring messages",
        source="local",
    )


@recurring_app.command("show")
def recurring_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one recurring rule and its next bounded occurrences."""

    def action() -> dict[str, Any]:
        context = core.cli_state(ctx).context
        service = RecurringService(
            context.state,
            core.draft_service(ctx),
            core.scheduler(ctx),
            automation_enabled=False,
        )
        return {
            "rule": service.get(reference),
            "next_occurrences": service.preview(reference, limit=10),
        }

    core.run(ctx, "recurring show", action, title="Recurring rule", source="local")


@recurring_app.command("create")
def recurring_create(
    ctx: typer.Context,
    draft: str = typer.Argument(..., help="Template local draft reference."),
    frequency: RecurrenceFrequency = typer.Option(..., case_sensitive=False),
    start: str = typer.Option(..., help="ISO-8601 first occurrence time with an offset."),
    interval: int = typer.Option(1, min=1, max=1000),
    activate: bool = typer.Option(False, help="Activate immediately (expert opt-in)."),
    count: int | None = typer.Option(None, min=1, help="Stop after this many messages."),
    until: str | None = typer.Option(None, help="Stop after this date, e.g. 2027-06-30."),
    on: str | None = typer.Option(
        None, help="Weekly only: which days, e.g. mon,wed. Default: the start day."
    ),
) -> None:
    """Send a draft again and again on a schedule. Starts paused until you resume it."""

    def action() -> Any:
        context = core.cli_state(ctx).context
        parsed_start = core.iso_datetime(start, parameter="--start")
        assert parsed_start is not None
        rule = RecurrenceRule(
            frequency=frequency,
            interval=interval,
            timezone=context.config.timezone,
            start_at=parsed_start,
            count=count,
            until=_recurring_until(until, context.config.timezone),
            weekdays=_weekday_numbers(on),
        )
        service = RecurringService(
            context.state,
            core.draft_service(ctx),
            core.scheduler(ctx),
            automation_enabled=activate and core.cli_state(ctx).assume_yes,
        )
        if activate:
            core.confirm(ctx, "Activate automatic recurring sends?", "recurring-activate")
        return service.create(draft, rule, activate=activate)

    core.run(ctx, "recurring create", action, title="Recurring rule")


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_numbers(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    days = []
    for part in value.split(","):
        name = part.strip().casefold()[:3]
        if name not in _WEEKDAYS:
            raise typer.BadParameter(f"Unknown day {part.strip()!r}. Use mon, tue, ... sun.")
        days.append(_WEEKDAYS.index(name))
    return tuple(sorted(set(days)))


def _recurring_until(value: str | None, timezone: str) -> datetime | None:
    """An end date includes that whole day in your time zone."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        parsed = core.iso_datetime(value, parameter="--until")
        assert parsed is not None
        return parsed
    return datetime.combine(day, time(23, 59, 59), tzinfo=ZoneInfo(timezone))


def _recurring_action(ctx: typer.Context, command: str, reference: str, method: str) -> None:
    def action() -> Any:
        context = core.cli_state(ctx).context
        service = RecurringService(
            context.state,
            core.draft_service(ctx),
            core.scheduler(ctx),
            automation_enabled=True,
        )
        if method in {"resume", "delete"}:
            core.confirm(
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

    core.run(ctx, command, action, title=command.title(), source="local")


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
        context = core.cli_state(ctx).context
        service = RecurringService(
            context.state,
            core.draft_service(ctx),
            core.scheduler(ctx),
            automation_enabled=True,
        )
        horizon = core.iso_datetime(through, parameter="--through")
        assert horizon is not None
        return service.materialize_until(horizon, limit=max_occurrences)

    core.run(ctx, "recurring materialize", action, title="Recurring occurrences")
