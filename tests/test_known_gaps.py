"""Regression tests for the gaps listed in the 1.1 status page, closed in 1.2."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from gucmail.cli import app
from gucmail.models import CalendarEvent
from gucmail.services.calendar import CalendarPatch, CalendarService
from gucmail.services.search import fuzzy_rerank


def _env(tmp_path) -> dict[str, str]:
    return {
        "GUCMAIL_CONFIG_DIR": str(tmp_path / "config"),
        "GUCMAIL_DATA_DIR": str(tmp_path / "data"),
        "GUCMAIL_CACHE_DIR": str(tmp_path / "cache"),
        "GUCMAIL_LOG_DIR": str(tmp_path / "logs"),
        "NO_COLOR": "1",
    }


def _cli(tmp_path, *args: str):
    return CliRunner().invoke(app, ["--non-interactive", *args], env=_env(tmp_path))


# --- saved searches -----------------------------------------------------------


def test_saved_search_refuses_an_empty_search(tmp_path):
    result = _cli(tmp_path, "saved-searches", "add", "exams", "   ")
    assert result.exit_code != 0
    assert "empty" in result.output


def test_saved_search_does_not_overwrite_without_replace(tmp_path):
    assert _cli(tmp_path, "saved-searches", "add", "exams", "subject:exam").exit_code == 0
    again = _cli(tmp_path, "saved-searches", "add", "exams", "subject:midterm")
    assert again.exit_code != 0
    assert "--replace" in again.output
    replaced = _cli(tmp_path, "saved-searches", "add", "exams", "subject:midterm", "--replace")
    assert replaced.exit_code == 0
    listed = _cli(tmp_path, "--json", "saved-searches", "list")
    assert "midterm" in listed.output and "subject:exam\"" not in listed.output


# --- calendar -----------------------------------------------------------------

START = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def _event(reference: str = "evt-1", **changes: object) -> CalendarEvent:
    event = CalendarEvent(
        reference=reference,
        subject="Thesis meeting",
        start=START,
        end=START + timedelta(hours=1),
        timezone="Africa/Cairo",
    )
    return replace(event, **changes)


class _Calendar:
    def __init__(self, *events: CalendarEvent) -> None:
        self.events = list(events)
        self.created: list[object] = []

    def list_events(self, **_kwargs: object) -> tuple[CalendarEvent, ...]:
        return tuple(self.events)

    def get_event(self, reference: str) -> CalendarEvent:
        return next(event for event in self.events if event.reference == reference)

    def create_event(self, plan: object) -> CalendarEvent:
        self.created.append(plan)
        return plan.event  # type: ignore[attr-defined]


def test_moving_only_the_start_keeps_the_event_length():
    service = CalendarService(_Calendar(_event()), display_timezone="Africa/Cairo")
    later = START + timedelta(hours=3)
    plan = service.plan_update("evt-1", CalendarPatch(start=later))
    assert plan.changes["start"] == later
    assert plan.changes["end"] == later + timedelta(hours=1)


def test_importing_the_same_ics_twice_adds_nothing_the_second_time():
    gateway = _Calendar(_event(uid="uid-123@guc"))
    service = CalendarService(gateway, display_timezone="Africa/Cairo")
    ics = service.export_ics(_event(uid="uid-123@guc"))
    plan = service.import_ics(ics)
    assert plan.events == ()
    assert len(plan.already_present) == 1
    assert "already in your calendar" in plan.warnings[0]


def test_ics_events_without_uid_match_by_subject_and_time():
    gateway = _Calendar(_event())
    service = CalendarService(gateway, display_timezone="Africa/Cairo")
    ics = service.export_ics([_event(), _event("evt-2", subject="Office hours")])
    plan = service.import_ics(ics)
    assert [event.subject for event in plan.events] == ["Office hours"]


# --- fuzzy search -------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "subject"),
    [("ahmad", "Re: Ahmed Hassan"), ("exma", "Final exam"), ("reprot", "Annual report")],
)
def test_one_letter_typos_are_found(typed, subject):
    item = {"subject": subject}
    ranked = fuzzy_rerank([item], typed, threshold=82)
    assert [result.item for result in ranked] == [item]


def test_unrelated_short_words_are_not_matched():
    assert fuzzy_rerank([{"subject": "Lunch"}], "exam", threshold=82) == []


# --- palette ------------------------------------------------------------------


def test_palette_shows_each_command_name_once():
    from gucmail.palette import PaletteEngine

    names = [
        (s.display_name or s.command.name).casefold()
        for s in PaletteEngine().suggest("drafts", limit=50)
    ]
    assert len(names) == len(set(names))
    for query in ("search calendar", "reply all", "schedule draft"):
        names = [s.command.name.casefold() for s in PaletteEngine().suggest(query, limit=50)]
        assert len(names) == len(set(names))


# --- readable output ----------------------------------------------------------


@pytest.mark.parametrize("command", [["doctor"], ["cache", "status"], ["quota", "local"]])
def test_status_commands_print_no_raw_json_or_byte_counts(tmp_path, command):
    result = _cli(tmp_path, *command)
    assert result.exit_code == 0, result.output
    assert "{" not in result.output and '"' not in result.output
    assert "True" not in result.output and "False" not in result.output
    machine = _cli(tmp_path, "--json", *command)
    assert json.loads(machine.output)  # scripts still get every detail


# --- recurring messages -------------------------------------------------------


def test_recurring_weekdays_and_end_date_are_parsed():
    from gucmail.cli.outbox import _recurring_until, _weekday_numbers

    assert _weekday_numbers("Mon, wed,FRIDAY") == (0, 2, 4)
    assert _weekday_numbers(None) == ()
    end = _recurring_until("2027-06-30", "Africa/Cairo")
    assert end is not None and (end.year, end.month, end.day, end.hour) == (2027, 6, 30, 23)


# --- keyboard -----------------------------------------------------------------


def test_bulk_mark_read_and_export_have_keys_in_the_mail_screen():
    from gucmail.tui.app import GUCmailApp

    keys = {binding.action: binding.key for binding in GUCmailApp.BINDINGS}  # type: ignore[union-attr]
    assert keys["mark_selected_read"] == "m"
    assert keys["export_selected"] == "e"


def test_shortcuts_saved_before_the_split_still_load():
    from gucmail.services.keyboard_bindings import normalize_binding_overrides

    old = {"gucmail.tui.app.HelpScreen:tutorials:alt+t:0": "alt+y"}
    assert normalize_binding_overrides(old) == {
        "gucmail.tui.help_screen.HelpScreen:tutorials:alt+t:0": "alt+y"
    }
