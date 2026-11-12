from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from gucmail.errors import ConfigurationError
from gucmail.ews.capabilities import DEFAULT_REGISTRY
from gucmail.models import Address, CalendarEvent, ContactInfo, TaskInfo
from gucmail.services.calendar import (
    CalendarImportPlan,
    CalendarPatch,
    CalendarService,
    EventCreatePlan,
    EventDeletePlan,
    EventUpdatePlan,
    InvitationPolicy,
    MeetingResponsePlan,
)
from gucmail.services.contacts import (
    ContactCreatePlan,
    ContactDeletePlan,
    ContactImportPlan,
    ContactPatch,
    ContactQuery,
    ContactsService,
    ContactUpdatePlan,
    DuplicateCandidate,
)
from gucmail.services.exchange_values import (
    exchange_email_label,
    exchange_phone_label,
    exchange_postal_label,
    normalize_availability,
    to_exchange_availability,
)
from gucmail.services.pim import MAX_PIM_PAGE_SIZE, BatchFailure, bounded_pim_limit
from gucmail.services.tasks import (
    TaskCreatePlan,
    TaskPatch,
    TaskQuery,
    TaskService,
    TaskUpdatePlan,
)


def _event(reference: str, *, hour: int = 10) -> CalendarEvent:
    start = datetime(2026, 8, 10, hour, tzinfo=UTC)
    return CalendarEvent(
        reference=reference,
        subject=f"Event {reference}",
        start=start,
        end=start + timedelta(hours=1),
        timezone="UTC",
    )


class CalendarGatewayFake:
    def __init__(self, events: Sequence[CalendarEvent] = ()) -> None:
        self.events = {event.reference: event for event in events}
        self.limits: list[int] = []
        self.fail_create_reference: str | None = None

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> Sequence[CalendarEvent]:
        del start, end, calendar_reference
        self.limits.append(limit)
        return tuple(self.events.values())[:limit]

    def get_event(self, reference: str) -> CalendarEvent:
        return self.events[reference]

    def create_event(self, plan: EventCreatePlan) -> CalendarEvent:
        if plan.event.reference == self.fail_create_reference:
            raise TimeoutError("synthetic timeout")
        self.events[plan.event.reference] = plan.event
        return plan.event

    def update_event(self, plan: EventUpdatePlan) -> CalendarEvent:
        current = self.events[plan.reference]
        updated = replace(current, **plan.changes)
        self.events[plan.reference] = updated
        return updated

    def delete_event(self, plan: EventDeletePlan) -> None:
        self.events.pop(plan.reference)

    def respond_to_meeting(self, plan: MeetingResponsePlan) -> CalendarEvent | None:
        return self.events.get(plan.reference)


class ContactsGatewayFake:
    def __init__(self, contacts: Sequence[ContactInfo] = ()) -> None:
        self.contacts = {contact.reference: contact for contact in contacts}
        self.limits: list[int] = []
        self.deletes: list[str] = []
        self.fail_delete_reference: str | None = None

    def list_contacts(self, *, folder_reference: str | None, limit: int) -> Sequence[ContactInfo]:
        del folder_reference
        self.limits.append(limit)
        return tuple(self.contacts.values())[:limit]

    def search_contacts(
        self,
        query: ContactQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> Sequence[ContactInfo]:
        del query, folder_reference
        self.limits.append(limit)
        return tuple(self.contacts.values())[:limit]

    def get_contact(self, reference: str) -> ContactInfo:
        return self.contacts[reference]

    def create_contact(self, plan: ContactCreatePlan) -> ContactInfo:
        self.contacts[plan.contact.reference] = plan.contact
        return plan.contact

    def update_contact(self, plan: ContactUpdatePlan) -> ContactInfo:
        updated = replace(self.contacts[plan.reference], **plan.changes)
        self.contacts[plan.reference] = updated
        return updated

    def delete_contact(self, plan: ContactDeletePlan) -> None:
        if plan.reference == self.fail_delete_reference:
            raise TimeoutError("synthetic timeout")
        self.deletes.append(plan.reference)
        self.contacts.pop(plan.reference, None)


class TasksGatewayFake:
    def __init__(self, tasks: Sequence[TaskInfo] = ()) -> None:
        self.tasks = {task.reference: task for task in tasks}
        self.limits: list[int] = []

    def list_tasks(self, *, folder_reference: str | None, limit: int) -> Sequence[TaskInfo]:
        del folder_reference
        self.limits.append(limit)
        return tuple(self.tasks.values())[:limit]

    def search_tasks(
        self,
        query: TaskQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> Sequence[TaskInfo]:
        del query, folder_reference
        self.limits.append(limit)
        return tuple(self.tasks.values())[:limit]

    def get_task(self, reference: str) -> TaskInfo:
        return self.tasks[reference]

    def create_task(self, plan: TaskCreatePlan) -> TaskInfo:
        self.tasks[plan.task.reference] = plan.task
        return plan.task

    def update_task(self, plan: TaskUpdatePlan) -> TaskInfo:
        updated = replace(self.tasks[plan.reference], **plan.changes)
        self.tasks[plan.reference] = updated
        return updated

    def delete_task(self, plan: object) -> None:
        del plan


def test_exchange_value_helpers_emit_exact_indexed_choices() -> None:
    from exchangelib.indexed_properties import PhoneNumber, PhysicalAddress
    from exchangelib.items import CalendarItem

    assert normalize_availability("Working Elsewhere") == "working_elsewhere"
    assert to_exchange_availability("working_elsewhere") == "WorkingElsewhere"
    assert to_exchange_availability("out-of-office") == "OOF"
    assert (
        CalendarItem(
            legacy_free_busy_status=to_exchange_availability("working_elsewhere")
        ).legacy_free_busy_status
        == "WorkingElsewhere"
    )
    assert exchange_phone_label("mobile") == "MobilePhone"
    assert exchange_phone_label("work fax") == "BusinessFax"
    assert exchange_postal_label("work") == "Business"
    assert exchange_postal_label("home") == "Home"
    assert exchange_email_label("primary") == "EmailAddress1"
    assert exchange_email_label(3) == "EmailAddress3"
    assert (
        PhoneNumber(label=exchange_phone_label("mobile"), phone_number="+20 100 000 0000").label
        == "MobilePhone"
    )
    assert PhysicalAddress(label=exchange_postal_label("home"), street="Synthetic").label == "Home"
    with pytest.raises(ConfigurationError, match="Unsupported contact phone label"):
        exchange_phone_label("satellite uplink")


def test_recurrence_capabilities_match_adapter_scope_support() -> None:
    calendar = DEFAULT_REGISTRY.get("CalendarRecurrence")
    assert calendar.implementation_status.value == "implemented"
    assert "does not claim this-and-future" in calendar.reason
    assert "lossless exception-history restore" in calendar.reason
    assert DEFAULT_REGISTRY.get("TaskRecurrence").implementation_status.value == "partial"


def test_batch_failure_detail_is_terminal_safe_and_redacted() -> None:
    failure = BatchFailure.from_exception(
        index=0,
        operation="create contact",
        reference="c1",
        error=TimeoutError("password=hunter2 for owner@example.test\x1b[31m"),
    )

    assert "hunter2" not in failure.detail
    assert "owner@example.test" not in failure.detail
    assert "\x1b" not in failure.detail


def test_single_page_pim_limits_are_clamped_before_gateway_calls() -> None:
    calendar_gateway = CalendarGatewayFake((_event("e1"),))
    CalendarService(calendar_gateway, default_limit=500).agenda(start=date(2026, 8, 10), days=1)
    contacts_gateway = ContactsGatewayFake((ContactInfo("c1", "Contact"),))
    ContactsService(contacts_gateway, default_limit=500).list(limit=999)
    tasks_gateway = TasksGatewayFake((TaskInfo("t1", "Task", "not_started"),))
    TaskService(tasks_gateway, default_limit=500).list(limit=999)

    assert calendar_gateway.limits == [MAX_PIM_PAGE_SIZE]
    assert contacts_gateway.limits == [MAX_PIM_PAGE_SIZE]
    assert tasks_gateway.limits == [MAX_PIM_PAGE_SIZE]
    assert bounded_pim_limit(1_000, default=20, subject="Test") == MAX_PIM_PAGE_SIZE
    with pytest.raises(ConfigurationError, match="positive"):
        bounded_pim_limit(0, default=20, subject="Test")


def test_calendar_patch_loads_current_state_for_cross_field_validation() -> None:
    event = _event("e1")
    gateway = CalendarGatewayFake((event,))
    service = CalendarService(gateway)

    with pytest.raises(ConfigurationError, match="end after it starts"):
        service.plan_update(
            event.reference,
            CalendarPatch(end=event.start - timedelta(minutes=1)),
        )


def test_contact_patch_loads_current_state_before_allowing_clear() -> None:
    contact = ContactInfo("c1", "Synthetic", emails=(Address("one@example.test"),))
    service = ContactsService(ContactsGatewayFake((contact,)))

    with pytest.raises(ConfigurationError, match="display name"):
        service.plan_update(contact.reference, ContactPatch(display_name=" "))


def test_task_patch_loads_current_state_for_lifecycle_validation() -> None:
    task = TaskInfo("t1", "Synthetic", status="not_started", percent_complete=0)
    service = TaskService(TasksGatewayFake((task,)))

    with pytest.raises(ConfigurationError, match="100 percent task"):
        service.plan_update(task.reference, TaskPatch(percent_complete=100))


def test_calendar_import_result_reports_partial_success_without_retrying() -> None:
    first, second, third = _event("e1"), _event("e2", hour=12), _event("e3", hour=14)
    gateway = CalendarGatewayFake()
    gateway.fail_create_reference = second.reference
    service = CalendarService(gateway)
    plan = CalendarImportPlan(
        (first, second, third),
        None,
        InvitationPolicy.SEND_NONE,
    )

    result = service.apply_import_result(plan)

    assert result.attempted == 3
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.is_partial
    assert result.completed[0].reference == first.reference
    assert result.failures[0].reference == second.reference
    assert result.failures[0].retry_safe is False
    assert result.skipped_references == (third.reference,)


def test_contact_import_result_blocks_imported_side_of_existing_duplicate() -> None:
    existing = ContactInfo(
        "existing",
        "Existing",
        emails=(Address("same@example.test"),),
    )
    imported = ContactInfo(
        "imported",
        "Imported",
        emails=(Address("same@example.test"),),
    )
    duplicate = DuplicateCandidate(existing, imported, 80, ("shared email address",))
    gateway = ContactsGatewayFake((existing,))
    service = ContactsService(gateway)
    plan = ContactImportPlan((imported,), (duplicate,))

    result = service.apply_import_result(plan)

    assert result.succeeded == 0
    assert result.failed == 0
    assert result.skipped_references == (imported.reference,)
    assert imported.reference not in gateway.contacts


def test_contact_merge_result_preserves_partial_delete_outcome() -> None:
    primary = ContactInfo("c1", "Primary")
    duplicate_one = ContactInfo("c2", "Duplicate one")
    duplicate_two = ContactInfo("c3", "Duplicate two")
    gateway = ContactsGatewayFake((primary, duplicate_one, duplicate_two))
    gateway.fail_delete_reference = duplicate_one.reference
    service = ContactsService(gateway)
    preview = service.merge_preview((primary, duplicate_one, duplicate_two))
    plan = service.plan_merge(preview)

    result = service.apply_merge_result(plan)

    assert result.attempted == 3
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.failures[0].reference == duplicate_one.reference
    assert result.skipped_references == (duplicate_two.reference,)
    assert gateway.deletes == []
