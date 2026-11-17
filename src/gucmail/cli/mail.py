"""Folders, messages, attachments and saved searches."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.cli.core import NaturalOrderGroup
from gucmail.cli.main import app
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
)
from gucmail.models import (
    SearchQuery,
    to_primitive,
)
from gucmail.services.attachment_preferences import AttachmentPreferences
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.folders import FolderService
from gucmail.services.gateway import DeferredGateway
from gucmail.services.index import SearchIndex
from gucmail.services.mail_preferences import (
    DeleteBehavior,
    MailDeletionPreferences,
    MailSortPreferences,
)
from gucmail.services.messages import MessageService
from gucmail.services.search import parse_query

folders_app = typer.Typer(help="List, create, rename and delete folders.", cls=NaturalOrderGroup)


messages_app = typer.Typer(help="List, read, search and organize messages.", cls=NaturalOrderGroup)


message_operations_app = typer.Typer(help="See the results of recent moves and deletes.")


messages_app.add_typer(message_operations_app, name="operations")


attachments_app = typer.Typer(help="List and save attachments.", cls=NaturalOrderGroup)


saved_searches_app = typer.Typer(
    help="Save searches you run often.", cls=NaturalOrderGroup
)


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

            zone_name = core.cli_state(ctx).context.config.timezone
            try:
                zone = ZoneInfo(zone_name)
            except ZoneInfoNotFoundError as exc:
                raise typer.BadParameter(
                    f"configured time zone {zone_name!r} is unknown", param_hint=parameter
                ) from exc
            return datetime(day.year, day.month, day.day, tzinfo=zone)
    return core.iso_datetime(value, parameter=parameter)


@folders_app.command("list")
def folders_list(
    ctx: typer.Context,
    parent: str | None = typer.Option(None, help="Parent path or local folder reference."),
    limit: int = typer.Option(25, min=1, max=500),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List one bounded page of child folders."""

    core.run(
        ctx,
        "folders list",
        lambda: FolderService(core.gateway(ctx)).list_folders(
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
        service = FolderService(core.gateway(ctx))
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

    core.run(ctx, "folders tree", action, title="Folder tree", source="live_server")


@folders_app.command("create")
def folders_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New folder display name."),
    parent: str | None = typer.Option(None, help="Parent folder path or reference."),
    dry_run: bool = typer.Option(False),
) -> None:
    """Preview and create one folder."""

    def action() -> Any:
        service = FolderService(core.gateway(ctx))
        plan = service.plan_create(name, parent=parent)
        scope = (
            None
            if dry_run
            else core.confirm(ctx, plan.summary, plan.confirmation_scope, required=False)
        )
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    core.run(ctx, "folders create", action, title="Folder creation")


def _folder_mutation(
    ctx: typer.Context,
    command: str,
    planner: Callable[[FolderService], Any],
    *,
    dry_run: bool,
) -> None:
    def action() -> Any:
        service = FolderService(core.gateway(ctx))
        plan = planner(service)
        scope = None if dry_run else core.confirm(ctx, plan.summary, plan.confirmation_scope)
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    core.run(ctx, command, action, title=command.title())


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
            else MailSortPreferences.from_settings(core.settings_service(ctx)).order_by
        )
        # Reject an invalid explicit order before resolving credentials.
        MessageService.validate_order(order)
        return MessageService(core.gateway(ctx)).list_messages(
            folder, limit=limit, offset=offset, order_by=order
        )

    core.run(
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

    core.run(
        ctx,
        "messages show",
        lambda: MessageService(core.gateway(ctx)).show_message(reference, mark_read=mark_read),
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

    core.run(
        ctx,
        "messages headers",
        lambda: MessageService(core.gateway(ctx)).show_message(reference).raw_headers,
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
            core.confirm(ctx, f"Overwrite {target}?", f"raw:{target}")
        data = core.gateway(ctx).raw_message(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": str(target), "bytes": len(data), "remote_content_loaded": False}

    core.run(ctx, "messages raw", action, title="Raw message export")


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
    """Search your mail."""

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
        core.run(
            ctx,
            "messages search",
            lambda: SearchIndex(core.cli_state(ctx).context.paths.index_database).search(
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
    core.run(
        ctx,
        "messages search",
        lambda: MessageService(core.gateway(ctx)).search_messages(
            query, limit=limit, offset=offset
        ),
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
            DeferredGateway(lambda: core.gateway(ctx)), state=core.cli_state(ctx).context.state
        )
        plan = planner(service)
        scope = (
            None
            if dry_run
            else core.confirm(
                ctx, plan.summary, plan.confirmation_scope, required=plan.requires_confirmation
            )
        )
        return service.execute_mutation(plan, dry_run=dry_run, confirmation_scope=scope)

    core.run(ctx, command, action, title=command.title())


@message_operations_app.command("list")
def message_operations_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List local operation records without connecting to Exchange."""
    core.run(
        ctx,
        "messages operations list",
        lambda: core.cli_state(ctx).context.state.list_message_operations(
            limit=limit, offset=offset
        ),
        title="Message operation history",
        source="local_state",
    )


@message_operations_app.command("show")
def message_operations_show(ctx: typer.Context, operation_id: str = typer.Argument(...)) -> None:
    """Show known/uncertain per-target effects; never replay a write."""

    def action() -> Any:
        result = core.cli_state(ctx).context.state.get_message_operation(operation_id)
        if result is None:
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError("The local message operation was not found.")
        return result

    core.run(
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
        preference = MailDeletionPreferences.from_settings(core.settings_service(ctx))
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


@attachments_app.command("list")
def attachments_list(
    ctx: typer.Context,
    message: str = typer.Argument(...),
    limit: int = typer.Option(100, min=1, max=200),
) -> None:
    """List attachment metadata without downloading content."""

    core.run(
        ctx,
        "attachments list",
        lambda: AttachmentService(core.gateway(ctx)).list_attachments(message, limit=limit),
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
        context = core.cli_state(ctx).context
        preferences = AttachmentPreferences.from_settings(core.settings_service(ctx))
        service = AttachmentService(
            core.gateway(ctx),
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
            else core.confirm(
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

    core.run(ctx, "attachments save", action, title="Attachment save")


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
        context = core.cli_state(ctx).context
        preferences = AttachmentPreferences.from_settings(core.settings_service(ctx))
        service = AttachmentService(
            core.gateway(ctx),
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
            else core.confirm(
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

    core.run(ctx, "attachments save-all", action, title="Attachments saved")


@saved_searches_app.command("list")
def saved_searches_list(ctx: typer.Context) -> None:
    """List named local search definitions."""

    core.run(
        ctx,
        "saved-searches list",
        lambda: core.cli_state(ctx).context.state.list_searches(),
        title="Saved searches",
        source="local",
    )


@saved_searches_app.command("add")
def saved_searches_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    query: str = typer.Argument(..., help='Search, e.g. "from:dr.x has:attachment".'),
    replace: bool = typer.Option(False, "--replace", help="Overwrite a search with this name."),
) -> None:
    """Save a search under a name, to run later with `saved-searches run`."""

    def action() -> dict[str, Any]:
        if not name.strip():
            raise ConfigurationError("Give the saved search a name.")
        if not query.strip():
            raise ConfigurationError("The search is empty. Add what to look for.")
        store = core.cli_state(ctx).context.state
        if store.get_search(name) is not None and not replace:
            raise ConfigurationError(
                f"You already have a saved search called {name!r}. Use --replace to overwrite it."
            )
        parsed = parse_query(query, timezone=core.cli_state(ctx).context.config.timezone)
        store.save_search(name, {"query": query, "ast": parsed.to_dict()})
        return {"name": name, "query": query}

    core.run(ctx, "saved-searches add", action, title="Saved search", source="local")


@saved_searches_app.command("remove")
def saved_searches_remove(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Remove one local saved search."""

    core.run(
        ctx,
        "saved-searches remove",
        lambda: {"name": name, "removed": core.cli_state(ctx).context.state.remove_search(name)},
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
        row = core.cli_state(ctx).context.state.get_search(name)
        if row is None:
            from gucmail.errors import ItemNotFoundError

            raise ItemNotFoundError(f"Saved search not found: {name}")
        payload = row.get("query")
        if isinstance(payload, dict):
            query_text = str(payload.get("query", ""))
        else:
            query_text = str(payload or "")
        context = core.cli_state(ctx).context
        results = MessageService(core.gateway(ctx)).search_expression(
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

    core.run(ctx, "saved-searches run", action, title="Saved search results")
