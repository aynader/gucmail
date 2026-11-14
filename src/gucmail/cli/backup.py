"""Backups and offline browsing."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfirmationRequiredError,
    ExitCode,
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

backup_app = typer.Typer(help="Back up your mailbox and browse backups offline.")


def _passphrase(ctx: typer.Context, *, stdin: bool, required: bool) -> str | None:
    if not required:
        return None
    if stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    elif core.cli_state(ctx).non_interactive or not sys.stdin.isatty():
        raise ConfirmationRequiredError(
            "An encrypted backup passphrase is required; use --passphrase-stdin."
        )
    else:
        value = getpass("Backup passphrase: ")
    if len(value) < 12:
        raise typer.BadParameter("Backup passphrases must contain at least 12 characters.")
    return value


def _backup_service(ctx: typer.Context) -> BackupService:
    context = core.cli_state(ctx).context
    return BackupService(context.paths.backup_dir, context.state)


@backup_app.command("list")
def backup_list(
    ctx: typer.Context,
    include_incomplete: bool = typer.Option(False),
    limit: int = typer.Option(200, min=1, max=10_000),
) -> None:
    """List local complete and optionally resumable incomplete backups."""

    core.run(
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
        gateway = core.gateway(ctx)
        if estimate_only:
            return service.estimate(gateway, request, encrypted=encrypt)
        passphrase = _passphrase(ctx, stdin=passphrase_stdin, required=encrypt or passphrase_stdin)
        return service.create(gateway, request, passphrase=passphrase)

    core.run(ctx, "backup create", action, title="Mailbox backup")


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
            core.gateway(ctx),
            parent,
            sync_state={
                "strategy": "bounded_rescan",
                "captured_at": datetime.now(UTC).isoformat(),
            },
            passphrase=passphrase,
        )

    core.run(ctx, "backup incremental", action, title="Incremental backup")


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
            core.gateway(ctx),
            passphrase=_passphrase(
                ctx,
                stdin=passphrase_stdin,
                required=encrypted or passphrase_stdin,
            ),
        )

    core.run(ctx, "backup resume", action, title="Resumed backup")


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
    core.run(
        ctx,
        "backup estimate",
        lambda: _backup_service(ctx).estimate(core.gateway(ctx), request, encrypted=encrypted),
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

    core.run(
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

    report = core.run(
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

    core.run(ctx, "backup browse", action, title="Offline backup", source="backup")


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

    core.run(ctx, "backup search", action, title="Offline search", source="backup")


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
            core.confirm(ctx, f"Overwrite backup export {output}?", f"backup-export:{output}")
        path = _backup_service(ctx).export(
            identifier,
            output,
            passphrase=_passphrase(
                ctx, stdin=passphrase_stdin, required=encrypted or passphrase_stdin
            ),
            overwrite=overwrite,
        )
        return {"path": str(path)}

    core.run(ctx, "backup export", action, title="Backup export", source="backup")


@backup_app.command("restore-plan")
def backup_restore_plan(
    ctx: typer.Context,
    identifier: str = typer.Argument(...),
    destination: str = typer.Option("Restored by GUC Mail CLI"),
    selected: list[str] = typer.Option([], "--selected"),
    another_copy: bool = typer.Option(
        False,
        "--another-copy",
        help="Review another copy of completed restores; unconfirmed items stay blocked.",
    ),
    encrypted: bool = typer.Option(False),
    passphrase_stdin: bool = typer.Option(False),
) -> None:
    """Build a no-mutation restore plan with destinations and duplicate policy."""

    core.run(
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
        False,
        "--another-copy",
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
                + (" No backup item matched --selected." if selected and not plan.entries else "")
            )
        if not apply or not plan.create_count:
            return plan
        core.confirm(
            ctx,
            f"Restore {plan.create_count} item(s) into {destination!r}?"
            + (
                " This deliberately creates another copy of recorded completed restores."
                if another_copy
                else ""
            )
            + (
                f" {plan.blocked_count} unconfirmed item(s) will not be repeated."
                if plan.blocked_count
                else ""
            ),
            plan.confirmation_token,
        )
        return service.restore(
            identifier,
            passphrase=passphrase,
            confirmed=True,
            target=core.gateway(ctx),
            confirmation_token=plan.confirmation_token,
            destination_root=destination,
            selected_references=selected or None,
            allow_completed_duplicates=another_copy,
        )

    result = core.run(ctx, "backup restore", action, title="Mailbox restore", warnings=warnings)
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

        journal = RestoreJournal(core.cli_state(ctx).context.state)
        if operation_id is None:
            return journal.operations(limit=limit, offset=offset)
        result = journal.operation(operation_id, limit=limit, offset=offset)
        if result is None:
            raise ItemNotFoundError("The local restore operation was not found.")
        return result

    core.run(
        ctx, "backup restore-status", action, title="Local restore recovery", source="local_state"
    )


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
        core.confirm(
            ctx,
            f"Move {len(planned.candidates)} backup(s) to local trash?",
            planned.confirmation_token,
        )
        return service.execute_prune(planned, confirmation_token=planned.confirmation_token)

    core.run(ctx, "backup prune", action, title="Backup retention", source="backup")
