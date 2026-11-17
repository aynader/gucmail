"""Previewed operating-system startup controls using the shared CLI service."""

from __future__ import annotations

import json
from contextlib import suppress
from threading import Event
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Static

from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.startup_runtime import StartupPreview, StartupService
from gucmail.tui.area_common import (
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
)


class StartupScreen(OperationalScreen):
    DEFAULT_CSS = """
    StartupScreen { background: $surface; }
    #startup-content { padding: 1 2; }
    #startup-content Label, #startup-status { height: auto; margin-top: 1; }
    #startup-content Input { max-width: 70; }
    .startup-actions { height: auto; min-height: 3; }
    .startup-actions Button { margin-right: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Back"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def __init__(self, service: StartupService) -> None:
        super().__init__(area_name="Windows Startup", operation_timeout_seconds=80)
        self.service = service
        self.current: StartupPreview | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="startup-content"):
            yield Label("Start alerts when you sign in")
            yield Static(
                "Checks for new mail in the background after you sign in to your computer. "
                "Your password must be saved for this to work.",
                markup=False,
            )
            yield Static("Checking…", id="area-state", markup=False)
            yield Label("Profile")
            yield Input("default", id="startup-profile")
            yield Label("Wait after sign-in (seconds)")
            yield Input("30", type="integer", id="startup-delay")
            yield Label("Wait before restarting after a problem (seconds)")
            yield Input("60", type="integer", id="startup-restart-delay")
            yield Label("Restart tries")
            yield Input("3", type="integer", id="startup-attempts")
            yield Static("", id="startup-status", markup=False)
            with Horizontal(classes="startup-actions"):
                yield Button("Turn on…", id="startup-enable", variant="primary")
                yield Button("Repair…", id="startup-repair")
                yield Button("Turn off…", id="startup-disable")
            with Horizontal(classes="startup-actions"):
                yield Button("Check", id="startup-refresh")
                yield Button("Remove…", id="startup-uninstall", variant="warning")
                yield Button("Back", classes="area-close")
            yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        record = self.service.context.state.get_ui_preference("preferences", scope="startup")
        preferences = record.value if record else {}
        if isinstance(preferences, dict):
            for key, selector in (
                ("profile", "startup-profile"),
                ("startup_delay", "startup-delay"),
                ("restart_delay", "startup-restart-delay"),
                ("restart_attempts", "startup-attempts"),
            ):
                if key in preferences:
                    self.query_one("#" + selector, Input).value = str(preferences[key])
        if not (isinstance(preferences, dict) and "profile" in preferences):
            # Show the profile that will actually be watched instead of "default".
            with suppress(Exception):
                active = NotificationConfigurationService(
                    self.service.context.state
                ).profiles.active_profile()
                if active is not None:
                    self.query_one("#startup-profile", Input).value = active.profile_id
        self.call_after_refresh(self.action_refresh)

    def _values(self) -> dict[str, object]:
        try:
            return {
                "profile": self.query_one("#startup-profile", Input).value.strip(),
                "startup_delay": int(self.query_one("#startup-delay", Input).value),
                "restart_delay": int(self.query_one("#startup-restart-delay", Input).value),
                "restart_attempts": int(self.query_one("#startup-attempts", Input).value),
            }
        except ValueError as exc:
            raise PublicOperationError("Delays and attempts must be whole numbers.") from exc

    def _request(self, operation: str | None) -> None:
        try:
            values = self._values()
        except PublicOperationError as exc:
            self._set_view_state(ViewState.ERROR, str(exc))
            return
        self._begin_operation(
            "Checking start at sign-in",
            lambda _cancel, _progress: self.service.preview(
                profile=str(values["profile"]),
                startup_delay=int(str(values["startup_delay"])),
                restart_delay=int(str(values["restart_delay"])),
                restart_attempts=int(str(values["restart_attempts"])),
                operation=operation,
            ),
            lambda result: self._preview_loaded(result, values),
            remember=False,
            retry_safe=True,
        )

    def action_refresh(self) -> None:
        self._request(None)

    def _preview_loaded(self, value: object, preferences: dict[str, object]) -> None:
        if not isinstance(value, StartupPreview):
            raise PublicOperationError("Startup inspection returned an invalid result.")
        self.current = value
        inspection = value.inspection
        installed = str(inspection.installed) if inspection.state_known else "Unknown"
        enabled = str(inspection.enabled) if inspection.state_known else "Unknown"
        self.query_one("#startup-status", Static).update(
            f"Integration: {inspection.platform.value}\nInstalled: {installed} · "
            f"Enabled: {enabled} · Healthy: {inspection.healthy}\n"
            f"Checks: {', '.join(item.value for item in inspection.issues) or 'passed'}\n"
            f"{inspection.detail or ''}"
        )
        for selector in ("enable", "repair", "disable", "uninstall"):
            self.query_one("#startup-" + selector, Button).disabled = not inspection.state_known
        self._set_view_state(
            ViewState.READY if inspection.state_known else ViewState.ERROR,
            "Startup state inspected; no mailbox request performed."
            if inspection.state_known
            else (
                "Startup state is unavailable. Resolve the reported error and refresh. "
                "No change attempted."
            ),
        )
        operation = value.operation
        if operation is None:
            return
        if not operation.can_apply or operation.no_changes:
            self._set_view_state(
                ViewState.READY if operation.can_apply else ViewState.ERROR, operation.reason
            )
            return
        detail = f"{operation.reason}\nManaged entry: {operation.service_name}\n"
        detail += f"Executable: {value.plan.process.executable}\n"
        detail += f"Arguments: {json.dumps(value.plan.process.arguments)}\n"
        detail += f"Working directory: {value.plan.process.working_directory}\n"
        detail += "Artifacts: " + ", ".join(str(item.path) for item in value.plan.artifacts) + "\n"
        detail += "\n".join(
            command.purpose for command in (*operation.commands, *operation.commands_after_removal)
        )
        detail += "\nOnly this managed startup entry is affected. No mail is sent by setup."

        def apply() -> None:
            def run(_cancel: Event, _progress: ProgressReporter) -> object:
                if _cancel.is_set():
                    raise PublicOperationError("Startup change cancelled before execution.")
                result = self.service.apply(value, confirmed=True)
                self.service.context.state.put_ui_preference(
                    "preferences", preferences, scope="startup"
                )
                return result

            self._begin_operation(
                "Changing start at sign-in",
                run,
                lambda _: self.action_refresh(),
                timeout_seconds=180,
                remember=False,
            )

        self._confirm(
            "Apply startup change?", detail, apply, confirm_label=operation.operation.value.title()
        )

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if identifier == "startup-refresh":
            self.action_refresh()
        elif identifier in {
            "startup-enable",
            "startup-repair",
            "startup-disable",
            "startup-uninstall",
        }:
            self._request(identifier.removeprefix("startup-"))
