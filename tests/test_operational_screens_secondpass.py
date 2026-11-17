from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, Static

from gucmail.tui.activity_screen import (
    ActivityClearPlanView,
    ActivityClearResultView,
    ActivityEventView,
    ActivityScreen,
    ActivitySnapshotView,
    WatcherStatusView,
)
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.backup_screen import (
    BackupBrowseScreen,
    BackupCreateRequest,
    BackupEstimateView,
    BackupExportPathScreen,
    BackupItemView,
    BackupPrunePlanView,
    BackupPruneResultView,
    BackupRecordView,
    BackupScreen,
    BackupVerificationView,
    RestorePlanEntryView,
    RestorePlanScreen,
    RestorePlanView,
    RestoreResultView,
)
from waiting import click_button, rounds


async def _wait_for(
    pilot: object,
    condition: Callable[[], bool],
    *,
    attempts: int = 100,
) -> None:
    for _ in rounds(attempts):
        if condition():
            return
        await pilot.pause(0.03)  # type: ignore[attr-defined]
    raise AssertionError("condition was not reached")


class BackupBackend:
    def __init__(self) -> None:
        self.estimate_requests: list[BackupCreateRequest] = []
        self.create_requests: list[BackupCreateRequest] = []
        self.resumed: list[str] = []
        self.verified: list[str] = []
        self.browsed: list[str] = []
        self.restore_plans: list[tuple[str, tuple[str, ...]]] = []
        self.restored: list[str] = []
        self.exports: list[tuple[str, str]] = []
        self.prune_plans: list[int] = []
        self.pruned: list[tuple[str, ...]] = []
        self.opened_folders = 0

    @staticmethod
    def record() -> BackupRecordView:
        return BackupRecordView(
            backup_id="backup-complete",
            kind="full",
            state="complete",
            created_at="2026-08-22 10:15",
            completed_at="2026-08-22 10:16",
            item_count=428,
            stored_bytes=37_842_944,
            destination="C:/synthetic/backups",
            encrypted=False,
            verification_state="verified",
        )

    def list_backups(self, *, cancel: Event, progress: object):
        return (self.record(),)

    def estimate_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: object,
    ) -> BackupEstimateView:
        self.estimate_requests.append(request)
        return BackupEstimateView(428, 84, 46_137_344, 3, "Bounded provider estimate.")

    def create_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None,
        cancel: Event,
        progress: object,
    ) -> BackupRecordView:
        self.create_requests.append(request)
        return self.record()

    def resume_backup(self, backup_id: str, **_kwargs: object) -> BackupRecordView:
        self.resumed.append(backup_id)
        return self.record()

    def verify_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None,
        cancel: Event,
        progress: object,
    ) -> BackupVerificationView:
        self.verified.append(backup_id)
        return BackupVerificationView(backup_id, True, True, 428, 37_842_944)

    def browse_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None,
        cancel: Event,
        progress: object,
    ) -> tuple[BackupItemView, ...]:
        self.browsed.append(backup_id)
        return (
            BackupItemView(
                "msg-1",
                "message",
                "Inbox",
                "Synthetic project update",
                "2026-08-22",
                18_240,
                2,
                "Safe local fixture.",
            ),
        )

    def create_restore_plan(
        self,
        backup_id: str,
        item_references: tuple[str, ...],
        *,
        passphrase: str | bytes | None,
        cancel: Event,
        progress: object,
    ) -> RestorePlanView:
        self.restore_plans.append((backup_id, item_references))
        entry = RestorePlanEntryView("msg-1", "message", "Restored/Inbox", "create")
        return RestorePlanView(backup_id, (entry,), 1, 0, 0, confirmation_token="token")

    def execute_restore(
        self,
        plan: RestorePlanView,
        *,
        confirmed: bool,
        passphrase: str | bytes | None,
        cancel: Event,
        progress: object,
    ) -> RestoreResultView:
        self.restored.append(plan.backup_id)
        return RestoreResultView(plan.backup_id, 1, 1, 0, 0, 0, False, True)

    def export_backup(
        self,
        backup_id: str,
        destination: str,
        **_kwargs: object,
    ) -> str:
        self.exports.append((backup_id, destination))
        return destination

    def plan_prune(
        self,
        keep_last: int,
        **_kwargs: object,
    ) -> BackupPrunePlanView:
        self.prune_plans.append(keep_last)
        return BackupPrunePlanView(
            ("backup-old",),
            ("backup-complete",),
            (),
            4_096,
            keep_last,
            "prune-token",
        )

    def execute_prune(
        self,
        plan: BackupPrunePlanView,
        **_kwargs: object,
    ) -> BackupPruneResultView:
        self.pruned.append(plan.candidates)
        return BackupPruneResultView(plan.candidates, plan.potential_bytes, "Recoverable trash.")

    def open_backup_folder(self, **_kwargs: object) -> str:
        self.opened_folders += 1
        return "C:/synthetic/backups"


class ActivityBackend:
    def __init__(self) -> None:
        self.retry_ids: list[str] = []
        self.tests = 0
        self.test_providers: list[str] = []
        self.exports = 0
        self.watcher_changes: list[bool] = []
        self.watcher_controls: list[str] = []
        self.clear_plans = 0
        self.cleared = 0

    @staticmethod
    def watcher(enabled: bool = True) -> WatcherStatusView:
        return WatcherStatusView(
            enabled,
            "running" if enabled else "stopped",
            "bounded polling",
            "bounded polling",
            "default",
            ("Inbox", "Archive/Projects"),
            "2026-08-22 10:25",
            None,
            "2026-08-22 10:30",
            4120 if enabled else None,
            1,
        )

    @staticmethod
    def events() -> tuple[ActivityEventView, ...]:
        return (
            ActivityEventView(
                "evt-sync",
                "2026-08-22 10:25:07",
                "watcher",
                "info",
                "Mailbox synchronization completed",
                "Bounded Inbox sync checked 12 changes.",
                "corr-sync",
            ),
            ActivityEventView(
                "evt-hook",
                "2026-08-22 10:20:14",
                "webhook",
                "warning",
                "Discord delivery is waiting to retry",
                "Endpoint and secret values are redacted.",
                "corr-hook",
                True,
                "discord",
                2,
                "2026-08-22 10:30",
            ),
        )

    def load_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        limit: int,
        cancel: Event,
        progress: object,
    ) -> ActivitySnapshotView:
        events = self.events()
        if category:
            events = tuple(item for item in events if item.category == category)
        if text:
            needle = text.casefold()
            events = tuple(item for item in events if needle in item.summary.casefold())
        return ActivitySnapshotView(self.watcher(), events[:limit], "30 days", limit)

    def set_watcher_enabled(
        self, enabled: bool, *, cancel: Event, progress: object
    ) -> WatcherStatusView:
        self.watcher_changes.append(enabled)
        return self.watcher(enabled)

    def control_watcher(self, action: str, *, cancel: Event, progress: object) -> WatcherStatusView:
        self.watcher_controls.append(action)
        enabled = action not in {"stop"}
        state = {
            "pause": "pause requested",
            "resume": "resume requested",
            "sync": "synchronization requested",
        }.get(action, "running" if enabled else "stopping")
        return replace(self.watcher(enabled), state=state)

    def retry_delivery(
        self, event_id: str, *, cancel: Event, progress: object
    ) -> ActivityEventView:
        self.retry_ids.append(event_id)
        return self.events()[1]

    def test_notifications(
        self,
        *,
        provider: str = "console",
        cancel: Event,
        progress: object,
    ) -> ActivityEventView:
        assert provider in {"console", "desktop"}
        self.tests += 1
        self.test_providers.append(provider)
        return self.events()[0]

    def export_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        cancel: Event,
        progress: object,
    ) -> str:
        self.exports += 1
        return "C:/synthetic/activity.ndjson"

    def plan_clear_activity(self, **_kwargs: object) -> ActivityClearPlanView:
        self.clear_plans += 1
        return ActivityClearPlanView(
            2,
            ("watcher", "webhook"),
            "2026-08-22 10:20:14",
            "2026-08-22 10:25:07",
            ("watcher checkpoints and leases", "scheduled jobs and recurrence state"),
            "clear-token",
        )

    def execute_clear_activity(
        self,
        plan: ActivityClearPlanView,
        **_kwargs: object,
    ) -> ActivityClearResultView:
        self.cleared += plan.event_count
        return ActivityClearResultView(
            plan.event_count,
            plan.newest_at or "2026-08-22 10:25:07",
            plan.preserved_operational_records,
        )


@pytest.mark.asyncio
async def test_backup_screen_controls_execute_real_synthetic_workflow() -> None:
    backend = BackupBackend()
    screen = BackupScreen(
        backend,
        default_destination="C:/synthetic/backups",
        default_kind="incremental",
        default_encrypted=False,
        default_verify_after_create=False,
        retention_count=7,
    )
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._selected_id == "backup-complete")

        assert screen.query_one("#backup-folders").disabled
        assert screen.query_one("#backup-content-mail").disabled
        assert "backup-complete" in str(screen.query_one("#backup-parent-summary", Static).render())

        await click_button(pilot, screen.query_one("#backup-estimate", Button))
        await _wait_for(pilot, lambda: bool(backend.estimate_requests))
        request = backend.estimate_requests[-1]
        assert request.kind == "incremental"
        assert request.parent_backup_id == "backup-complete"
        assert request.folders == () and request.content_types == ()
        assert not request.verify_after_create
        assert (
            "keep 7"
            in str(screen.query_one("#backup-retention-summary", Static).render()).casefold()
        )
        await _wait_for(
            pilot,
            lambda: (
                "Estimated items: 428" in str(screen.query_one("#backup-detail", Static).render())
            ),
        )

        await click_button(pilot, pilot.app.screen.query_one("#backup-create", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: bool(backend.create_requests))

        await click_button(pilot, pilot.app.screen.query_one("#backup-verify", Button))
        await _wait_for(pilot, lambda: backend.verified == ["backup-complete"])
        screen.action_restore_plan()
        await _wait_for(
            pilot,
            lambda: backend.restore_plans == [("backup-complete", ())],
        )
        await _wait_for(
            pilot,
            lambda: isinstance(app.screen, RestorePlanScreen)
            and app.screen.is_mounted
            and bool(app.screen.query("#restore-plan-range"))
            and app.screen.view_state is ViewState.READY,
        )
        app.screen.dismiss(None)
        await pilot.pause()

        screen.action_restore_selected_items()
        await _wait_for(pilot, lambda: backend.browsed == ["backup-complete"])
        await _wait_for(pilot, lambda: isinstance(app.screen, BackupBrowseScreen))
        await pilot.press("space")
        plan_selected = pilot.app.screen.query_one("#backup-browse-plan-selected", Button)
        await click_button(pilot, plan_selected)
        await _wait_for(
            pilot,
            lambda: (
                backend.restore_plans == [("backup-complete", ()), ("backup-complete", ("msg-1",))]
            ),
        )
        await _wait_for(
            pilot,
            lambda: isinstance(app.screen, RestorePlanScreen)
            and app.screen.is_mounted
            and bool(app.screen.query("#restore-plan-range"))
            and app.screen.view_state is ViewState.READY,
        )
        await pilot.pause()
        app.screen.query_one("#restore-execute", Button).focus()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.restored == ["backup-complete"])


@pytest.mark.asyncio
async def test_backup_palette_configuration_and_cooperative_stop_are_truthful() -> None:
    backend = BackupBackend()
    screen = BackupScreen(
        backend,
        default_destination="C:/synthetic/backups",
        default_kind="full",
        default_folders=("Inbox/Projects",),
    )
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._selected_id == "backup-complete")
        request = screen._parse_request()
        assert request.kind == "full"
        assert request.folders == ("Inbox/Projects",)
        assert screen.query_one("#backup-advanced").has_class("visible")

        screen.configure_create(kind="incremental", folders=("Archive/2026",))
        request = screen._parse_request()
        assert request.kind == "incremental"
        assert request.folders == ()
        assert request.parent_backup_id == "backup-complete"

        screen.action_pause_backup()
        assert "No backup creation or resume operation" in str(
            screen.query_one("#area-state", Static).render()
        )

        screen._begin_operation(
            "Creating backup",
            lambda cancel, _progress: cancel.wait(2),
            lambda _result: None,
        )
        await _wait_for(pilot, lambda: screen._operation_active)
        screen.action_pause_backup()
        assert not screen._operation_active
        assert "checkpoint was retained" in str(screen.query_one("#area-state", Static).render())

        # Cancellation stops waiting; a queued/running worker must finish its
        # cooperative cancellation before another backup can start.
        await _wait_for(pilot, lambda: not screen.mutation_pending)

        screen._begin_operation(
            "Resuming backup",
            lambda cancel, _progress: cancel.wait(2),
            lambda _result: None,
        )
        await _wait_for(pilot, lambda: screen._operation_active)
        screen.action_cancel_backup()
        assert not screen._operation_active
        assert "Backup cancelled safely" in str(screen.query_one("#area-state", Static).render())
        await _wait_for(pilot, lambda: not screen.mutation_pending)

        incomplete = BackupRecordView(
            backup_id="backup-incomplete",
            kind="incremental",
            state="incomplete",
            created_at="2026-08-22 10:20",
            completed_at=None,
            item_count=25,
            stored_bytes=1024,
            destination="C:/synthetic/backups",
            encrypted=False,
        )
        screen._records_by_id[incomplete.backup_id] = incomplete
        screen._selected_id = incomplete.backup_id
        screen.action_resume()
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.resumed == ["backup-incomplete"])


@pytest.mark.asyncio
async def test_backup_repository_controls_export_prune_and_open_safely() -> None:
    backend = BackupBackend()
    screen = BackupScreen(
        backend,
        default_destination="C:/synthetic/backups",
        retention_count=7,
    )
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._selected_id == "backup-complete")

        await click_button(pilot, pilot.app.screen.query_one("#backup-export", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, BackupExportPathScreen))
        app.screen.query_one(
            "#backup-export-destination", Input
        ).value = "C:/synthetic/portable-backup.zip"
        await pilot.press("enter")
        await _wait_for(
            pilot,
            lambda: backend.exports == [("backup-complete", "C:/synthetic/portable-backup.zip")],
        )

        await click_button(pilot, pilot.app.screen.query_one("#backup-prune", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        detail = str(screen.query_one("#backup-detail", Static).render())
        assert "backup-old" in detail
        assert "backup-complete" in detail
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.pruned == [("backup-old",)])
        assert backend.prune_plans == [7]

        await click_button(pilot, pilot.app.screen.query_one("#backup-open-folder", Button))
        # The status line updates after the worker returns, a moment after the backend call.
        await _wait_for(
            pilot,
            lambda: backend.opened_folders == 1
            and "C:/synthetic/backups" in str(screen.query_one("#area-state", Static).render()),
        )


@pytest.mark.asyncio
async def test_complete_backup_can_be_verified_and_browsed_while_offline() -> None:
    backend = BackupBackend()
    screen = BackupScreen(
        backend,
        default_destination="C:/synthetic/backups",
        offline=True,
    )
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._selected_id == "backup-complete")
        assert screen.query_one("#backup-create", Button).disabled
        assert screen.query_one("#backup-estimate", Button).disabled

        screen.action_verify()
        await _wait_for(pilot, lambda: backend.verified == ["backup-complete"])
        screen.action_browse()
        await _wait_for(pilot, lambda: backend.browsed == ["backup-complete"])
        await _wait_for(
            pilot,
            lambda: isinstance(app.screen, BackupBrowseScreen) and app.screen.is_mounted,
        )
        assert "Offline backup view" in str(app.screen.query_one("#area-state", Static).render())


@pytest.mark.asyncio
async def test_backup_and_activity_controls_remain_keyboard_reachable_when_narrow() -> None:
    backup = BackupScreen(BackupBackend(), default_destination="C:/synthetic/backups")
    app = App[None]()
    async with app.run_test(size=(79, 26)) as pilot:
        await app.push_screen(backup)
        await _wait_for(pilot, lambda: backup._selected_id == "backup-complete")
        assert backup.has_class("compact-backup")
        target = backup.query_one("#backup-restore-plan", Button)
        target.focus()
        await pilot.pause()
        assert 0 <= target.region.y < backup.size.height

    activity = ActivityScreen(ActivityBackend())
    app = App[None]()
    async with app.run_test(size=(79, 26)) as pilot:
        await app.push_screen(activity)
        await _wait_for(pilot, lambda: activity._snapshot is not None)
        assert activity.has_class("compact-activity")
        target = activity.query_one("#activity-test-notification", Button)
        target.focus()
        await pilot.pause()
        assert 0 <= target.region.y < activity.size.height


@pytest.mark.asyncio
async def test_activity_screen_filters_details_and_executes_safe_controls() -> None:
    backend = ActivityBackend()
    screen = ActivityScreen(backend)
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._snapshot is not None)
        assert len(screen._events) == 2

        screen.query_one("#activity-category", Select).value = "webhook"
        await _wait_for(pilot, lambda: [item.event_id for item in screen._events] == ["evt-hook"])
        screen._select_event("evt-hook")
        assert not screen.query_one("#activity-retry-delivery", Button).disabled
        assert "redacted" in str(screen.query_one("#activity-detail", Static).render()).casefold()

        await click_button(pilot, pilot.app.screen.query_one("#activity-retry-delivery", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.retry_ids == ["corr-hook"])

        await click_button(pilot, pilot.app.screen.query_one("#activity-test-notification", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.tests == 1)

        await click_button(pilot, pilot.app.screen.query_one("#activity-export", Button))
        await _wait_for(pilot, lambda: backend.exports == 1)
        await click_button(pilot, pilot.app.screen.query_one("#activity-clear-history", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert backend.clear_plans == 1
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.cleared == 2)
        await click_button(pilot, pilot.app.screen.query_one("#activity-toggle-watcher", Button))
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.watcher_controls == ["stop"])


@pytest.mark.asyncio
async def test_activity_screen_executes_exact_pause_resume_and_sync_controls() -> None:
    backend = ActivityBackend()
    screen = ActivityScreen(backend)
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._snapshot is not None)

        # The screen runs one change at a time, so each request waits until the last one
        # (and the reload after it) has finished.
        def done(*controls: str) -> bool:
            return backend.watcher_controls == list(controls) and not screen._operation_active

        screen.queue_watcher_control("pause")
        await _wait_for(pilot, lambda: done("pause"))
        screen.queue_watcher_control("resume")
        await _wait_for(pilot, lambda: done("pause", "resume"))
        screen.queue_watcher_control("sync")
        await _wait_for(
            pilot,
            lambda: backend.watcher_controls == ["pause", "resume", "sync"],
        )


@pytest.mark.asyncio
async def test_activity_screen_executes_exact_desktop_notification_test() -> None:
    backend = ActivityBackend()
    screen = ActivityScreen(backend, offline=True)
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._snapshot is not None)
        screen.queue_notification_test("desktop")
        await _wait_for(pilot, lambda: backend.test_providers == ["desktop"])


@pytest.mark.asyncio
async def test_activity_screen_queues_newest_retryable_webhook_with_confirmation() -> None:
    backend = ActivityBackend()
    screen = ActivityScreen(backend, offline=True)
    app = App[None]()
    async with app.run_test(size=(120, 35)) as pilot:
        await app.push_screen(screen)
        await _wait_for(pilot, lambda: screen._snapshot is not None)
        screen.queue_delivery_retry("webhook")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await _wait_for(pilot, lambda: backend.retry_ids == ["corr-hook"])
