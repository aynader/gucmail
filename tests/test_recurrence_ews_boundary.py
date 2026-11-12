"""Exercise installed EWS serializers and readers without an authenticated account."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from exchangelib import EWSTimeZone
from exchangelib.version import Build, Version
from lxml import etree

from gucmail.ews.adapter import EWSGateway
from gucmail.ews.mapping import _recurrence, map_calendar_event
from gucmail.services.recurrence import normalize_recurrence_mapping

PATTERNS = [
    {"type": "daily", "interval": 3},
    {
        "type": "weekly",
        "weekdays": ["monday", "wednesday"],
        "interval": 2,
        "first_day_of_week": "sunday",
    },
    {"type": "absolute_monthly", "day_of_month": 31, "interval": 2},
    {"type": "relative_monthly", "weekday": "weekday", "week_index": "last", "interval": 3},
    {"type": "absolute_yearly", "month": 2, "day_of_month": 29},
    {"type": "relative_yearly", "month": 11, "weekday": "thursday", "week_index": "fourth"},
]
BOUNDARIES = [
    {"type": "numbered", "start": "2026-09-09", "occurrences": 3},
    {"type": "end_date", "start": "2026-09-09", "end": "2026-12-31"},
    {"type": "no_end", "start": "2026-09-09"},
]


@pytest.mark.parametrize("target", ["calendar", "task"])
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_recurrence_xml_preserves_pattern_and_local_boundary_dates(target, pattern, boundary):
    canonical = normalize_recurrence_mapping(
        {"target": target, "pattern": pattern, "boundary": boundary}, task=target == "task"
    )
    value = object.__new__(EWSGateway)._ews_recurrence(
        canonical, target=target, start=date(2026, 9, 9)
    )
    version = Version(build=Build(15, 1, 2375, 7))
    value.clean(version=version)
    wire = value.to_xml(version=version)
    # EWS responses use UTC instants for calendar boundaries and date+zone for tasks.
    # Request date-only XML is not itself a server response.
    for node in wire.iter():
        if etree.QName(node).localname in {"StartDate", "EndDate"}:
            if target == "calendar":
                local = datetime.combine(
                    date.fromisoformat(node.text), time.min, tzinfo=ZoneInfo("Africa/Cairo")
                )
                node.text = local.astimezone(UTC).isoformat().replace("+00:00", "Z")
            else:
                node.text += "Z"
    parsed = type(value).from_xml(etree.fromstring(etree.tostring(wire)), account=None)
    assert _recurrence(parsed, task=target == "task", timezone="Africa/Cairo") == canonical
    if target == "calendar":
        item = SimpleNamespace(
            id="synthetic-event",
            start=datetime(2026, 9, 9, 6, tzinfo=UTC),
            end=datetime(2026, 9, 9, 7, tzinfo=UTC),
            _start_timezone=EWSTimeZone("Africa/Cairo"),
            recurrence=parsed,
        )
        mapped = map_calendar_event(item)
        assert mapped.timezone == "Africa/Cairo"
        assert mapped.recurrence == canonical
