from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gucmail.errors import ConfigurationError
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    TaskInfo,
)
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    EventCreatePlan,
    EventDeletePlan,
    EventUpdatePlan,
    MeetingResponsePlan,
    detect_conflicts,
    localize_datetime,
)
from gucmail.services.contacts import (
    ContactCreatePlan,
    ContactDeletePlan,
    ContactPatch,
    ContactQuery,
    ContactsService,
    ContactUpdatePlan,
    CorrespondenceDirection,
    CorrespondenceEvent,
    duplicate_score,
    find_duplicate_groups,
    preview_merge,
)
from gucmail.services.interchange import export_ics, export_vcards, parse_ics, parse_vcards
from gucmail.services.tasks import (
    TaskCreatePlan,
    TaskDeletePlan,
    TaskPatch,
    TaskQuery,
    TaskService,
    TaskUpdatePlan,
    normalize_task,
)


def _event(
    reference: str,
    start: datetime,
    end: datetime,
    *,
    subject: str | None = None,
    availability: str = "busy",
    timezone: str = "UTC",
) -> CalendarEvent:
    return CalendarEvent(
        reference=reference,
        subject=subject or f"Event {reference}",
        start=start,
        end=end,
        timezone=timezone,
        availability=availability,
    )


class CalendarMemoryGateway:
    def __init__(self, events: tuple[CalendarEvent, ...] = ()) -> None:
        self.events = {event.reference: event for event in events}
        self.queries: list[tuple[datetime, datetime, str | None, int]] = []
        self.responses: list[MeetingResponsePlan] = []

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> tuple[CalendarEvent, ...]:
        self.queries.append((start, end, calendar_reference, limit))
        result = [
            event
            for event in self.events.values()
            if event.start.astimezone(UTC) < end and event.end.astimezone(UTC) > start
        ]
        return tuple(result[:limit])

    def get_event(self, reference: str) -> CalendarEvent:
        return self.events[reference]

    def create_event(self, plan: EventCreatePlan) -> CalendarEvent:
        self.events[plan.event.reference] = plan.event
        return plan.event

    def update_event(self, plan: EventUpdatePlan) -> CalendarEvent:
        updated = replace(self.events[plan.reference], **plan.changes)
        self.events[plan.reference] = updated
        return updated

    def delete_event(self, plan: EventDeletePlan) -> None:
        self.events.pop(plan.reference)

    def respond_to_meeting(self, plan: MeetingResponsePlan) -> CalendarEvent | None:
        self.responses.append(plan)
        return self.events.get(plan.reference)


def test_calendar_rejects_nonexistent_and_ambiguous_dst_wall_times() -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        localize_datetime(datetime(2026, 3, 29, 2, 30), "Europe/Berlin")
    with pytest.raises(ConfigurationError, match="occurs twice"):
        localize_datetime(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")
    explicit = datetime.fromisoformat("2026-10-25T02:30:00+01:00")
    assert localize_datetime(explicit, "Europe/Berlin").fold == 1


def test_calendar_day_uses_dst_safe_utc_window_and_shapes_spanning_events() -> None:
    zone = ZoneInfo("Europe/Berlin")
    event = _event(
        "overnight",
        datetime(2026, 3, 28, 23, 30, tzinfo=zone),
        datetime(2026, 3, 29, 4, 0, tzinfo=zone),
        timezone="Europe/Berlin",
    )
    gateway = CalendarMemoryGateway((event,))
    service = CalendarService(gateway, display_timezone="Europe/Berlin")

    view = service.day(date(2026, 3, 29))

    assert view.days[0].events == (event,)
    start, end, _, _ = gateway.queries[0]
    assert start == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


def test_calendar_views_span_a_skipped_local_midnight() -> None:
    # Africa/Cairo starts daylight saving at local midnight on 2027-04-30, so that
    # wall time never exists. Views, agenda windows, and calendar search over that
    # date must use the first real instant of the day instead of failing.
    zone = ZoneInfo("Africa/Cairo")
    event = _event(
        "after-gap",
        datetime(2027, 4, 30, 9, tzinfo=zone),
        datetime(2027, 4, 30, 10, tzinfo=zone),
        timezone="Africa/Cairo",
    )
    gateway = CalendarMemoryGateway((event,))
    service = CalendarService(gateway, display_timezone="Africa/Cairo")

    view = service.day(date(2027, 4, 30))
    assert view.days[0].events == (event,)
    start, end, _, _ = gateway.queries[0]
    assert start == datetime(2027, 4, 29, 22, 0, tzinfo=UTC)
    assert end == datetime(2027, 4, 30, 21, 0, tzinfo=UTC)
    assert service.day(date(2027, 4, 29)).end.astimezone(UTC) == start
    agenda = service.agenda(start=date(2026, 11, 11), days=365)
    assert event in agenda.events
    with pytest.raises(ConfigurationError, match="does not exist"):
        localize_datetime(datetime(2027, 4, 30, 0, 0), "Africa/Cairo")


def test_calendar_week_and_month_views_have_deterministic_buckets() -> None:
    service = CalendarService(CalendarMemoryGateway(), display_timezone="UTC")
    week = service.week(date(2026, 8, 5))
    month = service.month(date(2026, 8, 5), include_adjacent_days=True)

    assert [bucket.date for bucket in week.days] == [
        date(2026, 8, 3) + timedelta(days=index) for index in range(7)
    ]
    assert month.days[0].date == date(2026, 7, 27)
    assert month.days[-1].date == date(2026, 9, 6)
    assert sum(bucket.in_primary_range for bucket in month.days) == 31


def test_calendar_conflicts_are_half_open_and_ignore_free_time() -> None:
    base = datetime(2026, 8, 5, 9, tzinfo=UTC)
    first = _event("a", base, base + timedelta(hours=1))
    adjacent = _event("b", base + timedelta(hours=1), base + timedelta(hours=2))
    overlap = _event("c", base + timedelta(minutes=30), base + timedelta(hours=2))
    free = _event(
        "d",
        base + timedelta(minutes=15),
        base + timedelta(minutes=45),
        availability="free",
    )

    conflicts = detect_conflicts((first, adjacent, overlap, free))

    assert {(item.first.reference, item.second.reference) for item in conflicts} == {
        ("a", "c"),
        ("c", "b"),
    }
    assert conflicts[0].duration == timedelta(minutes=30)


def test_calendar_update_and_meeting_response_are_previewed_plans() -> None:
    meeting = replace(
        _event(
            "meeting",
            datetime(2026, 8, 5, 9, tzinfo=UTC),
            datetime(2026, 8, 5, 10, tzinfo=UTC),
        ),
        is_meeting=True,
        organizer=Address("organizer@example.test", "Organizer"),
    )
    gateway = CalendarMemoryGateway((meeting,))
    service = CalendarService(gateway)

    update = service.plan_update(
        meeting.reference,
        CalendarPatch(location=None),
        current=meeting,
    )
    assert update.update_fields == ("location",)
    assert update.changes["location"] is None
    response = service.plan_response(
        meeting,
        CalendarResponse.TENTATIVE,
        proposed_start=datetime(2026, 8, 5, 11, tzinfo=UTC),
        proposed_end=datetime(2026, 8, 5, 12, tzinfo=UTC),
        send_response=False,
    )
    assert response.proposes_new_time
    assert not response.send_response
    assert gateway.responses == []
    service.respond(response)
    assert gateway.responses == [response]


def test_ics_round_trip_preserves_calendar_fields_and_escaped_text() -> None:
    zone = ZoneInfo("Europe/Berlin")
    original = replace(
        _event(
            "uid-1@example.test",
            datetime(2026, 8, 5, 9, tzinfo=zone),
            datetime(2026, 8, 5, 10, 30, tzinfo=zone),
            subject="Planning, review",
            timezone="Europe/Berlin",
        ),
        body="First line\nSecond; line",
        location="Room 1, Main Building",
        required_attendees=(Address("person@example.test", "Test Person"),),
        is_meeting=True,
        recurrence={"freq": "weekly", "count": 3, "byday": ("WE",)},
        reminder_minutes=15,
        categories=("Project, Alpha",),
    )

    rendered = export_ics(original)
    restored = parse_ics(rendered)[0]

    assert "BEGIN:VEVENT\r\n" in rendered
    assert restored.reference == original.reference
    assert restored.subject == original.subject
    assert restored.body == original.body
    assert restored.location == original.location
    assert restored.required_attendees[0].email == "person@example.test"
    assert restored.recurrence == {"freq": "weekly", "count": 3, "byday": "WE"}
    assert restored.reminder_minutes == 15
    assert restored.timezone == "Europe/Berlin"


def test_ics_import_rejects_nonexistent_dst_time() -> None:
    invalid = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:dst-test
DTSTART;TZID=Europe/Berlin:20260329T023000
DTEND;TZID=Europe/Berlin:20260329T033000
SUMMARY:Impossible time
END:VEVENT
END:VCALENDAR
"""
    with pytest.raises(ConfigurationError, match="does not exist"):
        parse_ics(invalid)


class ContactsMemoryGateway:
    def __init__(self, contacts: tuple[ContactInfo, ...] = ()) -> None:
        self.contacts = {contact.reference: contact for contact in contacts}
        self.updates: list[ContactUpdatePlan] = []
        self.deletes: list[ContactDeletePlan] = []

    def list_contacts(self, *, folder_reference: str | None, limit: int) -> tuple[ContactInfo, ...]:
        del folder_reference
        return tuple(self.contacts.values())[:limit]

    def search_contacts(
        self,
        query: ContactQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> tuple[ContactInfo, ...]:
        del folder_reference
        needle = (query.text or query.email or "").casefold()
        return tuple(
            contact
            for contact in self.contacts.values()
            if needle in contact.display_name.casefold()
            or any(needle in email.email.casefold() for email in contact.emails)
        )[:limit]

    def get_contact(self, reference: str) -> ContactInfo:
        return self.contacts[reference]

    def create_contact(self, plan: ContactCreatePlan) -> ContactInfo:
        self.contacts[plan.contact.reference] = plan.contact
        return plan.contact

    def update_contact(self, plan: ContactUpdatePlan) -> ContactInfo:
        self.updates.append(plan)
        updated = replace(self.contacts[plan.reference], **plan.changes)
        self.contacts[plan.reference] = updated
        return updated

    def delete_contact(self, plan: ContactDeletePlan) -> None:
        self.deletes.append(plan)
        self.contacts.pop(plan.reference)


def _contact(reference: str, name: str, email: str, **kwargs: Any) -> ContactInfo:
    return ContactInfo(reference, name, emails=(Address(email),), **kwargs)


def test_contact_create_edit_and_reopen_use_consistent_indexed_labels() -> None:
    class ExchangeLabelsGateway(ContactsMemoryGateway):
        def create_contact(self, plan: ContactCreatePlan) -> ContactInfo:
            value = replace(
                plan.contact,
                phones={"BusinessPhone": "0000000000"},
                addresses={"Business": "Synthetic address"},
            )
            self.contacts[value.reference] = value
            return value

    service = ContactsService(ExchangeLabelsGateway())
    created = service.create(ContactInfo("synthetic", "Synthetic contact"))
    assert created.phones == {"businessphone": "0000000000"}
    assert created.addresses == {"business": "Synthetic address"}
    assert service.get(created.reference) == created
    edited = service.update(created.reference, ContactPatch(company="Synthetic edit"))
    assert edited.phones == created.phones and edited.addresses == created.addresses
    assert service.get(created.reference) == edited


def test_vcard_round_trip_preserves_contact_fields() -> None:
    original = ContactInfo(
        reference="contact-1",
        display_name="Ada Example",
        given_name="Ada",
        family_name="Example",
        nickname="A",
        emails=(Address("ada@example.test", mailbox_type="work"),),
        phones={"mobile": "+20 100 000 0000"},
        addresses={"home": "1 Example Street, Cairo"},
        company="Example Org",
        department="Research",
        job_title="Engineer",
        birthday=date(1995, 5, 10),
        notes="Line one\nLine two",
        categories=("Friend", "Project"),
    )

    rendered = export_vcards(original)
    restored = parse_vcards(rendered)[0]

    assert restored.reference == original.reference
    assert restored.display_name == original.display_name
    assert restored.emails[0].mailbox_type == "work"
    assert restored.phones == original.phones
    assert restored.addresses == original.addresses
    assert restored.company == original.company
    assert restored.department == original.department
    assert restored.birthday == original.birthday
    assert restored.notes == original.notes


def test_contact_duplicate_detection_and_merge_preview_are_non_mutating() -> None:
    primary = _contact(
        "c1",
        "Ada Example",
        "ada@example.test",
        phones={"mobile": "+20 100 000 0000"},
        company="Example Org",
    )
    duplicate = _contact(
        "c2",
        "Ada E.",
        "ADA@example.test",
        phones={"work": "+20 200 000 0000"},
        company="Other Org",
    )
    unrelated = _contact("c3", "Grace Example", "grace@example.test")

    candidate = duplicate_score(primary, duplicate)
    groups = find_duplicate_groups((primary, duplicate, unrelated))
    preview = preview_merge((primary, duplicate), primary_reference="c1")

    assert candidate.score >= 80
    assert len(groups) == 1
    assert {item.email.casefold() for item in preview.merged.emails} == {"ada@example.test"}
    assert set(preview.merged.phones) == {"mobile", "work"}
    assert any(conflict.field == "company" for conflict in preview.conflicts)
    assert primary.company == "Example Org"
    assert duplicate.company == "Other Org"


def test_contact_patch_distinguishes_clear_from_preserve() -> None:
    contact = _contact(
        "c1",
        "Ada Example",
        "ada@example.test",
        nickname="Ada",
        company="Example Org",
    )
    service = ContactsService(ContactsMemoryGateway((contact,)))

    plan = service.plan_update("c1", ContactPatch(nickname=None), current=contact)

    assert plan.update_fields == ("nickname",)
    assert plan.changes == {"nickname": None}
    with pytest.raises(ConfigurationError, match="No contact fields"):
        service.plan_update("c1", ContactPatch(), current=contact)


def test_recent_and_frequent_correspondents_are_deduplicated_and_ranked() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    observations = (
        CorrespondenceEvent(
            Address("ada@example.test"), now - timedelta(days=2), CorrespondenceDirection.OUTBOUND
        ),
        CorrespondenceEvent(
            Address("ADA@example.test"), now - timedelta(hours=1), CorrespondenceDirection.INBOUND
        ),
        CorrespondenceEvent(
            Address("grace@example.test"),
            now - timedelta(minutes=5),
            CorrespondenceDirection.OUTBOUND,
        ),
    )
    known = (_contact("c1", "Ada Example", "ada@example.test"),)
    service = ContactsService(ContactsMemoryGateway())

    recent = service.recent_correspondents(observations, known_contacts=known)
    frequent = service.frequent_correspondents(observations, known_contacts=known)

    assert [item.address.email.casefold() for item in recent] == [
        "grace@example.test",
        "ada@example.test",
    ]
    assert frequent[0].address.name == "Ada Example"
    assert frequent[0].count == 2
    assert frequent[0].inbound_count == 1
    assert frequent[0].outbound_count == 1


class TasksMemoryGateway:
    def __init__(self, tasks: tuple[TaskInfo, ...] = ()) -> None:
        self.tasks = {task.reference: task for task in tasks}
        self.updates: list[TaskUpdatePlan] = []

    def list_tasks(self, *, folder_reference: str | None, limit: int) -> tuple[TaskInfo, ...]:
        del folder_reference
        return tuple(self.tasks.values())[:limit]

    def search_tasks(
        self,
        query: TaskQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> tuple[TaskInfo, ...]:
        del folder_reference
        needle = (query.text or "").casefold()
        return tuple(task for task in self.tasks.values() if needle in task.subject.casefold())[
            :limit
        ]

    def get_task(self, reference: str) -> TaskInfo:
        return self.tasks[reference]

    def create_task(self, plan: TaskCreatePlan) -> TaskInfo:
        self.tasks[plan.task.reference] = plan.task
        return plan.task

    def update_task(self, plan: TaskUpdatePlan) -> TaskInfo:
        self.updates.append(plan)
        updated = replace(self.tasks[plan.reference], **plan.changes)
        self.tasks[plan.reference] = updated
        return updated

    def delete_task(self, plan: TaskDeletePlan) -> None:
        self.tasks.pop(plan.reference)


def _task(reference: str = "task-1") -> TaskInfo:
    return TaskInfo(
        reference=reference,
        subject="Submit report",
        status="not_started",
        percent_complete=0,
        start_date=date(2026, 8, 5),
        due_date=date(2026, 8, 10),
    )


def test_task_patch_uses_exact_nonempty_update_fields_and_explicit_clear() -> None:
    task = _task()
    service = TaskService(TasksMemoryGateway((task,)))

    plan = service.plan_update(task.reference, TaskPatch(due_date=None), current=task)

    assert plan.update_fields == ("due_date",)
    assert plan.changes == {"due_date": None}
    with pytest.raises(ConfigurationError, match="No task fields"):
        service.plan_update(task.reference, TaskPatch(), current=task)


def test_task_complete_and_reopen_have_coherent_explicit_lifecycle_fields() -> None:
    task = _task()
    gateway = TasksMemoryGateway((task,))
    completed_at = datetime(2026, 8, 6, 10, tzinfo=UTC)
    service = TaskService(gateway, clock=lambda: completed_at)

    completed = service.complete(task.reference)
    reopened = service.reopen(task.reference)

    assert completed.status == "completed"
    assert completed.percent_complete == 100
    assert completed.completed_at == completed_at
    assert gateway.updates[0].update_fields == (
        "status",
        "percent_complete",
        "completed_at",
    )
    assert reopened.status == "not_started"
    assert reopened.percent_complete == 0
    assert reopened.completed_at is None
    assert gateway.updates[1].changes["completed_at"] is None


def test_task_validation_rejects_incoherent_status_and_dates() -> None:
    with pytest.raises(ConfigurationError, match="100 percent"):
        normalize_task(replace(_task(), status="completed", percent_complete=50))
    with pytest.raises(ConfigurationError, match="before its start"):
        normalize_task(replace(_task(), start_date=date(2026, 8, 10), due_date=date(2026, 8, 5)))
    with pytest.raises(ConfigurationError, match="without a time"):
        normalize_task(replace(_task(), due_date=datetime(2026, 8, 10, tzinfo=UTC)))


def test_task_export_is_deterministic_json_and_csv() -> None:
    service = TaskService(TasksMemoryGateway())
    task = _task()

    json_payload = service.export(task)
    csv_payload = service.export(task, format="csv")

    assert json.loads(json_payload)[0]["reference"] == "task-1"
    assert csv_payload.startswith("reference,subject,status")
    assert "task-1,Submit report,not_started" in csv_payload
