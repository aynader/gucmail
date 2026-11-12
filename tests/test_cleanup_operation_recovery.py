"""Cleanup must retain acknowledged/uncertain effects and stop before unsafe replay."""

import asyncio
from datetime import UTC, datetime
from threading import Event

import pytest
from qa.synthetic.mailbox import LocalMailbox
from textual.widgets import Button, DataTable, Static

from gucmail.errors import ConfigurationError, LocalStateError, NetworkError
from gucmail.models import Address, MessageSummary
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_backends import StorageAreaBackend
from gucmail.tui.area_common import ConfirmationScreen, PublicOperationError
from gucmail.tui.storage_screen import CleanupPlanScreen
from test_tui import context_for


def prepared(tmp_path, mode, monkeypatch):
    state = StateStore(tmp_path / "state.sqlite", account_key="synthetic-cleanup")
    gateway = LocalMailbox(tmp_path / "mail.sqlite")
    for reference in ("msg-a", "msg-b", "msg-c"):
        gateway.insert_many(
            "mail",
            [
                MessageSummary(
                    reference,
                    "Synthetic duplicate",
                    Address("course@example.test"),
                    datetime.now(UTC),
                    size=2048,
                    folder_path="/Archive",
                )
            ],
        )
    rows = tuple(
        {
            "reference": ref,
            "subject": "Synthetic duplicate",
            "folder_path": "/Archive",
            "sender": "course@example.test",
            "size_bytes": 2048,
            "content_sha256": "a" * 64,
        }
        for ref in ("msg-a", "msg-b", "msg-c")
    )

    def execute(candidate):
        if mode == "rejected":
            raise ConfigurationError("Synthetic definite rejection before move")
        result = gateway.delete_message(candidate.reference, permanent=False)
        if mode == "lost-ack":
            raise NetworkError("Synthetic acknowledgment lost after SQL commit")
        if mode == "object-result":
            return result
        return result.reference

    if mode == "undo-journal":
        original = state.put_ui_preference

        def write(*args, **kwargs):
            if kwargs.get("scope") == "cleanup-undo":
                raise LocalStateError("Synthetic undo write rejected")
            return original(*args, **kwargs)

        monkeypatch.setattr(state, "put_ui_preference", write)
    elif mode == "operation-ack":
        original_target = state.record_message_target

        def record(operation_id, ordinal, status, **kwargs):
            if status == "completed":
                raise LocalStateError("Synthetic outcome write rejected")
            return original_target(operation_id, ordinal, status, **kwargs)

        monkeypatch.setattr(state, "record_message_target", record)
    backend = StorageAreaBackend(
        quota_factory=None,
        message_loader=lambda _analysis, limit: rows[:limit],
        state=state,
        cleanup_executor=execute,
        local_paths={},
        export_directory=tmp_path / "exports",
        online=True,
        maximum_scan_items=20,
    )
    cancel, progress = Event(), lambda _value: None
    backend.analyze_storage("duplicates", limit=20, cancel=cancel, progress=progress)
    plan = backend.create_cleanup_plan("duplicates", ("msg-b",), cancel=cancel, progress=progress)
    return state, gateway, backend, plan, cancel, progress


@pytest.mark.parametrize(
    "mode,counts,stored_status",
    [
        ("accepted", (1, 0, 0, 0, 1), "completed"),
        ("rejected", (0, 1, 0, 0, 0), "failed"),
        ("lost-ack", (0, 0, 1, 0, 0), "uncertain"),
        ("undo-journal", (1, 0, 0, 1, 0), "completed"),
        ("operation-ack", (1, 0, 0, 1, 0), "uncertain"),
    ],
)
def test_cleanup_outcomes_match_persisted_effects_and_restart_journal(
    tmp_path, monkeypatch, mode, counts, stored_status
):
    state, gateway, backend, plan, cancel, progress = prepared(tmp_path, mode, monkeypatch)
    result = backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert (
        result.succeeded_count,
        result.failed_count,
        getattr(result, "uncertain_count", 0),
        getattr(result, "recovery_failed_count", 0),
        result.undoable_count,
    ) == counts
    expected_folder = "/Archive" if mode == "rejected" else "/Deleted Items"
    assert LocalMailbox(gateway.path).get("mail", "msg-b").folder_path == expected_folder
    restarted = StateStore(state.path, account_key=state.account_key)
    operations = restarted.list_message_operations()
    assert len(operations) == 1
    operation = restarted.get_message_operation(operations[0]["operation_id"])
    assert operation["outcomes"][0]["status"] == stored_status
    assert tuple(getattr(result, "operation_ids", ())) == (operation["operation_id"],)


def test_cleanup_consumes_reviewed_plan_after_a_dispatched_attempt(tmp_path, monkeypatch):
    _state, gateway, backend, plan, cancel, progress = prepared(tmp_path, "lost-ack", monkeypatch)
    backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert gateway.counts["delete_message"] == 1
    with pytest.raises(PublicOperationError, match=r"stale|used|review"):
        backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert gateway.counts["delete_message"] == 1


def test_failed_intent_write_never_calls_provider(tmp_path, monkeypatch):
    state, gateway, backend, plan, cancel, progress = prepared(tmp_path, "accepted", monkeypatch)

    def rejected(*_args, **_kwargs):
        raise LocalStateError("Synthetic intent unavailable")

    monkeypatch.setattr(state, "begin_message_operation", rejected)
    result = backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert result.succeeded_count == 0 and result.failed_count == 1
    assert gateway.counts["delete_message"] == 0
    assert gateway.get("mail", "msg-b").folder_path == "/Archive"


def test_cleanup_stops_after_uncertainty_and_leaves_later_target_untouched(tmp_path, monkeypatch):
    state, gateway, backend, _plan, cancel, progress = prepared(tmp_path, "lost-ack", monkeypatch)
    plan = backend.create_cleanup_plan(
        "duplicates", ("msg-b", "msg-c"), cancel=cancel, progress=progress
    )
    result = backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert result.uncertain_count == 1 and result.not_started_count == 1
    assert result.uncertain_references == ("msg-b",)
    assert gateway.get("mail", "msg-b").folder_path == "/Deleted Items"
    assert gateway.get("mail", "msg-c").folder_path == "/Archive"
    assert gateway.counts["delete_message"] == 1
    assert len(StateStore(state.path, account_key=state.account_key).list_message_operations()) == 1


def test_cleanup_normalizes_a_domain_result_reference_for_undo(tmp_path, monkeypatch):
    state, _gateway, backend, plan, cancel, progress = prepared(
        tmp_path, "object-result", monkeypatch
    )
    result = backend.execute_cleanup_plan(plan, confirmed=True, cancel=cancel, progress=progress)
    assert result.succeeded_count == result.undoable_count == 1
    from gucmail.tui.area_backends import ActivityAreaBackend

    activity = ActivityAreaBackend(state, export_directory=tmp_path / "activity")
    undo = activity.plan_cleanup_undo(None, cancel=cancel, progress=progress)
    assert undo.entries[0].current_reference == "msg-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
async def test_cleanup_recovery_details_scroll_and_survive_cursor_changes(
    tmp_path, monkeypatch, size
):
    _state, _gateway, backend, _plan, cancel, progress = prepared(tmp_path, "lost-ack", monkeypatch)
    plan = backend.create_cleanup_plan(
        "duplicates", ("msg-b", "msg-c"), cancel=cancel, progress=progress
    )
    app = GUCmailApp(context_for(tmp_path / "profile"), offline=True)
    screen = CleanupPlanScreen(plan, backend, offline=False)

    async def until(pilot, predicate):
        async with asyncio.timeout(15):
            while not predicate():
                await asyncio.sleep(0.005)
        await pilot.pause()

    async with app.run_test(size=size) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        execute = screen.query_one("#cleanup-plan-execute", Button)
        assert execute.region.bottom <= size[1]
        assert screen.query_one(".area-close", Button).region.bottom <= size[1]
        execute.focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await until(pilot, lambda: app.screen is screen and not screen._operation_active)
        detail = screen.query_one("#cleanup-plan-detail", Static)
        before = str(detail.content)
        assert "Uncertain: 1" in before and "Do not repeat" in before
        assert "Nothing was moved" not in str(
            screen.query_one("#cleanup-plan-summary", Static).content
        )
        table = screen.query_one("#cleanup-plan-table", DataTable)
        table.focus()
        await pilot.press("down", "up")
        assert str(detail.content) == before
        scroll = screen.query_one("#cleanup-plan-detail-scroll")
        scroll.focus()
        await pilot.press("end")
        await until(
            pilot,
            lambda: scroll.max_scroll_y > 0 and abs(scroll.scroll_y - scroll.max_scroll_y) < 0.5,
        )
        assert scroll.region.bottom <= size[1]
