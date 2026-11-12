"""Keyboard-accessible named layout save/load/remove over the shared Settings service."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static

from gucmail.errors import ConfigurationError
from gucmail.services.layout_presets import LayoutPresetCatalog, LayoutPresetService, validate_name
from gucmail.tui.area_common import OperationalScreen, ViewState, clean_cell, current_row_highlight
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class LayoutPresetsScreen(OperationalScreen):
    DEFAULT_CSS = """
    #layout-presets-root { width: 100%; height: 100%; padding: 1; }
    #layout-presets-title, #area-state, #area-progress { height: auto; }
    #layout-presets-table { height: 1fr; min-height: 5; }
    #layout-presets-name { height: 3; }
    #layout-presets-actions { height: 3; }
    #layout-presets-actions Button { width: 1fr; min-width: 0; margin-right: 1; }
    #layout-presets-detail { height: auto; max-height: 5; }
    """

    def __init__(
        self,
        service: LayoutPresetService,
        *,
        capture: Callable[[], dict[str, Any]],
        on_applied: Callable[[tuple[str, ...]], None],
    ) -> None:
        super().__init__(area_name="layout", offline=True)
        self.service = service
        self.capture = capture
        self.on_applied = on_applied
        self.catalog: LayoutPresetCatalog | None = None
        self.selected: str | None = None
        self.current: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="layout-presets-root"):
            yield Static("Saved layouts", id="layout-presets-title", markup=False)
            yield Static("Loading local layouts…", id="area-state", markup=False)
            yield Static("", id="area-progress", markup=False)
            yield DataTable(id="layout-presets-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Save the current arrangement with a name, or select a saved layout to load.",
                id="layout-presets-detail",
                markup=False,
            )
            yield Input(
                placeholder="Name for current layout (1-40 characters)", id="layout-presets-name"
            )
            with Horizontal(id="layout-presets-actions"):
                yield Button("Save", id="layout-presets-save", variant="primary")
                yield Button("Load", id="layout-presets-load")
                yield Button("Remove", id="layout-presets-delete")
                yield Button("Refresh", id="layout-presets-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#layout-presets-table", DataTable).add_columns(
            "Name", "Split", "Preview", "Sidebar"
        )
        self.action_refresh()

    def action_refresh(self) -> None:
        if self._guard_pending_mutation():
            return
        self.current = copy.deepcopy(self.capture())
        self._begin_operation(
            "Loading saved layouts",
            lambda _c, _p: self.service.catalog(),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        assert isinstance(result, LayoutPresetCatalog)
        self.catalog = result
        table = self.query_one("#layout-presets-table", DataTable)
        table.clear()
        for name, values in result.presets.items():
            table.add_row(
                clean_cell(name),
                str(values["layout.split_orientation"]),
                "Shown" if values["layout.preview_visible"] else "Hidden",
                "Shown" if values["layout.sidebar_visible"] else "Hidden",
                key=name,
            )
        self.selected = (
            self.selected if self.selected in result.presets else next(iter(result.presets), None)
        )
        if self.selected is not None:
            table.move_cursor(row=list(result.presets).index(self.selected))
        self._set_view_state(
            ViewState.READY, f"{len(result.presets)} saved layouts · local display preferences only"
        )
        self._detail()

    def _detail(self) -> None:
        if self.catalog is not None and self.selected is not None:
            values = self.catalog.presets[self.selected]
            text = (
                f"Selected: {self.selected} · "
                f"List {float(values['layout.message_list_ratio']):.0%} · "
                f"Sidebar {float(values['layout.sidebar_ratio']):.0%} · "
                f"Reader {values['layout.reader_mode']}\n"
                "Loading replaces layout preferences and preserves mailbox data, theme and account."
            )
        else:
            text = "No saved layout selected. Enter a name to save the current arrangement."
        self.query_one("#layout-presets-detail", Static).update(clean_cell(text, max_length=600))
        self.query_one("#layout-presets-load", Button).disabled = self.selected is None
        self.query_one("#layout-presets-delete", Button).disabled = self.selected is None

    @on(DataTable.RowHighlighted, "#layout-presets-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if current_row_highlight(event) and self.catalog is not None:
            name = str(event.row_key.value)
            if name in self.catalog.presets:
                self.selected = name
                self._detail()

    def action_save(self) -> None:
        if self.catalog is None or self._guard_pending_mutation():
            return
        try:
            name = validate_name(self.query_one("#layout-presets-name", Input).value)
        except ConfigurationError as exc:
            self._show_error("Save layout", exc)
            return
        catalog = self.catalog
        values = copy.deepcopy(self.capture())
        exists = any(key.casefold() == name.casefold() for key in catalog.presets)

        def save() -> None:
            self._begin_operation(
                "Saving layout",
                lambda _c, _p: self.service.save(
                    name, values, catalog=catalog, replace_existing=exists
                ),
                lambda _r: self.action_refresh(),
            )

        if exists:
            self._confirm(
                "Replace saved layout",
                f"Replace {clean_cell(name)} with the current arrangement?",
                save,
            )
        else:
            save()

    def action_load(self) -> None:
        if self.catalog is None or self.selected is None:
            return
        name, catalog = self.selected, self.catalog

        def loaded(result: object) -> None:
            assert isinstance(result, tuple)
            self.on_applied(result)
            self.action_refresh()

        self._begin_operation(
            "Loading selected layout",
            lambda _c, _p: self.service.load(name, catalog=catalog),
            loaded,
        )

    def action_delete(self) -> None:
        if self.catalog is None or self.selected is None:
            return
        name, catalog = self.selected, self.catalog
        self._confirm(
            "Remove saved layout",
            f"Remove {clean_cell(name)} from saved layouts? The active arrangement stays as it is.",
            lambda: self._begin_operation(
                "Removing saved layout",
                lambda _c, _p: self.service.delete(name, catalog=catalog),
                lambda _r: self.action_refresh(),
            ),
        )

    @on(Button.Pressed, "#layout-presets-save")
    def _save_pressed(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#layout-presets-load")
    def _load_pressed(self) -> None:
        self.action_load()

    @on(Button.Pressed, "#layout-presets-delete")
    def _delete_pressed(self) -> None:
        self.action_delete()

    @on(Button.Pressed, "#layout-presets-refresh")
    def _refresh_pressed(self) -> None:
        self.action_refresh()
