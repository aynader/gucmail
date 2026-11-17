"""Small dialogs: confirm, move, export, folder name and folder details."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Static,
)

from gucmail.models import (
    FolderInfo,
)
from gucmail.security import sanitize_terminal


class ConfirmationScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmationScreen { align: center middle; }
    #confirm-box {
        width: 76; max-width: 94%; height: 80%; max-height: 26; min-height: 10;
        padding: 1 2;
        border: thick $warning; background: $gucmail-modal-background;
    }
    #confirm-detail { height: 1fr; min-height: 1; }
    #confirm-detail Static { height: auto; }
    #confirm-buttons { height: 3; min-height: 3; margin-top: 1; align-horizontal: right; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, detail: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._dialog_title = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._dialog_title, classes="dialog-title", markup=False)
            with VerticalScroll(id="confirm-detail"):
                yield Static(self.detail, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, variant="warning", id="confirm")

    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class FolderMoveScreen(ModalScreen[str | None]):
    """Collect one destination selector before an exact move preview is shown."""

    DEFAULT_CSS = """
    FolderMoveScreen { align: center middle; }
    #folder-move-box {
        width: 72; max-width: 94%; height: auto; max-height: 95%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-move-source { height: auto; margin-bottom: 1; color: $text-muted; }
    #folder-move-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #folder-move-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, source_path: str, *, initial_destination: str = "") -> None:
        super().__init__()
        self.source_path = sanitize_terminal(source_path, max_length=500)
        self.initial_destination = initial_destination

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-move-box"):
            yield Label("Move folder", classes="dialog-title")
            yield Static(
                f"Source: {self.source_path}\n"
                "Choose the destination parent by path or id:<reference>.",
                id="folder-move-source",
                markup=False,
            )
            yield Input(
                value=self.initial_destination,
                placeholder="Destination folder path or id:<reference>",
                id="folder-move-destination",
            )
            with Horizontal(id="folder-move-actions"):
                yield Button("Cancel", id="folder-move-cancel")
                yield Button("Preview move", id="folder-move-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#folder-move-destination", Input).focus()

    def _submit(self) -> None:
        destination = self.query_one("#folder-move-destination", Input).value.strip()
        if destination:
            self.dismiss(destination)

    @on(Input.Submitted, "#folder-move-destination")
    def destination_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-move-submit":
            self._submit()
        elif event.button.id == "folder-move-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageExportPathScreen(ModalScreen[str | None]):
    """Collect an explicit directory before previewing a bounded mail export."""

    DEFAULT_CSS = """
    MessageExportPathScreen { align: center middle; background: $background 75%; }
    #message-export-box {
        width: 94%; max-width: 88; height: auto; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #message-export-detail { height: auto; margin-bottom: 1; color: $text-muted; }
    #message-export-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #message-export-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, default_destination: str, *, selection_count: int) -> None:
        super().__init__()
        self.default_destination = default_destination
        self.selection_count = selection_count

    def compose(self) -> ComposeResult:
        with Vertical(id="message-export-box"):
            yield Label("Export selected messages", classes="dialog-title")
            yield Static(
                f"Choose a directory for exactly {self.selection_count} selected message(s). "
                "Files are exported as raw .eml and existing files are never overwritten.",
                id="message-export-detail",
                markup=False,
            )
            yield Input(value=self.default_destination, id="message-export-destination")
            with Horizontal(id="message-export-actions"):
                yield Button("Cancel", id="message-export-cancel")
                yield Button("Preview export", id="message-export-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#message-export-destination", Input).focus()

    def _submit(self) -> None:
        destination = self.query_one("#message-export-destination", Input).value.strip()
        if destination:
            self.dismiss(destination)

    @on(Input.Submitted, "#message-export-destination")
    def destination_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "message-export-submit":
            self._submit()
        elif event.button.id == "message-export-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class FolderNameScreen(ModalScreen[str | None]):
    """Collect one folder display name before the service builds a mutation plan."""

    DEFAULT_CSS = """
    FolderNameScreen { align: center middle; }
    #folder-name-box {
        width: 72; max-width: 94%; height: auto; max-height: 95%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-name-detail { height: auto; margin-bottom: 1; color: $text-muted; }
    #folder-name-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #folder-name-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        title: str,
        detail: str,
        *,
        initial_name: str = "",
        submit_label: str = "Preview",
        placeholder: str = "Folder display name",
    ) -> None:
        super().__init__()
        self.dialog_title = sanitize_terminal(title, max_length=120)
        self.detail = sanitize_terminal(detail, max_length=1_000)
        self.initial_name = initial_name
        self.submit_label = submit_label
        self.placeholder = sanitize_terminal(placeholder, max_length=120)

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-name-box"):
            yield Label(self.dialog_title, classes="dialog-title", markup=False)
            yield Static(self.detail, id="folder-name-detail", markup=False)
            yield Input(
                value=self.initial_name,
                placeholder=self.placeholder,
                id="folder-name-input",
            )
            with Horizontal(id="folder-name-actions"):
                yield Button("Cancel", id="folder-name-cancel")
                yield Button(self.submit_label, id="folder-name-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#folder-name-input", Input).focus()

    def _submit(self) -> None:
        value = self.query_one("#folder-name-input", Input).value.strip()
        if value:
            self.dismiss(value)

    @on(Input.Submitted, "#folder-name-input")
    def name_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-name-submit":
            self._submit()
        elif event.button.id == "folder-name-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MailQueryScreen(FolderNameScreen):
    """Collect a literal mail query in a compact keyboard-accessible dialog."""

    DEFAULT_CSS = """
    MailQueryScreen #folder-name-box { max-width: 94%; max-height: 95%; }
    """


class FolderDetailsScreen(ModalScreen[None]):
    """Show sanitized folder identity, properties, and server-reported counts."""

    DEFAULT_CSS = """
    FolderDetailsScreen { align: center middle; }
    #folder-details-box {
        width: 76; max-width: 95%; height: 27; max-height: 90%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-details-scroll { height: 1fr; min-height: 1; }
    #folder-details-content { height: auto; }
    #folder-details-actions { height: auto; align-horizontal: right; margin-top: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, folder: FolderInfo) -> None:
        super().__init__()
        self.folder = folder

    @staticmethod
    def _value(value: object | None) -> str:
        return sanitize_terminal("Unavailable" if value is None else str(value), max_length=2_000)

    def compose(self) -> ComposeResult:
        folder = self.folder
        read_count = (
            max(folder.total_count - folder.unread_count, 0)
            if folder.total_count is not None and folder.unread_count is not None
            else None
        )
        details = "\n".join(
            (
                f"Name: {self._value(folder.name)}",
                f"Path: {self._value(folder.path)}",
                f"Parent reference: {self._value(folder.parent_reference)}",
                f"Folder class: {self._value(folder.folder_class)}",
                f"Distinguished role: {self._value(folder.distinguished_role)}",
                f"Total items: {self._value(folder.total_count)}",
                f"Unread items: {self._value(folder.unread_count)}",
                f"Read items: {self._value(read_count)}",
                f"Child folders: {self._value(folder.child_count)}",
                f"Local reference: {self._value(folder.reference)}",
                f"EWS ID: {self._value(folder.ews_id)}",
                f"Change key: {self._value(folder.change_key)}",
                "\nUse Show Folder Storage for size analysis across folders.",
            )
        )
        with Vertical(id="folder-details-box"):
            yield Label("Folder properties", classes="dialog-title")
            with VerticalScroll(id="folder-details-scroll"):
                yield Static(details, id="folder-details-content", markup=False)
            with Horizontal(id="folder-details-actions"):
                yield Button("Close", id="folder-details-close", variant="primary")

    @on(Button.Pressed, "#folder-details-close")
    def close_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
