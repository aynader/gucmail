from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text
from textual.widgets import Button, DataTable, Select
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import CalendarResponse, to_primitive
from gucmail.notification_policies import DigestCadence, DigestSchedule
from gucmail.notification_rules import RuleActionKind
from gucmail.services.calendar_preferences import (
    CALENDAR_OPERATION_SETTINGS,
    CalendarOperationPreferences,
)
from gucmail.services.interchange import export_ics
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.notifications import DeliveryState, NotificationDispatcher, ProviderBinding
from gucmail.services.profiled_notifications import (
    ProfiledNotificationDispatcher,
    StateStoreRoutingQueue,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.pim_screens import CalendarScreen, MeetingResponseScreen, PimActionRequested
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import ReadOnlyCalendar, settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_contact_operations import activate
from test_pim import _event
from test_profiled_notifications import NOW, CaptureProvider, _persist_event, _profile, _queue_rows
from test_profiled_notifications import _event as mail_event
from test_settings_secondpass import _context

VALUES = dict(
    zip(CALENDAR_OPERATION_SETTINGS, ("tentative", "availability", False, False), strict=True)
)


def meeting():
    now = datetime.now(UTC)
    return replace(
        _event("calendar-operation", now, now + timedelta(hours=1)),
        subject="Synthetic [red]literal[/red]\x1b[31m meeting",
        is_meeting=True,
        response_status="NoResponseReceived",
        categories=("Synthetic category [bold]",),
        availability="free",
    )


def configure(context):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in VALUES.items():
        session.set(key, value)
    session.apply()
    return service


def test_calendar_operations_canonical_persistence_cancel_scope_transfer_reset_undo_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = configure(context)
    expected = CalendarOperationPreferences(
        CalendarResponse.TENTATIVE, "availability", False, False
    )
    assert CalendarOperationPreferences.from_settings(settings_for(context)) == expected
    before = context.paths.config_file.read_bytes()
    service.begin().set("calendar.invitation_response", "decline").cancel()
    assert context.paths.config_file.read_bytes() == before
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert CalendarOperationPreferences.from_settings(other) == CalendarOperationPreferences()
    target = settings_for(_context(tmp_path / "transfer"))
    target.apply_import(target.plan_import(service.export(setting_ids=CALENDAR_OPERATION_SETTINGS)))
    assert CalendarOperationPreferences.from_settings(target) == expected
    for key in CALENDAR_OPERATION_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert CalendarOperationPreferences.from_settings(service) == expected
    before_environment = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__INVITATION_RESPONSE", "decline")
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__COLOR_BEHAVIOR", "category")
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__NOTIFICATIONS", "true")
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__SHOW_RELATED_EMAILS", "true")
    assert CalendarOperationPreferences.from_settings(service) == CalendarOperationPreferences(
        CalendarResponse.DECLINE, "category", True, True
    )
    assert context.paths.config_file.read_bytes() == before_environment


@pytest.mark.parametrize(
    "key,value",
    [
        ("calendar.invitation_response", "automatic"),
        ("calendar.color_behavior", "[red]"),
        ("calendar.notifications", "sometimes"),
        ("calendar.show_related_emails", "maybe"),
    ],
)
def test_invalid_calendar_operations_preserve_stored_config(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, value)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "availability,color",
    [
        ("free", "green"),
        ("working_elsewhere", "cyan"),
        ("tentative", "yellow"),
        ("busy", "blue"),
        ("out_of_office", "magenta"),
    ],
)
def test_event_colors_are_presentation_only_and_keep_json_and_ics(availability, color):
    event = replace(meeting(), availability=availability)
    before = to_primitive(event), export_ics((event,))
    assert CalendarOperationPreferences(color_behavior="availability").event_color(event) == color
    assert CalendarOperationPreferences().event_color(event) is None
    category = CalendarOperationPreferences(color_behavior="category").event_color(event)
    assert category == CalendarOperationPreferences(color_behavior="category").event_color(
        replace(event, categories=(event.categories[0].upper(),))
    )
    assert "[" not in category and "\x1b" not in category
    assert (
        CalendarOperationPreferences(color_behavior="category").event_color(
            replace(event, categories=())
        )
        is None
    )
    assert (to_primitive(event), export_ics((event,))) == before


@pytest.mark.parametrize("saved", ["accept", "tentative", "decline"])
@pytest.mark.parametrize("explicit", [None, "accept"])
def test_cli_response_omitted_uses_saved_but_explicit_wins_and_no_implicit_write(
    tmp_path, monkeypatch, saved, explicit
):
    context = _context(tmp_path)
    settings_for(context).set("calendar.invitation_response", saved)
    event = meeting()
    gateway = ReadOnlyCalendar()
    gateway.events = {event.reference: event}
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    args = ["respond", event.reference, *([explicit] if explicit else [])]
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "calendar", *args, "--dry-run"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["response"] == (explicit or saved)
    assert gateway.responses == []
    denied = CliRunner().invoke(
        cli.app, ["--json", "--non-interactive", "calendar", *args], env=_environment(tmp_path)
    )
    assert denied.exit_code != 0 and gateway.responses == []
    allowed = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "--yes", "calendar", *args, "--save-only"],
        env=_environment(tmp_path),
    )
    assert allowed.exit_code == 0, allowed.output
    assert len(gateway.responses) == 1
    assert gateway.responses[0].response.value == (explicit or saved)
    assert gateway.responses[0].send_response is False


@pytest.mark.parametrize("profiled", [False, True])
def test_shared_watcher_factory_reloads_calendar_filter_without_affecting_mail(tmp_path, profiled):
    context = _context(tmp_path)
    provider = CaptureProvider()
    if profiled:
        NotificationConfigurationService(context.state).profiles.save_profile(_profile())
    dispatcher = cli.core.profiled_notification_dispatcher(
        context, [ProviderBinding(provider)], profile_id="study" if profiled else None
    )
    settings_for(context).set("calendar.notifications", False)
    for kind in ("calendar_created", "calendar_updated", "calendar_deleted"):
        event = replace(mail_event(kind), event_type=kind, folder="Calendar")
        assert dispatcher.dispatch(event)[0].state is DeliveryState.SKIPPED
    assert provider.events == []
    assert dispatcher.dispatch(mail_event())[0].state is DeliveryState.DELIVERED
    assert len(provider.events) == 1
    settings_for(context).set("calendar.notifications", True)
    assert (
        dispatcher.dispatch(replace(mail_event("reenabled"), event_type="calendar_created"))[
            0
        ].state
        is DeliveryState.DELIVERED
    )
    assert len(provider.events) == 2


@pytest.mark.parametrize("profiled", [False, True])
def test_invalid_calendar_environment_blocks_calendar_but_keeps_mail_dispatch(
    tmp_path, monkeypatch, profiled
):
    context = _context(tmp_path)
    provider = CaptureProvider()
    if profiled:
        NotificationConfigurationService(context.state).profiles.save_profile(_profile())
    dispatcher = cli.core.profiled_notification_dispatcher(
        context, [ProviderBinding(provider)], profile_id="study" if profiled else None
    )
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__NOTIFICATIONS", "invalid-private-value")
    assert dispatcher.dispatch(mail_event())[0].state is DeliveryState.DELIVERED
    result = dispatcher.dispatch(
        replace(mail_event("calendar-invalid"), event_type="calendar_updated")
    )[0]
    assert result.state is DeliveryState.FAILED and result.attempts == 0
    assert "invalid-private-value" not in result.detail
    assert len(provider.events) == 1 and provider.events[0].event_type == "new_message"


@pytest.mark.parametrize("mode", ["quiet", "digest", "mixed"])
def test_calendar_opt_out_filters_queued_members_before_disclosure_and_mixed_digest(tmp_path, mode):
    context = _context(tmp_path)
    provider = CaptureProvider()
    queue = StateStoreRoutingQueue(context.state)
    calendar = replace(
        mail_event("calendar-private"),
        event_type="calendar_created",
        subject="Calendar must disappear",
    )
    mail = replace(mail_event("mail-kept"), subject="Mail must remain")
    schedule = DigestSchedule("hourly", "Hourly", DigestCadence.HOURLY, "UTC")
    profile = _profile(rule_kind=RuleActionKind.ROUTE_DIGEST, digest_schedules=(schedule,))
    for event in (calendar, mail) if mode == "mixed" else (calendar,):
        _persist_event(context.state, event)
        queue.enqueue(
            profile_id=profile.profile_id,
            provider_id="capture",
            event_id=event.event_id,
            mode="quiet" if mode == "quiet" else "digest",
            due_at=NOW,
            schedule_id="hourly",
            batch_key="shared",
        )
    settings_for(context).set("calendar.notifications", False)
    # Full privacy here makes any accidental inclusion visible to the fake provider.
    from gucmail.notification_events import PrivacyMode

    profile = _profile(privacy=PrivacyMode.FULL, digest_schedules=(schedule,))
    dispatcher = ProfiledNotificationDispatcher(
        profile,
        {"capture": provider},
        state=context.state,
        queue=queue,
        clock=lambda: NOW,
        event_allowed=lambda event: CalendarOperationPreferences.from_settings(
            settings_for(context)
        ).allows_notification(event),
    )
    results = dispatcher.drain_due()
    assert any(result.state is DeliveryState.SKIPPED for result in results)
    if mode == "mixed":
        assert len(provider.events) == 1
        digest = provider.events[0]
        assert "Mail must remain" in digest.preview
        assert "Calendar must disappear" not in digest.preview
        assert digest.subject == "1 new message"
    else:
        assert provider.events == []
    assert all(row["state"] == "delivered" for row in _queue_rows(context.state))
    assert dispatcher.drain_due() == ()


def test_invalid_notification_preference_fails_closed_without_leaking_failure(tmp_path):
    context = _context(tmp_path)
    provider = CaptureProvider()

    def broken(_event):
        raise RuntimeError("private-password-do-not-disclose")

    dispatcher = NotificationDispatcher([provider], state=context.state, event_allowed=broken)
    result = dispatcher.dispatch(mail_event())[0]
    assert result.state is DeliveryState.FAILED and result.attempts == 0
    assert "private-password" not in result.detail and provider.events == []
    assert "private-password" not in json.dumps(dispatcher.history())


async def stage(pilot, screen, key, value):
    await wait_for(pilot, lambda: not screen._operation_active)
    assert screen.action_select_setting(key)
    await pilot.pause()
    choice = screen.query_one("#settings-choice", Select)
    await wait_for(pilot, lambda: bool(choice.query("#label")))
    choice.value = ("on" if value else "off") if isinstance(value, bool) else value
    await activate(pilot, screen, "#settings-stage")
    await wait_for(pilot, lambda: key in screen._staged and not screen._operation_active)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_calendar_controls_response_override_colors_related_and_cancel(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    gateway = ReadOnlyCalendar()
    event = meeting()
    original = to_primitive(event)
    gateway.events = {event.reference: event}
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("calendar")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, CalendarScreen)
                and app.screen.is_mounted
                and len(app.screen._events) == 1
                and not app.screen._operation_active
            ),
        )
        calendar = app.screen
        queries = len(gateway.queries)
        assert queries == 1
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        for key, value in VALUES.items():
            await stage(pilot, screen, key, value)
        await activate(pilot, screen, "#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        assert context.paths.config_file.read_bytes() == before
        assert calendar._operations == CalendarOperationPreferences()
        for key, value in VALUES.items():
            await stage(pilot, screen, key, value)
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(
            pilot,
            lambda: (
                app.screen is calendar
                and calendar._operations.invitation_response is CalendarResponse.TENTATIVE
            ),
        )
        assert len(gateway.queries) == queries
        table = calendar.query_one("#calendar-table", DataTable)
        assert list(table.columns)[1].value == "subject"
        cell = table.get_cell(event.reference, "subject")
        assert isinstance(cell, Text) and str(cell.style) == "green"
        assert "[red]literal[/red]" in cell.plain and "\x1b" not in cell.plain
        assert calendar.query_one("#calendar-related", Button).disabled
        calendar.action_correspondence()
        assert app.screen is calendar and len(gateway.queries) == queries
        await activate(pilot, calendar, "#calendar-respond")
        await wait_for(
            pilot, lambda: isinstance(app.screen, MeetingResponseScreen) and app.screen.is_mounted
        )
        assert app.screen.query_one("#meeting-response-value", Select).value == "tentative"
        await activate(pilot, app.screen, "#meeting-response-send")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert "tentative" in app.screen.detail_text
        assert gateway.responses == []
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is calendar)
        calendar.action_respond(CalendarResponse.DECLINE)
        await wait_for(
            pilot, lambda: isinstance(app.screen, MeetingResponseScreen) and app.screen.is_mounted
        )
        assert app.screen.query_one("#meeting-response-value", Select).value == "decline"
        await activate(pilot, app.screen, "#meeting-response-cancel")
        await wait_for(pilot, lambda: app.screen is calendar)
        assert gateway.responses == [] and to_primitive(gateway.events[event.reference]) == original


def test_direct_filter_rechecks_between_providers_after_privacy_changes(tmp_path):
    context = _context(tmp_path)

    class ChangingProvider(CaptureProvider):
        def notify(self, event):
            result = super().notify(event)
            settings_for(context).set("calendar.notifications", False)
            return result

    first, second = ChangingProvider(), CaptureProvider()
    dispatcher = NotificationDispatcher(
        [first, second],
        state=context.state,
        event_allowed=lambda event: CalendarOperationPreferences.from_settings(
            settings_for(context)
        ).allows_notification(event),
    )
    results = dispatcher.dispatch(replace(mail_event(), event_type="calendar_created"))
    assert len(first.events) == 1 and second.events == []
    assert [result.state for result in results] == [DeliveryState.DELIVERED, DeliveryState.SKIPPED]


def test_digest_filter_rechecks_after_an_earlier_provider_changes_privacy(tmp_path):
    context = _context(tmp_path)

    class ChangingProvider(CaptureProvider):
        def notify(self, event):
            result = super().notify(event)
            settings_for(context).set("calendar.notifications", False)
            return result

    provider = ChangingProvider()
    queue = StateStoreRoutingQueue(context.state)
    mail = mail_event("quiet-mail")
    calendar = replace(mail_event("later-calendar"), event_type="calendar_updated")
    for event, mode in ((mail, "quiet"), (calendar, "digest")):
        _persist_event(context.state, event)
        queue.enqueue(
            profile_id="study",
            provider_id="capture",
            event_id=event.event_id,
            mode=mode,
            due_at=NOW,
            schedule_id="hourly",
            batch_key="late",
        )
    dispatcher = ProfiledNotificationDispatcher(
        _profile(),
        {"capture": provider},
        state=context.state,
        queue=queue,
        clock=lambda: NOW,
        event_allowed=lambda event: CalendarOperationPreferences.from_settings(
            settings_for(context)
        ).allows_notification(event),
    )
    results = dispatcher.drain_due()
    assert [event.event_id for event in provider.events] == [mail.event_id]
    assert any(result.state is DeliveryState.SKIPPED for result in results)


@pytest.mark.asyncio
async def test_related_preference_changed_during_event_read_blocks_mail_query(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    event = meeting()

    class ChangingGateway(ReadOnlyCalendar):
        def get_event(self, reference):
            assert reference == event.reference
            settings_for(context).set("calendar.show_related_emails", False)
            return event

        def search_messages(self, *args, **kwargs):
            pytest.fail("Late opt-out still queried mailbox subjects")

    gateway = ChangingGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    monkeypatch.setattr(
        app, "_render_palette_search", lambda *_: pytest.fail("Late opt-out painted results")
    )
    async with app.run_test() as pilot:
        app._search_calendar_related_messages((event.reference,))
        await pilot.pause()
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_disabled_calendar_related_route_and_late_result_never_queries_or_paints(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    settings_for(context).set("calendar.show_related_emails", False)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Disabled related mail resolved credentials")
    )
    monkeypatch.setattr(
        app, "_render_palette_search", lambda *_: pytest.fail("Disabled late result was painted")
    )
    async with app.run_test() as pilot:
        app.pim_action_requested(
            PimActionRequested("calendar", "related_messages", ("private-event",))
        )
        app._search_calendar_related_messages(("private-event",))
        await pilot.pause()
        await app.workers.wait_for_complete()
        app._render_calendar_related_search((), "private subject", "late results")
