"""One switch for new-mail notifications.

Turning it on sets up everything a desktop notification needs: the notification settings,
the Windows registration, the background task (which also sends scheduled mail) and a test
toast. Turning it off silences notifications and stops the task unless scheduled mail still
needs it. The CLI and the TUI both call this, so they behave the same.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gucmail.errors import GUCmailError

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext


@dataclass(frozen=True, slots=True)
class SwitchStep:
    label: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class SwitchStatus:
    on: bool
    task_running: bool
    pending_scheduled: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "notifications": "on" if self.on else "off",
            "background_task": "running" if self.task_running else "stopped",
            "scheduled_messages_waiting": self.pending_scheduled,
            "summary": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SwitchResult:
    on: bool
    steps: tuple[SwitchStep, ...]

    @property
    def working(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def summary(self) -> str:
        if not self.on:
            return "Notifications are off."
        if self.working:
            return "✓ Working. You'll get a notification when new mail arrives."
        return "Notifications are on, but something needs attention (see below)."

    def as_dict(self) -> dict[str, object]:
        return {
            "notifications": "on" if self.on else "off",
            "working": self.working,
            "summary": self.summary,
            "steps": [
                {"step": step.label, "ok": step.ok, "detail": step.detail} for step in self.steps
            ],
        }


def _plain_error(exc: BaseException) -> str:
    if isinstance(exc, GUCmailError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


class NotificationSwitch:
    """Turn new-mail notifications on or off in one step.

    The collaborators are plain callables so tests can replace the parts that touch
    Windows (registry, Task Scheduler, toasts).
    """

    def __init__(
        self,
        context: AppContext,
        *,
        startup: Any | None = None,
        desktop_setup: Any | None = None,
        send_test: Callable[[], bool] | None = None,
        old_scheduler: Any | None = None,
        windows: bool | None = None,
    ) -> None:
        from gucmail.services.notification_configuration import (
            NotificationConfigurationService,
        )

        self.context = context
        self.settings = NotificationConfigurationService(context.state)
        self._startup = startup
        self._desktop_setup = desktop_setup
        self._send_test = send_test
        self._old_scheduler = old_scheduler
        self.windows = sys.platform == "win32" if windows is None else windows

    # The real collaborators are built only when needed: they touch Windows.
    def startup(self) -> Any:
        if self._startup is None:
            from gucmail.services.startup_runtime import StartupService

            self._startup = StartupService(self.context)
        return self._startup

    def desktop_setup(self) -> Any:
        if self._desktop_setup is None:
            from gucmail.services.windows_desktop import desktop_setup_service

            self._desktop_setup = desktop_setup_service(self.context)
        return self._desktop_setup

    def old_scheduler(self) -> Any:
        if self._old_scheduler is None:
            self._old_scheduler = _OldSchedulerTask(self.context)
        return self._old_scheduler

    def send_test(self) -> bool:
        if self._send_test is not None:
            return self._send_test()
        from gucmail.models import NotificationEvent
        from gucmail.services.notifications import DeliveryState, NotificationDispatcher
        from gucmail.services.windows_desktop import desktop_notification_provider

        event = NotificationEvent(
            event_id=f"switch-test-{datetime.now(UTC).timestamp():.0f}",
            event_type="test",
            occurred_at=datetime.now(UTC),
            account_id="local",
            subject="Notifications are on",
            preview="This is a test. New mail will appear like this.",
        )
        results = NotificationDispatcher(
            [desktop_notification_provider(self.context)],
            state=self.context.state,
            deduplicate=False,
        ).dispatch(event)
        return any(result.state is DeliveryState.DELIVERED for result in results)

    # --- state ---------------------------------------------------------------------------

    def notifications_on(self) -> bool:
        profile = self.settings.profiles.active_profile()
        if profile is None or not profile.enabled:
            return False
        desktop_ids = {
            item.provider_id.casefold()
            for item in self.settings.providers.list_providers()
            if item.enabled and item.kind.casefold() == "desktop"
        }
        return any(
            policy.enabled and policy.provider_id.casefold() in desktop_ids
            for policy in profile.providers
        )

    def pending_scheduled(self) -> int:
        """Scheduled messages the background task will still send."""
        from gucmail.models import SendState

        waiting = (
            SendState.SCHEDULED,
            SendState.UNDO_DELAY,
            SendState.DUE,
            SendState.WAITING_CONNECTIVITY,
            SendState.WAITING_AUTHENTICATION,
            SendState.SENDING,
            SendState.RETRY_SCHEDULED,
        )
        try:
            return len(self.context.state.list_scheduled_jobs(states=waiting, limit=10_000))
        except GUCmailError:
            # Can't read the queue: assume mail is waiting so the task is never stopped by mistake.
            return 1

    def task_running(self) -> bool:
        try:
            inspection = self.startup().preview().inspection
        except GUCmailError:
            return False
        return bool(inspection.state_known and inspection.installed and inspection.enabled)

    def status(self) -> SwitchStatus:
        on = self.notifications_on()
        task = self.task_running()
        waiting = self.pending_scheduled()
        if on and task:
            detail = "On. The background task is checking for new mail."
        elif on:
            detail = "On, but the background task isn't running. Turn them on again to fix it."
        elif task:
            detail = "Off. The background task is still running to send scheduled mail."
        else:
            detail = "Off."
        return SwitchStatus(on, task, waiting, detail)

    # --- switching -----------------------------------------------------------------------

    def turn_on(self, *, test: bool = True) -> SwitchResult:
        steps = [self._enable_settings()]
        if self.windows:
            steps.append(self._register_windows())
        steps.append(self._start_task())
        removed = self._remove_old_scheduler()
        if removed is not None:
            steps.append(removed)
        if test and self.windows and steps[-1].ok:
            steps.append(self._test_toast())
        return SwitchResult(True, tuple(steps))

    def turn_off(self) -> SwitchResult:
        steps: list[SwitchStep] = []
        try:
            self.settings.set_desktop_notifications(enabled=False)
            steps.append(SwitchStep("Notifications", True, "Turned off."))
        except GUCmailError as exc:
            steps.append(SwitchStep("Notifications", False, _plain_error(exc)))
        waiting = self.pending_scheduled()
        if waiting:
            steps.append(
                SwitchStep(
                    "Background task",
                    True,
                    f"Kept running to send {waiting} scheduled message(s).",
                )
            )
        else:
            steps.append(self._task_operation("disable", "Stopped."))
        return SwitchResult(False, tuple(steps))

    def _enable_settings(self) -> SwitchStep:
        try:
            profile = self.settings.set_desktop_notifications(enabled=True)
            if not profile.enabled:
                self.settings.profiles.set_enabled(profile.profile_id, enabled=True)
        except GUCmailError as exc:
            return SwitchStep("Notification settings", False, _plain_error(exc))
        return SwitchStep("Notification settings", True, "Desktop notifications for all new mail.")

    def _register_windows(self) -> SwitchStep:
        label = "Windows registration"
        try:
            service = self.desktop_setup()
            preview = service.preview("setup")
            if preview.inspection.known and preview.inspection.installed:
                return SwitchStep(label, True, "Already registered.")
            if not preview.can_apply:
                return SwitchStep(label, False, preview.reason or "Windows refused the change.")
            service.apply(preview, confirmed=True)
        except (GUCmailError, OSError) as exc:
            return SwitchStep(label, False, _plain_error(exc))
        return SwitchStep(label, True, "Registered gucmail with Windows notifications.")

    def _start_task(self) -> SwitchStep:
        try:
            inspection = self.startup().preview().inspection
        except GUCmailError as exc:
            return SwitchStep("Background task", False, _plain_error(exc))
        if not inspection.state_known:
            return SwitchStep(
                "Background task", False, inspection.detail or "Couldn't read the task's state."
            )
        if inspection.installed and inspection.enabled and not inspection.issues:
            return SwitchStep("Background task", True, "Already running.")
        operation = "repair" if inspection.installed else "enable"
        return self._task_operation(operation, "Started. It runs whenever you're signed in.")

    def _task_operation(self, operation: str, done: str) -> SwitchStep:
        label = "Background task"
        try:
            preview = self.startup().preview(operation=operation)
            plan = preview.operation
            if plan is None or plan.no_changes:
                return SwitchStep(label, True, done)
            if not plan.can_apply:
                return SwitchStep(label, False, plan.reason)
            self.startup().apply(preview, confirmed=True)
        except GUCmailError as exc:
            return SwitchStep(label, False, _plain_error(exc))
        return SwitchStep(label, True, done)

    def _remove_old_scheduler(self) -> SwitchStep | None:
        task = self.old_scheduler()
        try:
            if not task.installed():
                return None
            task.remove()
        except (GUCmailError, OSError) as exc:
            return SwitchStep("Old Scheduler task", False, _plain_error(exc))
        return SwitchStep(
            "Old Scheduler task",
            True,
            "Removed. The background task now sends scheduled mail too.",
        )

    def _test_toast(self) -> SwitchStep:
        try:
            delivered = self.send_test()
        except (GUCmailError, OSError) as exc:
            return SwitchStep("Test notification", False, _plain_error(exc))
        if delivered:
            return SwitchStep("Test notification", True, "Sent. Check the corner of your screen.")
        return SwitchStep(
            "Test notification",
            False,
            "Windows didn't show it. Check that notifications are allowed in Windows Settings.",
        )


class _OldSchedulerTask:
    """The separate "GUC Mail Scheduler" task from 1.1. Its job now runs in the watcher."""

    def __init__(self, context: AppContext) -> None:
        self.context = context

    def _plan(self) -> Any:
        from gucmail.platform_scheduler import bound_scheduler_arguments, generate_scheduler_plan

        return generate_scheduler_plan(arguments=bound_scheduler_arguments(self.context.paths))

    def installed(self) -> bool:
        return any(artifact.path.exists() for artifact in self._plan().artifacts)

    def remove(self) -> None:
        from gucmail.platform_scheduler import uninstall_scheduler

        uninstall_scheduler(self._plan(), dry_run=False, confirmed=True)
