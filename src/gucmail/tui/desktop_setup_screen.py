"""Previewed current-user desktop identity setup shared with the CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from threading import Event
from typing import ClassVar, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, VerticalScroll
from textual.widgets import Button, Label, Static

from gucmail.services.windows_desktop import DesktopSetupPreview, WindowsDesktopService
from gucmail.tui.area_common import (
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
)


class DesktopSetupScreen(OperationalScreen):
    DEFAULT_CSS = """
    #desktop-setup-content { padding: 1 2; }
    #desktop-setup-content Label, #desktop-setup-status { height: auto; margin-bottom: 1; }
    #desktop-setup-actions { height: 3; }
    #desktop-setup-actions Button { margin-right: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Back"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def __init__(self, service_factory: Callable[[str], WindowsDesktopService]) -> None:
        super().__init__(area_name="Windows desktop setup")
        self.service_factory = service_factory

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="desktop-setup-content"):
            yield Label("Windows desktop notifications")
            yield Static(
                "Set up a current-user identity so notification clicks can open a read-only "
                "message. Install the optional desktop extra first; see Help → Windows "
                "desktop notifications.",
                markup=False,
            )
            yield Static("Inspecting desktop setup…", id="area-state", markup=False)
            yield Static("", id="desktop-setup-status", markup=False)
            with HorizontalScroll(id="desktop-setup-actions"):
                yield Button("Set up…", id="desktop-setup-enable", variant="primary", disabled=True)
                yield Button(
                    "Remove setup…", id="desktop-setup-remove", variant="warning", disabled=True
                )
                yield Button("Refresh", id="desktop-setup-refresh")
                yield Button("Back", classes="area-close")
            yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        self.call_after_refresh(self.action_refresh)

    def action_refresh(self) -> None:
        self._request("setup", confirm=False)

    def _request(self, operation: str, *, confirm: bool) -> None:
        def inspect(_cancel: Event, _progress: ProgressReporter) -> object:
            service = self.service_factory(operation)
            preview = service.preview(operation)
            removable = self.service_factory("remove").preview("remove").can_apply
            return service, preview, removable

        def loaded(value: object) -> None:
            service, preview, removable = cast(
                tuple[WindowsDesktopService, DesktopSetupPreview, bool], value
            )
            self._loaded(service, preview, removable, confirm=confirm)

        self._begin_operation("Inspecting desktop setup", inspect, loaded, remember=False)

    def _loaded(
        self,
        service: WindowsDesktopService,
        preview: DesktopSetupPreview,
        removable: bool,
        *,
        confirm: bool,
    ) -> None:
        identity, inspection = preview.identity, preview.inspection
        self.query_one("#desktop-setup-status", Static).update(
            "\n".join(
                (
                    f"Status: {inspection.detail}",
                    f"Application identity: {identity.app_id}",
                    f"Manifest: {identity.manifest}",
                    "Only this user's GUC Mail desktop registration is affected.",
                )
            )
        )
        self.query_one("#desktop-setup-enable", Button).disabled = not preview.can_apply
        self.query_one("#desktop-setup-remove", Button).disabled = not removable
        self._set_view_state(
            ViewState.READY if preview.can_apply else ViewState.ERROR, preview.reason
        )
        if not confirm or not preview.can_apply:
            return
        detail = f"{preview.operation.title()} this current-user identity?\n\n" + json.dumps(
            preview.as_dict(), indent=2
        )

        def apply() -> None:
            def run(cancel: Event, _progress: ProgressReporter) -> object:
                if cancel.is_set():
                    raise PublicOperationError("Desktop change cancelled before execution.")
                return service.apply(preview, confirmed=True)

            self._begin_operation(
                "Applying desktop setup", run, lambda _: self.action_refresh(), remember=False
            )

        self._confirm(
            "Apply desktop setup?", detail, apply, confirm_label=preview.operation.title()
        )

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "desktop-setup-refresh":
            self.action_refresh()
        elif event.button.id in {"desktop-setup-enable", "desktop-setup-remove"}:
            self._request(
                "setup" if event.button.id == "desktop-setup-enable" else "remove", confirm=True
            )
