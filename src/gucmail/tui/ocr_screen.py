"""Explicit local image selection, installed language review and OCR consent."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal
from gucmail.services.ocr import OcrPlan, OcrResult, OcrService
from gucmail.tui.area_common import ConfirmationScreen, OperationalScreen, ViewState
from gucmail.tui.literal_select import LiteralSelect as Select


class OcrScreen(OperationalScreen):
    DEFAULT_CSS = """
    OcrScreen { layout: vertical; }
    #ocr-scroll { height: 1fr; padding: 1 2; }
    #ocr-policy, #ocr-status, #ocr-preview { height: auto; padding: 1 0; }
    #ocr-scroll Input, #ocr-scroll Select { height: 3; }
    #ocr-actions { grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 3; height: 9; }
    #ocr-actions Button { width: 1fr; min-width: 0; }
    #ocr-result { height: 12; min-height: 6; }
    """

    def __init__(self, service: OcrService) -> None:
        super().__init__(area_name="ocr", offline=True, operation_timeout_seconds=25)
        self.service = service
        self._preview: OcrPlan | None = None
        self._choices: tuple[object, ...] = ("", Select.BLANK, "50000", "8")
        self._consent = False

    def compose(self) -> ComposeResult:
        yield Label("Local image OCR", classes="workspace-heading")
        yield Static("Inspecting installed OCR languages…", id="area-state", markup=False)
        with VerticalScroll(id="ocr-scroll"):
            yield Static(
                "Select an already saved PNG/JPEG. Recognition uses an installed Windows "
                "language on this computer. No mailbox fetch, cloud service, language "
                "download or automatic indexing. OCR can be inaccurate; review its text.",
                id="ocr-policy",
                markup=False,
            )
            yield Static("", id="ocr-status", markup=False)
            yield Label("Local image file")
            yield Input(placeholder="Path to a saved PNG or JPEG", id="ocr-path")
            yield Label("Installed recognition language")
            yield Select[str]([], prompt="Choose a language", id="ocr-language")
            yield Label("Maximum output characters (1-100000)")
            yield Input("50000", type="integer", id="ocr-max-chars")
            yield Label("Recognition timeout (1-15 seconds)")
            yield Input("8", type="number", id="ocr-timeout")
            yield Checkbox("Enable OCR for this selected image", id="ocr-enabled")
            yield Static("Preview an image before recognition.", id="ocr-preview", markup=False)
            with Grid(id="ocr-actions"):
                yield Button("Preview", id="ocr-preview-button")
                yield Button("Recognize…", id="ocr-recognize", disabled=True)
                yield Button("Refresh languages", id="ocr-refresh")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Retry", id="area-retry")
                yield Button("Close", classes="area-close")
            yield Label("Recognized text · ocr_attachment_text · review for accuracy")
            yield TextArea("", read_only=True, soft_wrap=True, id="ocr-result")
        yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        self.action_refresh()

    def _reset_preview(self) -> None:
        self._invalidate_operation()
        self._last_operation = None
        self._preview = None
        self.query_one("#ocr-result", TextArea).load_text("")
        self.query_one("#ocr-preview", Static).update("Preview the current image choices.")
        self.query_one("#ocr-recognize", Button).disabled = True

    @on(Input.Changed)
    @on(Select.Changed)
    def _choices_changed(self, event: Input.Changed | Select.Changed) -> None:
        if len(self.query(Input)) != 3 or not self.query("#ocr-result"):
            return
        choices = (
            self.query_one("#ocr-path", Input).value,
            self.query_one("#ocr-language", Select).value,
            self.query_one("#ocr-max-chars", Input).value,
            self.query_one("#ocr-timeout", Input).value,
        )
        if choices == self._choices:
            return
        self._choices = choices
        self._reset_preview()
        self.query_one("#ocr-enabled", Checkbox).value = False

    @on(Checkbox.Changed, "#ocr-enabled")
    def _consent_changed(self, event: Checkbox.Changed) -> None:
        if event.value == self._consent:
            return
        self._consent = event.value
        self._invalidate_operation()
        self._last_operation = None
        self.query_one("#ocr-result", TextArea).load_text("")
        self.query_one("#ocr-recognize", Button).disabled = not (
            event.value and self._preview is not None and self._preview.can_recognize
        )

    def action_refresh(self) -> None:
        self._reset_preview()
        self.query_one("#ocr-enabled", Checkbox).value = False
        self._begin_operation(
            "Inspect installed OCR languages",
            lambda cancel, _: self.service.status(cancel_event=cancel),
            self._show_status,
            retry_safe=True,
        )

    def _show_status(self, value: object) -> None:
        data = value if isinstance(value, dict) else {}
        languages = data.get("languages", [])
        language_options = [(tag, tag) for tag in languages if isinstance(tag, str)]
        self.query_one("#ocr-language", Select).set_options(language_options)
        self.query_one("#ocr-status", Static).update(
            "Installed Windows OCR languages: "
            + (
                ", ".join(tag for tag, _ in language_options)
                if data.get("available")
                else "unavailable on this device. Ordinary text extraction remains available."
            )
        )

    def _show_preview(self, value: object) -> None:
        if not isinstance(value, OcrPlan):
            raise ConfigurationError("OCR preview is unavailable.")
        self._preview = value
        self.query_one("#ocr-preview", Static).update(
            sanitize_terminal(
                f"{value.path.name}\nLanguage: {value.language} · {value.width} x {value.height} "
                f"pixels · {value.input_bytes} bytes\nSHA-256: {value.sha256}\n"
                f"Time limit: {value.timeout_seconds:g}s · output: {value.max_output_chars} chars\n"
                f"Available for recognition: {value.can_recognize}\n"
                "8 MiB input, 2048 pixels per dimension, first image only. "
                "Temporary private copy removed afterward; no OS memory quota. "
                "Enable this operation, then review the confirmation before recognizing.",
                max_length=2_000,
            )
        )
        self.query_one("#ocr-recognize", Button).disabled = not (
            value.can_recognize and self.query_one("#ocr-enabled", Checkbox).value
        )

    @on(Button.Pressed, "#ocr-preview-button")
    def _preview_pressed(self) -> None:
        language = self.query_one("#ocr-language", Select).value
        try:
            if not isinstance(language, str):
                raise ConfigurationError("Choose an installed OCR language.")
            path = Path(self.query_one("#ocr-path", Input).value)
            maximum = int(self.query_one("#ocr-max-chars", Input).value)
            timeout = float(self.query_one("#ocr-timeout", Input).value)
        except (ConfigurationError, ValueError) as exc:
            self._set_view_state(
                ViewState.ERROR,
                str(exc)
                if isinstance(exc, ConfigurationError)
                else "Enter valid OCR timeout and output limits.",
            )
            return
        self._reset_preview()
        self._begin_operation(
            "Preview local image",
            lambda cancel, _: self.service.preview(
                path,
                language=language,
                timeout_seconds=timeout,
                max_output_chars=maximum,
                cancel_event=cancel,
            ),
            self._show_preview,
            retry_safe=True,
        )

    @on(Button.Pressed, "#ocr-recognize")
    def _recognize_pressed(self) -> None:
        plan = self._preview
        if plan is None or not self.query_one("#ocr-enabled", Checkbox).value:
            self._set_view_state(ViewState.ERROR, "Preview and explicitly enable this image first.")
            return

        def confirmed(accepted: bool | None) -> None:
            if not accepted:
                return
            if self._preview != plan or not self.query_one("#ocr-enabled", Checkbox).value:
                self._set_view_state(ViewState.ERROR, "OCR choices changed. Preview again.")
                return
            self._begin_operation(
                "Recognize selected image",
                lambda cancel, _: self.service.extract(plan, enabled=True, cancel_event=cancel),
                self._show_result,
                retry_safe=True,
            )

        self.app.push_screen(
            ConfirmationScreen(
                "Recognize this local image?",
                f"{plan.path.name}\nLanguage: {plan.language}\nSHA-256: {plan.sha256}\n"
                f"{plan.width} x {plan.height} pixels; {plan.input_bytes} bytes; "
                f"timeout {plan.timeout_seconds:g}s; "
                f"up to {plan.max_output_chars} output chars.\n\n"
                "An owned Windows process uses an already installed local recognizer. "
                "A temporary image copy is removed afterward. No mailbox fetch, remote "
                "service, download or automatic indexing. OCR may be inaccurate; review the text.",
                confirm_label="Recognize",
            ),
            confirmed,
        )

    def _show_result(self, value: object) -> None:
        if not isinstance(value, OcrResult):
            self._set_view_state(ViewState.ERROR, "The OCR result is unavailable.")
            return
        self.query_one("#ocr-result", TextArea).load_text(value.text)
        if value.status != "success":
            self._set_view_state(ViewState.ERROR, value.error or "Local OCR is unavailable.")
        else:
            self.query_one("#ocr-preview", Static).update(
                f"Recognized {len(value.text)} characters · Language: {value.language}\n"
                f"Truncated: {value.truncated} · Label: {value.label}\n"
                "Review the text for recognition errors. No mailbox or index was changed."
            )

    @on(Button.Pressed, "#ocr-refresh")
    def _refresh_pressed(self) -> None:
        self.action_refresh()
