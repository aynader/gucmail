"""Deterministic synthetic academic workloads. Runtime databases stay private."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gucmail.models import (
    Address,
    CalendarEvent,
    ContactInfo,
    DraftData,
    FolderInfo,
    Importance,
    ItemSource,
    MessageSummary,
    TaskInfo,
)
from gucmail.services.drafts import draft_content_hash, draft_to_dict
from gucmail.services.index import IndexDocument, SearchIndex
from qa.offline.guard import install_from_env
from qa.synthetic.context import synthetic_context
from qa.synthetic.mailbox import LocalMailbox

NOW = datetime(2026, 10, 29, 9, tzinfo=UTC)
SIZES = {
    "baseline": dict(
        mail=1000,
        folder=80,
        contact=500,
        event=120,
        task=500,
        drafts=100,
        activity=200,
        schedules=100,
    ),
    "large": dict(
        mail=100000,
        folder=2000,
        contact=50000,
        event=6000,
        task=30000,
        drafts=5000,
        activity=20000,
        schedules=10000,
    ),
    "metadata": dict(
        mail=1000000, folder=2000, contact=0, event=0, task=0, drafts=0, activity=0, schedules=0
    ),
}


def folders(count):
    roles = ("Inbox", "Sent Items", "Drafts", "Deleted Items", "Junk", "Archive")
    for i, name in enumerate(roles):
        yield FolderInfo(
            f"fld_role_{i}",
            name,
            "/" + name,
            None,
            total_count=0,
            unread_count=0,
            child_count=0,
            folder_class="IPF.Note",
        )
    previous = None
    path = ""
    for i in range(count - len(roles)):
        depth = i % 17
        if not depth:
            previous, path = None, ""
        name = f"Course {i:04} · " + (
            "هندسة" if i % 7 == 0 else "研究" if i % 11 == 0 else "Academic"
        )
        if i % 29 == 0:
            name += " [b]literal[/b] % _ ; '"
        path += "/" + name
        reference = f"fld_{i:06}"
        yield FolderInfo(
            reference,
            name,
            path,
            previous,
            total_count=0,
            unread_count=0,
            child_count=0 if depth == 16 else 1,
            folder_class="IPF.Note",
        )
        previous = reference


def messages(count, folder_paths):
    subjects = (
        "Research methods tutorial",
        "Registration and academic calendar",
        "Library reminder",
        "Design review",
        "Scholarship opportunity",
        "Laboratory report · next steps",
        "محاضرة الأسبوع القادم",
        "[bold]Literal markup[/bold]",
        "研究プロジェクト",
    )
    for i in range(count):
        position = i % 100
        folder = (
            "/Inbox"
            if position < 55
            else "/Sent Items"
            if position < 65
            else "/Deleted Items"
            if position < 70
            else folder_paths[i % len(folder_paths)]
        )
        title = f"{subjects[i % len(subjects)]} {i:07}"
        if i % 97 == 0:
            title = ""
        elif i % 89 == 0:
            title += " · a deliberately long academic subject " * 30
        elif i % 211 == 0:
            title += "\x1b[31m hostile terminal control\x1b[0m"
        yield MessageSummary(
            f"msg_{i:07}",
            title,
            None
            if i % 101 == 0
            else Address(f"person{i % 5000}@example.test", f"Academic contact {i % 5000}"),
            None if i % 119 == 0 else NOW - timedelta(minutes=i),
            is_read=i % 9 != 0,
            has_attachments=i % 4 == 0,
            importance=Importance.HIGH if i % 37 == 0 else Importance.NORMAL,
            flagged=i % 43 == 0,
            categories=(f"Course-{i % 40}",),
            size=1024 + i * 37,
            folder_path=folder,
            conversation_id=f"thread-{i // 5}",
            preview="Synthetic academic preview; local audit only.",
            source=ItemSource.LOCAL,
        )


def contacts(count):
    for i in range(count):
        label = f"Student {i % 45000:05}" if i % 7 else f"أحمد {i % 45000:05}"
        if i % 109 == 0:
            label += " — very long faculty and department display name " * 8
        yield ContactInfo(
            f"con_{i:06}",
            label,
            given_name=label.split()[0],
            family_name=f"Family {i % 900}",
            emails=() if i % 41 == 0 else (Address(f"student{i}@example.test"),),
            phones={} if i % 13 == 0 else {"mobile": f"+200000{i:06}"},
            company="Synthetic University",
            department=f"Department {i % 75}",
            categories=(f"Faculty {i % 12}",),
            source=ItemSource.LOCAL,
        )


def events(count):
    academic = datetime(2026, 9, 1, 8, tzinfo=UTC)
    zones = ("Africa/Cairo", "Europe/Berlin", "America/New_York")
    for i in range(count):
        recurring = i < count * 3 // 5
        series, occurrence = divmod(i, 12)
        start = (
            academic
            + timedelta(
                days=(series % 70) + (180 if series % 2 else 0) + occurrence * 7, hours=series % 7
            )
            if recurring
            else academic + timedelta(minutes=(i * 97) % (364 * 1440))
        )
        original = start
        exception = recurring and occurrence == 5
        if exception:
            start += timedelta(hours=1)
        zone = zones[i % len(zones)]
        all_day = i % 71 == 0
        if all_day:
            start = start.astimezone(ZoneInfo(zone)).replace(hour=0, minute=0, second=0)
        end = start + (
            timedelta(days=1) if all_day else timedelta(minutes=120 if i % 5 == 0 else 60)
        )
        yield CalendarEvent(
            f"evt_{i:06}",
            f"Academic session {i:05} · {'Series exception' if exception else 'Lecture'}",
            start,
            end,
            zone,
            location=f"Building {i % 8} · Room {i % 150}",
            body="Synthetic academic calendar content.",
            required_attendees=(Address("student@example.test"),) if i % 3 == 0 else (),
            is_meeting=i % 3 == 0,
            is_all_day=all_day,
            recurrence={"freq": "weekly", "count": 12} if recurring and occurrence == 0 else None,
            item_type="Exception"
            if exception
            else "Occurrence"
            if recurring and occurrence
            else "RecurringMaster"
            if recurring
            else "Single",
            recurrence_id=original if exception else None,
            uid=f"audit-series-{series}@example.test" if recurring else f"single-{i}@example.test",
            categories=("Teaching",),
            source=ItemSource.LOCAL,
        )


def tasks(count):
    states = ("not_started", "in_progress", "completed", "deferred", "waiting_on_others")
    for i in range(count):
        state = states[i % len(states)]
        yield TaskInfo(
            f"tsk_{i:06}",
            f"Academic task {i:06} · {'Prepare tutorial' if i % 3 else 'Review submissions'}",
            state,
            percent_complete=100 if state == "completed" else 35 if state == "in_progress" else 0,
            start_date=date(2026, 9, 1),
            due_date=date(2026, 10, 1) + timedelta(days=i % 270),
            completed_at=NOW if state == "completed" else None,
            body="Synthetic task body.",
            categories=(f"Course-{i % 40}",),
            recurrence={"freq": "weekly", "count": 6} if i % 17 == 0 else None,
            importance=Importance.HIGH if i % 29 == 0 else Importance.NORMAL,
            source=ItemSource.LOCAL,
        )


def seed(scale, root: Path, *, index=True):
    counts = SIZES[scale]
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "dataset.json"
    if manifest.is_file():
        report = json.loads(manifest.read_text())
        if report.get("complete") and report.get("counts") == counts:
            return report
        raise RuntimeError("Incomplete dataset exists; choose a new private run directory.")
    mailbox = LocalMailbox(root / "mailbox.sqlite")
    durations = {}

    def load(kind, iterator):
        before = time.perf_counter()
        mailbox.insert_many(kind, iterator)
        durations[kind] = time.perf_counter() - before
        assert mailbox.count(kind) == counts[kind]

    folder_rows = tuple(folders(counts["folder"]))
    load("folder", folder_rows)
    paths = [f.path for f in folder_rows[6:]]
    load("mail", messages(counts["mail"], paths))
    load("contact", contacts(counts["contact"]))
    load("event", events(counts["event"]))
    load("task", tasks(counts["task"]))
    context = synthetic_context(root / "profile")
    context.state.initialize()
    before = time.perf_counter()
    with context.state.transaction(immediate=True) as db:
        for i in range(counts["drafts"]):
            draft = DraftData(
                f"dft-audit-{i:06}",
                to=[Address("recipient@example.test")],
                subject=f"Synthetic draft {i:06}",
                body_text="Academic draft content. " * (20 if i % 13 == 0 else 2),
            )
            db.execute(
                "INSERT INTO local_drafts(reference,draft_json,revision,state,content_hash,created_at,updated_at) VALUES (?,?,1,'local',?,?,?)",
                (
                    draft.reference,
                    json.dumps(draft_to_dict(draft)),
                    draft_content_hash(draft),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
        db.executemany(
            "INSERT INTO local_audit_events(action,target_reference,detail_json,occurred_at) VALUES (?,?,?,?)",
            (
                (
                    "synthetic.audit.event",
                    f"msg_{i % max(1, counts['mail']):07}",
                    '{"synthetic":true}',
                    (NOW - timedelta(minutes=i)).isoformat(),
                )
                for i in range(counts["activity"])
            ),
        )
        for i in range(counts["schedules"]):
            state = (
                "sent"
                if i % 5 == 0
                else "cancelled"
                if i % 11 == 0
                else "send_state_uncertain"
                if i % 53 == 0
                else "scheduled"
            )
            db.execute(
                "INSERT INTO scheduled_jobs(reference,draft_reference,scheduled_for,timezone,original_expression,state,idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"sch-audit-{i:06}",
                    f"dft-audit-{i % max(1, counts['drafts']):06}",
                    (NOW + timedelta(days=1, minutes=i)).isoformat(),
                    "Africa/Cairo",
                    "synthetic future schedule",
                    state,
                    f"audit-queue-{i}",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
    durations["local_state"] = time.perf_counter() - before
    indexed = 0
    if index and scale != "metadata":
        search = SearchIndex(context.paths.index_database)
        search.initialize()
        batch = []
        before = time.perf_counter()

        def fixture_batch(values):
            # The batched production path now maintains stable FTS identities.
            # Previously generated v1 fixtures remain unchanged; new fixtures
            # must not bypass the v2 mapping or manufacture ingestion timings.
            return search.upsert_many(values)

        for i, summary in enumerate(messages(counts["mail"], paths)):
            item = IndexDocument.from_value(summary)
            if i % 4 == 0:
                from dataclasses import replace

                item = replace(
                    item,
                    attachment_names=f"notes-{i}.pdf",
                    attachment_text="Synthetic optics tutorial attachment text.",
                )
            batch.append(item)
            if len(batch) == 500:
                indexed += fixture_batch(batch)
                batch.clear()
        if batch:
            indexed += fixture_batch(batch)
        durations["index"] = time.perf_counter() - before
        assert search.stats().documents == counts["mail"]
    report = {
        "scale": scale,
        "complete": True,
        "counts": counts,
        "attachment_index_records": (counts["mail"] + 3) // 4 if indexed else 0,
        "index_documents": indexed,
        "seed_seconds": durations,
        "generation": "Streaming dataclasses into SQL. New index fixtures use bounded production upsert_many batches; locked historical manifests retain their original generation method. Local state bulk seeded in its real schema.",
        "privacy": "Synthetic example.test data only; private runtime databases are not published.",
    }
    manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=SIZES, default="baseline")
    args = parser.parse_args()
    guard = install_from_env()
    report = seed(args.scale, guard.private / "datasets" / args.scale)
    (guard.output / f"dataset-{args.scale}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
