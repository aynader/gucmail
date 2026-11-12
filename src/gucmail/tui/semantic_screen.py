"""Keyboard-accessible consent and lifecycle controls for private topic search."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, Static

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal
from gucmail.services.semantic_search import SemanticSearchService
from gucmail.tui.area_common import ConfirmationScreen, OperationalScreen, ViewState, human_bytes


class SemanticScreen(OperationalScreen):
    DEFAULT_CSS = """
    SemanticScreen { layout: vertical; }
    #semantic-root { height: 1fr; padding: 1 2; }
    #semantic-root Input { height: 3; }
    #semantic-policy, #semantic-details { height: auto; padding: 1 0; }
    #semantic-actions { grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 3;
        height: 9; margin-top: 1; }
    #semantic-actions Button { width: 1fr; min-width: 0; }
    """

    def __init__(
        self,
        service: SemanticSearchService,
        *,
        set_enabled: Callable[[bool], bool],
    ) -> None:
        super().__init__(area_name="semantic", offline=True, operation_timeout_seconds=90)
        self.service = service
        self.set_enabled = set_enabled
        self._preview: dict[str, Any] | None = None
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Label("Local topic search · setup", classes="workspace-heading")
        yield Static("Loading local model status…", id="area-state", markup=False)
        with VerticalScroll(id="semantic-root"):
            yield Static(
                "Optional local-lsa-v1 learns topics from existing indexed text. It saves "
                "vocabulary, title/folder metadata and embeddings on this computer. "
                "Derived data reveals content. No mailbox fetch, model download or remote "
                "embedding service. Topic quality depends on local word co-occurrence; "
                "this is not a pretrained language model.",
                id="semantic-policy",
                markup=False,
            )
            yield Label("Latest local records (2-2000; default 500)")
            yield Input("500", type="integer", id="semantic-limit")
            yield Label("Maximum topic dimensions (1-96; default 32)")
            yield Input("32", type="integer", id="semantic-dimensions")
            yield Checkbox("Include already indexed body chunks", id="semantic-body")
            yield Checkbox(
                "Retrain the model instead of updating its embeddings", id="semantic-rebuild"
            )
            yield Static("", id="semantic-details", markup=False)
            with Grid(id="semantic-actions"):
                yield Button("Enable…", id="semantic-enable")
                yield Button("Disable", id="semantic-disable")
                yield Button("Preview", id="semantic-preview")
                yield Button("Build / update…", id="semantic-build", disabled=True)
                yield Button("Clear model…", id="semantic-clear")
                yield Button("Refresh", id="semantic-refresh")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Retry", id="area-retry")
                yield Button("Close", classes="area-close")
        yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        self.query_one("#area-retry").display = False
        self.query_one("#area-cancel-operation").display = False
        self.action_refresh()

    def _show(self, value: object) -> None:
        data = value if isinstance(value, dict) else {}
        if "cleared" in data:
            rendered = (
                f"Cleared {data['cleared']} local semantic records and the model.\n"
                "Lexical index, mailbox and backups retained.\n" + str(data.get("note", ""))
            )
        elif "selected_documents" in data:
            rendered = (
                f"Build preview · local-lsa-v1\nSelected {data['selected_documents']} of "
                f"{data['total_local_documents']} local records (limit {data['limit']}).\n"
                f"Coverage truncated: {data['truncated']}\n"
                f"Include indexed bodies: {data['include_body']}\n"
                f"Maximum topics: {data['requested_dimensions']} · Retrain: {data['rebuild']}\n\n"
                f"Privacy\n{data['privacy']}\n\nCost and limits\n{data['bounds']}\n\n"
                f"Ranking limits\n{data['quality_limit']}\n\n"
                "Review these choices before Build / update."
            )
        else:
            rendered = (
                f"Local topic model · Enabled: {bool(data.get('enabled'))}\n"
                f"Built: {bool(data.get('built'))} · Records: {data.get('documents', 0)}\n"
                f"Used topics: {data.get('dimensions', 0)} · "
                f"Indexed bodies: {bool(data.get('include_body'))}\n"
                f"Local model storage: {human_bytes(int(data.get('model_bytes', 0)))} · "
                f"Database: {human_bytes(int(data.get('database_bytes', 0)))}\n"
                f"Last update: {data.get('updated_at') or 'none'}\n"
            )
            if "fitting_dependency_available" in data:
                rendered += (
                    f"Fitting dependency available: {data['fitting_dependency_available']}\n"
                    "Existing model queries do not require that dependency.\n"
                )
            if data.get("stale"):
                rendered += "Selected indexed text changed. Preview an update before searching.\n"
            if data.get("truncated"):
                rendered += "Coverage is limited to the selected latest local records.\n"
            if "updated_documents" in data:
                rendered += (
                    f"Updated: {data['updated_documents']} · Unchanged: "
                    f"{data['unchanged_documents']} · Removed: {data['removed_documents']}\n"
                    f"Retrained: {data['retrained']}\n"
                )
            if data.get("note"):
                rendered += str(data["note"])
        self.query_one("#semantic-details", Static).update(
            sanitize_terminal(rendered, max_length=16_384)
        )

    def _loaded(self, value: object) -> None:
        status = dict(value) if isinstance(value, dict) else {}
        if not self._ready and status.get("built"):
            self.query_one("#semantic-limit", Input).value = str(status["source_limit"])
            self.query_one("#semantic-dimensions", Input).value = str(
                status["requested_dimensions"]
            )
            self.query_one("#semantic-body", Checkbox).value = bool(status["include_body"])
        self._ready = True
        self._show(status)

    def action_refresh(self) -> None:
        self._preview = None
        if self.is_mounted:
            self.query_one("#semantic-build", Button).disabled = True
        self._begin_operation(
            "Local model status",
            lambda cancel, progress: self.service.status(),
            self._loaded,
            retry_safe=True,
        )

    def _options(self) -> dict[str, Any]:
        try:
            limit = int(self.query_one("#semantic-limit", Input).value)
            dimensions = int(self.query_one("#semantic-dimensions", Input).value)
        except ValueError as exc:
            raise ConfigurationError("Enter whole numbers for records and dimensions.") from exc
        if not 2 <= limit <= 2000 or not 1 <= dimensions <= 96:
            raise ConfigurationError("Use 2-2000 records and 1-96 dimensions.")
        return {
            "limit": limit,
            "dimensions": dimensions,
            "include_body": self.query_one("#semantic-body", Checkbox).value,
            "rebuild": self.query_one("#semantic-rebuild", Checkbox).value,
        }

    @on(Input.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._preview = None
        self.query_one("#semantic-build", Button).disabled = True

    def _previewed(self, value: object) -> None:
        self._preview = dict(value) if isinstance(value, dict) else None
        if self._preview is not None:
            current = self._options()
            if any(
                self._preview[key] != current[option]
                for key, option in (
                    ("limit", "limit"),
                    ("requested_dimensions", "dimensions"),
                    ("include_body", "include_body"),
                    ("rebuild", "rebuild"),
                )
            ):
                self._preview = None
        self._show(value)
        self.query_one("#semantic-build", Button).disabled = not (
            self._preview and self._preview["enabled"] and self._preview["selected_documents"] >= 2
        )

    def _consent(self, enabled: bool) -> None:
        def done(value: object) -> None:
            self._preview = None
            self.query_one("#semantic-build", Button).disabled = True
            self.service.store.enabled = bool(value)
            self._show(
                {"enabled": bool(value), "note": "Preview the selected fields before building."}
            )

        self._begin_operation(
            "Local semantic consent",
            lambda cancel, progress: self.set_enabled(enabled),
            done,
            retry_safe=False,
        )

    def _build_confirmed(self, accepted: bool, options: dict[str, Any]) -> None:
        if not accepted or self._preview is None:
            return
        options["expected_source_digest"] = self._preview["source_digest"]
        self._preview = None
        self.query_one("#semantic-build", Button).disabled = True
        self._begin_operation(
            "Local model build",
            lambda cancel, progress: self.service.build(
                confirmed=True, cancelled=cancel.is_set, **options
            ),
            self._show,
            retry_safe=False,
            timeout_seconds=90,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        selected = event.button.id
        if selected not in {
            "semantic-enable",
            "semantic-disable",
            "semantic-preview",
            "semantic-build",
            "semantic-clear",
            "semantic-refresh",
        }:
            return
        event.stop()
        if self._guard_pending_mutation():
            return
        try:
            if selected == "semantic-refresh":
                self.action_refresh()
            elif selected == "semantic-preview":
                options = self._options()
                self._begin_operation(
                    "Local build preview",
                    lambda cancel, progress: self.service.preview(**options),
                    self._previewed,
                    retry_safe=True,
                )
            elif selected == "semantic-build" and self._preview is not None:
                options = self._options()
                self.app.push_screen(
                    ConfirmationScreen(
                        "Build local topic model?",
                        f"Selected records: {self._preview['selected_documents']}; "
                        f"indexed bodies: {options['include_body']}; "
                        f"retrain: {options['rebuild']}.\n{self._preview['privacy']}\n"
                        f"{self._preview['bounds']}",
                        confirm_label="Build locally",
                    ),
                    lambda accepted: self._build_confirmed(bool(accepted), options),
                )
            elif selected == "semantic-enable":
                self.app.push_screen(
                    ConfirmationScreen(
                        "Enable private topic search?",
                        "Enable local-lsa-v1. Builds save vocabulary, topic basis, title/folder "
                        "metadata and embeddings locally. Bodies require selection in the preview. "
                        "No model download or remote processing. This consent does not build data.",
                    ),
                    lambda accepted: self._consent(True) if accepted else None,
                )
            elif selected == "semantic-disable":
                self._consent(False)
            elif selected == "semantic-clear":
                self.app.push_screen(
                    ConfirmationScreen(
                        "Clear local topic data?",
                        "Remove semantic vocabulary, model and embeddings. Lexical index and "
                        "mailbox stay intact. Filesystem snapshots and forensic physical erasure "
                        "are outside this guarantee.",
                        confirm_label="Clear local data",
                    ),
                    self._clear_confirmed,
                )
        except ConfigurationError as exc:
            self._set_view_state(ViewState.ERROR, str(exc))

    def _clear_confirmed(self, accepted: bool | None) -> None:
        if accepted:
            self._preview = None
            self.query_one("#semantic-build", Button).disabled = True
            self._begin_operation(
                "Clear local semantic data",
                lambda cancel, progress: self.service.clear(confirmed=True),
                self._show,
                retry_safe=False,
            )
