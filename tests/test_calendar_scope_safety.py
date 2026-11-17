"""Series identity, preflight, and actual mounted scope/confirmation workflows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Select

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import CalendarEvent
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    EventUpdatePlan,
    RecurrenceScope,
)
from gucmail.state import StateStore
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.pim_screens import (
    CalendarEditorScreen,
    CalendarEditorValue,
    CalendarScopeScreen,
    CalendarScreen,
    CalendarServiceBackend,
)
from test_calendar_secondpass import (
    _CalendarHarness,
    _MutableCalendarGateway,
    _wait_for_calendar_rows,
)
from waiting import wait_until


def _series() -> tuple[CalendarEvent, CalendarEvent]:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    occurrence = CalendarEvent(
        "occurrence",
        "Recurring lesson",
        now + timedelta(hours=1),
        now + timedelta(hours=2),
        timezone="UTC",
        item_type="Occurrence",
    )
    master = replace(
        occurrence,
        reference="master",
        item_type="RecurringMaster",
        start=occurrence.start - timedelta(days=7),
        end=occurrence.end - timedelta(days=7),
        recurrence={"freq": "daily", "count": 20},
    )
    return occurrence, master


class _SeriesGateway(_MutableCalendarGateway):
    def __init__(self) -> None:
        occurrence, master = _series()
        super().__init__((occurrence, master))
        self.updates: list[EventUpdatePlan] = []
        self.deletes: list[object] = []
        self.fail_delete = False

    def list_events(self, **kwargs):
        return tuple(
            event for event in super().list_events(**kwargs) if event.reference != "master"
        )

    def get_event_series(self, reference):
        assert reference in {"occurrence", "master", "second-occurrence"}
        return self.events["master"]

    def update_event(self, plan):
        self.updates.append(plan)
        return super().update_event(plan)

    def delete_event(self, plan):
        self.deletes.append(plan)
        if self.fail_delete:
            raise TimeoutError("The outcome of this synthetic write is uncertain.")
        super().delete_event(plan)


def test_partial_series_patch_validates_against_real_master() -> None:
    gateway = _SeriesGateway()
    occurrence, master = _series()
    # This end is before the selected occurrence, but after the real master start.
    new_end = master.end + timedelta(minutes=20)
    service = CalendarService(gateway)
    plan = service.plan_update(
        occurrence.reference,
        CalendarPatch(end=new_end),
        scope=RecurrenceScope.SERIES,
    )
    assert plan.reference == "master"
    updated = service.update(
        occurrence.reference,
        CalendarPatch(end=new_end),
        scope=RecurrenceScope.SERIES,
    )
    assert updated.start == gateway.events["master"].start
    assert updated.end == new_end
    assert gateway.events["occurrence"].end == occurrence.end


def test_unsafe_master_occurrence_and_future_scopes_cannot_mutate() -> None:
    gateway = _SeriesGateway()
    service = CalendarService(gateway)
    with pytest.raises(ConfigurationError, match="entire series"):
        service.update("master", CalendarPatch(subject="wrong scope"))
    with pytest.raises(ConfigurationError, match="repetition pattern"):
        service.update("occurrence", CalendarPatch(recurrence=None))
    with pytest.raises(UnsupportedFeatureError, match="future"):
        service.update("occurrence", CalendarPatch(subject="wrong"), scope="future")
    assert not gateway.updates


def test_occurrence_editor_preserves_series_pattern_at_service_boundary() -> None:
    gateway = _SeriesGateway()
    occurrence = gateway.events["occurrence"]
    backend = CalendarServiceBackend(CalendarService(gateway))
    value = CalendarEditorValue(
        "Changed occurrence",
        occurrence.start,
        occurrence.end,
        "UTC",
        recurrence=None,
    )
    backend.update_event("occurrence", value, cancel=Event(), progress=lambda _: None)
    assert gateway.updates[0].reference == "occurrence"
    assert "recurrence" not in gateway.updates[0].changes
    assert gateway.events["master"].recurrence


@pytest.mark.parametrize("uncertain", [False, True])
def test_bulk_series_delete_never_replays_same_master(uncertain: bool) -> None:
    gateway = _SeriesGateway()
    gateway.events["second-occurrence"] = replace(
        gateway.events["occurrence"], reference="second-occurrence"
    )
    gateway.fail_delete = uncertain
    # Preserve synthetic readback of a removed master to model a stale page.
    master = gateway.events["master"]
    gateway.get_event_series = lambda _: master
    result = CalendarServiceBackend(CalendarService(gateway)).delete_events(
        ("occurrence", "second-occurrence"),
        cancel=Event(),
        progress=lambda _: None,
        scope=RecurrenceScope.SERIES,
    )
    assert len(gateway.deletes) == 1
    assert result.completed == (0 if uncertain else 2)


class _ProviderEvent(SimpleNamespace):
    def refresh(self):
        pass

    def save(self, **kwargs):
        self.writes.append(("save", kwargs))

    def detach(self, values):
        self.writes.append(("detach", values))


def _provider_gateway(tmp_path: Path):
    occurrence, master = _series()
    writes = []
    master_item = _ProviderEvent(
        id="ews-master",
        changekey="master-ck",
        type="RecurringMaster",
        subject=master.subject,
        start=master.start,
        end=master.end,
        attachments=(),
        writes=writes,
    )
    occurrence_item = _ProviderEvent(
        id="ews-occurrence",
        changekey="occurrence-ck",
        type="Occurrence",
        subject=occurrence.subject,
        start=occurrence.start,
        end=occurrence.end,
        attachments=(),
        recurring_master=lambda: master_item,
        writes=writes,
    )
    state = StateStore(tmp_path / "scope.sqlite3")
    gateway = EWSGateway(AppConfig(), state=state, account_factory=lambda: SimpleNamespace())
    reference = gateway._remember("event", occurrence_item)
    return gateway, reference, master_item, state, writes


def test_provider_series_read_and_write_keep_occurrence_identity_after_reopen(tmp_path):
    gateway, occurrence_ref, master_item, state, writes = _provider_gateway(tmp_path)
    master = gateway.get_event_series(occurrence_ref)
    assert master.start == master_item.start
    assert master.reference != occurrence_ref
    updated = gateway.update_event(
        EventUpdatePlan(
            occurrence_ref,
            {"subject": "Updated series"},
            scope=RecurrenceScope.SERIES,
        )
    )
    reopened = StateStore(state.path)
    assert reopened.resolve_reference(occurrence_ref, "event")["ews_id"] == "ews-occurrence"
    assert reopened.resolve_reference(updated.reference, "event")["ews_id"] == "ews-master"
    assert updated.reference == master.reference
    assert len(writes) == 1
    assert writes[0][1]["update_fields"] == ["subject"]


@pytest.mark.parametrize("invalid", ["missing-file", "foreign-attachment"])
def test_provider_attachment_preflight_prevents_any_remote_save(tmp_path, invalid):
    gateway, reference, _, _, writes = _provider_gateway(tmp_path)
    plan = EventUpdatePlan(
        reference,
        {"subject": "must not save"},
        attachment_paths=(tmp_path / "missing.pdf",) if invalid == "missing-file" else (),
        remove_attachment_references=("someone-elses-attachment",)
        if invalid == "foreign-attachment"
        else (),
    )
    with pytest.raises(ConfigurationError):
        gateway.update_event(plan)
    assert not writes


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_mounted_series_edit_loads_master_and_requires_exact_scope_confirmation(size):
    gateway = _SeriesGateway()
    app = _CalendarHarness(CalendarServiceBackend(CalendarService(gateway)))
    async with app.run_test(size=size) as pilot:
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        screen.action_edit()
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, CalendarScopeScreen),
            interval=0.05,
            pause_first=True,
        )
        chooser = app.screen
        assert isinstance(chooser, CalendarScopeScreen)
        for button in chooser.query(Button):
            assert button.region.bottom <= size[1]
        # The chooser draws its buttons after it opens.
        await wait_until(pilot, lambda: chooser.query("#calendar-scope-series"))
        chooser.query_one("#calendar-scope-series", Button).press()
        await wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, CalendarEditorScreen)
                and app.screen.is_mounted
                and app.screen.query_one("#calendar-edit-availability", Select).value
                is not Select.BLANK
            ),
            interval=0.05,
            pause_first=True,
        )
        editor = app.screen
        assert isinstance(editor, CalendarEditorScreen)
        await pilot.pause()
        assert editor.event.reference == "master"
        assert editor._value().start == gateway.events["master"].start
        assert not editor.query_one("#calendar-edit-repeat", Select).disabled
        editor.query_one("#calendar-edit-subject", Input).value = "Reviewed series"
        editor.action_save()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Entire series" in app.screen.detail_text
        assert "Reviewed series" in app.screen.detail_text
        assert not gateway.updates
        await pilot.press("y")
        await wait_until(pilot, lambda: gateway.updates, interval=0.05, pause_first=True)
        assert gateway.updates[0].reference == "master"
        assert gateway.updates[0].scope is RecurrenceScope.SERIES
        assert gateway.events["occurrence"].subject == "Recurring lesson"


@pytest.mark.asyncio
async def test_occurrence_scope_cancel_and_series_delete_preview_are_safe():
    gateway = _SeriesGateway()
    app = _CalendarHarness(CalendarServiceBackend(CalendarService(gateway)))
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, CalendarScopeScreen)
        await pilot.press("escape")
        assert not gateway.deletes
        screen.action_edit()
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, CalendarScopeScreen),
            interval=0.05,
            pause_first=True,
        )
        # The chooser draws its buttons after it opens.
        await wait_until(pilot, lambda: app.screen.query("#calendar-scope-occurrence"))
        app.screen.query_one("#calendar-scope-occurrence", Button).press()
        await wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, CalendarEditorScreen)
                and app.screen.query_one("#calendar-edit-repeat", Select).disabled
            ),
            interval=0.05,
            pause_first=True,
        )
        assert isinstance(app.screen, CalendarEditorScreen)
        assert app.screen.query_one("#calendar-edit-repeat", Select).disabled
        await pilot.press("escape")
        assert not gateway.updates
        screen.action_delete()
        await pilot.pause()
        # The chooser draws its buttons after it opens.
        await wait_until(pilot, lambda: app.screen.query("#calendar-scope-series"))
        app.screen.query_one("#calendar-scope-series", Button).press()
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ConfirmationScreen),
            interval=0.05,
            pause_first=True,
        )
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Every date" in app.screen.detail_text
        await pilot.press("escape")
        await wait_until(pilot, lambda: isinstance(app.screen, CalendarScreen))
        assert isinstance(app.screen, CalendarScreen)
        assert not gateway.deletes
