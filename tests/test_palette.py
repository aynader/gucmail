from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gucmail.palette import PaletteKind, interpret_palette


def test_known_search_phrase() -> None:
    result = interpret_palette("Open unread messages with PDFs")
    assert result.command == ("search", "unread:true attachment:pdf")
    assert not result.mutation


def test_scheduling_phrase_resolves_and_confirms() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    result = interpret_palette(
        "Schedule this draft for tomorrow 9 AM", timezone="Africa/Cairo", now=now
    )
    assert result.resolved_time is not None
    assert result.resolved_time.local.isoformat() == "2026-08-06T09:00:00+03:00"
    assert result.mutation
    assert result.confirmation_required


def test_unknown_phrase_falls_back_to_search_not_mutation() -> None:
    result = interpret_palette("delete all mail from somebody maybe")
    assert result.kind is PaletteKind.SEARCH
    assert result.command[0] == "search"
    assert not result.mutation
    assert not result.destructive


def test_legacy_projection_never_drops_structured_search_semantics() -> None:
    result = interpret_palette("oldest unread messages not from Student Affairs")

    assert result.invocation is not None
    assert result.invocation.command_id == "search.mail"
    assert result.command == ()
    assert not result.cli_available
    assert "exclusions" in result.cli_reason
    assert "ranking" in result.cli_reason
