"""Calendar views, events and invitations."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfigurationError,
    NetworkError,
)
from gucmail.models import (
    CalendarEvent,
    CalendarResponse,
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
from gucmail.services.gateway import DeferredGateway
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.pim_preferences import (
    UNSET as PIM_UNSET,
)
from gucmail.services.pim_preferences import CalendarCreationPreferences

calendar_app = typer.Typer(help="See your calendar, add events and answer invitations.")


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
        preferences = CalendarDisplayPreferences.from_settings(core.settings_service(ctx))
        selected_kind = kind or preferences.default_view
        working_days = (
            CalendarWorkingTime.from_settings(core.settings_service(ctx)).weekdays
            if selected_kind is CalendarViewKind.WORK_WEEK
            else (0, 1, 2, 3, 4)
        )
        return load_calendar_view(
            CalendarService(
                DeferredGateway(lambda: core.gateway(ctx)), display_timezone=preferences.timezone
            ),
            kind=selected_kind,
            anchor=anchor,
            first_weekday=preferences.first_weekday,
            working_days=working_days,
            agenda_days=agenda_days,
            limit=limit,
        )

    core.run(
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
        anchor=core.iso_date(day, parameter="day"),
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
        anchor=core.iso_date(day, parameter="day"),
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
        anchor=core.iso_date(day, parameter="day"),
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
        preferences = CalendarDisplayPreferences.from_settings(core.settings_service(ctx))
        view = CalendarService(
            DeferredGateway(lambda: core.gateway(ctx)), display_timezone=preferences.timezone
        ).agenda(days=days, limit=limit)
        return tuple(
            event for event in view.events if needle in calendar_event_search_text(event).casefold()
        )

    def present(events: tuple[CalendarEvent, ...]) -> Any:
        assert preferences is not None
        return [event_display_data(event, preferences.timezone) for event in events]

    core.run(ctx, "calendar search", action, title="Calendar search results", human_data=present)


@calendar_app.command("show")
def calendar_show(ctx: typer.Context, reference: str = typer.Argument(...)) -> None:
    """Show one event in the display zone; JSON retains original event metadata."""

    preferences: CalendarDisplayPreferences | None = None

    def action() -> CalendarEvent:
        nonlocal preferences
        preferences = CalendarDisplayPreferences.from_settings(core.settings_service(ctx))
        return CalendarService(
            DeferredGateway(lambda: core.gateway(ctx)), display_timezone=preferences.timezone
        ).get(reference)

    def present(event: CalendarEvent) -> Any:
        assert preferences is not None
        return event_display_data(event, preferences.timezone)

    core.run(ctx, "calendar show", action, title="Calendar event", human_data=present)


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
        preferences = CalendarDisplayPreferences.from_settings(core.settings_service(ctx))
        return CalendarService(DeferredGateway(lambda: core.gateway(ctx))).history(
            reference, limit=limit
        )

    def present(result: CalendarSeriesHistory) -> str:
        assert preferences is not None
        return history_text(result, preferences.timezone)

    core.run(
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
        parsed_start = core.iso_datetime(start, parameter="--start")
        parsed_end = core.iso_datetime(end, parameter="--end")
        assert parsed_start is not None
        if no_reminder and reminder_minutes is not None:
            raise ConfigurationError("Choose either --reminder-minutes or --no-reminder.")
        preferences = CalendarCreationPreferences.from_settings(core.settings_service(ctx))
        service = CalendarService(
            DeferredGateway(lambda: core.gateway(ctx)),
            display_timezone=core.cli_state(ctx).context.config.timezone,
        )
        event = replace(
            preferences.new_event(
                subject=subject,
                start=parsed_start,
                end=parsed_end,
                timezone=core.cli_state(ctx).context.config.timezone,
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
            required_attendees=tuple(core.parse_addresses(required)),
            optional_attendees=tuple(core.parse_addresses(optional)),
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
        core.confirm(ctx, f"Create calendar event {subject!r}?", f"event-create:{subject}")
        return service.gateway.create_event(plan)

    core.run(ctx, "calendar create", action, title="Calendar event")


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
                ("start", core.iso_datetime(start, parameter="--start")),
                ("end", core.iso_datetime(end, parameter="--end")),
                ("location", location),
                ("body", body),
            )
            if value is not None
        }
        service = CalendarService(
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
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
        core.confirm(
            ctx,
            f"Update {len(plan.changes)} field(s) on event {reference}?",
            f"event-update:{reference}",
        )
        return service.gateway.update_event(plan)

    core.run(ctx, "calendar update", action, title="Calendar update")


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
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
        )
        plan = service.plan_delete(reference, send_cancellation=send_cancellation, reason=reason)
        if dry_run:
            return plan
        core.confirm(ctx, f"Delete calendar event {reference}?", f"event-delete:{reference}")
        service.delete(plan)
        return {"reference": reference, "deleted": True}

    core.run(ctx, "calendar delete", action, title="Calendar deletion")


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
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
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
        core.confirm(
            ctx, f"Cancel meeting {reference} and notify attendees?", f"event-cancel:{reference}"
        )
        service.delete(plan)
        return {"reference": reference, "cancelled": True, "attendees_notified": True}

    core.run(ctx, "calendar cancel", action, title="Meeting cancellation")


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
                core.settings_service(ctx)
            ).invitation_response
        )
        service = CalendarService(
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
        )
        event = service.gateway.get_event(reference)
        plan = service.plan_response(
            event,
            chosen_response,
            send_response=send_response,
            comment=comment,
            proposed_start=core.iso_datetime(proposed_start, parameter="--proposed-start"),
            proposed_end=core.iso_datetime(proposed_end, parameter="--proposed-end"),
        )
        if dry_run:
            return plan
        core.confirm(
            ctx,
            f"{chosen_response.value.title()} meeting {reference}?",
            f"meeting:{chosen_response}:{reference}",
        )
        return service.respond(plan)

    core.run(ctx, "calendar respond", action, title="Meeting response")


@calendar_app.command("availability")
def calendar_availability(
    ctx: typer.Context,
    attendee: list[str] = typer.Option(..., "--attendee"),
    start: str = typer.Option(..., help="ISO-8601 start with an offset."),
    end: str = typer.Option(..., help="ISO-8601 end with an offset."),
    interval: int = typer.Option(30, min=5, max=1440),
) -> None:
    """Read attendee free/busy data through EWS GetUserAvailability."""

    parsed_start = core.iso_datetime(start, parameter="--start")
    parsed_end = core.iso_datetime(end, parameter="--end")
    assert parsed_start is not None and parsed_end is not None
    if parsed_end <= parsed_start:
        raise typer.BadParameter("--end must be later than --start.")
    core.run(
        ctx,
        "calendar availability",
        lambda: core.gateway(ctx).get_availability(
            core.parse_addresses(attendee),
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
        parsed_start = core.iso_datetime(start, parameter="--start")
        parsed_end = core.iso_datetime(end, parameter="--end")
        assert parsed_start is not None and parsed_end is not None
        attendees = tuple(core.parse_addresses(attendee))
        validate_free_time_request(attendees, parsed_start, parsed_end, interval, duration_minutes)
        timezone = CalendarDisplayPreferences.from_settings(core.settings_service(ctx)).timezone
        working_time = (
            None if all_hours else CalendarWorkingTime.from_settings(core.settings_service(ctx))
        )
        if working_time is not None:
            working_time.windows(parsed_start, parsed_end, timezone)
        values = core.gateway(ctx).get_availability(
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

    core.run(ctx, "calendar free-time", action, title="Calendar free-time suggestion")


@calendar_app.command("conflicts")
def calendar_conflicts(
    ctx: typer.Context,
    days: int = typer.Option(30, min=1, max=366),
    limit: int = typer.Option(200, min=2, max=200),
) -> None:
    """Detect overlapping busy events in a bounded local calendar view."""

    def action() -> Any:
        view = CalendarService(
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
        ).agenda(days=days, limit=limit)
        return {
            "window": {"start": view.start, "end": view.end},
            "conflicts": detect_conflicts(view.events),
        }

    core.run(ctx, "calendar conflicts", action, title="Calendar conflicts")


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
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
        )
        plan = service.import_ics(path.read_text(encoding="utf-8"))
        if not apply:
            return plan
        core.confirm(ctx, f"Create {len(plan.events)} imported event(s)?", "calendar-import")
        return service.apply_import(plan)

    core.run(ctx, "calendar import", action, title="Calendar import")


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
            core.gateway(ctx), display_timezone=core.cli_state(ctx).context.config.timezone
        )
        content = service.export_event(reference)
        if output.exists() and not overwrite:
            raise ConfigurationError(
                f"Refusing to overwrite {output}. Pass --overwrite after reviewing the target."
            )
        if output.exists():
            core.confirm(ctx, f"Overwrite existing ICS file {output}?", f"calendar-export:{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(output), "events": 1}

    core.run(ctx, "calendar export", action, title="Calendar export")


@calendar_app.command("watch")
def calendar_watch(
    ctx: typer.Context,
    forever: bool = typer.Option(False, help="Continue until interrupted."),
    notify_existing: bool = typer.Option(False, help="Also report the initial stored events."),
    reset_checkpoint: bool = typer.Option(False, help="Rebuild this local calendar baseline."),
) -> None:
    """Report calendar creations, updates and deletions using bounded EWS sync pages."""

    def action() -> object:
        context = core.cli_state(ctx).context
        monitor = MailboxMonitor(
            core.gateway(ctx),
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
            core.cli_state(ctx).writer.emit(
                result, command="calendar watch", title="Calendar changes"
            )
        return None

    core.run(ctx, "calendar watch", action, title="Calendar watch")
