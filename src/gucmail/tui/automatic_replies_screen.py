"""Read, edit and confirm automatic replies through the shared policy service."""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal
from gucmail.services.automatic_replies import (
    AutomaticReplyPlan,
    AutomaticReplyService,
    AutomaticReplySettings,
    AutomaticReplyState,
    ExternalReplyAudience,
    parse_reply_time,
    validate_automatic_replies,
)
from gucmail.tui.area_common import OperationalScreen, PublicOperationError, ViewState
from gucmail.tui.literal_select import LiteralSelect as Select


class AutomaticReplyEditor(ModalScreen[AutomaticReplySettings | None]):
    DEFAULT_CSS = """
    AutomaticReplyEditor { align: center middle; }
    #automatic-reply-editor { width: 94%; max-width: 100; height: 95%; max-height: 40;
        background: $surface; border: round $border; padding: 1 2; }
    #automatic-reply-fields { height: 1fr; }
    #automatic-reply-fields TextArea { height: 8; }
    #automatic-reply-editor-actions { height: 3; }
    #automatic-reply-editor-error { height: auto; color: $error; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "review", "Review"),
    ]

    def __init__(self, current: AutomaticReplySettings) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="automatic-reply-editor"):
            yield Label("Automatic replies · review before applying")
            with VerticalScroll(id="automatic-reply-fields"):
                yield Label("State")
                yield Select(
                    [(state.value.title(), state.value) for state in AutomaticReplyState],
                    value=self.current.state.value,
                    allow_blank=False,
                    id="oof-state",
                )
                yield Label("External senders")
                yield Select(
                    [
                        ("No external replies", "none"),
                        ("Known external senders", "known"),
                        ("All external senders", "all"),
                    ],
                    value=self.current.external_audience.value,
                    allow_blank=False,
                    id="oof-audience",
                )
                yield Label("Scheduled start/end (ISO date, time and UTC offset)")
                yield Input(
                    value=self.current.start.isoformat() if self.current.start else "",
                    id="oof-start",
                    placeholder="2026-10-02T09:00:00+03:00",
                )
                yield Input(
                    value=self.current.end.isoformat() if self.current.end else "",
                    id="oof-end",
                    placeholder="2026-10-05T17:00:00+03:00",
                )
                yield Label("Internal reply")
                yield TextArea(
                    sanitize_terminal(self.current.internal_reply, max_length=131072),
                    id="oof-internal",
                )
                yield Label("External reply")
                yield TextArea(
                    sanitize_terminal(self.current.external_reply, max_length=131072),
                    id="oof-external",
                )
            yield Static("", id="automatic-reply-editor-error", markup=False)
            with Horizontal(id="automatic-reply-editor-actions"):
                yield Button("Cancel", id="oof-cancel")
                yield Button("Review", id="oof-review", variant="primary")

    def value(self) -> AutomaticReplySettings:
        changes = {
            name: text
            for name, text in (
                ("internal_reply", self.query_one("#oof-internal", TextArea).text),
                ("external_reply", self.query_one("#oof-external", TextArea).text),
            )
            if text != sanitize_terminal(getattr(self.current, name), max_length=131072)
        }
        return validate_automatic_replies(
            replace(
                self.current,
                state=AutomaticReplyState(str(self.query_one("#oof-state", Select).value)),
                external_audience=ExternalReplyAudience(
                    str(self.query_one("#oof-audience", Select).value)
                ),
                start=parse_reply_time(self.query_one("#oof-start", Input).value),
                end=parse_reply_time(self.query_one("#oof-end", Input).value),
                **changes,
            )
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "oof-review":
            self.action_review()
        elif event.button.id == "oof-cancel":
            self.action_cancel()

    def action_review(self) -> None:
        try:
            value = self.value()
        except (ConfigurationError, ValueError) as exc:
            self.query_one("#automatic-reply-editor-error", Static).update(
                sanitize_terminal(str(exc), max_length=700)
            )
        else:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AutomaticRepliesScreen(OperationalScreen):
    DEFAULT_CSS = """
    AutomaticRepliesScreen { layout: vertical; }
    #automatic-reply-details { height: 1fr; padding: 1 2; }
    #automatic-reply-toolbar { height: 3; }
    """

    def __init__(self, service: AutomaticReplyService) -> None:
        super().__init__(area_name="automatic-replies")
        self.service = service
        self.current: AutomaticReplySettings | None = None

    def compose(self) -> ComposeResult:
        yield Label("Automatic replies", classes="workspace-heading")
        yield Static("", id="area-state", markup=False)
        with VerticalScroll(id="automatic-reply-details"):
            yield Static("Loading the current Exchange policy…", id="oof-details", markup=False)
        yield Static("", id="area-progress", markup=False)
        with Horizontal(id="automatic-reply-toolbar"):
            yield Button("Refresh", id="oof-refresh")
            yield Button("Edit policy", id="oof-edit", disabled=True)
            yield Button("Retry", id="area-retry")
            yield Button("Cancel operation", id="area-cancel-operation")
            yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry").display = False
        self.query_one("#area-cancel-operation").display = False
        self.action_refresh()

    def action_refresh(self) -> None:
        self._begin_operation(
            "Read automatic replies", lambda *_: self.service.get(), self._loaded, retry_safe=True
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, AutomaticReplySettings):
            raise PublicOperationError("Exchange returned an invalid automatic-reply policy.")
        self.current = result
        summary = (
            f"State: {result.state.value}\nExternal audience: {result.external_audience.value}\n"
            f"Start: {result.start or '—'}\nEnd: {result.end or '—'}\n\n"
            f"Internal reply:\n{result.internal_reply}\n\nExternal reply:\n{result.external_reply}"
        )
        self.query_one("#oof-details", Static).update(sanitize_terminal(summary, max_length=270000))
        self.query_one("#oof-edit", Button).disabled = False
        self._set_view_state(ViewState.READY, "Policy loaded. Reading it does not enable replies.")

    def action_edit(self) -> None:
        if self.current is None:
            return
        current = self.current
        self.app.push_screen(
            AutomaticReplyEditor(current), lambda value: self._review(current, value)
        )

    def _review(
        self, current: AutomaticReplySettings, value: AutomaticReplySettings | None
    ) -> None:
        if value is None:
            return
        plan = self.service.plan(value, current=current)
        self._confirm(
            "Apply automatic replies?",
            f"State: {value.state.value}\nExternal audience: {value.external_audience.value}\n"
            f"Start: {value.start or '—'}\nEnd: {value.end or '—'}\n"
            "Incoming mail may receive the replies reviewed in the editor. "
            "Exchange settings are checked again before applying.",
            lambda: self._apply(plan),
            confirm_label="Apply policy",
        )

    def _apply(self, plan: AutomaticReplyPlan) -> None:
        self._begin_operation(
            "Apply automatic replies",
            lambda *_: self.service.apply(plan, confirmed=True),
            self._loaded,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "oof-refresh":
            self.action_refresh()
        elif event.button.id == "oof-edit":
            self.action_edit()
