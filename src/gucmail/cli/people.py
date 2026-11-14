"""Contacts, contact groups, the directory and recent recipients."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.models import (
    ContactInfo,
)
from gucmail.output import OutputFormat
from gucmail.services.compose import normalize_recipient
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
from gucmail.services.gateway import DeferredGateway
from gucmail.services.pim import PimPage
from gucmail.services.search import fuzzy_rerank

contacts_app = typer.Typer(help="Your contacts.")


contact_groups_app = typer.Typer(help="Groups of contacts you email together.")


directory_app = typer.Typer(help="Look people up in the GUC directory.")


recipients_app = typer.Typer(help="People you email most and most recently.")


def _contact_group_service(ctx: typer.Context, source: GroupSource) -> ContactGroupService:
    return ContactGroupService(
        LocalContactGroups(core.cli_state(ctx).context.state)
        if source is GroupSource.LOCAL
        else core.gateway(ctx)
    )


@contact_groups_app.command("list")
def contact_groups_list(
    ctx: typer.Context,
    source: GroupSource = typer.Option(GroupSource.LOCAL),
    limit: int = typer.Option(50, min=1, max=200),
    offset: int = typer.Option(0, min=0),
) -> None:
    """List a bounded group page. Default local groups require no mailbox connection."""
    core.run(
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
    core.run(ctx, "contact-groups show", lambda: _contact_group_service(ctx, source).get(reference))


@contact_groups_app.command("recipients")
def contact_groups_recipients(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    source: GroupSource = typer.Option(GroupSource.LOCAL),
) -> None:
    """Expand reviewed SMTP recipients for scripting. Does not compose or send mail."""
    core.run(
        ctx,
        "contact-groups recipients",
        lambda: _contact_group_service(ctx, source).recipients(reference),
    )


def _apply_contact_group_plan(
    ctx: typer.Context, service: ContactGroupService, plan: ContactGroupPlan, *, dry_run: bool
) -> Any:
    if dry_run:
        return {"dry_run": True, "plan": plan}
    if core.cli_state(ctx).output_format is OutputFormat.HUMAN:
        core.cli_state(ctx).writer.emit(
            plan, command="contact-groups preview", title="Review group and recipients"
        )
    core.confirm(
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

    core.run(ctx, "contact-groups create", action)


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

    core.run(ctx, "contact-groups edit", action)


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

    core.run(ctx, "contact-groups delete", action)


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


def _contacts_service(
    ctx: typer.Context, *, index_warning: Callable[[str], None] | None = None
) -> ContactsService:
    gateway = DeferredGateway(lambda: core.gateway(ctx))
    return ContactsService(
        gateway,
        correspondence_gateway=gateway,
        operation_preferences=lambda: ContactOperationPreferences.from_settings(
            core.settings_service(ctx)
        ),
        history_index=ContactHistoryIndex(core.cli_state(ctx).context.state),
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
            core.settings_service(ctx), threshold_override=threshold_override
        )
        return action(_contacts_service(ctx), display)

    core.run(
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
        lambda service, display: core.pim_page_output(
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
            return core.pim_page_output(
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
        return core.pim_page_output(
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
        service = ContactsService(core.gateway(ctx))
        contact = ContactInfo(
            reference="preview:contact",
            display_name=display_name,
            given_name=given_name,
            middle_name=middle_name,
            family_name=family_name,
            nickname=nickname,
            emails=tuple(core.parse_addresses(email)),
            phones=_labeled_options(phone, parameter="--phone"),
            addresses=_labeled_options(address, parameter="--address"),
            company=company,
            department=department,
            job_title=job_title,
            birthday=core.iso_date(birthday, parameter="--birthday"),
            notes=notes,
            categories=tuple(category),
        )
        plan = service.plan_create(contact)
        if dry_run:
            return plan
        core.confirm(ctx, f"Create contact {display_name!r}?", f"contact-create:{display_name}")
        return service.gateway.create_contact(plan)

    core.run(ctx, "contacts create", action, title="Contact creation")


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
                ("birthday", core.iso_date(birthday, parameter="--birthday")),
                ("notes", notes),
            )
            if value is not None
        }
        if email:
            changes["emails"] = tuple(core.parse_addresses(email))
        if phone:
            changes["phones"] = _labeled_options(phone, parameter="--phone")
        if address:
            changes["addresses"] = _labeled_options(address, parameter="--address")
        if category:
            changes["categories"] = tuple(category)
        service = ContactsService(core.gateway(ctx))
        plan = service.plan_update(reference, ContactPatch(**changes))
        if dry_run:
            return plan
        core.confirm(ctx, f"Update contact {reference}?", f"contact-update:{reference}")
        return service.gateway.update_contact(plan)

    core.run(ctx, "contacts update", action, title="Contact update")


@contacts_app.command("delete")
def contacts_delete(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    permanent: bool = typer.Option(False),
    dry_run: bool = typer.Option(False),
) -> None:
    """Move a contact to Deleted Items; permanent deletion is explicit."""

    def action() -> Any:
        service = ContactsService(core.gateway(ctx))
        plan = service.plan_delete(reference, hard_delete=permanent)
        if dry_run:
            return plan
        core.confirm(ctx, f"Delete contact {reference}?", f"contact-delete:{reference}")
        service.delete(plan)
        return {"reference": reference, "deleted": True, "permanent": permanent}

    core.run(ctx, "contacts delete", action, title="Contact deletion")


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
        service = ContactsService(core.gateway(ctx))
        preview = service.merge_preview(
            (service.get(reference) for reference in references),
            primary_reference=primary,
        )
        plan = service.plan_merge(preview)
        if not apply:
            return plan
        core.confirm(
            ctx,
            f"Merge {len(references)} contacts and move {len(plan.deletes)} "
            "duplicate(s) to Deleted Items?",
            f"contacts-merge:{preview.primary_reference}:{len(plan.deletes)}",
        )
        return service.apply_merge(plan)

    core.run(ctx, "contacts merge", action, title="Contact merge")


@contacts_app.command("groups")
def contacts_groups(
    ctx: typer.Context,
    limit: int = typer.Option(200, min=1, max=200),
) -> None:
    """Group a bounded contact view by Exchange contact category."""

    def action() -> dict[str, Any]:
        contacts = ContactsService(core.gateway(ctx)).list(limit=limit)
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

    core.run(ctx, "contacts groups", action, title="Contact groups")


@contacts_app.command("export")
def contacts_export(
    ctx: typer.Context,
    reference: str = typer.Argument(...),
    output: Path = typer.Option(...),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """Export one contact as vCard 4.0."""

    def action() -> dict[str, Any]:
        content = ContactsService(core.gateway(ctx)).export_contact(reference)
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            core.confirm(
                ctx, f"Overwrite existing vCard file {output}?", f"contacts-export:{output}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "contacts": 1}

    core.run(ctx, "contacts export", action, title="Contact export")


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
        service = ContactsService(core.gateway(ctx))
        plan = service.import_vcards(
            path.read_text(encoding="utf-8"), allow_duplicates=allow_duplicates
        )
        if not apply:
            return plan
        core.confirm(ctx, f"Create up to {len(plan.contacts)} contact(s)?", "contacts-import")
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

    core.run(ctx, "contacts import", action, title="Contact import", warnings=warnings)


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

    core.run(ctx, "contacts correspondence", action, title="Contact correspondence")


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
    core.run(
        ctx,
        "directory search",
        lambda: _contacts_service(ctx).search_directory(value, limit=limit),
        title="Directory results",
        source="live_server",
        human_data=lambda rows: (
            rows
            or (
                "No directory matches. The GUC directory may not be searchable from this "
                "account; search your own contacts with `gucmail contacts search`."
            )
        ),
    )


@directory_app.command("resolve")
def directory_resolve(ctx: typer.Context, query: str = typer.Argument(...)) -> None:
    """Require one unambiguous Exchange directory result."""

    core.run(
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
        preferences = ContactOperationPreferences.from_settings(core.settings_service(ctx))
        index = ContactHistoryIndex(core.cli_state(ctx).context.state)
        if clear:
            core.confirm(
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

    core.run(
        ctx, "contacts history-index", action, title="Local contact history", source="local_state"
    )


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

    parsed_since = core.iso_datetime(since, parameter="--since")
    warnings = [f"Bounded Inbox/Sent metadata: at most {scan} observations; no message bodies."]

    def action() -> Any:
        service = _contacts_service(ctx, index_warning=warnings.append)
        service.operation_preferences().require("history")
        email_value = core.cli_state(ctx).context.config.email
        excluded: tuple[str, ...] = (email_value,) if email_value else ()
        return service.recent_correspondents(
            service.read_correspondence(since=parsed_since, limit=scan),
            excluded_addresses=excluded,
            since=parsed_since,
            limit=limit,
        )

    core.run(ctx, "recipients recent", action, title="Recent correspondents", warnings=warnings)


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

    parsed_since = core.iso_datetime(since, parameter="--since")
    warnings = [f"Bounded Inbox/Sent metadata: at most {scan} observations; no message bodies."]

    def action() -> Any:
        service = _contacts_service(ctx, index_warning=warnings.append)
        service.operation_preferences().require("frequent")
        email_value = core.cli_state(ctx).context.config.email
        excluded: tuple[str, ...] = (email_value,) if email_value else ()
        return service.frequent_correspondents(
            service.read_correspondence(since=parsed_since, limit=scan),
            excluded_addresses=excluded,
            since=parsed_since,
            minimum_count=minimum_count,
            limit=limit,
        )

    core.run(ctx, "recipients frequent", action, title="Frequent correspondents", warnings=warnings)
