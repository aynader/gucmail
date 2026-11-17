"""Guided local/mailbox group editing and explicit recipient review for composition."""

from __future__ import annotations

from email.utils import formataddr
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.errors import GUCmailError
from gucmail.models import Address
from gucmail.security import sanitize_terminal
from gucmail.services.compose import normalize_recipient
from gucmail.services.contact_groups import (
    ContactGroup,
    ContactGroupPlan,
    ContactGroupService,
    GroupSource,
    validate_group,
)
from gucmail.tui.area_common import OperationalScreen, PublicOperationError, ViewState
from gucmail.tui.events import (  # noqa: F401 - moved, re-exported
    ContactGroupComposeRequested,
    ContactGroupsRequested,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class ContactGroupEditor(ModalScreen[ContactGroupPlan | None]):
    DEFAULT_CSS = """
    ContactGroupEditor { align: center middle; }
    #contact-group-editor { width: 94%; max-width: 100; height: 90%; max-height: 36;
        background: $surface; border: round $border; padding: 1 2; }
    #contact-group-fields { height: 1fr; }
    #contact-group-fields TextArea { height: 1fr; min-height: 5; }
    #contact-group-editor-actions { height: 3; }
    #contact-group-editor-error { height: auto; color: $error; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "review", "Review"),
    ]

    def __init__(self, before: ContactGroup | None = None) -> None:
        super().__init__()
        self.before = before

    def compose(self) -> ComposeResult:
        with Vertical(id="contact-group-editor"):
            yield Label("Contact group · explicit SMTP members · review before saving")
            with VerticalScroll(id="contact-group-fields"):
                yield Label("Group name")
                yield Input(
                    value=sanitize_terminal(self.before.name) if self.before else "",
                    id="group-name",
                )
                yield Label("SMTP members · one per line · maximum 200")
                yield TextArea(
                    "\n".join(
                        formataddr((member.name or "", member.email))
                        for member in self.before.members
                    )
                    if self.before
                    else "",
                    id="group-members",
                )
            yield Static("", id="contact-group-editor-error", markup=False)
            with HorizontalScroll(id="contact-group-editor-actions"):
                yield Button("Cancel", id="group-editor-cancel")
                yield Button("Review group", id="group-editor-review", variant="primary")

    def action_review(self) -> None:
        try:
            name, members = validate_group(
                self.query_one("#group-name", Input).value,
                tuple(
                    normalize_recipient(line.strip())
                    for line in self.query_one("#group-members", TextArea).text.splitlines()
                    if line.strip()
                ),
            )
        except GUCmailError as exc:
            self.query_one("#contact-group-editor-error", Static).update(
                sanitize_terminal(str(exc))
            )
        else:
            self.dismiss(ContactGroupPlan(name, members, before=self.before))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "group-editor-review":
            self.action_review()
        elif event.button.id == "group-editor-cancel":
            self.action_cancel()


class ContactGroupsScreen(OperationalScreen):
    DEFAULT_CSS = """
    ContactGroupsScreen { layout: vertical; }
    #contact-group-source { height: 3; }
    #contact-groups-table { height: 1fr; min-height: 4; }
    #contact-groups-details { height: 2fr; min-height: 4; padding: 0 1; }
    #contact-groups-toolbar { height: 3; }
    """

    def __init__(
        self,
        local: ContactGroupService,
        mailbox: ContactGroupService | None = None,
        *,
        page_size: int = 50,
    ) -> None:
        super().__init__(area_name="contact-groups")
        self.services = {GroupSource.LOCAL: local}
        if mailbox is not None:
            self.services[GroupSource.MAILBOX] = mailbox
        self.source = GroupSource.LOCAL
        self.page_size = min(max(page_size, 1), 200)
        self.page_offset = 0
        self.groups: dict[str, ContactGroup] = {}
        self.current: ContactGroup | None = None

    @property
    def service(self) -> ContactGroupService:
        return self.services[self.source]

    def compose(self) -> ComposeResult:
        yield Label("Contact groups", classes="workspace-heading")
        yield Static("", id="area-state", markup=False)
        sources = [("Local · this account", "local")]
        if GroupSource.MAILBOX in self.services:
            sources.append(("Mailbox · personal groups", "mailbox"))
        yield Select(
            sources,
            value="local",
            allow_blank=False,
            id="contact-group-source",
        )
        yield DataTable(id="contact-groups-table", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="contact-groups-details"):
            yield Static(
                "Select a group to inspect its recipients.",
                id="contact-group-details",
                markup=False,
            )
        yield Static("", id="area-progress", markup=False)
        with HorizontalScroll(id="contact-groups-toolbar"):
            for label, identifier in (
                ("Create", "group-create"),
                ("Edit members", "group-edit"),
                ("Delete", "group-delete"),
                ("Compose (Bcc)", "group-compose"),
                ("Previous", "group-prev"),
                ("Next", "group-next"),
                ("Refresh", "group-refresh"),
                ("Retry", "area-retry"),
                ("Cancel operation", "area-cancel-operation"),
            ):
                yield Button(label, id=identifier)
            yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry").display = False
        self.query_one("#area-cancel-operation").display = False
        self.query_one("#contact-groups-table", DataTable).add_columns("Name", "Reference")
        self._detail(None)
        self.action_refresh()

    @on(Select.Changed, "#contact-group-source")
    def _source_changed(self, event: Select.Changed) -> None:
        if event.value == Select.BLANK or str(event.value) == self.source.value:
            return
        if self._guard_pending_mutation():
            self.query_one("#contact-group-source", Select).value = self.source.value
            return
        self.source = GroupSource(str(event.value))
        self.page_offset = 0
        self.groups.clear()
        self._detail(None)
        self.action_refresh()

    def action_refresh(self) -> None:
        service = self.service
        offset = self.page_offset
        self._begin_operation(
            "Loading contact groups",
            lambda *_: service.list(limit=self.page_size, offset=offset),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or not all(
            isinstance(group, ContactGroup) for group in result
        ):
            raise PublicOperationError("Invalid contact-group listing.")
        self.groups = {group.reference: group for group in result}
        table = self.query_one("#contact-groups-table", DataTable)
        table.clear()
        for group in result:
            table.add_row(Text(sanitize_terminal(group.name)), group.reference, key=group.reference)
        self.query_one("#group-prev", Button).disabled = self.page_offset == 0
        self.query_one("#group-next", Button).disabled = len(result) < self.page_size
        self._detail(None)
        if result:
            self._load_detail(result[0].reference)
        else:
            self._set_view_state(ViewState.EMPTY, "No contact groups on this page.")

    @on(DataTable.RowSelected, "#contact-groups-table")
    def _selected(self, event: DataTable.RowSelected) -> None:
        reference = str(event.row_key.value)
        if reference in self.groups:
            self._load_detail(reference)

    def _load_detail(self, reference: str) -> None:
        service = self.service
        self._begin_operation(
            "Loading group members",
            lambda *_: service.get(reference),
            self._detail,
            retry_safe=True,
        )

    def _detail(self, result: object) -> None:
        self.current = result if isinstance(result, ContactGroup) else None
        current = self.current
        for identifier in ("#group-edit", "#group-delete", "#group-compose"):
            self.query_one(identifier, Button).disabled = current is None
        if current is not None:
            self.query_one("#group-edit", Button).disabled = not current.editable
            self.query_one("#group-compose", Button).disabled = bool(current.unresolved_members)
            text = f"{current.name}\n{current.source.value} · {current.reference}\n" + (
                f"{len(current.members)} SMTP members; {current.unresolved_members} unresolved.\n"
                + "\n".join(
                    formataddr((member.name or "", member.email)) for member in current.members
                )
            )
            if not current.editable:
                text += "\nLinked or unsupported membership: inspect in Outlook before editing."
            self._set_view_state(
                ViewState.READY, "Recipients loaded. Enter opens another highlighted group."
            )
        else:
            text = "Select a group and press Enter to inspect its recipients."
        self.query_one("#contact-group-details", Static).update(
            sanitize_terminal(text, max_length=100000)
        )

    def _edit(self, *, create: bool = False) -> None:
        if self._guard_pending_mutation():
            return
        if not create and (self.current is None or not self.current.editable):
            return
        service = self.service
        self.app.push_screen(
            ContactGroupEditor(None if create else self.current),
            lambda plan: self._review(service, plan),
        )

    def _review(self, service: ContactGroupService, plan: ContactGroupPlan | None) -> None:
        if plan is None:
            return
        if not plan.delete:
            plan = service.plan(plan.name, plan.members, before=plan.before)
        detail = (
            f"Group: {plan.name}\nReference: {plan.before.reference if plan.before else 'new'}\n"
            + ("\n".join(formataddr((member.name or "", member.email)) for member in plan.members))
        )
        self._confirm(
            "Delete contact group?" if plan.delete else "Save contact group?",
            sanitize_terminal(detail)
            + (
                "\nThe group moves to Deleted Items."
                if plan.delete and plan.before and plan.before.source is GroupSource.MAILBOX
                else "\nThis saved group is removed."
                if plan.delete
                else "\nThese recipients replace the saved membership."
                if plan.before
                else "\nSave this new group and its recipients."
            )
            + " No message is sent.",
            lambda: self._apply(service, plan),
        )

    def _apply(self, service: ContactGroupService, plan: ContactGroupPlan) -> None:
        self._begin_operation(
            "Saving contact group",
            lambda *_: service.apply(plan, confirmed=True),
            lambda _: self.action_refresh(),
        )

    def action_compose_group(self) -> None:
        if self.current is None:
            return
        service = self.service
        reference = self.current.reference
        self._begin_operation(
            "Reviewing group members",
            lambda *_: service.recipients(reference),
            self._review_compose,
            retry_safe=True,
        )

    def _review_compose(self, result: object) -> None:
        if not isinstance(result, tuple) or not all(isinstance(value, Address) for value in result):
            raise PublicOperationError("Invalid group recipients.")

        def open_draft() -> None:
            self.post_message(ContactGroupComposeRequested(result))

        self._confirm(
            "Open a Bcc draft for these recipients?",
            sanitize_terminal(
                "\n".join(formataddr((member.name or "", member.email)) for member in result)
            )
            + "\nThis opens the composer. Sending requires its normal separate review.",
            open_draft,
            confirm_label="Open draft",
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        identifier = event.button.id
        if identifier and identifier.startswith("group-") and self._guard_pending_mutation():
            return
        if identifier == "group-create":
            self._edit(create=True)
        elif identifier == "group-edit":
            self._edit()
        elif identifier == "group-delete" and self.current:
            self._review(self.service, self.service.plan_delete(self.current))
        elif identifier == "group-compose":
            self.action_compose_group()
        elif identifier in {"group-prev", "group-next"}:
            self.page_offset = max(
                0, self.page_offset + self.page_size * (-1 if identifier == "group-prev" else 1)
            )
            self.action_refresh()
        elif identifier == "group-refresh":
            self.action_refresh()
