"""Inspect and review native Exchange policies with an ordinary simple-rule form."""

from __future__ import annotations

import json
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.errors import GUCmailError
from gucmail.models import to_primitive
from gucmail.security import sanitize_terminal
from gucmail.services.inbox_rules import (
    InboxRule,
    InboxRuleOperation,
    InboxRulePlan,
    InboxRuleService,
    SimpleInboxRule,
    validate_simple_rule,
)
from gucmail.tui.area_common import (
    OperationalScreen,
    PublicOperationError,
    ViewState,
    current_row_highlight,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class SimpleInboxRuleEditor(ModalScreen[SimpleInboxRule | None]):
    DEFAULT_CSS = """
    SimpleInboxRuleEditor { align: center middle; }
    #inbox-rule-editor { width: 94%; max-width: 100; height: 95%; max-height: 40;
        background: $surface; border: round $border; padding: 1 2; }
    #inbox-rule-fields { height: 1fr; }
    #inbox-rule-fields TextArea { height: 4; }
    #inbox-rule-editor-actions { height: 3; }
    #inbox-rule-editor-error { height: auto; color: $error; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "review", "Review"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox-rule-editor"):
            yield Label("New Exchange inbox rule · created disabled")
            with VerticalScroll(id="inbox-rule-fields"):
                yield Label("Name")
                yield Input(id="inbox-rule-name")
                yield Label("Priority (0-10000; lower runs earlier)")
                yield Input(value="0", type="integer", id="inbox-rule-priority")
                yield Label("Sender contains (one alternative per line)")
                yield TextArea(id="inbox-rule-senders")
                yield Label("Subject contains (one alternative per line)")
                yield TextArea(id="inbox-rule-subjects")
                yield Label("Attachments · different conditions are combined with AND")
                yield Select(
                    [("Any", "any"), ("Has attachments", "true"), ("No attachments", "false")],
                    value="any",
                    allow_blank=False,
                    id="inbox-rule-attachments",
                )
                yield Label("Assign categories (one per line)")
                yield TextArea(id="inbox-rule-categories")
                yield Label("Move to mailbox folder (optional path/reference)")
                yield Input(id="inbox-rule-folder")
                yield Checkbox("Mark incoming matching mail read", id="inbox-rule-mark-read")
                yield Checkbox("Stop processing later rules", id="inbox-rule-stop")
            yield Static("", id="inbox-rule-editor-error", markup=False)
            with HorizontalScroll(id="inbox-rule-editor-actions"):
                yield Button("Cancel", id="inbox-rule-cancel")
                yield Button("Review disabled rule", id="inbox-rule-review", variant="primary")

    def value(self) -> SimpleInboxRule:
        def lines(identifier: str) -> tuple[str, ...]:
            return tuple(
                value.strip()
                for value in self.query_one(identifier, TextArea).text.splitlines()
                if value.strip()
            )

        attachment = str(self.query_one("#inbox-rule-attachments", Select).value)
        return validate_simple_rule(
            SimpleInboxRule(
                name=self.query_one("#inbox-rule-name", Input).value.strip(),
                priority=int(self.query_one("#inbox-rule-priority", Input).value),
                sender_contains=lines("#inbox-rule-senders"),
                subject_contains=lines("#inbox-rule-subjects"),
                has_attachments=None if attachment == "any" else attachment == "true",
                categories=lines("#inbox-rule-categories"),
                mark_read=self.query_one("#inbox-rule-mark-read", Checkbox).value,
                move_to_folder=self.query_one("#inbox-rule-folder", Input).value.strip() or None,
                stop_processing=self.query_one("#inbox-rule-stop", Checkbox).value,
            )
        )

    def action_review(self) -> None:
        try:
            value = self.value()
        except (GUCmailError, ValueError) as exc:
            self.query_one("#inbox-rule-editor-error", Static).update(
                sanitize_terminal(str(exc), max_length=700)
            )
        else:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "inbox-rule-review":
            self.action_review()
        elif event.button.id == "inbox-rule-cancel":
            self.action_cancel()


class InboxRulesScreen(OperationalScreen):
    DEFAULT_CSS = """
    InboxRulesScreen { layout: vertical; }
    #inbox-rules-table { height: 2fr; min-height: 4; }
    #inbox-rules-details { height: 3fr; min-height: 4; padding: 0 1; }
    #inbox-rules-toolbar { height: 3; }
    """

    def __init__(self, service: InboxRuleService) -> None:
        super().__init__(area_name="inbox-rules")
        self.service = service
        self.rules: dict[str, InboxRule] = {}
        self.selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Label(
            "Exchange inbox rules · Outlook rule data is preserved", classes="workspace-heading"
        )
        yield Static("", id="area-state", markup=False)
        yield DataTable(id="inbox-rules-table", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="inbox-rules-details"):
            yield Static(
                "Select a policy to inspect its conditions and actions.",
                id="inbox-rule-details",
                markup=False,
            )
        yield Static("", id="area-progress", markup=False)
        with HorizontalScroll(id="inbox-rules-toolbar"):
            yield Button("Refresh", id="inbox-rules-refresh")
            yield Button("Create disabled rule", id="inbox-rules-create")
            yield Button("Enable / disable", id="inbox-rules-toggle", disabled=True)
            yield Button("Delete", id="inbox-rules-delete", disabled=True)
            yield Button("Retry", id="area-retry")
            yield Button("Cancel operation", id="area-cancel-operation")
            yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry").display = False
        self.query_one("#area-cancel-operation").display = False
        self.query_one("#inbox-rules-table", DataTable).add_columns(
            "State", "Priority", "Name", "Support"
        )
        self.action_refresh()

    def action_refresh(self) -> None:
        self._begin_operation(
            "Read Exchange rules",
            lambda *_: self.service.list(limit=1000),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or not all(isinstance(rule, InboxRule) for rule in result):
            raise PublicOperationError("Exchange returned an invalid rule list.")
        self.rules = {rule.reference: rule for rule in result}
        table = self.query_one("#inbox-rules-table", DataTable)
        table.clear()
        for rule in result:
            table.add_row(
                "On" if rule.enabled else "Off",
                str(rule.priority),
                Text(sanitize_terminal(rule.name, max_length=255)),
                "Unsupported" if rule.unsupported else "Error" if rule.in_error else "Supported",
                key=rule.reference,
            )
        self.selected = (
            self.selected if self.selected in self.rules else next(iter(self.rules), None)
        )
        if self.selected:
            table.move_cursor(row=table.get_row_index(self.selected))
        self._show_selected()
        self._set_view_state(
            ViewState.READY, f"{len(result)} Exchange rules loaded. New rules start disabled."
        )

    @on(DataTable.RowHighlighted, "#inbox-rules-table")
    def _selection(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self.selected = str(event.row_key.value)
        self._show_selected()

    def _show_selected(self) -> None:
        current = self.rules.get(self.selected or "")
        self.query_one("#inbox-rules-toggle", Button).disabled = (
            current is None or current.unsupported or current.in_error
        )
        self.query_one("#inbox-rules-delete", Button).disabled = current is None
        self.query_one("#inbox-rule-details", Static).update(
            sanitize_terminal(
                json.dumps(to_primitive(current), ensure_ascii=False, indent=2), max_length=262144
            )
            if current
            else "No Exchange inbox rules."
        )

    def action_create(self) -> None:
        self.app.push_screen(SimpleInboxRuleEditor(), self._create_selected)

    def _create_selected(self, value: SimpleInboxRule | None) -> None:
        if value is not None:
            self._begin_operation(
                "Preview new inbox rule",
                lambda *_: self.service.plan_create(value),
                self._review,
                retry_safe=True,
            )

    def action_toggle_rule(self) -> None:
        current = self.rules.get(self.selected or "")
        if current is not None:
            try:
                plan = self.service.plan(
                    current.reference,
                    InboxRuleOperation.DISABLE if current.enabled else InboxRuleOperation.ENABLE,
                    current=current,
                )
            except GUCmailError as exc:
                self._set_view_state(ViewState.ERROR, sanitize_terminal(str(exc)))
            else:
                self._review(plan)

    def action_delete(self) -> None:
        current = self.rules.get(self.selected or "")
        if current is not None:
            self._review(
                self.service.plan(current.reference, InboxRuleOperation.DELETE, current=current)
            )

    def _review(self, result: object) -> None:
        if not isinstance(result, InboxRulePlan):
            raise PublicOperationError("Invalid inbox-rule preview.")
        details = json.dumps(to_primitive(result), ensure_ascii=False, indent=2)
        self._confirm(
            f"{result.operation.value.title()} Exchange inbox rule?",
            sanitize_terminal(details, max_length=262144)
            + "\nEnabled rules can change incoming mail. "
            "Outlook rule data is preserved. The policy is checked again before applying.",
            lambda: self._apply(result),
            confirm_label=f"{result.operation.value.title()} rule",
        )

    def _apply(self, plan: InboxRulePlan) -> None:
        self._begin_operation(
            "Update Exchange inbox rule",
            lambda *_: self.service.apply(plan, confirmed=True),
            lambda _: self.action_refresh(),
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        actions = {
            "inbox-rules-refresh": self.action_refresh,
            "inbox-rules-create": self.action_create,
            "inbox-rules-toggle": self.action_toggle_rule,
            "inbox-rules-delete": self.action_delete,
        }
        if event.button.id in actions:
            actions[event.button.id]()
