"""README screenshots from a made-up mailbox (offline guard only).

The people and addresses are invented; none of it comes from a real mailbox. Run from
the repository root (Windows, needs Microsoft Edge for rasterize.py):

    python qa/offline/run.py --private <tmp>/p --output <tmp>/o script qa/screenshots/capture.py
    python qa/offline/run.py ... script qa/screenshots/capture.py builtin.tokyo-night browse
    python qa/offline/run.py ... script qa/screenshots/capture.py builtin.tokyo-night palette,palette-frames
    python qa/screenshots/rasterize.py <tmp>/png <tmp>/o/readme/*.svg
    python qa/screenshots/rasterize.py <tmp>/frames <tmp>/o/readme/frames/*.svg
    python qa/screenshots/make_gif.py docs/img/mail-browse-demo.gif 0 \\
        1600,380,380,380,2600,380,380,380,380,380,380,380,3600 <tmp>/frames/browse_*.png
    python qa/screenshots/make_gif.py docs/img/palette-demo.gif 0 \\
        1200,170x15,3800 <tmp>/frames/palette_*.png <tmp>/png/palette.png

Then copy <tmp>/png/*.png into docs/img.
"""

import asyncio
import json
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from qa.offline.guard import install_from_env
from qa.synthetic.mailbox import LocalMailbox

GUARD = install_from_env()
PRIVATE = GUARD.private
OUT = GUARD.output / "readme"
OUT.mkdir(parents=True, exist_ok=True)
GIF_DIR = OUT / "frames"

from textual.widgets import DataTable, Input, TextArea  # noqa: E402

from gucmail.bootstrap import AppContext  # noqa: E402
from gucmail.config import AppConfig, AppPaths, ConfigStore  # noqa: E402
from gucmail.credentials import CredentialResolver, CredentialStore  # noqa: E402
from gucmail.models import (  # noqa: E402
    Address,
    AttachmentInfo,
    CalendarEvent,
    ContactInfo,
    DraftData,
    FolderInfo,
    Importance,
    ItemSource,
    MessageDetail,
    MessageSummary,
    TaskInfo,
)
from gucmail.services.backup import BackupItem, BackupService  # noqa: E402
from gucmail.services.drafts import draft_content_hash, draft_to_dict  # noqa: E402
from gucmail.settings import SettingsService  # noqa: E402
from gucmail.state import StateStore  # noqa: E402
from gucmail.tui.app import ComposeScreen, GUCmailApp, PaletteScreen  # noqa: E402

THEME = sys.argv[1] if len(sys.argv) > 1 else "builtin.tokyo-night"
ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 and sys.argv[2] else None
SIZE = (140, 40)
CAIRO = ZoneInfo("Africa/Cairo")
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
ME = Address("nada.mostafa@student.guc.edu.eg", "Nada Mostafa")
NOTES: dict = {}

MESSAGES = [
    (
        "Dr. Mona Farouk",
        "mona.farouk@guc.edu.eg",
        "Lab 6 feedback and grading rubric",
        False,
        True,
        Importance.HIGH,
    ),
    (
        "Registrar Office",
        "registrar@guc.edu.eg",
        "Winter semester registration opens Sunday",
        False,
        False,
        Importance.HIGH,
    ),
    (
        "Nour Ashraf",
        "nour.ashraf@student.guc.edu.eg",
        "Re: Project milestone 2 meeting notes",
        False,
        False,
        Importance.NORMAL,
    ),
    (
        "Laila Samir",
        "laila.samir@guc.edu.eg",
        "Tutorial slides for week 7",
        True,
        True,
        Importance.NORMAL,
    ),
    (
        "Library Services",
        "library@guc.edu.eg",
        "Reminder: borrowed books due next week",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Student Affairs",
        "student.affairs@guc.edu.eg",
        "Campus career fair schedule",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Course Team",
        "course.team@guc.edu.eg",
        "Quiz 3 results are available",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "IT Helpdesk",
        "helpdesk@guc.edu.eg",
        "Planned maintenance on Saturday night",
        True,
        False,
        Importance.LOW,
    ),
    (
        "Youssef Hamdy",
        "youssef.hamdy@guc.edu.eg",
        "Seminar invitation: distributed systems",
        True,
        True,
        Importance.NORMAL,
    ),
    (
        "Sports Club",
        "sports@guc.edu.eg",
        "Weekend tournament sign-up",
        True,
        False,
        Importance.NORMAL,
    ),
    ("Exams Office", "exams@guc.edu.eg", "Midterm seating plan", True, True, Importance.HIGH),
    (
        "Omar Khaled",
        "omar.khaled@student.guc.edu.eg",
        "Shared notes from today's lecture",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Thesis Office",
        "thesis@guc.edu.eg",
        "Bachelor thesis proposal deadline",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Alumni Network",
        "alumni@guc.edu.eg",
        "Mentorship programme kickoff",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Dr. Karim Adel",
        "karim.adel@guc.edu.eg",
        "Office hours moved to Wednesday",
        True,
        False,
        Importance.NORMAL,
    ),
    (
        "Language Centre",
        "language@guc.edu.eg",
        "German A2 placement test results",
        True,
        True,
        Importance.NORMAL,
    ),
    (
        "Finance Office",
        "finance@guc.edu.eg",
        "Tuition receipt for the autumn term",
        True,
        True,
        Importance.NORMAL,
    ),
    (
        "Robotics Society",
        "robotics@guc.edu.eg",
        "Hackathon team formation",
        True,
        False,
        Importance.NORMAL,
    ),
]

BODY = (
    "Hello everyone,\n\n"
    "Lab 6 is graded. Your feedback and the rubric are attached.\n\n"
    "Highlights from this round:\n"
    "  * Most teams handled leader election correctly.\n"
    "  * Document your failure-recovery assumptions.\n"
    "  * Include test logs in the final report.\n\n"
    "Please read the rubric before Thursday's tutorial.\n"
    "Office hours: Wednesday 12:00-14:00, C7.214.\n\n"
    "Best regards,\nMona Farouk\nCSEN 1001, Winter 2026"
)


class ShowcaseMailbox(LocalMailbox):
    def folder_path(self, folder):  # type: ignore[override]
        if isinstance(folder, str) and folder.startswith("fld_"):
            try:
                return self.get("folder", folder).path
            except Exception:
                return folder
        return LocalMailbox.folder_path(folder)

    def show_message(self, reference, mark_read=False):
        summary = self.get("mail", reference)
        body = (
            BODY
            if reference == "msg_show_000"
            else (
                f"Hello,\n\n{summary.subject}.\n\nDetails are in the course portal.\n\nKind regards,\n"
                f"{summary.sender.name if summary.sender else 'Synthetic sender'}"
            )
        )
        attachments = ()
        if summary.has_attachments:
            attachments = (
                AttachmentInfo("att_rubric", "lab6-rubric.pdf", 48213, "application/pdf"),
                AttachmentInfo(
                    "att_feedback",
                    "team-07-feedback.docx",
                    23110,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )
        return MessageDetail(
            summary=summary,
            to=(ME,),
            cc=(Address("laila.samir@guc.edu.eg", "Laila Samir"),)
            if summary.sender and summary.sender.email.startswith("mona.")
            else (),
            body_text=body,
            attachments=attachments,
        )


def make_context(root: Path) -> AppContext:
    paths = AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config/config.toml",
        state_database=root / "data/state.sqlite",
        recovery_dir=root / "data/recovery",
        index_database=root / "data/index.sqlite",
        backup_dir=root / "data/backups",
    )
    store = ConfigStore(paths)
    config = (
        AppConfig(email="nada.mostafa@student.guc.edu.eg", theme=THEME, timezone="Africa/Cairo")
        if "timezone" in AppConfig.__dataclass_fields__
        else AppConfig(email="nada.mostafa@student.guc.edu.eg", theme=THEME)
    )
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database, account_key="synthetic-demo"),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def week_start() -> datetime:
    local = NOW.astimezone(CAIRO)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0)
    return monday


def seed(root: Path) -> ShowcaseMailbox:
    box = ShowcaseMailbox(root / "mailbox.sqlite")
    names = [
        ("Inbox", 18, 3),
        ("Sent Items", 42, 0),
        ("Drafts", 2, 0),
        ("Deleted Items", 7, 0),
        ("Junk Email", 1, 1),
        ("Archive", 312, 0),
    ]
    folders = [
        FolderInfo(
            f"fld_role_{i}",
            n,
            "/" + n,
            None,
            total_count=t,
            unread_count=u,
            child_count=0,
            folder_class="IPF.Note",
        )
        for i, (n, t, u) in enumerate(names)
    ]
    folders.append(
        FolderInfo(
            "fld_courses",
            "Courses",
            "/Courses",
            None,
            total_count=0,
            unread_count=0,
            child_count=2,
            folder_class="IPF.Note",
        )
    )
    folders.append(
        FolderInfo(
            "fld_c1",
            "Distributed Systems",
            "/Courses/Distributed Systems",
            "fld_courses",
            total_count=24,
            unread_count=2,
            child_count=0,
            folder_class="IPF.Note",
        )
    )
    folders.append(
        FolderInfo(
            "fld_c2",
            "Software Engineering",
            "/Courses/Software Engineering",
            "fld_courses",
            total_count=31,
            unread_count=0,
            child_count=0,
            folder_class="IPF.Note",
        )
    )
    box.insert_many("folder", folders)
    rows = []
    for i, (name, email, subject, read, attach, importance) in enumerate(MESSAGES):
        rows.append(
            MessageSummary(
                f"msg_show_{i:03}",
                subject,
                Address(email, name),
                NOW - timedelta(hours=i * 7 + 1),
                is_read=read,
                has_attachments=attach,
                importance=importance,
                flagged=i in (0, 10),
                size=18000 + i * 2300,
                folder_path="/Inbox",
                conversation_id=f"c{i}",
                categories=("Courses",) if i in (0, 3) else (),
                preview=subject,
                source=ItemSource.LOCAL,
            )
        )
    box.insert_many("mail", rows)

    monday = week_start()

    def at(day, hour, minute=0):
        return (monday + timedelta(days=day, hours=hour, minutes=minute)).astimezone(UTC)

    events = [
        ("Distributed Systems lecture", 0, 10, 90, "Hall H2"),
        ("Software Engineering tutorial", 0, 14, 90, "Room C7.201"),
        ("Project milestone 2 meeting", 1, 12, 60, "Library study room 3"),
        ("Office hours: Dr. Mona Farouk", 2, 12, 120, "Room B3.12"),
        ("Distributed Systems lecture", 2, 10, 90, "Hall H2"),
        ("German A2 class", 3, 9, 90, "Language Centre"),
        ("Lab 6 tutorial", 3, 14, 90, "Lab C3.104"),
        ("Career fair", 4, 11, 180, "Main campus courtyard"),
        ("Robotics hackathon kickoff", 5, 16, 120, "Workshop W1"),
        ("Thesis proposal review", 1, 15, 60, "Online meeting"),
    ]
    box.insert_many(
        "event",
        [
            CalendarEvent(
                f"evt_show_{i:02}",
                subject,
                at(day, hour),
                at(day, hour) + timedelta(minutes=minutes),
                "Africa/Cairo",
                location=location,
                body="Bring your laptop and the latest milestone draft.",
                organizer=Address("mona.farouk@guc.edu.eg", "Dr. Mona Farouk")
                if i in (2, 3, 9)
                else None,
                required_attendees=(ME, Address("nour.ashraf@student.guc.edu.eg", "Nour Ashraf"))
                if i in (2, 9)
                else (),
                is_meeting=i in (2, 3, 9),
                categories=("Courses",) if i < 7 else (),
                reminder_minutes=15,
                response_status="accepted" if i in (2, 3) else None,
                source=ItemSource.LOCAL,
            )
            for i, (subject, day, hour, minutes, location) in enumerate(events)
        ],
    )
    people = [
        (
            "Mona Farouk",
            "Dr.",
            "mona.farouk@guc.edu.eg",
            "Faculty of Media Engineering",
            "Associate Professor",
            "Ext. 1001",
        ),
        (
            "Karim Adel",
            "Dr.",
            "karim.adel@guc.edu.eg",
            "Faculty of Engineering",
            "Lecturer",
            "Ext. 1002",
        ),
        ("Omar Khaled", "", "omar.khaled@student.guc.edu.eg", "Computer Science", "Student", ""),
        ("Nour Ashraf", "", "nour.ashraf@student.guc.edu.eg", "Computer Science", "Student", ""),
        (
            "Laila Samir",
            "",
            "laila.samir@guc.edu.eg",
            "Faculty of Media Engineering",
            "Teaching Assistant",
            "Ext. 1005",
        ),
        (
            "Registrar Office",
            "",
            "registrar@guc.edu.eg",
            "Student Services",
            "Registrar",
            "Ext. 1006",
        ),
        (
            "Hassan Tarek",
            "",
            "hassan.tarek@alumni.guc.edu.eg",
            "Alumni Network",
            "Software Engineer",
            "",
        ),
        ("Sara Nabil", "", "library@guc.edu.eg", "Library Services", "Librarian", "Ext. 1008"),
        (
            "Youssef Hamdy",
            "",
            "youssef.hamdy@guc.edu.eg",
            "Research Lab",
            "Research Assistant",
            "Ext. 1009",
        ),
        ("IT Helpdesk", "", "helpdesk@guc.edu.eg", "IT Services", "Support", "Ext. 1010"),
    ]
    box.insert_many(
        "contact",
        [
            ContactInfo(
                f"con_show_{i:02}",
                (title + " " if title else "") + name,
                given_name=name.split()[0],
                family_name=name.split()[-1],
                emails=(Address(email, name),),
                phones={"BusinessPhone": phone} if phone else {},
                company="German University in Cairo",
                department=dept,
                job_title=job,
                categories=("Faculty",) if title else ("Students",) if job == "Student" else (),
                notes="",
                source=ItemSource.LOCAL,
            )
            for i, (name, title, email, dept, job, phone) in enumerate(people)
        ],
    )
    today = NOW.astimezone(CAIRO).date()
    tasks = [
        ("Submit Lab 6 report", "in_progress", 60, 2, Importance.HIGH),
        ("Prepare milestone 2 slides", "in_progress", 35, 3, Importance.NORMAL),
        ("Read chapter 5: consensus", "not_started", 0, 4, Importance.NORMAL),
        ("Register for winter courses", "not_started", 0, 5, Importance.HIGH),
        ("Return library books", "not_started", 0, 7, Importance.NORMAL),
        ("Draft thesis proposal outline", "waiting_on_others", 20, 10, Importance.NORMAL),
        ("Book German A2 exam", "completed", 100, -1, Importance.NORMAL),
        ("Quiz 3 revision", "completed", 100, -3, Importance.NORMAL),
    ]
    box.insert_many(
        "task",
        [
            TaskInfo(
                f"tsk_show_{i:02}",
                subject,
                status,
                percent_complete=pct,
                start_date=today - timedelta(days=2),
                due_date=today + timedelta(days=due),
                completed_at=NOW if status == "completed" else None,
                body="",
                categories=("Courses",) if i < 3 else (),
                importance=imp,
                source=ItemSource.LOCAL,
            )
            for i, (subject, status, pct, due, imp) in enumerate(tasks)
        ],
    )
    return box


def seed_state(context: AppContext) -> None:
    context.state.initialize()
    stamp = NOW.isoformat()
    drafts = [
        (
            "dft-show-1",
            [Address("nour.ashraf@student.guc.edu.eg", "Nour Ashraf")],
            "Milestone 2 summary and next steps",
            "Hi all, attached is the milestone 2 summary.",
            1,
            9,
            "tomorrow 9 AM",
            "scheduled",
        ),
        (
            "dft-show-2",
            [Address("mona.farouk@guc.edu.eg", "Dr. Mona Farouk")],
            "Question about Lab 6 rubric",
            "Dear Dr. Mona, I have a question about criterion 3.",
            0,
            0,
            "in 45 minutes",
            "scheduled",
        ),
        (
            "dft-show-3",
            [Address("registrar@guc.edu.eg", "Registrar Office")],
            "Course registration confirmation",
            "Hello, please confirm my registration.",
            3,
            8,
            "Monday 8:30 AM",
            "scheduled",
        ),
        (
            "dft-show-4",
            [Address("omar.khaled@student.guc.edu.eg", "Omar Khaled")],
            "Lecture notes for week 7",
            "Here are my notes.",
            -1,
            18,
            "yesterday 6 PM",
            "sent",
        ),
    ]
    with context.state.transaction(immediate=True) as db:
        for i, (ref, to, subject, body, days, hour, expression, state) in enumerate(drafts):
            draft = DraftData(ref, to=to, subject=subject, body_text=body)
            db.execute(
                "INSERT INTO local_drafts(reference,draft_json,revision,state,content_hash,created_at,updated_at)"
                " VALUES (?,?,1,'local',?,?,?)",
                (ref, json.dumps(draft_to_dict(draft)), draft_content_hash(draft), stamp, stamp),
            )
            local = NOW.astimezone(CAIRO)
            when = (
                (local + timedelta(minutes=45))
                if days == 0
                else (local + timedelta(days=days)).replace(hour=hour, minute=30 if i == 2 else 0)
            )
            db.execute(
                "INSERT INTO scheduled_jobs(reference,draft_reference,scheduled_for,timezone,original_expression,"
                "state,idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"sch-show-{i}",
                    ref,
                    when.astimezone(UTC).isoformat(),
                    "Africa/Cairo",
                    expression,
                    state,
                    f"show-queue-{i}",
                    stamp,
                    stamp,
                ),
            )
        for ref, subject in (("dft-show-5", "Seminar RSVP"),):
            draft = DraftData(
                ref,
                to=[Address("youssef.hamdy@guc.edu.eg", "Youssef Hamdy")],
                subject=subject,
                body_text="I will attend.",
            )
            db.execute(
                "INSERT INTO local_drafts(reference,draft_json,revision,state,content_hash,created_at,updated_at)"
                " VALUES (?,?,1,'local',?,?,?)",
                (ref, json.dumps(draft_to_dict(draft)), draft_content_hash(draft), stamp, stamp),
            )


def seed_backup(context: AppContext) -> None:
    items = [
        BackupItem(
            f"msg_show_{i:03}",
            "message",
            {"subject": subject, "sender": email, "body_text": "Backed-up message body."},
            folder_path="Inbox",
        )
        for i, (_n, email, subject, *_r) in enumerate(MESSAGES)
    ]
    items += [
        BackupItem(f"evt_b_{i}", "event", {"subject": s}, folder_path="Calendar")
        for i, s in enumerate(("Distributed Systems lecture", "Career fair", "Lab 6 tutorial"))
    ]
    items += [
        BackupItem(f"con_b_{i}", "contact", {"display_name": s}, folder_path="Contacts")
        for i, s in enumerate(("Dr. Mona Farouk", "Omar Khaled"))
    ]
    service = BackupService(context.paths.backup_dir, state=context.state)
    service.create(items, backup_id="weekly-" + NOW.strftime("%Y%m%d"))


async def until(pilot, predicate, timeout=20.0, label=""):
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.03)
    except TimeoutError:
        raise AssertionError(f"timeout waiting for {label}") from None
    await pilot.pause()


PRIVATE_PATHS: list[str] = []
SYNTHETIC_BACKUP_DIR = r"C:\Users\student\AppData\Local\GUC Mail CLI\backups"


def _scrub(svg: str) -> str:
    """Replace private scratch paths in rendered text with a same-width synthetic path."""
    import html as _html
    import re as _re

    def fix(match):
        raw = _html.unescape(match.group(2))
        original = raw
        for private in PRIVATE_PATHS:
            stripped = raw.strip()
            if len(stripped) > 6 and private.endswith(stripped) and stripped != private:
                # the wrapped tail of a long path on its own line
                raw = ""
                break
            if private in raw:
                raw = raw.replace(private, SYNTHETIC_BACKUP_DIR)
            else:
                # truncated occurrence at the end of a run
                for cut in range(len(private), 12, -1):
                    if raw.endswith(private[:cut]):
                        raw = raw[: len(raw) - cut] + SYNTHETIC_BACKUP_DIR[:cut]
                        break
        if raw == original:
            return match.group(0)
        raw = raw[: len(original)].ljust(len(original))
        return (
            match.group(1)
            + raw.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace(" ", "&#160;")
            + match.group(3)
        )

    return _re.sub(r"(<text [^>]*>)([^<]*)(</text>)", fix, svg)


def shot(app, name, directory=None):
    directory = directory or OUT
    app.save_screenshot(f"{name}.svg", str(directory))
    path = directory / f"{name}.svg"
    path.write_text(_scrub(path.read_text(encoding="utf-8")), encoding="utf-8")
    NOTES.setdefault("shots", []).append(name)


def find_row(table, text):
    for index in range(table.row_count):
        if any(text in str(cell) for cell in table.get_row_at(index)):
            return index
    return None


async def back_to_main(app, pilot):
    for _ in range(6):
        if app.screen is app.screen_stack[0]:
            return
        await pilot.press("escape")
        await pilot.pause(0.3)


def want(name):
    return ONLY is None or name in ONLY


async def main():
    root = PRIVATE / ("readme-" + uuid4().hex[:8])
    gateway = seed(root)
    context = make_context(root / "profile")
    PRIVATE_PATHS.extend([str(context.paths.backup_dir), str(root)])
    seed_state(context)
    seed_backup(context)
    settings = SettingsService(context.config_store)
    settings.set("composer.server_draft_sync", False)
    settings.set("mail.mark_read_on_open", False)
    context._gateway = gateway
    attachment = root / "milestone-2-summary.pdf"
    attachment.write_bytes(b"%PDF-1.4\n")
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=SIZE) as pilot:
        table = app.query_one("#message-table", DataTable)
        await until(pilot, lambda: table.row_count > 0 and not app.ui.busy, label="rows")
        await pilot.pause(1.0)
        table.focus()

        async def phase(name, fn):
            if not want(name):
                return
            try:
                await fn()
            except Exception as exc:
                NOTES[name] = (
                    f"FAILED {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
                )
                print(NOTES[name], flush=True)
            await back_to_main(app, pilot)
            table.focus()

        async def mail():
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(1.0)
            await until(pilot, lambda: not app.ui.busy, label="reader")
            if app.screen is not app.screen_stack[0]:
                NOTES["mail_screen"] = type(app.screen).__name__
                await pilot.press("escape")
                await pilot.pause(0.3)
            shot(app, "mail")

        async def browse():
            GIF_DIR.mkdir(parents=True, exist_ok=True)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(1.0)
            await until(pilot, lambda: not app.ui.busy, label="first open")
            frame = 0
            shot(app, f"browse_{frame:03}", GIF_DIR)
            plan = [
                "down",
                "down",
                "down",
                "enter",
                "down",
                "down",
                "down",
                "down",
                "down",
                "down",
                "down",
                "enter",
            ]
            for key in plan:
                await pilot.press(key)
                await pilot.pause(1.0 if key == "enter" else 0.25)
                await until(pilot, lambda: not app.ui.busy, label=key)
                frame += 1
                shot(app, f"browse_{frame:03}", GIF_DIR)

        async def palette():
            await pilot.press("ctrl+k")
            await until(pilot, lambda: isinstance(app.screen, PaletteScreen), label="palette")
            field = app.screen.query_one(Input)
            record = want("palette-frames")
            if record:
                GIF_DIR.mkdir(parents=True, exist_ok=True)
                await pilot.pause(0.5)
                shot(app, "palette_000", GIF_DIR)
            for index, ch in enumerate("mail from Laila Samir with pdfs", start=1):
                await pilot.press(ch if ch != " " else "space")
                if record and index % 2 == 0:
                    await pilot.pause(0.3)
                    shot(app, f"palette_{index // 2:03}", GIF_DIR)
            await pilot.pause(1.0)
            NOTES["palette_value"] = field.value
            shot(app, "palette")

        async def compose():
            await pilot.press("c")
            await until(pilot, lambda: isinstance(app.screen, ComposeScreen), label="composer")
            comp = app.screen
            await until(pilot, lambda: not comp._attempt_pending, label="composer ready")
            comp.query_one(
                "#compose-to", Input
            ).value = "Nour Ashraf <nour.ashraf@student.guc.edu.eg>"
            comp.query_one("#compose-cc", Input).value = "Laila Samir <laila.samir@guc.edu.eg>"
            comp.query_one("#compose-subject", Input).value = "Milestone 2 summary and next steps"
            comp.query_one("#compose-body", TextArea).load_text(
                "Hi all,\n\nAttached is the summary of milestone 2. Before Thursday's meeting,\n"
                "please review the open items:\n\n  1. Finalise the failure-recovery section.\n"
                "  2. Re-run the integration tests and attach the logs.\n  3. Split the demo script.\n\n"
                "Thanks,\nNada"
            )
            comp.query_one("#compose-attachments", Input).value = str(attachment)
            await pilot.pause(0.6)
            shot(app, "compose")
            await pilot.press("escape")
            await pilot.pause(0.4)
            if app.screen is not app.screen_stack[0]:
                await pilot.press("y")
                await pilot.pause(0.4)

        async def area(name, label, settle=1.5):
            app._open_area(name)
            await pilot.pause(settle)
            await until(
                pilot, lambda: not getattr(app.screen, "_render_pending", False), label=label
            )
            await pilot.pause(0.5)
            NOTES[label + "_screen"] = type(app.screen).__name__
            shot(app, label)

        await phase("browse", browse)
        await phase("mail", mail)
        await phase("palette", palette)
        await phase("compose", compose)
        for name, label in (
            ("calendar", "calendar"),
            ("contacts", "contacts"),
            ("tasks", "tasks"),
            ("scheduled", "scheduled"),
            ("themes", "themes"),
            ("backups", "backups"),
            ("settings", "settings"),
        ):
            await phase(label, lambda name=name, label=label: area(name, label))
    context.close()
    (OUT / "notes.json").write_text(json.dumps(NOTES, indent=2, default=str), encoding="utf-8")
    print(json.dumps(NOTES, indent=2, default=str)[:3000])


if __name__ == "__main__":
    asyncio.run(main())
