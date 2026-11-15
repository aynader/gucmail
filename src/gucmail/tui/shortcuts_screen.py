"""Offline browser of actual Textual screen/control bindings and current global keys."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Static

from gucmail.help.models import ShortcutHelpRecord
from gucmail.help.reference import shortcut_conflicts
from gucmail.security import sanitize_terminal
from gucmail.services.keyboard_bindings import KEYBOARD_SETTING, keyboard_catalog
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable

REQUIRED_SCOPES = (
    "Global",
    "Mail",
    "Composer",
    "Calendar",
    "Contacts",
    "Tasks",
    "Backup",
    "Search",
    "Command palette",
)


def binding_scope(owner: type[Any]) -> str:
    name = owner.__name__.casefold()
    module = owner.__module__.rsplit(".", 1)[-1]
    if "palette" in name:
        return "Command palette"
    if "compose" in name:
        return "Composer"
    if "calendar" in name or "meeting" in name:
        return "Calendar"
    if "contact" in name or "correspondent" in name or module == "contact_groups_screen":
        return "Contacts"
    if "task" in name:
        return "Tasks"
    if "backup" in name or "restore" in name:
        return "Backup"
    if "search" in name:
        return "Search"
    if module in {"appearance_screen", "branding_screen"}:
        return "Appearance"
    if module == "settings_screen":
        return "Settings"
    if module == "keyboard_editor":
        return "Keyboard"
    if "reader" in name or "folder" in name or "message" in name or "draft" in name:
        return "Mail"
    if "help" in name or "tutorial" in name:
        return "Help"
    return "Other screens"


def runtime_shortcut_records(
    app: App[Any], configured: Mapping[str, str]
) -> tuple[ShortcutHelpRecord, ...]:
    """Read metadata only: never instantiate screens, services or a mailbox session."""
    # Include lazily imported routes before their first use. Read class metadata
    # without instantiating a screen, resolving credentials or opening a mailbox.
    from gucmail.tui.desktop_setup_screen import DesktopSetupScreen
    from gucmail.tui.notification_reader import NotificationReaderApp
    from gucmail.tui.task_delete_screen import TaskDeleteOptionsScreen
    from gucmail.tui.unanswered_screen import UnansweredScreen

    owners: set[type[Any]] = {
        DesktopSetupScreen,
        NotificationReaderApp,
        TaskDeleteOptionsScreen,
        UnansweredScreen,
    }
    pending = list(Screen.__subclasses__())
    visited: set[type[Screen[Any]]] = set()
    while pending:
        owner = pending.pop()
        if owner in visited:
            continue
        visited.add(owner)
        pending.extend(owner.__subclasses__())
        if owner.__module__.startswith("gucmail.tui."):
            owners.add(owner)
    mounted = {type(screen): screen for screen in app.screen_stack}
    records: list[ShortcutHelpRecord] = []
    definitions = keyboard_catalog()[0]
    overrides = getattr(app, "_binding_overrides", {})
    labels = ("Open Command Palette", "Open Command Palette", "Previous theme", "Next theme")
    for (identifier, default), label in zip(SHORTCUT_DEFAULTS.items(), labels, strict=True):
        records.append(
            ShortcutHelpRecord(
                identifier,
                "Global",
                configured.get(identifier, default),
                label,
                True,
                identifier,
                "palette.open" if identifier.startswith("palette.") else None,
                "Global",
                "palette" if identifier.startswith("palette.") else identifier,
            )
        )

    def append_bindings(owner: type[Any], binding_map: Any, scope: str, context: str) -> None:
        context_id = f"{owner.__module__}.{owner.__name__}"
        owned = tuple(row for row in definitions if row.context == context_id)
        shown: set[str] = set()
        for key, entries in sorted(binding_map.key_to_bindings.items()):
            for index, binding in enumerate(entries):
                if binding.action in {"palette", "app.palette", "previous_theme", "next_theme"}:
                    continue
                actual_scope = scope
                definition = next(
                    (
                        row
                        for row in owned
                        if row.action == binding.action
                        and key in {row.default_key, overrides.get(row.binding_id, row.default_key)}
                    ),
                    None,
                )
                if definition is not None:
                    if definition.binding_id in shown:
                        continue
                    shown.add(definition.binding_id)
                    current_key = overrides.get(definition.binding_id, definition.default_key)
                else:
                    current_key = key
                if owner is type(app) and binding.action in {
                    "compose",
                    "reply",
                    "reply_all",
                    "forward",
                    "delete",
                    "search",
                    "toggle_read",
                    "toggle_message_selection",
                    "toggle_sidebar",
                    "preview_narrower",
                    "preview_wider",
                    "reset_layout",
                    "toggle_preview",
                    "detach_reader",
                }:
                    actual_scope = "Mail"
                records.append(
                    ShortcutHelpRecord(
                        definition.binding_id if definition else f"{context_id}:{key}:{index}",
                        actual_scope,
                        current_key,
                        binding.description or binding.action.replace("_", " "),
                        customizable=definition is not None,
                        setting_id=KEYBOARD_SETTING if definition else None,
                        context=context,
                        binding_action=binding.action,
                        priority=binding.priority,
                        context_id=context_id,
                    )
                )
        for definition in owned:
            if definition.binding_id not in shown:
                records.append(
                    ShortcutHelpRecord(
                        definition.binding_id,
                        definition.scope,
                        overrides.get(definition.binding_id, definition.default_key),
                        definition.description or definition.action.replace("_", " "),
                        True,
                        KEYBOARD_SETTING,
                        context=context,
                        binding_action=definition.action,
                        priority=definition.priority,
                        context_id=context_id,
                    )
                )

    append_bindings(type(app), app._bindings, "Global", "Mail shell")
    for owner in sorted(owners, key=lambda cls: (cls.__module__, cls.__name__)):
        instance = mounted.get(owner)
        bindings = instance._bindings if instance is not None else owner._merged_bindings
        append_bindings(owner, bindings, binding_scope(owner), owner.__name__)
        if getattr(owner, "USES_GLOBAL_PALETTE_SHORTCUTS", False):
            for identifier in ("palette.primary_shortcut", "palette.secondary_shortcut"):
                key = configured.get(identifier, SHORTCUT_DEFAULTS[identifier])
                if key:
                    records.append(
                        ShortcutHelpRecord(
                            f"{owner.__module__}.{owner.__name__}:{identifier}",
                            binding_scope(owner),
                            key,
                            "Open Command Palette",
                            True,
                            identifier,
                            "palette.open",
                            owner.__name__,
                            "app.palette",
                            True,
                            f"{owner.__module__}.{owner.__name__}",
                        )
                    )
        for index, binding in enumerate(getattr(owner, "CONTEXT_BINDINGS", ())):
            records.append(
                ShortcutHelpRecord(
                    f"{owner.__module__}.{owner.__name__}:context:{index}",
                    binding_scope(owner),
                    binding.key,
                    binding.description,
                    context=owner.__name__,
                    binding_action=binding.action,
                    context_id=f"{owner.__module__}.{owner.__name__}",
                )
            )
    controls: set[type[Widget]] = set()
    widget_pending = list(Widget.__subclasses__())
    while widget_pending:
        control = widget_pending.pop()
        if control in controls:
            continue
        controls.add(control)
        widget_pending.extend(control.__subclasses__())
    for control in sorted(controls, key=lambda cls: (cls.__module__, cls.__name__)):
        if issubclass(control, Screen):
            continue
        local = control.__module__.startswith("gucmail.tui.")
        if not local and not control.__module__.startswith("textual.widgets."):
            continue
        scope = binding_scope(control) if local else "Focused controls"
        contextual = tuple(getattr(control, "CONTEXT_BINDINGS", ()))
        overridden = {binding.key for binding in contextual}
        # Conditional event handlers take precedence over inherited generic scrolling.
        original_bindings = control._merged_bindings
        if original_bindings is None:
            continue
        bindings = original_bindings.copy()
        for key in overridden:
            bindings.key_to_bindings.pop(key, None)
        append_bindings(control, bindings, scope, control.__name__)
        for index, binding in enumerate(contextual):
            records.append(
                ShortcutHelpRecord(
                    f"{control.__module__}.{control.__name__}:context:{index}",
                    scope,
                    binding.key,
                    binding.description,
                    context=control.__name__,
                    binding_action=binding.action,
                    context_id=f"{control.__module__}.{control.__name__}",
                )
            )
    return tuple(
        sorted(records, key=lambda row: (row.scope, row.context, row.key, row.shortcut_id))
    )


class ShortcutsRequested(Message):
    """Open the local binding browser from Help."""


class ShortcutSettingRequested(Message):
    def __init__(self, setting_id: str, binding_id: str | None = None) -> None:
        super().__init__()
        self.setting_id = setting_id
        self.binding_id = binding_id


class ShortcutsScreen(Screen[None]):
    DEFAULT_CSS = """
    ShortcutsScreen #shortcut-title { height: 1; background: $panel; padding: 0 1; }
    ShortcutsScreen #shortcut-filters { height: 3; }
    ShortcutsScreen #shortcut-scope { width: 24; }
    ShortcutsScreen #shortcut-search { width: 1fr; }
    ShortcutsScreen #shortcut-table { height: 1fr; min-height: 3; }
    ShortcutsScreen #shortcut-detail { height: 4; padding: 0 1; background: $panel; }
    ShortcutsScreen #shortcut-status { height: 2; padding: 0 1; }
    ShortcutsScreen #shortcut-actions { height: 3; }
    ShortcutsScreen #shortcut-actions Button { min-width: 12; }
    """
    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("ctrl+enter", "customize", "Customize", show=False),
    ]

    def __init__(
        self,
        records: Sequence[ShortcutHelpRecord],
        *,
        notice: str = "",
        record_provider: Callable[[], Sequence[ShortcutHelpRecord]] | None = None,
    ) -> None:
        super().__init__()
        self.records = tuple(records)
        self.notice = notice
        self.record_provider = record_provider
        self.conflicts = shortcut_conflicts(self.records)
        self._by_id = {record.shortcut_id: record for record in self.records}

    def compose(self) -> ComposeResult:
        yield Static("Keyboard shortcuts · current assignments", id="shortcut-title")
        scopes = (*REQUIRED_SCOPES, *sorted({r.scope for r in self.records} - set(REQUIRED_SCOPES)))
        with Horizontal(id="shortcut-filters"):
            yield Select(
                [("All areas", ""), *((s, s) for s in scopes)],
                value="",
                allow_blank=False,
                id="shortcut-scope",
            )
            yield Input(placeholder="Search key, action or screen…", id="shortcut-search")
        yield DataTable(id="shortcut-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="shortcut-detail", markup=False)
        yield Static("", id="shortcut-status", markup=False)
        with Horizontal(id="shortcut-actions"):
            yield Button("Customize", id="shortcut-customize", disabled=True)
            yield Button("Edit keys", id="shortcut-editor")
            yield Button("Close", id="shortcut-close")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#shortcut-table", DataTable)
        table.add_columns(
            "Current key", "Action", "Area", "Screen / control", "Customizable", "Conflict / reuse"
        )
        self._filter()
        table.focus()

    def on_screen_resume(self) -> None:
        table = self.query("#shortcut-table")
        if self.record_provider is not None and table and table.first(DataTable).columns:
            self.records = tuple(self.record_provider())
            self.conflicts = shortcut_conflicts(self.records)
            self._by_id = {record.shortcut_id: record for record in self.records}
            self._filter()

    @on(Button.Pressed, "#shortcut-editor")
    def open_editor(self) -> None:
        row = self._selected()
        self.post_message(
            ShortcutSettingRequested(
                KEYBOARD_SETTING,
                row.shortcut_id if row and row.setting_id == KEYBOARD_SETTING else None,
            )
        )

    def _conflict_label(self, row: ShortcutHelpRecord) -> str:
        if (
            (row.context_id or row.context or row.scope).casefold(),
            row.key.casefold(),
        ) in self.conflicts:
            return "Conflict: different actions"
        if (
            row.key
            and row.scope != "Global"
            and any(other.scope == "Global" and other.key == row.key for other in self.records)
        ):
            return "Also Global; context applies"
        return "None"

    def _filter(self) -> None:
        scope = self.query_one("#shortcut-scope", Select).value
        query = self.query_one("#shortcut-search", Input).value.strip().casefold()
        table = self.query_one("#shortcut-table", DataTable)
        table.clear()
        for row in self.records:
            if scope and row.scope != scope:
                continue
            if (
                query
                not in f"{row.key} {row.action} {row.context} {row.setting_id or ''}".casefold()
            ):
                continue
            table.add_row(
                *(
                    Text(sanitize_terminal(value))
                    for value in (
                        row.key or "Disabled",
                        row.action,
                        row.scope,
                        row.context,
                        "Yes" if row.customizable else "No",
                        self._conflict_label(row),
                    )
                ),
                key=row.shortcut_id,
            )
        status = (
            f"{table.row_count} of {len(self.records)} bindings. Application keys are editable."
        )
        status += " " + (self.notice or "Typing/navigation depends on the focused control.")
        self.query_one("#shortcut-status", Static).update(status)
        self._show_selected()

    def _selected(self) -> ShortcutHelpRecord | None:
        table = self.query_one("#shortcut-table", DataTable)
        if not table.row_count:
            return None
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return self._by_id.get(key)

    def _show_selected(self) -> None:
        row = self._selected()
        self.query_one("#shortcut-customize", Button).disabled = not row or not row.customizable
        detail = "No matching bindings. Clear the search or choose another area."
        if row:
            detail = (
                f"{row.key or 'Disabled'}: {row.action}\n{row.scope} / {row.context}\n"
                f"{self._conflict_label(row)}. "
                + (
                    f"Edit {row.setting_id} in Settings; Stage, Apply or Cancel."
                    if row.customizable
                    else "This binding is fixed for its screen/control."
                )
            )
        self.query_one("#shortcut-detail", Static).update(sanitize_terminal(detail))

    @on(Select.Changed, "#shortcut-scope")
    @on(Input.Changed, "#shortcut-search")
    def filter_changed(self) -> None:
        if self.is_mounted:
            self._filter()

    @on(DataTable.RowHighlighted, "#shortcut-table")
    def highlighted(self) -> None:
        self._show_selected()

    def action_focus_search(self) -> None:
        self.query_one("#shortcut-search", Input).focus()

    @on(Button.Pressed, "#shortcut-customize")
    def action_customize(self) -> None:
        row = self._selected()
        if row and row.customizable and row.setting_id:
            self.post_message(
                ShortcutSettingRequested(
                    row.setting_id,
                    row.shortcut_id if row.setting_id == KEYBOARD_SETTING else None,
                )
            )

    @on(Button.Pressed, "#shortcut-close")
    def action_close(self) -> None:
        # dismiss() closes only this screen. A second close request (a double click
        # on Close) must not close the screen underneath as well.
        self.dismiss()
