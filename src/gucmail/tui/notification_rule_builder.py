"""Keyboard-accessible condition tree and ordered action forms for notification rules."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea, Tree
from textual.widgets.tree import TreeNode

from gucmail.notification_rules import (
    BooleanField,
    ChoiceField,
    RuleActionKind,
    TextField,
    TextOperator,
    action_from_data,
    action_to_data,
    condition_from_data,
    condition_to_data,
)
from gucmail.security import sanitize_terminal
from gucmail.services.notification_rule_editing import (
    ConditionPath,
    condition_at,
    edit_condition_tree,
)
from gucmail.tui.area_common import current_row_highlight
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


def _label(value: object) -> str:
    return sanitize_terminal(str(value), max_length=160)


class RuleValueForm(ModalScreen[dict[str, Any] | None]):
    """One typed predicate or action, with no serialization syntax to learn."""

    DEFAULT_CSS = """
    RuleValueForm { align: center middle; }
    #rule-value-dialog { width: 94%; max-width: 78; height: 94%; max-height: 38;
        background: $surface; border: round $border; padding: 1 2; }
    #rule-value-fields { height: 1fr; }
    #rule-value-values { height: 5; }
    .rule-value-field { height: auto; }
    #rule-value-buttons { height: 3; }
    #rule-value-error { height: auto; color: $error; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Use value"),
    ]

    def __init__(
        self,
        value: dict[str, Any] | None = None,
        *,
        action: bool = False,
        provider_id: str = "desktop",
    ) -> None:
        super().__init__()
        self.value = value or {}
        self.is_action = action
        self.provider_id = provider_id

    def compose(self) -> ComposeResult:
        value = self.value
        kinds = (
            list(RuleActionKind)
            if self.is_action
            else [
                "text",
                "boolean",
                "choice",
                "size",
                "time",
                "weekday",
            ]
        )
        with Vertical(id="rule-value-dialog"):
            yield Label("Edit action" if self.is_action else "Edit condition", markup=False)
            with VerticalScroll(id="rule-value-fields"):
                yield Select(
                    [(str(kind).replace("_", " ").title(), str(kind)) for kind in kinds],
                    value=value.get("kind" if self.is_action else "type", str(kinds[0])),
                    allow_blank=False,
                    id="rule-value-kind",
                )
                fields = tuple(dict.fromkeys((*TextField, *BooleanField, *ChoiceField)))
                with Vertical(classes="rule-value-field", id="rule-value-field-row"):
                    yield Label("Field")
                    yield Select(
                        [(field.value.replace("_", " ").title(), field.value) for field in fields],
                        value=value.get("field", "subject"),
                        allow_blank=False,
                        id="rule-value-field",
                    )
                with Vertical(classes="rule-value-field", id="rule-value-operator-row"):
                    yield Label("Comparison")
                    yield Select(
                        [(op.value.replace("_", " ").title(), op.value) for op in TextOperator],
                        value=value.get("operator", "contains"),
                        allow_blank=False,
                        id="rule-value-operator",
                    )
                with Vertical(classes="rule-value-field", id="rule-value-values-row"):
                    yield Label("Alternative values (one per line)")
                    yield TextArea("\n".join(value.get("values", [])), id="rule-value-values")
                inputs = {
                    "minimum_bytes": (
                        "Minimum size in bytes (optional)",
                        value.get("minimum_bytes"),
                    ),
                    "maximum_bytes": (
                        "Maximum size in bytes (optional)",
                        value.get("maximum_bytes"),
                    ),
                    "start": ("From local time (HH:MM)", value.get("start", "09:00")),
                    "end": ("Until local time (HH:MM)", value.get("end", "17:00")),
                    "timezone": ("Timezone (blank uses profile timezone)", value.get("timezone")),
                    "weekdays": (
                        "Weekdays (0=Monday to 6=Sunday, comma separated)",
                        ",".join(str(day) for day in value.get("weekdays", [0, 1, 2, 3, 4])),
                    ),
                    "provider_id": ("Provider ID", value.get("provider_id", self.provider_id)),
                    "value": ("Action value (optional: urgency or sound name)", value.get("value")),
                }
                for name, (label, initial) in inputs.items():
                    with Vertical(classes="rule-value-field", id=f"rule-value-{name}-row"):
                        yield Label(label, markup=False)
                        yield Input(
                            value=str(initial) if initial is not None else "",
                            id=f"rule-value-{name}",
                        )
                for name, label in [
                    ("expected", "Expected true"),
                    ("case_sensitive", "Match case"),
                    ("negate", "Exclude these choices"),
                ]:
                    yield Checkbox(
                        label,
                        value=bool(value.get(name, name == "expected")),
                        id=f"rule-value-{name}",
                    )
            yield Static("", id="rule-value-error", markup=False)
            with HorizontalScroll(id="rule-value-buttons"):
                yield Button("Cancel", id="rule-value-cancel")
                yield Button("Use value", id="rule-value-save", variant="primary")

    def on_mount(self) -> None:
        self._show_fields()

    @on(Select.Changed, "#rule-value-kind")
    def _kind_changed(self, event: Select.Changed) -> None:
        if self.is_mounted:
            self._show_fields()

    def _show_fields(self) -> None:
        kind = str(self.query_one("#rule-value-kind", Select).value)
        visible = {
            "text": {"field", "operator", "values", "case_sensitive"},
            "boolean": {"field", "expected"},
            "choice": {"field", "values", "negate"},
            "size": {"minimum_bytes", "maximum_bytes"},
            "time": {"start", "end", "timezone"},
            "weekday": {"weekdays", "timezone"},
            "route_immediate": {"provider_id"},
            "route_digest": {"provider_id"},
            "play_sound": {"value"},
            "set_urgency": {"value"},
        }.get(kind, set())
        for row in self.query(".rule-value-field"):
            row.display = (row.id or "").removeprefix("rule-value-").removesuffix("-row") in visible
        for name in ("expected", "case_sensitive", "negate"):
            self.query_one(f"#rule-value-{name}").display = name in visible
        if not self.is_action and kind in {"text", "boolean", "choice"}:
            fields = {"text": TextField, "boolean": BooleanField, "choice": ChoiceField}[kind]
            select = self.query_one("#rule-value-field", Select)
            selected = select.value
            options = [(field.value.replace("_", " ").title(), field.value) for field in fields]
            select.set_options(options)
            select.value = (
                selected if selected in {option[1] for option in options} else options[0][1]
            )

    def build_value(self) -> dict[str, Any]:
        kind = str(self.query_one("#rule-value-kind", Select).value)

        def text(name: str) -> str:
            return self.query_one(f"#rule-value-{name}", Input).value.strip()

        data: dict[str, Any]
        if self.is_action:
            data = {
                "kind": kind,
                "provider_id": text("provider_id")
                if kind in {"route_immediate", "route_digest"}
                else None,
                "value": text("value") or None if kind in {"play_sound", "set_urgency"} else None,
            }
            return action_to_data(action_from_data(data))
        data = {"type": kind}
        if kind in {"text", "boolean", "choice"}:
            data["field"] = str(self.query_one("#rule-value-field", Select).value)
        if kind in {"text", "choice"}:
            data["values"] = [
                part
                for part in self.query_one("#rule-value-values", TextArea).text.splitlines()
                if part
            ]
        if kind == "text":
            data["operator"] = str(self.query_one("#rule-value-operator", Select).value)
            data["case_sensitive"] = self.query_one("#rule-value-case_sensitive", Checkbox).value
        elif kind == "choice":
            data["negate"] = self.query_one("#rule-value-negate", Checkbox).value
        elif kind == "boolean":
            data["expected"] = self.query_one("#rule-value-expected", Checkbox).value
        elif kind == "size":
            for name in ("minimum_bytes", "maximum_bytes"):
                data[name] = int(text(name)) if text(name) else None
        elif kind == "time":
            data.update(start=text("start"), end=text("end"), timezone=text("timezone") or None)
        elif kind == "weekday":
            data.update(
                weekdays=[int(day.strip()) for day in text("weekdays").split(",")],
                timezone=text("timezone") or None,
            )
        return condition_to_data(condition_from_data(data))

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "rule-value-cancel":
            self.action_cancel()
        elif event.button.id == "rule-value-save":
            self.action_save()

    def action_save(self) -> None:
        try:
            value = self.build_value()
        except (ValueError, TypeError) as exc:
            self.query_one("#rule-value-error", Static).update(_label(exc))
        else:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RuleBuilder(Vertical):
    """Tree operations and ordered actions use exactly the runtime engine's schema."""

    DEFAULT_CSS = """
    RuleBuilder { height: auto; }
    #rule-builder-tree { height: 10; min-height: 3; }
    #rule-builder-actions { height: 6; min-height: 3; }
    RuleBuilder HorizontalScroll { height: 3; }
    RuleBuilder Button { min-width: 8; width: auto; }
    RuleBuilder .rule-builder-action-controls { height: auto; }
    RuleBuilder.condition-only .rule-builder-action-controls { display: none; }
    #rule-builder-error { height: auto; color: $error; }
    """

    class Changed(Message):
        def __init__(self, builder: RuleBuilder) -> None:
            super().__init__()
            self.condition = deepcopy(builder.condition)
            self.actions = deepcopy(builder.actions)

    def __init__(
        self,
        condition: dict[str, Any],
        actions: list[dict[str, Any]],
        *,
        provider_id: str,
        condition_only: bool = False,
    ) -> None:
        super().__init__(id="rule-builder")
        self.condition = condition_to_data(condition_from_data(condition))
        self.actions = [action_to_data(action_from_data(action)) for action in actions]
        self.provider_id = provider_id
        self.path: ConditionPath = ()
        self.action_index = 0
        self.set_class(condition_only, "condition-only")

    def compose(self) -> ComposeResult:
        yield Label("WHEN · choose a condition or group")
        yield Tree("Condition", data=(), id="rule-builder-tree")
        with HorizontalScroll():
            for operation, label in [
                ("add", "Add"),
                ("edit", "Edit"),
                ("and", "Group AND"),
                ("or", "Group OR"),
                ("not", "NOT"),
                ("duplicate", "Duplicate"),
                ("up", "Up"),
                ("down", "Down"),
                ("remove", "Remove"),
            ]:
                yield Button(label, id=f"rule-tree-{operation}")
        with Vertical(classes="rule-builder-action-controls"):
            yield Label("THEN · actions run in this order")
            yield DataTable(id="rule-builder-actions", cursor_type="row")
            with HorizontalScroll():
                for operation in ("add", "edit", "duplicate", "up", "down", "remove"):
                    yield Button(operation.title(), id=f"rule-action-{operation}")
        yield Static("", id="rule-builder-error", markup=False)

    def on_mount(self) -> None:
        self.query_one("#rule-builder-actions", DataTable).add_columns("Order", "Action", "Target")
        self.refresh_values()

    def refresh_values(self) -> None:
        tree = self.query_one("#rule-builder-tree", Tree)
        tree.clear()
        nodes = 0

        def populate(
            tree_node: TreeNode[ConditionPath], value: dict[str, Any], path: ConditionPath
        ) -> None:
            nonlocal nodes
            nodes += 1
            kind = value["type"]
            if kind in {"and", "or", "not"}:
                label = kind.upper()
            elif kind == "size":
                label = _label(
                    f"Size: {value.get('minimum_bytes') or 0} to "
                    f"{value.get('maximum_bytes') or 'any'} bytes"
                )
            elif kind == "time":
                label = _label(
                    f"Time: {value['start']} to {value['end']} "
                    f"({value.get('timezone') or 'profile timezone'})"
                )
            elif kind == "weekday":
                days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
                label = _label(
                    "Days: "
                    + ", ".join(days[d] for d in value["weekdays"])
                    + f" ({value.get('timezone') or 'profile timezone'})"
                )
            else:
                label = _label(
                    f"{value.get('field', kind)} {value.get('operator', '')} "
                    f"{'NOT ' if value.get('negate') else ''}"
                    f"{value.get('values', value.get('expected', ''))}"
                )
            tree_node.set_label(Text(label))
            tree_node.data = path
            children = value.get("children", [value["child"]] if kind == "not" else [])
            for index, child in enumerate(children):
                populate(tree_node.add("", data=(*path, index), expand=True), child, (*path, index))
            tree_node.expand()

        populate(tree.root, self.condition, ())
        tree.styles.height = min(10, max(3, nodes))
        self.path = ()
        table = self.query_one("#rule-builder-actions", DataTable)
        table.styles.height = min(6, max(3, len(self.actions) + 1))
        table.clear()
        for index, action in enumerate(self.actions):
            table.add_row(
                str(index + 1),
                _label(action["kind"].replace("_", " ")),
                _label(action.get("provider_id") or action.get("value") or ""),
                key=str(index),
            )
        if self.actions:
            self.action_index = min(self.action_index, len(self.actions) - 1)
            table.move_cursor(row=self.action_index)

    @on(Tree.NodeSelected, "#rule-builder-tree")
    def _node_selected(self, event: Tree.NodeSelected[ConditionPath]) -> None:
        self.path = event.node.data or ()

    @on(DataTable.RowHighlighted, "#rule-builder-actions")
    def _action_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if current_row_highlight(event) and event.row_key.value is not None:
            self.action_index = int(event.row_key.value)

    def _commit(self) -> None:
        self.refresh_values()
        self.post_message(self.Changed(self))

    def _condition_result(
        self, path: ConditionPath, operation: str, value: dict[str, Any] | None
    ) -> None:
        if value is None:
            return
        try:
            self.condition = edit_condition_tree(self.condition, path, operation, value)
        except (ValueError, TypeError) as exc:
            self.query_one("#rule-builder-error", Static).update(_label(exc))
        else:
            self._commit()

    def _action_result(self, index: int | None, value: dict[str, Any] | None) -> None:
        if value is None:
            return
        if index is None:
            if len(self.actions) >= 256:
                self.query_one("#rule-builder-error", Static).update("Limit: 256 actions per rule.")
                return
            self.actions.append(value)
            self.action_index = len(self.actions) - 1
        else:
            self.actions[index] = value
        self._commit()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        event.stop()
        identifier = event.button.id or ""
        try:
            if identifier.startswith("rule-tree-"):
                operation = identifier.removeprefix("rule-tree-")
                path = self.path
                if operation in {"add", "edit"}:
                    value = condition_at(self.condition, path) if operation == "edit" else None
                    if value and value["type"] in {"and", "or", "not"}:
                        raise ValueError("Select a leaf to edit; use group and NOT controls here.")

                    def callback(result: dict[str, Any] | None) -> None:
                        self._condition_result(
                            path, "replace" if operation == "edit" else "add", result
                        )

                    self.app.push_screen(RuleValueForm(value), callback)
                else:
                    self.condition = edit_condition_tree(self.condition, path, operation)
                    self._commit()
            elif identifier.startswith("rule-action-"):
                operation = identifier.removeprefix("rule-action-")
                index = self.action_index
                if operation in {"add", "edit"}:
                    value = self.actions[index] if operation == "edit" else None
                    self.app.push_screen(
                        RuleValueForm(value, action=True, provider_id=self.provider_id),
                        lambda result: self._action_result(
                            index if operation == "edit" else None, result
                        ),
                    )
                elif operation == "duplicate":
                    if len(self.actions) >= 256:
                        raise ValueError("Limit: 256 actions per rule.")
                    self.actions.insert(index + 1, deepcopy(self.actions[index]))
                    self._commit()
                elif operation == "remove":
                    if len(self.actions) == 1:
                        raise ValueError("Keep at least one action in this rule.")
                    del self.actions[index]
                    self._commit()
                else:
                    destination = index + (-1 if operation == "up" else 1)
                    if not 0 <= destination < len(self.actions):
                        raise ValueError("This action is already at the edge of the list.")
                    self.actions[index], self.actions[destination] = (
                        self.actions[destination],
                        self.actions[index],
                    )
                    self.action_index = destination
                    self._commit()
        except (ValueError, TypeError, IndexError) as exc:
            self.query_one("#rule-builder-error", Static).update(_label(exc))
