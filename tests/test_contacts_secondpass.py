from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from textual.app import App
from textual.widgets import DataTable, Input, Static

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.ews.adapter import EWSGateway
from gucmail.ews.mapping import map_contact
from gucmail.models import Address, ContactInfo
from gucmail.services.contacts import (
    ContactCreatePlan,
    ContactDeletePlan,
    ContactQuery,
    ContactsGateway,
    ContactsService,
    ContactUpdatePlan,
    CorrespondenceDirection,
    CorrespondenceEvent,
    contact_search_text,
)
from gucmail.tui.pim_screens import (
    ContactEditorScreen,
    ContactsScreen,
    ContactsServiceBackend,
    CorrespondentsScreen,
    PimActionRequested,
)


class _ContactsGateway:
    def __init__(self) -> None:
        self.contact = ContactInfo(
            reference="contact-1",
            display_name="Ada Example",
            middle_name="Synthetic",
            nickname="AE",
            emails=(Address("ada@example.test", "Ada Example"),),
            phones={"mobile": "+20 100 000 0000"},
            addresses={"business": "New Cairo"},
            company="Example University",
            department="Computing",
            job_title="Professor",
            notes="Research collaborator",
            categories=("Faculty",),
        )
        now = datetime(2026, 8, 22, 12, tzinfo=UTC)
        self.history = (
            CorrespondenceEvent(
                self.contact.emails[0],
                now,
                CorrespondenceDirection.OUTBOUND,
                "message-out",
            ),
            CorrespondenceEvent(
                Address("other@example.test"),
                now - timedelta(days=1),
                CorrespondenceDirection.INBOUND,
                "message-other",
            ),
        )

    def list_contacts(self, **_kwargs: object) -> tuple[ContactInfo, ...]:
        return (self.contact,)

    def search_contacts(self, query: ContactQuery, **_kwargs: object) -> tuple[ContactInfo, ...]:
        return (
            (self.contact,)
            if (query.text or "").casefold() in contact_search_text(self.contact).casefold()
            else ()
        )

    def get_contact(self, reference: str) -> ContactInfo:
        assert reference == self.contact.reference
        return self.contact

    def create_contact(self, plan: ContactCreatePlan) -> ContactInfo:
        return plan.contact

    def update_contact(self, plan: ContactUpdatePlan) -> ContactInfo:
        self.contact = replace(self.contact, **plan.changes)
        return self.contact

    def delete_contact(self, plan: ContactDeletePlan) -> None:
        assert plan.reference == self.contact.reference

    def search_directory(self, query: str, *, limit: int = 25) -> tuple[Address, ...]:
        del query, limit
        return (
            Address("ada.one@example.test", "Ada One", "Mailbox"),
            Address("ada.two@example.test", "Ada Two", "Mailbox"),
        )

    def list_correspondence(
        self, *, since: datetime | None, limit: int
    ) -> tuple[CorrespondenceEvent, ...]:
        return tuple(
            event for event in self.history if since is None or event.occurred_at >= since
        )[:limit]


class _DeferredContactsGateway:
    def __init__(self) -> None:
        self.resolution_attempts = 0

    def __getattr__(self, _name: str) -> object:
        self.resolution_attempts += 1
        raise AssertionError("Screen construction must not resolve a deferred gateway")


def test_contact_search_directory_ambiguity_and_correspondence_are_bounded() -> None:
    gateway = _ContactsGateway()
    service = ContactsService(gateway, correspondence_gateway=gateway)

    assert service.search("collaborator")[0].reference == "contact-1"
    assert "new cairo" in contact_search_text(gateway.contact).casefold()
    assert len(service.search_directory("Ada", limit=10)) == 2
    with pytest.raises(ConfigurationError, match="ambiguous"):
        service.resolve_directory("Ada")

    history = service.correspondence_history(gateway.contact.emails, limit=1)
    assert [event.message_reference for event in history] == ["message-out"]


def test_contacts_backend_capability_check_does_not_resolve_deferred_gateway() -> None:
    gateway = _DeferredContactsGateway()

    backend = ContactsServiceBackend(
        ContactsService(cast(ContactsGateway, gateway), correspondence_gateway=None)
    )

    assert gateway.resolution_attempts == 0
    assert backend.capabilities["directory"].available


def test_contact_mapping_retains_photo_metadata() -> None:
    mapped = map_contact(
        SimpleNamespace(
            id="ews-contact",
            display_name="Photo Contact",
            email_addresses=(),
            phone_numbers=(),
            physical_addresses=(),
            categories=(),
            has_picture=True,
            photo=SimpleNamespace(size=512, content_type="image/jpeg"),
        )
    )

    assert mapped.has_photo
    assert mapped.photo_size == 512
    assert mapped.photo_content_type == "image/jpeg"


def test_ews_contact_labels_are_mapped_to_supported_index_labels() -> None:
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    gateway._construct = lambda _kind, **values: SimpleNamespace(**values)  # type: ignore[method-assign]

    indexed = gateway._contact_indexed(
        ContactInfo(
            "contact-labels",
            "Label Test",
            phones={"mobile": "1", "custom desk": "2"},
            addresses={"work": "Office", "campus": "Campus"},
        )
    )

    assert [item.label for item in indexed["phone_numbers"]] == [
        "MobilePhone",
        "BusinessPhone",
    ]
    assert [item.label for item in indexed["physical_addresses"]] == [
        "Business",
        "Home",
    ]


def test_ews_directory_denial_is_reported_instead_of_becoming_no_results() -> None:
    error_type = type("ErrorAccessDenied", (Exception,), {})
    account = SimpleNamespace(
        protocol=SimpleNamespace(
            resolve_names=lambda *_args, **_kwargs: [error_type("synthetic denial")],
            close=lambda: None,
        )
    )
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)

    with pytest.raises(UnsupportedFeatureError, match="does not support or permit"):
        gateway.search_directory("Ada")


def test_ews_correspondence_uses_body_free_bounded_folder_projections() -> None:
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    inbound = SimpleNamespace(
        id="inbound",
        changekey="ck-in",
        sender=SimpleNamespace(email_address="ada@example.test", name="Ada"),
        author=None,
        to_recipients=(),
        cc_recipients=(),
        bcc_recipients=(),
        datetime_received=now,
        datetime_sent=None,
    )
    outbound = SimpleNamespace(
        id="outbound",
        changekey="ck-out",
        sender=None,
        author=None,
        to_recipients=(SimpleNamespace(email_address="ada@example.test", name="Ada"),),
        cc_recipients=(),
        bcc_recipients=(),
        datetime_received=None,
        datetime_sent=now - timedelta(hours=1),
    )

    def folder(name: str, item: object) -> SimpleNamespace:
        return SimpleNamespace(
            id=name,
            changekey=f"{name}-ck",
            name=name,
            all=lambda: (item,),
        )

    account = SimpleNamespace(
        inbox=folder("Inbox", inbound),
        sent=folder("Sent", outbound),
        protocol=SimpleNamespace(close=lambda: None),
    )
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)

    history = gateway.list_correspondence(since=None, limit=10)

    assert [event.message_reference for event in history]
    assert [event.direction for event in history] == [
        CorrespondenceDirection.INBOUND,
        CorrespondenceDirection.OUTBOUND,
    ]


class _ContactsHarness(App[None]):
    def __init__(self, backend: ContactsServiceBackend) -> None:
        super().__init__()
        self.backend = backend
        self.pim_actions: list[PimActionRequested] = []

    def on_mount(self) -> None:
        self.push_screen(ContactsScreen(self.backend))

    def on_pim_action_requested(self, event: PimActionRequested) -> None:
        self.pim_actions.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (79, 26)])
async def test_contacts_screen_directory_correspondence_and_responsiveness(
    size: tuple[int, int],
) -> None:
    gateway = _ContactsGateway()
    backend = ContactsServiceBackend(ContactsService(gateway, correspondence_gateway=gateway))
    app = _ContactsHarness(backend)

    async with app.run_test(size=size) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            screen = app.screen
            if (
                isinstance(screen, ContactsScreen)
                and screen.query_one("#contacts-table", DataTable).row_count
            ):
                break
        contacts_screen = app.screen
        assert isinstance(contacts_screen, ContactsScreen)
        assert contacts_screen.query_one("#contacts-root").region.size == size
        assert contacts_screen.query_one("#contacts-content").region.height > 0

        contacts_screen.query_one("#contacts-search", Input).value = "Ada"
        contacts_screen.action_directory()
        for _ in range(30):
            await pilot.pause(0.05)
            if "Exchange directory" in str(
                contacts_screen.query_one("#contacts-detail", Static).content
            ):
                break
        assert "Ambiguous result" in str(
            contacts_screen.query_one("#contacts-detail", Static).content
        )

        contacts_screen.action_correspondence()
        for _ in range(30):
            await pilot.pause(0.05)
            if "Correspondence history" in str(
                contacts_screen.query_one("#contacts-detail", Static).content
            ):
                break
        assert "message-out" in str(contacts_screen.query_one("#contacts-detail", Static).content)

        contacts_screen.action_open_correspondence()
        await pilot.pause()
        assert len(app.pim_actions) == 1
        assert app.pim_actions[0].area == "contacts"
        assert app.pim_actions[0].action == "correspondence_messages"
        assert app.pim_actions[0].references == ("contact-1",)
        assert app.pim_actions[0].payload == {"addresses": ("ada@example.test",)}

        contacts_screen.action_schedule_meeting()
        await pilot.pause()
        assert app.pim_actions[-1].action == "schedule_meeting"
        assert app.pim_actions[-1].payload == {"attendees": ("ada@example.test",)}


@pytest.mark.asyncio
async def test_contact_create_can_be_prefilled_from_a_message_sender() -> None:
    gateway = _ContactsGateway()
    app = _ContactsHarness(
        ContactsServiceBackend(ContactsService(gateway, correspondence_gateway=gateway))
    )

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ContactsScreen) and app.screen.is_mounted:
                break
        screen = app.screen
        assert isinstance(screen, ContactsScreen)
        screen.action_create_prefilled(Address("sender@example.test", "Selected Sender"))
        await pilot.pause()
        assert isinstance(app.screen, ContactEditorScreen)
        assert app.screen.query_one("#contact-edit-name", Input).value == "Selected Sender"
        assert app.screen.query_one("#contact-edit-emails", Input).value == "sender@example.test"
        assert "Create" in str(app.screen.query_one("#contact-editor-title", Static).content)


class _CorrespondentsHarness(App[None]):
    def __init__(self, screen: CorrespondentsScreen) -> None:
        super().__init__()
        self.screen_to_open = screen
        self.pim_actions: list[PimActionRequested] = []

    def on_mount(self) -> None:
        self.push_screen(self.screen_to_open)

    def on_pim_action_requested(self, event: PimActionRequested) -> None:
        self.pim_actions.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["recent", "frequency"])
async def test_correspondents_screen_is_bounded_actionable_and_responsive(order: str) -> None:
    gateway = _ContactsGateway()
    backend = ContactsServiceBackend(ContactsService(gateway, correspondence_gateway=gateway))
    screen = CorrespondentsScreen(backend, order=order, page_size=25)
    app = _CorrespondentsHarness(screen)

    async with app.run_test(size=(79, 26)) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if screen.query_one("#correspondents-table", DataTable).row_count == 2:
                break
        assert screen.query_one("#correspondents-table", DataTable).row_count == 2
        assert screen.query_one("#correspondents-content").region.height > 0
        assert "Messages:" in str(screen.query_one("#correspondents-detail", Static).content)

        screen.action_compose()
        screen.action_open_mail()
        await pilot.pause()
        assert [event.action for event in app.pim_actions] == [
            "compose",
            "correspondence_messages",
        ]
