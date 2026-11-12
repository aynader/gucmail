from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button

from gucmail.state import StateStore
from gucmail.tui.activity_screen import ActivityScreen, ActivityUndoPlanView
from gucmail.tui.area_backends import ActivityAreaBackend, StorageAreaBackend
from gucmail.tui.area_common import ConfirmationScreen, PublicOperationError
from gucmail.tui.palette_routes import RouteKind, catalog_route
from gucmail.tui.storage_screen import CleanupPlanScreen, StorageScreen


def _duplicate_dataset() -> tuple[dict[str, object], ...]:
    digest = "a" * 64
    return (
        {
            "reference": "message-a",
            "subject": "Synthetic duplicate",
            "sender": "sender@example.test",
            "folder_path": "Inbox",
            "size_bytes": 2_048,
            "content_sha256": digest,
        },
        {
            "reference": "message-b",
            "subject": "Synthetic duplicate",
            "sender": "sender@example.test",
            "folder_path": "Archive/Synthetic",
            "size_bytes": 2_048,
            "content_sha256": digest,
        },
        {
            "reference": "message-c",
            "subject": "Independent message",
            "sender": "other@example.test",
            "folder_path": "Inbox",
            "size_bytes": 1_024,
            "content_sha256": "b" * 64,
        },
    )


def _storage_backend(
    tmp_path: Path,
    moved: list[str],
    *,
    state: StateStore,
) -> StorageAreaBackend:
    return StorageAreaBackend(
        quota_factory=None,
        message_loader=lambda _analysis, limit: _duplicate_dataset()[:limit],
        state=state,
        cleanup_executor=lambda candidate: moved.append(candidate.reference) or candidate.reference,
        local_paths={},
        export_directory=tmp_path / "exports",
        online=True,
        maximum_scan_items=20,
    )


def test_duplicate_cleanup_execution_and_durable_undo_journal(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db", account_key="synthetic-account")
    moved: list[str] = []
    storage = _storage_backend(tmp_path, moved, state=state)
    cancel = Event()

    def progress(_value: object) -> None:
        return None

    analysis = storage.analyze_storage(
        "duplicates",
        limit=20,
        cancel=cancel,
        progress=progress,
    )
    assert analysis.analysis_id == "duplicates"
    assert [row.row_id for row in analysis.rows if row.cleanup_candidate] == ["message-b"]
    assert analysis.rows[0].source == "complete-content hash"

    plan = storage.create_cleanup_plan(
        "duplicates",
        ("message-b",),
        cancel=cancel,
        progress=progress,
    )
    assert plan.executable
    assert "confirmation_token=" not in repr(plan)
    with pytest.raises(PublicOperationError, match="explicit confirmation"):
        storage.execute_cleanup_plan(
            plan,
            confirmed=False,
            cancel=cancel,
            progress=progress,
        )
    result = storage.execute_cleanup_plan(
        plan,
        confirmed=True,
        cancel=cancel,
        progress=progress,
    )
    assert moved == ["message-b"]
    assert result.succeeded_count == 1
    assert result.undoable_count == 1

    restored: list[tuple[str, str]] = []
    activity = ActivityAreaBackend(
        state,
        export_directory=tmp_path / "activity-exports",
        cleanup_restorer=lambda reference, folder: restored.append((reference, folder)),
    )
    undo = activity.plan_cleanup_undo(None, cancel=cancel, progress=progress)
    assert isinstance(undo, ActivityUndoPlanView)
    assert undo.entries[0].original_folder == "Archive/Synthetic"
    assert undo.confirmation_token not in repr(undo)
    with pytest.raises(PublicOperationError, match="explicit confirmation"):
        activity.execute_cleanup_undo(
            undo,
            confirmed=False,
            cancel=cancel,
            progress=progress,
        )
    undone = activity.execute_cleanup_undo(
        undo,
        confirmed=True,
        cancel=cancel,
        progress=progress,
    )
    assert undone.restored_count == 1
    assert restored == [("message-b", "Archive/Synthetic")]
    assert state.list_ui_preferences(scope="cleanup-undo") == ()


async def _wait_for(
    pilot: object,
    condition: Callable[[], bool],
    *,
    attempts: int = 120,
) -> None:
    for _ in range(attempts):
        if condition():
            return
        await pilot.pause(0.03)  # type: ignore[attr-defined]
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_cleanup_screen_runs_duplicate_analysis_preview_and_confirmation(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.db", account_key="synthetic-account")
    moved: list[str] = []
    backend = _storage_backend(tmp_path, moved, state=state)
    screen = StorageScreen(backend, analysis_limit=20)
    app = App[None]()

    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._snapshot is not None)
        screen.action_analyze("duplicates")
        await _wait_for(
            pilot,
            lambda: screen._analysis is not None and screen._analysis.analysis_id == "duplicates",
        )
        assert screen.query_one("#storage-analysis-select").value == "duplicates"  # type: ignore[attr-defined]
        screen.action_cleanup_plan()
        await _wait_for(pilot, lambda: isinstance(app.screen, CleanupPlanScreen))
        await pilot.pause()
        execute = app.screen.query_one("#cleanup-plan-execute", Button)
        assert not execute.disabled
        execute.focus()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(
            pilot,
            lambda: (
                moved == ["message-b"] and bool(state.list_ui_preferences(scope="cleanup-undo"))
            ),
        )

    restored: list[tuple[str, str]] = []
    activity_backend = ActivityAreaBackend(
        state,
        export_directory=tmp_path / "activity-exports",
        cleanup_restorer=lambda reference, folder: restored.append((reference, folder)),
    )
    activity_screen = ActivityScreen(activity_backend)
    activity_app = App[None]()
    async with activity_app.run_test(size=(120, 35)) as pilot:
        await activity_app.push_screen(activity_screen)
        await _wait_for(
            pilot,
            lambda: any(event.undoable for event in activity_screen._events),
        )
        undo_event = next(event for event in activity_screen._events if event.undoable)
        activity_screen._select_event(undo_event.event_id)
        assert await pilot.click("#activity-undo-cleanup")
        await _wait_for(
            pilot,
            lambda: isinstance(activity_app.screen, ConfirmationScreen),
        )
        await pilot.press("y")
        await _wait_for(
            pilot,
            lambda: restored == [("message-b", "Archive/Synthetic")],
        )

    duplicate_route = catalog_route("cleanup.find_duplicate_messages")
    execute_route = catalog_route("cleanup.execute_cleanup_plan")
    undo_route = catalog_route("cleanup.undo_recent_move_delete_when_possible")
    assert duplicate_route.kind is RouteKind.SCREEN_ACTION
    assert duplicate_route.filter_values()["analysis"] == "duplicates"
    assert execute_route.kind is RouteKind.SCREEN_ACTION
    assert undo_route.kind is RouteKind.SCREEN_ACTION
