from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from exchangelib import CalendarItem
from exchangelib.properties import FieldPath, RecurringMasterItemId
from exchangelib.recurrence import Occurrence
from exchangelib.services import GetItem
from exchangelib.version import Build, Version
from textual.widgets import Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, NetworkError, UnsupportedFeatureError
from gucmail.ews.adapter import CALENDAR_HISTORY_FIELDS, CALENDAR_METADATA_FIELDS, EWSGateway
from gucmail.ews.mapping import map_calendar_history
from gucmail.models import to_primitive
from gucmail.services.calendar import CalendarService
from gucmail.services.calendar_history import history_text
from gucmail.state import StateStore
from gucmail.tui.area_common import PublicOperationError
from gucmail.tui.pim_screens import CalendarServiceBackend
from test_calendar_scope_safety import _SeriesGateway
from test_calendar_secondpass import _CalendarHarness, _wait_for_calendar_rows
from test_cli import _environment
from test_compose_preferences import wait_for
from test_contact_operations import activate

START = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def history(reference="occurrence", *, limit=100):
    first = SimpleNamespace(id="first", start=START, end=START + timedelta(minutes=20))
    last = SimpleNamespace(
        id="last", start=START + timedelta(days=7), end=START + timedelta(days=7, minutes=20)
    )
    item = SimpleNamespace(
        type="RecurringMaster",
        first_occurrence=first,
        last_occurrence=last,
        modified_occurrences=[
            SimpleNamespace(
                id="changed",
                start=START + timedelta(hours=1),
                end=START + timedelta(hours=1, minutes=20),
                original_start=START,
            )
        ],
        deleted_occurrences=[SimpleNamespace(start=START + timedelta(days=2))],
    )
    return map_calendar_history(
        item,
        requested_reference=reference,
        series_reference="master",
        observed_at=START,
        limit=limit,
    )


def test_mapping_preserves_original_times_identities_and_dst_display():
    value = history()
    assert value.modified_available and value.deleted_available
    assert value.modified_occurrences[0].original_start == START
    assert value.deleted_occurrences[0].reference is None
    before = to_primitive(value)
    text = history_text(value, "Europe/Berlin")
    assert "2026-10-25T02:30:00+02:00" in text
    assert "2026-10-25T02:30:00+01:00" in text
    assert "not a complete change audit trail" in text and "cannot be restored" in text
    assert (
        to_primitive(value) == before and "changed" not in value.modified_occurrences[0].reference
    )


@pytest.mark.parametrize("values,available", [(None, False), ([], True)])
def test_missing_and_known_empty_lists_are_distinct(values, available):
    value = map_calendar_history(
        SimpleNamespace(modified_occurrences=values, deleted_occurrences=values),
        requested_reference="request",
        series_reference="master",
        observed_at=START,
    )
    assert value.modified_available is available and value.deleted_available is available
    assert not value.modified_occurrences and not value.deleted_occurrences
    assert ("Modified: returned" if available else "Modified: unavailable") in history_text(
        value, "UTC"
    )


def test_lazy_metadata_truncation_consumes_only_limit_plus_one_per_list():
    consumed = {"modified": 0, "deleted": 0}

    def entries(kind):
        for index in range(1000):
            consumed[kind] += 1
            yield SimpleNamespace(start=START + timedelta(days=index))

    value = map_calendar_history(
        SimpleNamespace(
            modified_occurrences=entries("modified"), deleted_occurrences=entries("deleted")
        ),
        requested_reference="request",
        series_reference="master",
        observed_at=START,
        limit=2,
    )
    assert consumed == {"modified": 3, "deleted": 3}
    assert len(value.modified_occurrences) == len(value.deleted_occurrences) == 2
    assert value.modified_truncated and value.deleted_truncated
    assert "Modified: truncated" in history_text(value, "UTC")


@pytest.mark.parametrize(
    "values", ["hostile-list", [None], [42], [SimpleNamespace(start=datetime(2026, 1, 1))]]
)
def test_malformed_metadata_fails_without_echoing_hostile_input(values):
    with pytest.raises(ValueError) as exc:
        map_calendar_history(
            SimpleNamespace(modified_occurrences=values),
            requested_reference="request",
            series_reference="master",
            observed_at=START,
        )
    assert "hostile-list" not in str(exc.value)


@pytest.mark.parametrize("limit", [0, 201, True, "1", 1.0, None])
def test_invalid_limit_stops_before_gateway_access(limit):
    class Forbidden:
        def __getattr__(self, _name):
            pytest.fail("Invalid history request accessed gateway")

    with pytest.raises(ConfigurationError):
        CalendarService(Forbidden()).history("event", limit=limit)


def test_unsupported_gateway_is_explicit():
    with pytest.raises(UnsupportedFeatureError, match="occurrence history"):
        CalendarService(SimpleNamespace()).history("event")


def test_gateway_cannot_exceed_a_requested_history_bound():
    class Gateway:
        def get_calendar_history(self, reference, *, limit):
            return history(reference, limit=limit + 1)

    with pytest.raises(ConfigurationError, match="exceeded"):
        CalendarService(Gateway()).history("event", limit=2)


@pytest.mark.parametrize("before", [True, False])
def test_history_cancellation_before_read_and_before_presentation(before):
    cancel = Event()
    calls = []

    class Gateway:
        def get_calendar_history(self, reference, *, limit):
            calls.append(reference)
            cancel.set()
            return history(reference, limit=limit)

    if before:
        cancel.set()
    backend = CalendarServiceBackend(CalendarService(Gateway()))
    with pytest.raises(PublicOperationError, match="cancelled"):
        backend.history("event", cancel=cancel, progress=lambda _: None)
    assert calls == ([] if before else ["event"])


def test_native_master_fetch_keeps_special_identity_body_free_projection_and_durable_refs(tmp_path):
    occurrence = CalendarItem(
        id="selected-occurrence", changekey="occurrence-key", type="Occurrence"
    )
    master = CalendarItem(
        id="series-master",
        changekey="master-key",
        type="RecurringMaster",
        modified_occurrences=[
            Occurrence(
                id="modified", start=START, end=START + timedelta(hours=1), original_start=START
            )
        ],
        deleted_occurrences=[],
    )
    requests = []

    def fetch(*, ids, only_fields):
        requests.append((ids, only_fields))
        if isinstance(ids[0], tuple):
            assert ids == [(occurrence.id, occurrence.changekey)]
            return iter((occurrence,))
        assert isinstance(ids[0], CalendarItem)
        assert isinstance(ids[0]._id, RecurringMasterItemId) and ids[0].id == occurrence.id
        return iter((master,))

    state = StateStore(tmp_path / "history.sqlite3")
    gateway = EWSGateway(
        AppConfig(), state=state, account_factory=lambda: SimpleNamespace(fetch=fetch)
    )
    reference = gateway._remember("event", occurrence)
    value = CalendarService(gateway).history(reference, limit=7)
    assert len(requests) == 2 and all(fields == CALENDAR_HISTORY_FIELDS for _, fields in requests)
    assert "body" not in CALENDAR_HISTORY_FIELDS and "attachments" not in CALENDAR_HISTORY_FIELDS
    assert not set(CALENDAR_HISTORY_FIELDS[1:]) & set(CALENDAR_METADATA_FIELDS)
    assert value.requested_reference == reference and value.series_reference != reference
    reopened = StateStore(state.path)
    assert reopened.resolve_reference(reference, "event")["ews_id"] == "selected-occurrence"
    assert reopened.resolve_reference(value.series_reference, "event")["ews_id"] == "series-master"
    assert (
        reopened.resolve_reference(value.modified_occurrences[0].reference, "event")["ews_id"]
        == "modified"
    )


def test_real_getitem_xml_uses_recurring_master_identity_and_metadata_only_shape():
    version = Version(build=Build(15, 0, 0, 0))
    service = GetItem(account=SimpleNamespace(version=version, protocol=SimpleNamespace()))
    occurrence = CalendarItem(id="selected-occurrence", type="Occurrence")
    payload = service.get_payload(
        items=[occurrence.recurring_master()],
        additional_fields=[
            FieldPath(field=CalendarItem.FIELDS[name]) for name in CALENDAR_HISTORY_FIELDS
        ],
        shape="IdOnly",
    )
    ns = {
        "m": "http://schemas.microsoft.com/exchange/services/2006/messages",
        "t": "http://schemas.microsoft.com/exchange/services/2006/types",
    }
    master_id = payload.find("m:ItemIds/t:RecurringMasterItemId", ns)
    assert master_id is not None and master_id.get("OccurrenceId") == "selected-occurrence"
    assert payload.find("m:ItemIds/t:ItemId", ns) is None
    fields = {
        field.get("FieldURI")
        for field in payload.findall("m:ItemShape/t:AdditionalProperties/t:FieldURI", ns)
    }
    assert fields == {
        "calendar:CalendarItemType",
        "calendar:FirstOccurrence",
        "calendar:LastOccurrence",
        "calendar:ModifiedOccurrences",
        "calendar:DeletedOccurrences",
    }


def test_cli_json_human_and_invalid_bound_are_read_only(tmp_path, monkeypatch):
    environment = _environment(tmp_path)
    calls = []

    class Gateway:
        def get_calendar_history(self, reference, *, limit):
            calls.append((reference, limit))
            return history(reference, limit=limit)

    monkeypatch.setattr(cli, "_gateway", lambda *_: Gateway())
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--json", "--non-interactive", "calendar", "history", "occurrence", "--limit", "2"],
        env=environment,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["requested_reference"] == "occurrence"
    human = runner.invoke(
        cli.app, ["--no-color", "calendar", "history", "occurrence"], env=environment
    )
    assert human.exit_code == 0 and "read-only" in human.output and "Modified:" in human.output
    denied = runner.invoke(
        cli.app, ["calendar", "history", "occurrence", "--limit", "201"], env=environment
    )
    assert denied.exit_code != 0 and calls == [("occurrence", 2), ("occurrence", 100)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_actual_calendar_keyboard_history_and_late_result_keep_original_event(size):
    class Gateway(_SeriesGateway):
        def __init__(self):
            super().__init__()
            self.history_calls = []
            self.fail = False

        def get_calendar_history(self, reference, *, limit):
            self.history_calls.append((reference, limit))
            if self.fail:
                raise NetworkError("Synthetic history read failed.")
            return history(reference, limit=limit)

    gateway = Gateway()
    before = to_primitive(gateway.events)
    app = _CalendarHarness(CalendarServiceBackend(CalendarService(gateway)))
    async with app.run_test(size=size) as pilot:
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        await activate(pilot, screen, "#calendar-history")
        await wait_for(
            pilot, lambda: not screen._operation_active and len(gateway.history_calls) == 1
        )
        content = str(screen.query_one("#calendar-detail", Static).content)
        assert (
            "read-only" in content
            and "Modified: returned" in content
            and "Deleted: returned" in content
        )
        scroll = screen.query_one("#calendar-detail-scroll")
        scroll.focus()
        await pilot.press("end")
        await wait_for(pilot, lambda: scroll.scroll_y == scroll.max_scroll_y)
        assert scroll.max_scroll_y > 0
        assert scroll.scroll_y > 0
        await pilot.press("home")
        await wait_for(pilot, lambda: scroll.scroll_y == 0)
        root = screen.query_one("#calendar-root")
        root.focus()
        await pilot.press("end")
        await wait_for(pilot, lambda: root.scroll_y == root.max_scroll_y)
        assert screen.query_one("#calendar-footer").region.bottom <= size[1]
        screen._history_loaded(replace(history("other"), notes=("late private history",)))
        assert str(screen.query_one("#calendar-detail", Static).content) == content
        assert gateway.history_calls == [("occurrence", 100)]
        gateway.fail = True
        await activate(pilot, screen, "#calendar-history")
        await wait_for(pilot, lambda: not screen._operation_active)
        await pilot.pause()
        assert len(gateway.history_calls) == 2
        assert "Synthetic history read failed" in str(
            screen.query_one("#area-state", Static).content
        )
        assert screen.query_one("#area-retry").display
        assert str(screen.query_one("#calendar-detail", Static).content) == content
        gateway.fail = False
        await activate(pilot, screen, "#area-retry")
        await wait_for(pilot, lambda: not screen._operation_active)
        assert gateway.history_calls == [("occurrence", 100)] * 3
        assert "metadata loaded" in str(screen.query_one("#area-state", Static).content)
        assert (
            to_primitive(gateway.events) == before and not gateway.updates and not gateway.deletes
        )
