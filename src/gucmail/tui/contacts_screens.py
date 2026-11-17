"""Contacts screens: the contact list, editor and correspondents."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar, cast
from uuid import uuid4

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError
from gucmail.models import (
    Address,
    ContactInfo,
)
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import (
    CorrespondenceEvent,
    CorrespondentSummary,
    find_duplicate_groups,
)
from gucmail.services.contacts_display import ContactsDisplayPreferences
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.contact_fields import ContactValuesEditor
from gucmail.tui.events import PimActionRequested
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.pim_backends import (
    _addresses,
    _bounded_limit,
    _csv,
    _optional,
    _parse_date,
)
from gucmail.tui.pim_common import (
    BatchActionResult,
    ContactEditorValue,
    ContactMergePreviewView,
    ContactsScreenBackend,
    ExportResult,
    FilePathScreen,
    _batch_message,
    _capability,
    _capability_summary,
    _PagedPimScreen,
    _PimEditorScreen,
)


class ContactEditorScreen(_PimEditorScreen[ContactEditorValue | None]):
    """Validated Contacts create/edit form."""

    DEFAULT_CSS = """
    ContactEditorScreen { align: center middle; }
    #contact-editor {
        width: 96%; max-width: 96; height: 96%; max-height: 38; border: round $border;
        background: $surface; padding: 1 2;
    }
    #contact-editor-title { height: 2; text-style: bold; }
    #contact-editor-fields { height: 1fr; }
    #contact-editor-fields Input { height: 3; }
    #contact-editor-notes { height: 7; }
    #contact-editor-error { min-height: 1; height: auto; color: $error; }
    #contact-editor-actions { height: 3; align-horizontal: right; }
    #contact-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, contact: ContactInfo | None, *, creating: bool = False) -> None:
        super().__init__()
        self.contact = contact
        self.creating = creating

    def compose(self) -> ComposeResult:
        contact = self.contact
        with Vertical(id="contact-editor"):
            yield Label(
                "Create contact" if contact is None or self.creating else "Edit contact",
                id="contact-editor-title",
                markup=False,
            )
            with VerticalScroll(id="contact-editor-fields"):
                yield Label("Display name")
                yield Input(
                    value=contact.display_name if contact else "",
                    placeholder="Display name",
                    id="contact-edit-name",
                )
                yield Label("First name")
                yield Input(
                    value=contact.given_name or "" if contact else "",
                    placeholder="Given name",
                    id="contact-edit-given",
                )
                yield Label("Middle name")
                yield Input(
                    value=contact.middle_name or "" if contact else "",
                    placeholder="Middle name",
                    id="contact-edit-middle",
                )
                yield Label("Family name")
                yield Input(
                    value=contact.family_name or "" if contact else "",
                    placeholder="Family name",
                    id="contact-edit-family",
                )
                yield Label("Nickname")
                yield Input(
                    value=contact.nickname or "" if contact else "",
                    placeholder="Nickname",
                    id="contact-edit-nickname",
                )
                yield Label("Email addresses · comma separated")
                yield Input(
                    value=", ".join(address.email for address in contact.emails) if contact else "",
                    placeholder="Email addresses (comma separated)",
                    id="contact-edit-emails",
                )
                yield ContactValuesEditor(
                    "Phone",
                    contact.phones if contact else {},
                    (
                        ("Mobile", "MobilePhone"),
                        ("Work", "BusinessPhone"),
                        ("Home", "HomePhone"),
                        ("Primary", "PrimaryPhone"),
                        ("Work 2", "BusinessPhone2"),
                        ("Home 2", "HomePhone2"),
                        ("Other", "OtherTelephone"),
                        ("Company", "CompanyMainPhone"),
                        ("Work fax", "BusinessFax"),
                        ("Home fax", "HomeFax"),
                        ("Other fax", "OtherFax"),
                        ("Assistant", "AssistantPhone"),
                        ("Callback", "Callback"),
                        ("Car", "CarPhone"),
                        ("Pager", "Pager"),
                        ("Radio", "RadioPhone"),
                        ("Telex", "Telex"),
                        ("TTY/TDD", "TtyTddPhone"),
                        ("ISDN", "Isdn"),
                    ),
                    id="contact-edit-phones",
                )
                yield ContactValuesEditor(
                    "Postal address",
                    contact.addresses if contact else {},
                    (("Home", "Home"), ("Work", "Business"), ("Other", "Other")),
                    id="contact-edit-addresses",
                    multiline=True,
                )
                yield Label("Company")
                yield Input(
                    value=contact.company or "" if contact else "",
                    placeholder="Company",
                    id="contact-edit-company",
                )
                yield Label("Department")
                yield Input(
                    value=contact.department or "" if contact else "",
                    placeholder="Department",
                    id="contact-edit-department",
                )
                yield Label("Job title")
                yield Input(
                    value=contact.job_title or "" if contact else "",
                    placeholder="Job title",
                    id="contact-edit-job-title",
                )
                yield Label("Birthday · YYYY-MM-DD")
                yield Input(
                    value=contact.birthday.isoformat() if contact and contact.birthday else "",
                    placeholder="Birthday (YYYY-MM-DD)",
                    id="contact-edit-birthday",
                )
                yield Label("Categories · comma separated")
                yield Input(
                    value=", ".join(contact.categories) if contact else "",
                    placeholder="Categories (comma separated)",
                    id="contact-edit-categories",
                )
                yield Label("Notes")
                yield TextArea(contact.notes or "" if contact else "", id="contact-editor-notes")
            yield Static("", id="contact-editor-error", markup=False)
            with Horizontal(id="contact-editor-actions"):
                yield Button("Cancel", id="contact-edit-cancel")
                yield Button("Save", id="contact-edit-save", variant="primary")

    def _value(self) -> ContactEditorValue:
        display_name = self.query_one("#contact-edit-name", Input).value.strip()
        if not display_name:
            raise PublicOperationError("A contact needs a display name.", retryable=False)
        return ContactEditorValue(
            display_name=display_name,
            given_name=_optional(self.query_one("#contact-edit-given", Input).value),
            middle_name=_optional(self.query_one("#contact-edit-middle", Input).value),
            family_name=_optional(self.query_one("#contact-edit-family", Input).value),
            nickname=_optional(self.query_one("#contact-edit-nickname", Input).value),
            emails=_addresses(self.query_one("#contact-edit-emails", Input).value),
            phones=self.query_one("#contact-edit-phones", ContactValuesEditor).values(),
            addresses=self.query_one("#contact-edit-addresses", ContactValuesEditor).values(),
            company=_optional(self.query_one("#contact-edit-company", Input).value),
            department=_optional(self.query_one("#contact-edit-department", Input).value),
            job_title=_optional(self.query_one("#contact-edit-job-title", Input).value),
            birthday=_parse_date(
                self.query_one("#contact-edit-birthday", Input).value,
                field="Birthday",
            ),
            notes=_optional(self.query_one("#contact-editor-notes", TextArea).text),
            categories=_csv(self.query_one("#contact-edit-categories", Input).value),
        )

    def action_save(self) -> None:
        if self._submission_blocked():
            return
        try:
            value = self._value()
        except (PublicOperationError, ValueError) as exc:
            self._validation_failed(str(exc))
            return
        self._submit_value(value)

    def action_cancel(self) -> None:
        self._cancel_edit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "contact-edit-save":
            self.action_save()
        elif event.button.id == "contact-edit-cancel":
            self.action_cancel()


class ContactsScreen(_PagedPimScreen):
    """Bounded mailbox Contacts view with CRUD, vCard, and reviewed merging."""

    DEFAULT_CSS = """
    ContactsScreen { layout: vertical; }
    #contacts-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #contacts-title { height: 2; text-style: bold; }
    #contacts-toolbar { height: 3; }
    #contacts-search { width: 1fr; margin-right: 1; }
    #contacts-toolbar Button { margin-right: 1; }
    #contacts-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #contacts-content { height: 1fr; min-height: 4; }
    #contacts-table { width: 3fr; height: 1fr; }
    #contacts-detail-pane {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1;
    }
    #contacts-detail-pane:focus { border-left: solid $accent; }
    #contacts-detail { width: 1fr; height: auto; }
    #contacts-actions, #contacts-footer { height: auto; min-height: 3; }
    #contacts-actions Button { margin-right: 1; }
    #contacts-footer { dock: bottom; height: 3; align-horizontal: right; }
    #contacts-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create", "New contact", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("space", "toggle_selection", "Select", show=False),
        Binding("enter", "details", "Details", show=False),
    ]

    def __init__(
        self,
        backend: ContactsScreenBackend,
        *,
        page_size: int = 100,
        sort_order: str = "display_name",
        display_preferences: Callable[[], ContactsDisplayPreferences] | None = None,
        operation_preferences: Callable[[], ContactOperationPreferences] | None = None,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="contacts",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.page_size = _bounded_limit(page_size)
        self._reset_paging()
        self.display_preferences = display_preferences or (
            lambda: ContactsDisplayPreferences(sort_order=sort_order)
        )
        self._display = self.display_preferences()
        self.operation_preferences = operation_preferences or ContactOperationPreferences
        self._operations = self.operation_preferences()
        self.contact_sort_order = self._display.sort_order
        self._complete_detail_reference: str | None = None
        self._contact_report: str | None = None
        self._contacts: tuple[ContactInfo, ...] = ()
        self._by_reference: dict[str, ContactInfo] = {}
        self._highlighted: str | None = None
        self._selected: set[str] = set()
        self._pending_notice: tuple[ViewState, str] | None = None

    @property
    def selected_contact_value(self) -> str | None:
        """Return the selected contact's first address, falling back to its reference."""

        contact = self._by_reference.get(self._highlighted or "")
        if contact is None:
            return None
        return contact.emails[0].email if contact.emails else contact.reference

    def compose(self) -> ComposeResult:
        with Vertical(id="contacts-root"):
            yield Label("Contacts", id="contacts-title")
            yield Static("Loading a contact page…", id="area-state", markup=False)
            yield Input(
                placeholder="Search contacts…",
                id="contacts-search",
            )
            with Horizontal(id="contacts-toolbar"):
                yield Button("Search", id="contacts-search-button")
                yield Button("Clear", id="contacts-clear")
                yield Button("Find duplicates", id="contacts-duplicates")
                yield Button("Directory", id="contacts-directory")
                yield Button("Contact groups", id="contacts-groups")
                yield Button("Local history", id="contacts-history-index")
            yield Static(
                _capability_summary(self.backend.capabilities),
                id="contacts-capabilities",
                markup=False,
            )
            with Horizontal(id="contacts-content"):
                yield DataTable(id="contacts-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="contacts-detail-pane"):
                    yield Static(
                        "Select a contact to inspect it.", id="contacts-detail", markup=False
                    )
            with HorizontalScroll(id="contacts-actions"):
                yield Button("Create", id="contacts-create", variant="primary")
                yield Button("Edit", id="contacts-edit", disabled=True)
                yield Button("Delete", id="contacts-delete", disabled=True)
                yield Button("Merge selected", id="contacts-merge", disabled=True)
                yield Button("Import vCard", id="contacts-import")
                yield Button("Export vCard", id="contacts-export", disabled=True)
                yield Button("Compose", id="contacts-compose", disabled=True)
                yield Button("Meeting", id="contacts-meeting", disabled=True)
                yield Button("Correspondence", id="contacts-correspondence", disabled=True)
                yield Button("Open mail", id="contacts-correspondence-mail", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with HorizontalScroll(id="contacts-footer"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                yield Button("Previous", id="contacts-prev", disabled=True)
                yield Button("Next", id="contacts-next", disabled=True)
                yield Button("Refresh", id="contacts-refresh")
                yield Button("Close", classes="area-close")

    @on(Button.Pressed, "#contacts-groups")
    def _contact_groups_button(self, event: Button.Pressed) -> None:
        from gucmail.tui.events import ContactGroupsRequested

        event.stop()
        self.post_message(ContactGroupsRequested())

    def on_mount(self) -> None:
        table = self.query_one("#contacts-table", DataTable)
        table.add_column("✓", key="selected", width=2)
        table.add_column("Name", key="name")
        table.add_column("Email", key="email")
        table.add_column("Company", key="company")
        table.add_column("Phone", key="phone")
        self._apply_capabilities()
        self.query_one("#contacts-capabilities").display = bool(
            _capability_summary(self.backend.capabilities)
        )
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Contacts are unavailable offline. Reopen without --offline to connect.",
            )
            return
        self.call_after_refresh(self._initial_refresh)

    def _initial_refresh(self) -> None:
        # A palette action may arrive before the first rendered frame. Never let
        # the deferred startup refresh cancel its directory/detail operation.
        if self._operation_generation == 0:
            self.action_refresh()

    def _apply_capabilities(self) -> None:
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        self.query_one("#contacts-create", Button).disabled = not mutate
        self.query_one("#contacts-import", Button).disabled = self.offline or not _capability(
            self.backend.capabilities, "import"
        )
        self._update_buttons()

    def on_screen_resume(self) -> None:
        if not self.is_mounted:
            return
        try:
            self._operations = self.operation_preferences()
            if (self._contact_report == "directory" and not self._operations.directory_lookup) or (
                self._contact_report == "history" and not self._operations.correspondence_history
            ):
                self.query_one("#contacts-detail", Static).update(
                    "This contact report is disabled in Contacts settings."
                )
                self._contact_report = None
            refresh = getattr(self.backend, "refresh_capabilities", None)
            if callable(refresh):
                refresh()
            self._apply_capabilities()
            self.query_one("#contacts-capabilities", Static).update(
                _capability_summary(self.backend.capabilities)
            )
            self.query_one("#contacts-capabilities").display = bool(
                _capability_summary(self.backend.capabilities)
            )
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)

    def _allow_contact_operation(self, operation: str) -> bool:
        try:
            self._operations = self.operation_preferences()
            self._operations.require(operation)
            return True
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)
            return False

    def action_refresh(self) -> None:
        self._reset_paging()
        self._load_page()

    def _load_page(self) -> None:
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Contacts are unavailable offline. Reopen without --offline to connect.",
            )
            return
        try:
            self._display = self.display_preferences()
            self.contact_sort_order = self._display.sort_order
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)
            return
        query = self.query_one("#contacts-search", Input).value.strip()
        offset = self._prepare_page(query)
        page_loader = getattr(self.backend, "list_contacts_page", None)
        self._begin_operation(
            "Loading contacts",
            lambda cancel, progress: (
                page_loader(query, self.page_size, offset=offset, cancel=cancel, progress=progress)
                if callable(page_loader)
                else tuple(
                    self.backend.list_contacts(
                        query,
                        self.page_size,
                        cancel=cancel,
                        progress=progress,
                    )
                )
            ),
            self._contacts_loaded,
            retry_safe=True,
        )

    def _contacts_loaded(self, result: object) -> None:
        contacts = cast(tuple[ContactInfo, ...], self._page_result(result))
        if not all(isinstance(item, ContactInfo) for item in contacts):
            raise PublicOperationError("The Contacts backend returned invalid records.")
        by_reference = {contact.reference: contact for contact in contacts}
        if len(by_reference) != len(contacts):
            raise PublicOperationError("The Contacts backend returned duplicate references.")
        self._contacts = contacts
        self._by_reference = by_reference
        self._complete_detail_reference = None
        self._selected.intersection_update(by_reference)
        self._render_contacts()
        if contacts:
            state, message = self._pending_notice or (
                ViewState.READY,
                f"{len(contacts)} contacts loaded from one page.",
            )
        else:
            state, message = self._pending_notice or (
                ViewState.EMPTY,
                "No contacts match this contact view.",
            )
        if self._page_notice:
            message = (
                message + " " + self._page_summary(len(contacts))
                if self._pending_notice
                else self._page_summary(len(contacts))
            )
        self._pending_notice = None
        self._set_view_state(state, message)

    def refresh_display_preferences(self) -> None:
        """Apply presentation and duplicate sensitivity locally to the loaded page."""
        self._display = self.display_preferences()
        self.contact_sort_order = self._display.sort_order
        if self.is_mounted:
            self._render_contacts()

    def _render_contacts(self) -> None:
        contacts = self._display.sort(
            self._by_reference[contact.reference] for contact in self._contacts
        )
        self._contacts = contacts
        table = self.query_one("#contacts-table", DataTable)
        table.clear()
        for contact in contacts:
            first_email = contact.emails[0].email if contact.emails else ""
            first_phone = next(iter(contact.phones.values()), "")
            table.add_row(
                "●" if contact.reference in self._selected else "",
                clean_cell(self._display.label(contact), max_length=100),
                clean_cell(first_email, max_length=100),
                clean_cell(contact.company or "", max_length=70),
                clean_cell(first_phone, max_length=40),
                key=contact.reference,
            )
        if contacts:
            reference = (
                self._highlighted
                if self._highlighted in self._by_reference
                else contacts[0].reference
            )
            self._select(reference)
            table.move_cursor(
                row=next(i for i, item in enumerate(contacts) if item.reference == reference)
            )
        else:
            self._highlighted = None
            self.query_one("#contacts-detail", Static).update("No contacts match this view.")
            self._update_buttons()

    @on(DataTable.RowHighlighted, "#contacts-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#contacts-table")
    def _row_selected(self) -> None:
        self.action_details()

    def _select(self, reference: str) -> None:
        contact = self._by_reference.get(reference)
        if contact is None:
            return
        changed = reference != self._highlighted
        self._contact_report = None
        self._highlighted = reference
        self.query_one("#contacts-detail", Static).update(
            _render_contact(
                contact,
                complete=reference == self._complete_detail_reference,
                preferences=self._display,
            )
        )
        if changed:
            self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._update_buttons()

    def _targets(self) -> tuple[str, ...]:
        if self._selected:
            return tuple(
                reference for reference in self._by_reference if reference in self._selected
            )
        return (
            (self._highlighted,)
            if self._highlighted is not None and self._highlighted in self._by_reference
            else ()
        )

    def _update_buttons(self) -> None:
        targets = self._targets()
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        single = len(targets) == 1
        self.query_one("#contacts-edit", Button).disabled = not single or not mutate
        self.query_one("#contacts-delete", Button).disabled = not targets or not mutate
        self.query_one("#contacts-merge", Button).disabled = (
            len(targets) < 2 or self.offline or not _capability(self.backend.capabilities, "merge")
        )
        self.query_one("#contacts-export", Button).disabled = not targets or not _capability(
            self.backend.capabilities, "export"
        )
        has_email = any(self._by_reference[reference].emails for reference in targets)
        self.query_one("#contacts-compose", Button).disabled = not has_email
        self.query_one("#contacts-meeting", Button).disabled = not has_email
        correspondence_disabled = (
            not targets
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
            or not self._operations.correspondence_history
        )
        self.query_one("#contacts-correspondence", Button).disabled = correspondence_disabled
        self.query_one("#contacts-correspondence-mail", Button).disabled = (
            correspondence_disabled or not has_email
        )
        self.query_one("#contacts-directory", Button).disabled = (
            self.offline
            or not self._operations.directory_lookup
            or not _capability(self.backend.capabilities, "directory")
        )
        self.query_one("#contacts-duplicates", Button).disabled = (
            len(self._contacts) < 2 or not self._operations.duplicate_detection
        )

    def action_toggle_selection(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        if reference in self._selected:
            self._selected.remove(reference)
        else:
            self._selected.add(reference)
        self.query_one("#contacts-table", DataTable).update_cell(
            reference,
            "selected",
            "●" if reference in self._selected else "",
        )
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"{len(self._selected)} contacts selected for batch actions.",
        )

    def action_details(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading contact details",
            lambda cancel, progress: self.backend.get_contact(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._detail_loaded,
            retry_safe=True,
        )

    def _detail_loaded(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned invalid details.")
        self._by_reference[result.reference] = result
        self._complete_detail_reference = result.reference
        self.query_one("#contacts-detail", Static).update(
            _render_contact(result, complete=True, preferences=self._display)
        )
        self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._set_view_state(ViewState.READY, "Complete contact details loaded.")

    def action_create(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create contact", OfflineOperationError())
            return
        self._open_submission_editor(ContactEditorScreen(None), self._create_value)

    def action_create_prefilled(self, sender: Address) -> None:
        """Open the create form with a safely bounded message sender."""

        email = sender.email.strip()
        if not email:
            self._set_view_state(ViewState.EMPTY, "The selected message has no sender address.")
            return
        prefill = ContactInfo(
            reference=f"prefill-{uuid4().hex}",
            display_name=(sender.name or email).strip(),
            emails=(Address(email, sender.name),),
        )
        self._open_submission_editor(
            ContactEditorScreen(prefill, creating=True), self._create_value
        )

    def configure_search(self, query: str, *, directory: bool = False) -> None:
        """Apply a contextual palette search after the screen has mounted."""

        search = self.query_one("#contacts-search", Input)
        search.value = query.strip()
        search.focus()
        if not search.value:
            self._set_view_state(ViewState.EMPTY, "No contact search value was available.")
        elif directory:
            self.action_directory()
        else:
            self.action_refresh()

    def _create_value(self, value: ContactEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Creating contact",
            lambda cancel, progress: self.backend.create_contact(
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def action_edit(self) -> None:
        targets = self._targets()
        if len(targets) != 1 or self.offline:
            return
        reference = targets[0]
        self._begin_operation(
            "Loading contact for edit",
            lambda cancel, progress: self.backend.get_contact(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._open_editor,
            retry_safe=True,
        )

    def _open_editor(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned invalid details.")
        self._open_submission_editor(
            ContactEditorScreen(result),
            lambda value: self._edit_value(result.reference, value),
        )

    def _edit_value(self, reference: str, value: ContactEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Updating contact",
            lambda cancel, progress: self.backend.update_contact(
                reference,
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def _mutation_loaded(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned an invalid contact.")
        self._highlighted = result.reference
        self._pending_notice = (ViewState.READY, "Contact mutation completed.")
        self.action_refresh()

    def action_delete(self) -> None:
        references = self._targets()
        if not references or self.offline or not _capability(self.backend.capabilities, "create"):
            return
        self._confirm(
            "Delete selected contacts?",
            f"Contacts: {len(references)}\n\n"
            "They are moved to Deleted Items. Only this selection is affected.",
            lambda: self._start_delete(references),
            confirm_label="Delete",
        )

    def _start_delete(self, references: tuple[str, ...]) -> None:
        self._begin_operation(
            "Deleting contacts",
            lambda cancel, progress: self.backend.delete_contacts(
                references,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="contact"),
        )

    def _batch_finished(self, result: object, *, noun: str) -> None:
        if not isinstance(result, BatchActionResult):
            raise PublicOperationError("The Contacts backend returned an invalid batch result.")
        self._selected.clear()
        self._pending_notice = _batch_message(result, noun=noun)
        self.action_refresh()

    def action_find_duplicates(self) -> None:
        if not self._allow_contact_operation("duplicates"):
            return
        threshold = self._display.duplicate_threshold
        groups = find_duplicate_groups(self._contacts, threshold=threshold)
        if not groups:
            self._set_view_state(
                ViewState.READY,
                f"No explainable duplicates met threshold {threshold} in this page.",
            )
            return
        group = groups[0]
        self._selected = {contact.reference for contact in group.contacts}
        table = self.query_one("#contacts-table", DataTable)
        for reference in self._by_reference:
            table.update_cell(
                reference,
                "selected",
                "●" if reference in self._selected else "",
            )
        reasons = sorted({reason for candidate in group.candidates for reason in candidate.reasons})
        self.query_one("#contacts-detail", Static).update(
            "Possible duplicate group\n"
            f"Contacts: {len(group.contacts)}\n"
            f"Maximum score: {group.maximum_score}\n"
            f"Threshold: {threshold}\n"
            f"Reasons: {', '.join(reasons) or '(none)'}\n\n"
            "Review the selected records, then use Merge selected."
        )
        self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"Selected the strongest of {len(groups)} duplicate group(s) in this page.",
        )

    def action_merge(self) -> None:
        references = self._targets()
        if (
            len(references) < 2
            or self.offline
            or not _capability(self.backend.capabilities, "merge")
        ):
            return
        self._begin_operation(
            "Preparing contact merge",
            lambda cancel, progress: self.backend.preview_merge(
                references,
                cancel=cancel,
                progress=progress,
            ),
            self._merge_preview_loaded,
            retry_safe=True,
        )

    def _merge_preview_loaded(self, result: object) -> None:
        if not isinstance(result, ContactMergePreviewView):
            raise PublicOperationError("The Contacts backend returned an invalid merge preview.")
        self._confirm(
            "Apply this contact merge?",
            result.render()
            + "\n\nThe primary is updated first; duplicates then move to Deleted Items.",
            lambda: self._start_merge(result),
            confirm_label="Merge",
        )

    def _start_merge(self, preview: ContactMergePreviewView) -> None:
        self._begin_operation(
            "Applying contact merge",
            lambda cancel, progress: self.backend.apply_merge(
                preview,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="merge"),
        )

    def action_import(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "import"):
            return
        self.app.push_screen(
            FilePathScreen("Import contacts", "Path to .vcf or .vcard file"),
            self._import_path,
        )

    def _import_path(self, path: str | None) -> None:
        if path is None:
            return
        self._confirm(
            "Import contacts from this vCard file?",
            f"Source: {clean_cell(path, max_length=400)}\n"
            "The operation checks one mailbox contact page for duplicates and imports "
            "at most 200 records.",
            lambda: self._start_import(path),
            confirm_label="Import",
        )

    def _start_import(self, path: str) -> None:
        self._begin_operation(
            "Importing contacts",
            lambda cancel, progress: self.backend.import_vcard(
                path,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="import"),
        )

    def action_export(self) -> None:
        references = self._targets()
        if not references or not _capability(self.backend.capabilities, "export"):
            return
        self.app.push_screen(
            FilePathScreen("Export selected contacts", "New .vcf destination"),
            lambda path: self._export_path(references, path),
        )

    def _export_path(self, references: tuple[str, ...], path: str | None) -> None:
        if path is None:
            return
        self._begin_operation(
            "Exporting contacts",
            lambda cancel, progress: self.backend.export_vcard(
                references,
                path,
                cancel=cancel,
                progress=progress,
            ),
            self._export_finished,
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            raise PublicOperationError("The Contacts backend returned an invalid export result.")
        self._set_view_state(
            ViewState.READY,
            f"Exported {result.item_count} contacts to {clean_cell(result.path, max_length=300)}.",
        )

    def action_compose(self) -> None:
        references = self._targets()
        recipients = tuple(
            address.email
            for reference in references
            for address in self._by_reference[reference].emails
        )
        if not recipients:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "compose",
                references,
                payload={"recipients": recipients},
            )
        )

    def action_schedule_meeting(self) -> None:
        references = self._targets()
        attendees = tuple(
            address.email
            for reference in references
            for address in self._by_reference[reference].emails
            if address.email.strip()
        )
        if not attendees:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "schedule_meeting",
                references,
                payload={"attendees": tuple(dict.fromkeys(attendees))},
            )
        )

    def action_directory(self) -> None:
        if not self._allow_contact_operation("directory") or self.offline:
            return
        query = self.query_one("#contacts-search", Input).value.strip()
        if not query:
            self._set_view_state(
                ViewState.EMPTY,
                "Enter a name or address in Contact search before checking the directory.",
            )
            self.query_one("#contacts-search", Input).focus()
            return
        self._begin_operation(
            "Searching the GUC directory",
            lambda cancel, progress: tuple(
                self.backend.search_directory(
                    query,
                    limit=min(self.page_size, 100),
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._directory_loaded,
            retry_safe=True,
        )

    def _directory_loaded(self, result: object) -> None:
        if not self._allow_contact_operation("directory"):
            return
        self._contact_report = "directory"
        values = cast(tuple[Address, ...], result)
        if not all(isinstance(value, Address) for value in values):
            raise PublicOperationError("The directory backend returned invalid entries.")
        if not values:
            self.query_one("#contacts-detail", Static).update(
                "No Exchange directory entries matched this lookup."
            )
            self._set_view_state(ViewState.EMPTY, "No directory matches.")
            return
        lines = [f"Exchange directory · {len(values)} result(s)"]
        lines.extend(
            f"{clean_cell(value.name or '(no display name)', max_length=140)} · "
            f"{clean_cell(value.email, max_length=180)} · "
            f"{clean_cell(value.mailbox_type or 'unknown type', max_length=60)}"
            for value in values
        )
        if len(values) > 1:
            lines.append("Ambiguous result: choose a specific address before sending.")
        self.query_one("#contacts-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, "Directory lookup completed without mutation.")

    def action_correspondence(self) -> None:
        if not self._allow_contact_operation("history"):
            return
        references = self._targets()
        if (
            not references
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
        ):
            return
        self._begin_operation(
            "Loading correspondence history",
            lambda cancel, progress: tuple(
                self.backend.correspondence_history(
                    references,
                    limit=self.page_size,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._correspondence_loaded,
            retry_safe=True,
        )

    def _correspondence_loaded(self, result: object) -> None:
        if not self._allow_contact_operation("history"):
            return
        self._contact_report = "history"
        events = cast(tuple[CorrespondenceEvent, ...], result)
        if not all(isinstance(value, CorrespondenceEvent) for value in events):
            raise PublicOperationError("The correspondence backend returned invalid history.")
        if not events:
            self.query_one("#contacts-detail", Static).update(
                "No correspondence was found in the inbox/sent history."
            )
            self._set_view_state(ViewState.EMPTY, "No correspondence history.")
            return
        lines = [f"Correspondence history · {len(events)} message observation(s)"]
        lines.extend(
            f"{event.occurred_at.isoformat()} · {event.direction.value} · "
            f"{clean_cell(event.address.email, max_length=160)} · "
            f"message {clean_cell(event.message_reference or '(unknown)', max_length=120)}"
            for event in events
        )
        self.query_one("#contacts-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, "Correspondence history loaded.")

    def action_open_correspondence(self) -> None:
        """Ask the application shell to render bounded related mail for selected contacts."""

        if not self._allow_contact_operation("history"):
            return
        references = self._targets()[:10]
        recipients = tuple(
            dict.fromkeys(
                address.email.strip()
                for reference in references
                for address in self._by_reference[reference].emails
                if address.email.strip()
            )
        )[:20]
        if (
            not recipients
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
        ):
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "correspondence_messages",
                references,
                payload={"addresses": recipients},
            )
        )

    @on(Input.Submitted, "#contacts-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "contacts-history-index":
            self.post_message(PimActionRequested("contacts", "history_index", ()))
            return
        actions = {
            "contacts-search-button": self.action_refresh,
            "contacts-prev": self.action_previous_page,
            "contacts-next": self.action_next_page,
            "contacts-duplicates": self.action_find_duplicates,
            "contacts-create": self.action_create,
            "contacts-edit": self.action_edit,
            "contacts-delete": self.action_delete,
            "contacts-merge": self.action_merge,
            "contacts-import": self.action_import,
            "contacts-export": self.action_export,
            "contacts-compose": self.action_compose,
            "contacts-meeting": self.action_schedule_meeting,
            "contacts-correspondence": self.action_correspondence,
            "contacts-correspondence-mail": self.action_open_correspondence,
            "contacts-directory": self.action_directory,
            "contacts-refresh": self.action_refresh,
        }
        if event.button.id == "contacts-clear":
            self.query_one("#contacts-search", Input).value = ""
            self.action_refresh()
            return
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


class CorrespondentsScreen(OperationalScreen):
    """Actionable, bounded recent/frequent recipient history."""

    DEFAULT_CSS = """
    CorrespondentsScreen { layout: vertical; }
    #correspondents-root { width: 100%; height: 100%; padding: 1 2; }
    #correspondents-title { height: 2; text-style: bold; }
    #correspondents-content { height: 1fr; min-height: 8; }
    #correspondents-table { width: 3fr; height: 1fr; }
    #correspondents-detail {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1; overflow-y: auto;
    }
    #correspondents-actions { height: auto; min-height: 3; }
    #correspondents-actions Button { margin-right: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("enter", "open_mail", "Open mail", show=False),
        Binding("c", "compose", "Compose", show=False),
    ]

    def __init__(
        self,
        backend: ContactsScreenBackend,
        *,
        order: str,
        page_size: int = 50,
        operation_preferences: Callable[[], ContactOperationPreferences] | None = None,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if order not in {"recent", "frequency"}:
            raise ValueError(f"Unknown correspondent order: {order}")
        super().__init__(
            area_name="contacts",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.order = order
        self.page_size = _bounded_limit(page_size)
        self.operation_preferences = operation_preferences or ContactOperationPreferences
        self._values: dict[str, CorrespondentSummary] = {}
        self._highlighted: str | None = None

    def compose(self) -> ComposeResult:
        label = "Recent recipients" if self.order == "recent" else "Frequent correspondents"
        with Vertical(id="correspondents-root"):
            yield Label(f"Contacts · {label}", id="correspondents-title", markup=False)
            yield Static("Loading correspondence history…", id="area-state", markup=False)
            with Horizontal(id="correspondents-content"):
                yield DataTable(id="correspondents-table", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "Select a correspondent for counts and actions.",
                    id="correspondents-detail",
                    markup=False,
                )
            with HorizontalScroll(id="correspondents-actions"):
                yield Button("Compose", id="correspondents-compose", disabled=True)
                yield Button("Open mail", id="correspondents-open-mail", disabled=True)
                yield Button("Refresh", id="correspondents-refresh")
                yield Button("Close", classes="area-close")
            yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#correspondents-table", DataTable)
        table.add_column("Name", key="name")
        table.add_column("Address", key="address")
        table.add_column("Messages", key="count", width=10)
        table.add_column("In", key="inbound", width=6)
        table.add_column("Out", key="outbound", width=6)
        table.add_column("Last seen", key="last_seen")
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Correspondence history requires the configured mail-history gateway.",
            )
            return
        self.call_after_refresh(self.action_refresh)

    def _history_allowed(self) -> bool:
        try:
            self.operation_preferences().require(
                "frequent" if self.order == "frequency" else "history"
            )
        except ConfigurationError as exc:
            self._values.clear()
            self._highlighted = None
            self.query_one("#correspondents-table", DataTable).clear()
            self.query_one("#correspondents-detail", Static).update("History access is disabled.")
            self.query_one("#correspondents-compose", Button).disabled = True
            self.query_one("#correspondents-open-mail", Button).disabled = True
            self._show_error("Contacts preferences", exc)
            return False
        return True

    def action_refresh(self) -> None:
        if self.offline or not self._history_allowed():
            return
        self._begin_operation(
            "Loading correspondents",
            lambda cancel, progress: tuple(
                self.backend.list_correspondents(
                    self.order,
                    self.page_size,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._loaded,
            retry_safe=True,
        )

    def on_screen_resume(self) -> None:
        # Hide old results immediately if privacy changed while another
        # operation was in flight. Late results recheck the same policy.
        if self.is_mounted and self._history_allowed() and not self._operation_active:
            self.action_refresh()

    def _loaded(self, result: object) -> None:
        if not self._history_allowed():
            return
        values = cast(tuple[CorrespondentSummary, ...], result)
        if not all(isinstance(value, CorrespondentSummary) for value in values):
            raise PublicOperationError("The Contacts backend returned invalid correspondents.")
        self._values = {value.address.email.casefold(): value for value in values}
        table = self.query_one("#correspondents-table", DataTable)
        table.clear()
        for key, value in self._values.items():
            table.add_row(
                clean_cell(value.address.name or "(no display name)", max_length=100),
                clean_cell(value.address.email, max_length=160),
                str(value.count),
                str(value.inbound_count),
                str(value.outbound_count),
                value.last_seen.isoformat(timespec="minutes"),
                key=key,
            )
        if not values:
            self._highlighted = None
            self.query_one("#correspondents-detail", Static).update(
                "No correspondence history is available yet."
            )
            self._set_actions(False)
            self._set_view_state(ViewState.EMPTY, "No correspondents found.")
            return
        self._select(values[0].address.email.casefold())
        ordering = "most recent first" if self.order == "recent" else "highest frequency first"
        self._set_view_state(
            ViewState.READY,
            f"{len(values)} correspondent(s) · {ordering}.",
        )

    @on(DataTable.RowHighlighted, "#correspondents-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#correspondents-table")
    def _row_selected(self) -> None:
        self.action_open_mail()

    def _select(self, key: str) -> None:
        value = self._values.get(key)
        if value is None:
            return
        self._highlighted = key
        self.query_one("#correspondents-detail", Static).update(
            "\n".join(
                (
                    clean_cell(value.address.name or "(no display name)", max_length=160),
                    clean_cell(value.address.email, max_length=200),
                    f"Messages: {value.count}",
                    f"Inbound: {value.inbound_count}",
                    f"Outbound: {value.outbound_count}",
                    f"First seen: {value.first_seen.isoformat()}",
                    f"Last seen: {value.last_seen.isoformat()}",
                )
            )
        )
        self._set_actions(True)

    def _set_actions(self, enabled: bool) -> None:
        self.query_one("#correspondents-compose", Button).disabled = not enabled
        self.query_one("#correspondents-open-mail", Button).disabled = not enabled

    def _selected(self) -> CorrespondentSummary | None:
        return self._values.get(self._highlighted or "")

    @property
    def selected_contact_value(self) -> str | None:
        value = self._selected()
        return value.address.email if value is not None else None

    def action_compose(self) -> None:
        value = self._selected()
        if value is None:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "compose",
                (),
                payload={"recipients": (value.address.email,)},
            )
        )

    def action_open_mail(self) -> None:
        if not self._history_allowed():
            return
        value = self._selected()
        if value is None:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "correspondence_messages",
                (),
                payload={"addresses": (value.address.email,)},
            )
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "correspondents-compose": self.action_compose,
            "correspondents-open-mail": self.action_open_mail,
            "correspondents-refresh": self.action_refresh,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


_PHONE_LABELS = {
    "businessphone": "Work",
    "businessphone2": "Work 2",
    "businessfax": "Work fax",
    "homephone": "Home",
    "homephone2": "Home 2",
    "mobilephone": "Mobile",
    "otherphone": "Other",
    "pager": "Pager",
    "assistantphone": "Assistant",
    "companymainphone": "Company",
    "primaryphone": "Main",
}


_ADDRESS_LABELS = {"business": "Work", "home": "Home", "other": "Other"}


def _labelled(values: Mapping[str, str], labels: Mapping[str, str], *, max_length: int) -> str:
    return ", ".join(
        f"{labels.get(key.casefold(), clean_cell(key, max_length=40))} "
        f"{clean_cell(value, max_length=max_length)}"
        for key, value in values.items()
    )


def _render_contact(
    contact: ContactInfo,
    *,
    complete: bool = False,
    preferences: ContactsDisplayPreferences | None = None,
) -> str:
    display = preferences or ContactsDisplayPreferences()
    lines = [clean_cell(display.label(contact), max_length=180)]

    def add(label: str, value: str | None) -> None:
        if value:
            lines.append(f"{label}: {value}")

    add("Email", ", ".join(clean_cell(item.email, max_length=160) for item in contact.emails))
    add("Phone", _labelled(contact.phones, _PHONE_LABELS, max_length=80))
    add("Company", clean_cell(contact.company or "", max_length=160))
    add("Department", clean_cell(contact.department or "", max_length=120))
    add("Job title", clean_cell(contact.job_title or "", max_length=120))
    add("Categories", ", ".join(clean_cell(item, max_length=80) for item in contact.categories))
    if complete:
        if contact.display_name and contact.display_name != display.label(contact):
            add("Saved as", clean_cell(contact.display_name, max_length=180))
        add("Nickname", clean_cell(contact.nickname or "", max_length=100))
        if contact.birthday:
            add("Birthday", f"{contact.birthday.day} {contact.birthday.strftime('%B')}")
        add("Address", _labelled(contact.addresses, _ADDRESS_LABELS, max_length=240))
        if contact.notes:
            lines.extend(("", clean_cell(contact.notes, max_length=2_000)))
    return "\n".join(lines)
