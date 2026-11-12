"""Repeatable labelled contact values without a serialization language."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, TextArea

from gucmail.tui.literal_select import LiteralSelect as Select


class ContactValueRow(Horizontal):
    DEFAULT_CSS = """
    ContactValueRow { height: 3; }
    ContactValueRow Select { width: 24; }
    ContactValueRow Input { width: 1fr; }
    ContactValueRow Button { width: 9; min-width: 9; }
    ContactValueRow.multiline { height: 5; }
    ContactValueRow TextArea { width: 1fr; height: 5; }
    """

    def __init__(
        self, choices: Sequence[tuple[str, str]], label: str, value: str, *, multiline: bool = False
    ) -> None:
        super().__init__(classes="multiline" if multiline else "")
        self.choices = choices
        self.initial_label = label
        self.initial_value = value
        self.multiline = multiline

    def compose(self) -> ComposeResult:
        yield Select(self.choices, value=self.initial_label, allow_blank=False)
        if self.multiline:
            yield TextArea(self.initial_value, classes="contact-value")
        else:
            yield Input(self.initial_value, placeholder="Phone number", classes="contact-value")
        yield Button("Remove", classes="contact-value-remove")

    @on(Button.Pressed, ".contact-value-remove")
    async def _remove(self, event: Button.Pressed) -> None:
        event.stop()
        parent = self.parent
        await self.remove()
        if isinstance(parent, ContactValuesEditor):
            parent.query_one(".contact-value-add", Button).focus()


class ContactValuesEditor(Vertical):
    DEFAULT_CSS = """
    ContactValuesEditor { height: auto; }
    ContactValuesEditor > Button { width: auto; }
    """

    def __init__(
        self,
        title: str,
        values: Mapping[str, str],
        choices: Sequence[tuple[str, str]],
        *,
        id: str,
        multiline: bool = False,
    ) -> None:
        super().__init__(id=id)
        self.title_text = title
        self.multiline = multiline
        self.initial_values = dict(values)
        self._canonical_labels = {value.casefold(): value for _label, value in choices}
        self.choices = tuple(choices) + tuple(
            (key, key) for key in values if key.casefold() not in self._canonical_labels
        )

    def compose(self) -> ComposeResult:
        yield Label(f"{self.title_text} · type / value", markup=False)
        for key, value in self.initial_values.items():
            label = self._canonical_labels.get(key.casefold(), key)
            yield ContactValueRow(self.choices, label, value, multiline=self.multiline)
        yield Button(f"Add {self.title_text.casefold()}", classes="contact-value-add")

    @on(Button.Pressed, ".contact-value-add")
    async def _add(self, event: Button.Pressed) -> None:
        event.stop()
        used = {row.query_one(Select).value for row in self.query(ContactValueRow)}
        label = next((key for _name, key in self.choices if key not in used), self.choices[0][1])
        row = ContactValueRow(self.choices, label, "", multiline=self.multiline)
        await self.mount(row, before=event.button)
        row.query_one(".contact-value").focus()

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in self.query(ContactValueRow):
            value = (
                row.query_one(TextArea).text if self.multiline else row.query_one(Input).value
            ).strip()
            if not value:
                continue
            key = str(row.query_one(Select).value)
            if key.casefold() in {label.casefold() for label in result}:
                raise ValueError(f"Choose a different type for each {self.title_text.casefold()}.")
            result[key] = value
        return result
