"""Numbered, keyboard-navigable preference rows for the Settings form."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


class SettingRow(Widget, can_focus=True):
    """A preference is one focus target; Enter opens its editor."""

    DEFAULT_CSS = """
    SettingRow { height: 2; width: 100%; background: transparent; }
    SettingRow.group-start { border-top: solid $gucmail-divider; margin-top: 1; padding-top: 1;
        height: 4; }
    SettingRow.selected { height: 3; }
    SettingRow.selected.group-start { height: 5; }
    SettingRow .setting-line { height: 1; width: 100%; }
    SettingRow .setting-pointer { width: 3; color: $gucmail-primary-accent; }
    SettingRow .setting-number { width: 6; color: $gucmail-muted-text; }
    SettingRow .setting-label { width: 1fr; color: $gucmail-primary-text; }
    SettingRow .setting-value { width: 29%; min-width: 12; color: $gucmail-primary-text; }
    SettingRow .setting-hint { display: none; height: 1; margin-left: 9;
        color: $gucmail-muted-text; }
    SettingRow.selected .setting-hint { display: block; }
    SettingRow.selected .setting-value { color: $gucmail-primary-accent; }
    SettingRow:focus { background: transparent; }
    SettingRow:focus .setting-label { text-style: underline; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("enter,space", "edit", "Change", show=False),
        Binding("up", "previous_row", "Previous", show=False),
        Binding("down", "next_row", "Next", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "first_row", "First", show=False),
        Binding("end", "last_row", "Last", show=False),
    ]

    class Selected(Message):
        def __init__(self, row: SettingRow) -> None:
            super().__init__()
            self.row = row

    class EditRequested(Message):
        def __init__(self, row: SettingRow) -> None:
            super().__init__()
            self.row = row

    def __init__(
        self,
        setting_id: str,
        number: int,
        label: str,
        value: str,
        description: str,
        *,
        group_start: bool = False,
        staged: bool = False,
    ) -> None:
        super().__init__(classes="group-start" if group_start else "")
        self.setting_id = setting_id
        self.number = number
        self.label = label
        self.value = value
        self.description = description
        self.staged = staged

    def compose(self) -> ComposeResult:
        with Horizontal(classes="setting-line"):
            yield Static("", classes="setting-pointer", markup=False)
            yield Static(f"{self.number:02}", classes="setting-number", markup=False)
            yield Static(
                Text(self.label, no_wrap=True, overflow="ellipsis"), classes="setting-label"
            )
            yield Static(
                Text(self.value + (" *" if self.staged else ""), no_wrap=True, overflow="ellipsis"),
                classes="setting-value",
            )
        yield Static(
            Text(self.description, no_wrap=True, overflow="ellipsis"), classes="setting-hint"
        )

    def set_selected(self, selected: bool) -> None:
        self.set_class(selected, "selected")
        pointers = self.query(".setting-pointer")
        if pointers:
            pointers.first(Static).update(">" if selected else "")

    def on_mount(self) -> None:
        self.set_selected(self.has_class("selected"))

    def on_focus(self) -> None:
        self.post_message(self.Selected(self))

    @on(events.Click)
    def clicked(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        self.action_edit()

    def action_edit(self) -> None:
        self.post_message(self.EditRequested(self))

    def _move(self, step: int, *, absolute: bool = False) -> None:
        rows = list(self.parent.query(SettingRow)) if self.parent is not None else []
        if not rows:
            return
        index = step if absolute else rows.index(self) + step
        self.screen.set_focus(rows[max(0, min(index, len(rows) - 1))])

    def action_previous_row(self) -> None:
        self._move(-1)

    def action_next_row(self) -> None:
        self._move(1)

    def action_page_up(self) -> None:
        self._move(-max(1, self.screen.size.height // 3))

    def action_page_down(self) -> None:
        self._move(max(1, self.screen.size.height // 3))

    def action_first_row(self) -> None:
        self._move(0, absolute=True)

    def action_last_row(self) -> None:
        self._move(10**6, absolute=True)


class SettingsForm(Vertical):
    DEFAULT_CSS = """
    SettingsForm { width: 100%; height: auto; }
    """

    @property
    def row_count(self) -> int:
        return len(self.query(SettingRow))

    def focus_setting(self, setting_id: str) -> None:
        for row in self.query(SettingRow):
            if row.setting_id == setting_id:
                self.screen.set_focus(row)
                return
