from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button, DataTable, Input, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError, NetworkError
from gucmail.models import Address, MessageSummary
from gucmail.services.unanswered import UnansweredService
from gucmail.tui.unanswered_screen import UnansweredScreen


def message(reference, hour, *, sender="other@example.test", conversation="thread"):
    timestamp = (
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hour) if hour is not None else None
    )
    return MessageSummary(
        reference,
        "Synthetic request",
        Address(sender) if sender else None,
        timestamp,
        sent_at=timestamp,
        conversation_id=conversation,
    )


class Gateway:
    def __init__(self, inbox=(), sent=()):
        self.folders = {"inbox": inbox, "sent": sent}
        self.calls = []
        self.cancel = None

    def list_messages(self, *, folder, limit, offset, order_by):
        assert order_by == "-datetime_sent"
        assert 1 <= limit <= 200
        self.calls.append((folder, limit, offset))
        if self.cancel:
            self.cancel.set()
        values = sorted(
            self.folders[folder],
            key=lambda item: item.sent_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return values[offset : offset + limit]


def analyze(gateway, **kwargs):
    return UnansweredService(gateway).analyze(self_addresses=("me@example.test",), **kwargs)


def test_conversations_compare_aliases_across_inbox_and_sent_without_guessing_subjects():
    gateway = Gateway(
        (message("old", 1), message("latest", 2), message("other-thread", 2, conversation="other")),
        (message("reply", 3, sender="ALIAS@example.test"),),
    )
    result = UnansweredService(gateway).analyze(
        self_addresses=("me@example.test", "alias@example.test")
    )
    assert result.later_self_message == 1
    assert [value.message.reference for value in result.findings] == ["other-thread"]
    assert result.no_reply_found == 1 and not result.uncertain
    assert all(part.complete for part in result.coverage)
    assert len(gateway.calls) == 2


@pytest.mark.parametrize("case", ["unknown_sender", "unknown_time", "tie", "unlinked_self"])
def test_missing_or_ambiguous_evidence_is_uncertain(case):
    incoming = message("incoming", 2)
    outgoing = {
        "unknown_sender": message("outgoing", 3, sender=None),
        "unknown_time": message("outgoing", None, sender="me@example.test"),
        "tie": message("outgoing", 2, sender="me@example.test"),
        "unlinked_self": message("outgoing", 3, sender="me@example.test", conversation=None),
    }[case]
    result = analyze(Gateway((incoming,), (outgoing,)))
    assert result.uncertain == 1 and result.no_reply_found == 0
    assert result.findings[0].status == "uncertain"


def test_truncated_window_cannot_claim_an_old_incoming_message_is_unanswered():
    gateway = Gateway(
        (message("older", 1, conversation="older"), message("newer", 10, conversation="newer")),
        tuple(
            message(
                f"sent-{number}", number, sender="me@example.test", conversation=f"sent-{number}"
            )
            for number in range(3, 7)
        ),
    )
    result = analyze(gateway, limit=2)
    assert result.no_reply_found == 1 and result.uncertain == 1
    assert result.coverage[0].complete and not result.coverage[1].complete
    findings = {finding.message.reference: finding.status for finding in result.findings}
    assert findings == {"newer": "no_reply_found", "older": "uncertain"}


@pytest.mark.parametrize("count,complete", [(200, True), (201, False), (1000, False)])
def test_lookahead_and_paging_respect_per_folder_bounds(count, complete):
    gateway = Gateway(
        tuple(message(str(number), number, conversation=str(number)) for number in range(count))
    )
    result = analyze(gateway, limit=200)
    assert gateway.calls == [("inbox", 200, 0), ("inbox", 1, 200), ("sent", 200, 0)]
    assert result.coverage[0].complete is complete
    assert result.scanned == 200
    assert result.coverage[0].fetched == min(count, 201)


def test_unknown_conversation_id_is_reported_and_timezones_compare_in_utc():
    incoming = message("incoming", 1)
    own = replace(
        message("own", 2, sender="me@example.test"),
        sent_at=datetime(2026, 1, 1, 4, tzinfo=timezone(timedelta(hours=2))),
    )
    result = analyze(Gateway((incoming, message("unknown-thread", 3, conversation=None)), (own,)))
    assert result.later_self_message == 1 and result.missing_conversation_id == 1
    assert result.findings == ()


def test_cancel_validation_and_network_failure_do_not_return_false_empty_results():
    gateway = Gateway((message("incoming", 1),))
    with pytest.raises(ConfigurationError):
        UnansweredService(gateway).analyze(self_addresses=())
    assert not gateway.calls
    cancel = Event()
    gateway.cancel = cancel
    with pytest.raises(InterruptedError):
        analyze(gateway, cancelled=cancel.is_set)
    assert len(gateway.calls) == 1

    class Failure(Gateway):
        def list_messages(self, **kwargs):
            if kwargs["folder"] == "sent":
                raise NetworkError("Synthetic Sent failure")
            return super().list_messages(**kwargs)

    with pytest.raises(NetworkError, match="Sent failure"):
        analyze(Failure((message("incoming", 1),)))


def test_cli_outputs_coverage_and_validates_identity_before_gateway(tmp_path, monkeypatch):
    calls = []
    gateway = Gateway((message("incoming", 1),))

    def connect(_ctx):
        calls.append(True)
        return gateway

    monkeypatch.setattr(cli, "_gateway", connect)
    env = {
        f"GUCMAIL_{name}_DIR": str(tmp_path / name) for name in ("CONFIG", "DATA", "CACHE", "LOG")
    }
    runner = CliRunner()
    missing = runner.invoke(cli.app, ["--json", "insights", "unanswered"], env=env)
    assert missing.exit_code != 0 and not calls
    result = runner.invoke(
        cli.app, ["--json", "insights", "unanswered", "--self-address", "me@example.test"], env=env
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["no_reply_found"] == 1 and data["coverage"][1]["folder"] == "sent"
    assert data["notes"] and data["findings"][0]["message"]["reference"] == "incoming"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_screen_analyzes_scope_then_opens_message_and_keeps_actions_visible(size):
    gateway = Gateway((message("incoming", 1),))
    opened = []
    screen = UnansweredScreen(
        UnansweredService(gateway), address="me@example.test", open_message=opened.append
    )
    app = App()
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        assert not gateway.calls  # Showing the review form does not authenticate/read mail.
        button = screen.query_one("#response-analyze", Button)
        # Widget regions use screen coordinates; size excludes this screen's padding.
        assert button.region.y >= 0 and button.region.bottom <= screen.region.bottom
        assert await pilot.click("#response-analyze")
        for _ in range(100):
            await pilot.pause(0.025)
            if screen._report and not screen._operation_active:
                break
        assert screen.query_one("#response-table", DataTable).row_count == 1
        assert "1 without a later self message" in str(
            screen.query_one("#area-state", Static).render()
        )
        screen.query_one("#response-addresses", Input).value = "alias@example.test"
        await pilot.pause()
        assert screen._report is None
        assert screen.query_one("#response-open", Button).disabled
        screen.action_refresh()
        for _ in range(100):
            await pilot.pause(0.025)
            if screen._report and not screen._operation_active:
                break
        screen.query_one("#response-table", DataTable).focus()
        await pilot.press("enter")
        assert opened == ["incoming"] and app.screen is not screen


@pytest.mark.asyncio
async def test_registered_palette_command_opens_review_without_a_scan(tmp_path):
    from gucmail.command_catalog import CORE_COMMANDS
    from gucmail.commands import CommandInvocation
    from gucmail.tui.app import GUCmailApp
    from test_tui import FakeGateway, context_for

    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        command = CORE_COMMANDS.get("insights.unanswered")
        assert command.cli_path == ("insights", "unanswered")
        app._palette_handlers()["insights.unanswered"](
            CommandInvocation(command.command_id), app._palette_context()
        )
        await pilot.pause()
        assert isinstance(app.screen, UnansweredScreen)
        assert app.screen._report is None
        await pilot.press("escape")
        assert not isinstance(app.screen, UnansweredScreen)


@pytest.mark.asyncio
async def test_scope_edit_cancels_inflight_scan_and_discards_its_late_result():
    started, release = Event(), Event()

    class Delayed(Gateway):
        def list_messages(self, **kwargs):
            started.set()
            release.wait(3)
            return super().list_messages(**kwargs)

    gateway = Delayed((message("incoming", 1),))
    screen = UnansweredScreen(
        UnansweredService(gateway), address="me@example.test", open_message=lambda _: None
    )
    app = App()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(screen)
            screen.action_refresh()
            for _ in range(100):
                await pilot.pause(0.01)
                if started.is_set():
                    break
            assert started.is_set()
            screen.query_one("#response-sent", Input).value = "Another folder"
            await pilot.pause()
            assert not screen._operation_active
            release.set()
            await pilot.pause(0.1)
            assert screen._report is None
            assert screen.query_one("#response-table", DataTable).row_count == 0
            assert len(gateway.calls) == 1
    finally:
        release.set()
