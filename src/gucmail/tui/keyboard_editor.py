"""Guided local keyboard assignments with staged canonical Settings writes."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static

from gucmail.errors import ConfigurationError
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
    KEYBOARD_SETTING_IDS,
    keyboard_catalog,
)
from gucmail.services.keyboard_preferences import KeyboardPreferencesService, KeyboardSnapshot
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS
from gucmail.settings import SettingsImportPlan
from gucmail.tui.area_common import OperationalScreen, ViewState, clean_cell, current_row_highlight
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class KeyboardEditorScreen(OperationalScreen):
    DEFAULT_CSS = """
    #keyboard-root { width: 100%; height: 1fr; padding: 0 1; }
    #keyboard-title, #area-state, #area-progress { height: auto; }
    #keyboard-filters, #keyboard-actions, #keyboard-transfer { height: 3; }
    #keyboard-scope { width: 22; }
    #keyboard-search { width: 1fr; }
    #keyboard-table { height: 8; min-height: 4; }
    #keyboard-detail { height: auto; max-height: 4; }
    #keyboard-actions Button, #keyboard-transfer Button { width: 1fr; min-width: 0; }
    KeyboardEditorScreen.compact #keyboard-table { height: 5; }
    """
    BINDINGS: ClassVar = [
        Binding("ctrl+s", "apply", "Apply"),
        Binding("ctrl+z", "cancel_changes", "Cancel staged"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("ctrl+enter", "stage", "Stage"),
    ]

    def __init__(
        self,
        service: KeyboardPreferencesService,
        *,
        on_applied: Callable[[tuple[str, ...]], None],
        binding_id: str | None = None,
    ) -> None:
        super().__init__(area_name="keyboard", offline=True)
        self.service = service
        self.on_applied = on_applied
        self.selected = binding_id
        self.snapshot: KeyboardSnapshot | None = None
        self.staged: dict[str, Any] = {}
        self.definitions = {row.binding_id: row for row in keyboard_catalog()[0]}
        self.import_plan: SettingsImportPlan | None = None
        self._success_notice = ""

    def compose(self) -> ComposeResult:
        yield Static("Keyboard assignments · Stage then Apply", id="keyboard-title")
        yield Static("Loading local settings…", id="area-state", markup=False)
        yield Static("", id="area-progress", markup=False)
        with VerticalScroll(id="keyboard-root"):
            with Horizontal(id="keyboard-filters"):
                yield Select(
                    [
                        ("All areas", ""),
                        *(
                            (scope, scope)
                            for scope in sorted(
                                {"Global", *(row.scope for row in self.definitions.values())}
                            )
                        ),
                    ],
                    value="",
                    allow_blank=False,
                    id="keyboard-scope",
                )
                yield Input(placeholder="Find action or screen…", id="keyboard-search")
            yield DataTable(id="keyboard-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="keyboard-detail", markup=False)
            yield Input(placeholder="Ctrl+Alt+R or F8; empty disables", id="keyboard-key")
            with Horizontal(id="keyboard-actions"):
                yield Button("Stage", id="keyboard-stage")
                yield Button("Apply", id="keyboard-apply", variant="primary")
                yield Button("Cancel", id="keyboard-cancel")
                yield Button("Reset key", id="keyboard-reset-key")
                yield Button("Reset all", id="keyboard-reset-all")
            yield Input(
                value=str(self.service.settings.config_store.paths.config_dir / "keyboard.json"),
                placeholder="Keyboard export/import file",
                id="keyboard-path",
            )
            with Horizontal(id="keyboard-transfer"):
                yield Button("Export", id="keyboard-export")
                yield Button("Preview", id="keyboard-import", tooltip="Preview keyboard import")
                yield Button(
                    "Import",
                    id="keyboard-apply-import",
                    disabled=True,
                    tooltip="Apply the reviewed keyboard import",
                )
                yield Button("Refresh", id="keyboard-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.set_class(self.size.height < 25, "compact")
        self._columns()
        self.action_refresh()

    def _columns(self) -> None:
        table = self.query_one("#keyboard-table", DataTable)
        content_width = max(32, self.size.width - 12)
        area = min(16, max(8, content_width // 7))
        key = min(22, max(12, content_width // 4))
        widths = (area, max(8, content_width - area - 2 * key), key, key)
        if tuple(column.width for column in table.columns.values()) != widths:
            table.clear(columns=True)
            for label, width in zip(("Area", "Action", "Current", "Proposed"), widths, strict=True):
                table.add_column(label, width=width)

    def on_resize(self) -> None:
        self.set_class(self.size.height < 25, "compact")
        if self.snapshot is not None:
            self._render_bindings()

    def action_refresh(self) -> None:
        if self.staged:
            self._set_view_state(
                ViewState.READY, "Apply or Cancel staged changes before refreshing."
            )
            return
        self._begin_operation(
            "Loading keyboard assignments",
            lambda _c, _p: self.service.snapshot(),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        assert isinstance(result, KeyboardSnapshot)
        self.snapshot = result
        self.import_plan = None
        self.query_one("#keyboard-apply-import", Button).disabled = True
        self._render_bindings()
        self._set_view_state(
            ViewState.READY,
            result.warning
            or self._success_notice
            or "Changes stay local until Apply. Navigation remains fixed.",
        )
        self._success_notice = ""

    def _rows(self) -> list[tuple[str, str, str, str, str]]:
        if self.snapshot is None:
            return []
        rows = []
        labels = (
            "Open palette (primary)",
            "Open palette (secondary)",
            "Previous theme",
            "Next theme",
        )
        for identifier, label in zip(SHORTCUT_DEFAULTS, labels, strict=True):
            current = str(self.snapshot.effective[identifier])
            proposed = str(self.staged.get(identifier, self.snapshot.configured[identifier]))
            rows.append((identifier, "Global", label, current, proposed))
        proposed_map = self.staged.get(KEYBOARD_SETTING, self.snapshot.configured[KEYBOARD_SETTING])
        effective_map = self.snapshot.effective[KEYBOARD_SETTING]
        for row in self.definitions.values():
            rows.append(
                (
                    row.binding_id,
                    row.scope,
                    (row.description or row.action.replace("_", " "))
                    + " · "
                    + row.context.rsplit(".", 1)[-1],
                    effective_map.get(row.binding_id, row.default_key),
                    proposed_map.get(row.binding_id, row.default_key),
                )
            )
        return rows

    def _render_bindings(self) -> None:
        self._columns()
        table = self.query_one("#keyboard-table", DataTable)
        scope = self.query_one("#keyboard-scope", Select).value
        search = self.query_one("#keyboard-search", Input).value.casefold().strip()
        visible = [
            row
            for row in self._rows()
            if (not scope or row[1] == scope) and search in " ".join(row).casefold()
        ]
        table.clear()
        for identifier, area, action, current, proposed in visible:
            table.add_row(
                area,
                clean_cell(action),
                current or "Disabled",
                proposed or "Disabled",
                key=identifier,
            )
        if self.selected not in {row[0] for row in visible}:
            self.selected = visible[0][0] if visible else None
        if self.selected:
            table.move_cursor(
                row=next(i for i, row in enumerate(visible) if row[0] == self.selected)
            )
        self._detail()
        self.query_one("#keyboard-apply", Button).disabled = not self.staged
        self.query_one("#keyboard-cancel", Button).disabled = not self.staged

    def _setting_id(self) -> str:
        return self.selected if self.selected in SHORTCUT_DEFAULTS else KEYBOARD_SETTING

    def _detail(self) -> None:
        row = next((row for row in self._rows() if row[0] == self.selected), None)
        editable = bool(row and self.snapshot and self._setting_id() not in self.snapshot.blocked)
        self.query_one("#keyboard-stage", Button).disabled = not editable
        self.query_one("#keyboard-reset-key", Button).disabled = not editable
        self.query_one("#keyboard-key", Input).disabled = not editable
        if row:
            identifier = row[0]
            default = SHORTCUT_DEFAULTS.get(identifier)
            if default is None:
                default = self.definitions[identifier].default_key
            self.query_one("#keyboard-key", Input).value = row[4]
            reason = self.snapshot.blocked.get(self._setting_id(), "") if self.snapshot else ""
            self.query_one("#keyboard-detail", Static).update(
                clean_cell(
                    f"{row[2]}\nDefault: {default} · Current: {row[3] or 'Disabled'} · "
                    f"Proposed: {row[4] or 'Disabled'}\n{reason}",
                    max_length=500,
                )
            )

    @on(DataTable.RowHighlighted, "#keyboard-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if current_row_highlight(event):
            self.selected = str(event.row_key.value)
            self._detail()

    @on(Select.Changed, "#keyboard-scope")
    @on(Input.Changed, "#keyboard-search")
    def _filter_changed(self) -> None:
        if self.is_mounted:
            self._render_bindings()

    def action_focus_search(self) -> None:
        self.query_one("#keyboard-search", Input).focus()

    def action_stage(self) -> None:
        if self.snapshot is None or self.selected is None or self._guard_pending_mutation():
            return
        identifier = self._setting_id()
        if identifier in self.snapshot.blocked:
            self._show_error(
                "Stage shortcut", ConfigurationError(self.snapshot.blocked[identifier])
            )
            return
        raw = self.query_one("#keyboard-key", Input).value
        value: Any = raw
        if identifier == KEYBOARD_SETTING:
            value = copy.deepcopy(self.staged.get(identifier, self.snapshot.configured[identifier]))
            value[self.selected] = raw
        try:
            self.staged[identifier] = self.service.parse(identifier, value)
        except Exception as exc:
            self._show_error("Stage shortcut", exc)
            return
        self.import_plan = None
        self.query_one("#keyboard-apply-import", Button).disabled = True
        self._render_bindings()
        self._set_view_state(
            ViewState.READY, "Staged. Apply checks every assignment for conflicts."
        )

    def action_cancel_changes(self) -> None:
        if self._guard_pending_mutation():
            return
        self.staged.clear()
        self.import_plan = None
        self.query_one("#keyboard-apply-import", Button).disabled = True
        self._render_bindings()
        self._set_view_state(ViewState.READY, "Staged changes canceled; saved keys are unchanged.")

    def action_apply(self) -> None:
        if self.snapshot is None or not self.staged:
            return
        if self.import_plan is not None:
            self.action_apply_import()
            return
        changes = copy.deepcopy(self.staged)
        revision = self.snapshot.revision
        self._begin_operation(
            "Applying keyboard assignments",
            lambda _c, _p: self.service.apply(changes, revision=revision),
            self._applied,
        )

    def _applied(self, result: object) -> None:
        assert isinstance(result, tuple)
        self.staged.clear()
        self.on_applied(result)
        self._success_notice = "Keyboard assignments saved; current keys refreshed."
        self.action_refresh()

    def action_reset_key(self) -> None:
        if self.snapshot is None or self.selected is None or self._guard_pending_mutation():
            return
        self.import_plan = None
        self.query_one("#keyboard-apply-import", Button).disabled = True
        identifier = self._setting_id()
        if identifier in self.snapshot.blocked:
            return
        if identifier == KEYBOARD_SETTING:
            mapping = copy.deepcopy(
                self.staged.get(identifier, self.snapshot.configured[identifier])
            )
            mapping.pop(self.selected, None)
            self.staged[identifier] = mapping
        else:
            self.staged[identifier] = SHORTCUT_DEFAULTS[identifier]
        self._render_bindings()
        self._set_view_state(ViewState.READY, "Default staged; Apply or Cancel.")

    def action_reset_all(self) -> None:
        if self.snapshot is None or self._guard_pending_mutation():
            return
        snapshot = self.snapshot

        def stage_defaults() -> None:
            self.import_plan = None
            self.query_one("#keyboard-apply-import", Button).disabled = True
            self.staged.update(
                {
                    identifier: {}
                    if identifier == KEYBOARD_SETTING
                    else SHORTCUT_DEFAULTS[identifier]
                    for identifier in KEYBOARD_SETTING_IDS
                    if identifier not in snapshot.blocked
                }
            )
            self._render_bindings()
            self._set_view_state(ViewState.READY, "All unlocked defaults staged; Apply or Cancel.")

        self._confirm(
            "Reset keyboard assignments",
            "Stage defaults for all application action keys "
            "and four global keys? Locked overrides stay active. Apply remains separate.",
            stage_defaults,
        )

    def _path(self) -> Path | None:
        raw = self.query_one("#keyboard-path", Input).value.strip()
        try:
            if not raw or "\x00" in raw:
                raise ValueError("Missing or invalid path")
            path = Path(raw).expanduser().resolve()
            if path.exists() and not path.is_file():
                raise ValueError("Expected a file")
            return path
        except (OSError, ValueError, RuntimeError):
            self._show_error(
                "Keyboard file",
                ConfigurationError("Choose a valid file path for keyboard export or import."),
            )
            return None

    def action_export(self) -> None:
        if self._guard_pending_mutation():
            return
        if self.staged:
            self._set_view_state(
                ViewState.READY, "Apply or Cancel first; export includes saved keys."
            )
            return
        path = self._path()
        if path is None:
            return
        overwrite = path.exists()

        def export() -> None:
            self._begin_operation(
                "Exporting keyboard assignments",
                lambda _c, _p: self.service.export(path, overwrite=overwrite),
                lambda _r: self._set_view_state(
                    ViewState.READY, "Saved all keyboard assignments; no other settings."
                ),
            )

        if overwrite:
            self._confirm("Replace keyboard export", f"Replace exactly this file?\n{path}", export)
        else:
            export()

    def action_preview_import(self) -> None:
        if self._guard_pending_mutation():
            return
        if self.staged:
            self._set_view_state(ViewState.READY, "Apply or Cancel staged changes before import.")
            return
        path = self._path()
        if path is None:
            return
        self._begin_operation(
            "Previewing keyboard import",
            lambda _c, _p: self.service.preview_import_state(path),
            self._import_loaded,
            retry_safe=True,
        )

    def _import_loaded(self, result: object) -> None:
        assert isinstance(result, tuple)
        snapshot, plan = result
        assert isinstance(snapshot, KeyboardSnapshot) and isinstance(plan, SettingsImportPlan)
        self.snapshot = snapshot
        self.import_plan = plan
        self.staged = {change.setting_id: change.after for change in plan.changes}
        self.query_one("#keyboard-apply-import", Button).disabled = not plan.changes
        self._render_bindings()
        self._set_view_state(
            ViewState.READY, "Import preview in Proposed column; saved keys unchanged."
        )

    def action_apply_import(self) -> None:
        plan = self.import_plan
        if plan is None:
            return
        self._confirm(
            "Apply keyboard import",
            "Apply the reviewed Proposed assignments, including global keys? "
            "Only keyboard preferences change; mailbox data is unaffected.",
            lambda: self._begin_operation(
                "Applying keyboard import",
                lambda _c, _p: self.service.apply_import(plan),
                self._applied,
            ),
        )

    def action_close(self) -> None:
        if self._guard_pending_mutation():
            return
        if self.staged:
            self._confirm(
                "Discard staged keys",
                "Close and discard unapplied keyboard changes?",
                lambda: super(KeyboardEditorScreen, self).action_close(),
            )
        else:
            super().action_close()

    @on(Button.Pressed)
    def _buttons(self, event: Button.Pressed) -> None:
        actions = {
            "keyboard-stage": self.action_stage,
            "keyboard-apply": self.action_apply,
            "keyboard-cancel": self.action_cancel_changes,
            "keyboard-reset-key": self.action_reset_key,
            "keyboard-reset-all": self.action_reset_all,
            "keyboard-export": self.action_export,
            "keyboard-import": self.action_preview_import,
            "keyboard-apply-import": self.action_apply_import,
            "keyboard-refresh": self.action_refresh,
        }
        action = actions.get(event.button.id or "")
        if action:
            event.stop()
            action()
