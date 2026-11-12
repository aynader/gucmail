"""Durable service/CLI workflows. Every write targets a disposable synthetic copy."""

from __future__ import annotations

import json
import sqlite3
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    NetworkError,
    SendOutcomeUncertainError,
)
from gucmail.models import Address, CalendarEvent, ContactInfo, DraftData, TaskInfo
from gucmail.services.calendar import CalendarPatch, CalendarService, InvitationPolicy
from gucmail.services.compose import ComposeService
from gucmail.services.contacts import ContactPatch, ContactsService
from gucmail.services.drafts import DraftService
from gucmail.services.messages import MessageService
from gucmail.services.tasks import TaskPatch, TaskService
from qa.offline.guard import install_from_env
from qa.synthetic.context import synthetic_context
from qa.synthetic.datasets import seed
from qa.synthetic.mailbox import LocalMailbox


def copy_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as original:
        clone = sqlite3.connect(target)
        original.backup(clone)
        clone.close()


def main():
    guard = install_from_env()
    root = guard.private / ("workflows-" + uuid4().hex)
    fixture = guard.private / "datasets/baseline"
    seed("baseline", fixture)
    copy_database(fixture / "mailbox.sqlite", root / "mailbox.sqlite")
    context = synthetic_context(root / "profile")
    gateway = LocalMailbox(root / "mailbox.sqlite")
    context._gateway = gateway
    rows = []

    def check(name, operation, boundary="Production service + persistent synthetic transport"):
        start = time.perf_counter()
        try:
            details = operation() or {}
            rows.append(
                {
                    "id": name,
                    "outcome": "passed",
                    "seconds": time.perf_counter() - start,
                    "boundary": boundary,
                    "details": details,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "id": name,
                    "outcome": "failed-needs-triage",
                    "seconds": time.perf_counter() - start,
                    "boundary": boundary,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        (guard.output / "workflow-results.json").write_text(json.dumps(rows, indent=2) + "\n")

    def expect(error, operation):
        try:
            operation()
        except error:
            return
        raise AssertionError("Expected " + error.__name__)

    messages = MessageService(gateway)
    ref = "msg_0000001"

    def message_lifecycle():
        before = gateway.get("mail", ref)
        assert messages.show_message(ref).summary.is_read == before.is_read
        plan = messages.plan_mark_read([ref], read=not before.is_read)
        assert not messages.execute_mutation(plan, dry_run=True).executed
        messages.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
        assert LocalMailbox(gateway.path).get("mail", ref).is_read != before.is_read
        for plan in (
            messages.plan_flag([ref]),
            messages.plan_categorize([ref], ["Academic", "Academic"], mode="add"),
            messages.plan_copy([ref], "/Archive"),
        ):
            messages.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
        assert gateway.get("mail", ref).flagged
        plan = messages.plan_move([ref], "/Archive")
        messages.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
        assert gateway.get("mail", ref).folder_path == "/Archive"
        plan = messages.plan_delete([ref])
        expect(ConfirmationRequiredError, lambda: messages.execute_mutation(plan))
        messages.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
        assert gateway.get("mail", ref).folder_path == "/Deleted Items"
        return {
            "read_without_mutation": True,
            "restart_readback": True,
            "dry_run": True,
            "copy_move_flag_category_delete": True,
        }

    check("mail.lifecycle", message_lifecycle)

    def contact_lifecycle():
        service = ContactsService(gateway)
        item = service.create(
            ContactInfo("", "Synthetic audit student", emails=(Address("audit@example.test"),))
        )
        item = service.update(item.reference, ContactPatch(company="University"))
        assert (
            ContactsService(LocalMailbox(gateway.path)).get(item.reference).company == "University"
        )
        assert "VCARD" in service.export_contact(item.reference)
        service.delete(service.plan_delete(item.reference))
        assert gateway.count("contact") == 500
        return {"create_edit_export_delete": True, "restart_readback": True}

    check("contacts.lifecycle", contact_lifecycle)

    def task_lifecycle():
        service = TaskService(gateway)
        expect(ConfigurationError, lambda: service.create(TaskInfo("", "", "not_started")))
        item = service.create(TaskInfo("", "Audit assignment", "not_started"))
        item = service.update(item.reference, TaskPatch(due_date=datetime.now(UTC).date()))
        assert service.complete(item.reference).status == "completed"
        assert service.reopen(item.reference).status == "not_started"
        assert TaskService(LocalMailbox(gateway.path)).get(item.reference).due_date is not None
        service.delete(service.plan_delete(item.reference))
        assert gateway.count("task") == 500
        return {
            "invalid_input_rejected": True,
            "create_edit_complete_reopen_delete": True,
            "restart_readback": True,
        }

    check("tasks.lifecycle", task_lifecycle)

    def calendar_lifecycle():
        service = CalendarService(gateway, display_timezone="Africa/Cairo")
        start = datetime(2027, 1, 20, 9, tzinfo=UTC)
        expect(
            ConfigurationError,
            lambda: service.create(
                CalendarEvent("", "Invalid", start, start - timedelta(hours=1), "UTC")
            ),
        )
        item = service.create(
            CalendarEvent("", "Audit seminar", start, start + timedelta(hours=1), "UTC"),
            invitations=InvitationPolicy.SEND_NONE,
        )
        item = service.update(
            item.reference, CalendarPatch(location="Room A"), invitations=InvitationPolicy.SEND_NONE
        )
        assert CalendarService(LocalMailbox(gateway.path)).get(item.reference).location == "Room A"
        assert "VCALENDAR" in service.export_event(item.reference)
        service.delete(service.plan_delete(item.reference))
        assert gateway.count("event") == 120
        return {
            "invalid_range_rejected": True,
            "create_edit_export_delete": True,
            "restart_readback": True,
        }

    check("calendar.lifecycle", calendar_lifecycle)

    def draft_lifecycle():
        service = DraftService(context.state, context.paths.recovery_dir)
        draft = service.create(
            subject="Audit draft", to=[Address("recipient@example.test")], body_text="Original body"
        )
        revision = service.get_record(draft.reference).draft.revision
        updated = service.save(replace(draft, body_text="Saved body"), expected_revision=revision)
        expect(
            ConflictError,
            lambda: service.save(
                replace(updated, body_text="Stale overwrite"), expected_revision=revision
            ),
        )
        assert (
            DraftService(context.state, context.paths.recovery_dir).get(draft.reference).body_text
            == "Saved body"
        )
        duplicate = service.duplicate(draft.reference)
        assert duplicate.reference != draft.reference
        service.write_recovery(replace(updated, body_text="Recovered body"))
        assert service.read_recovery(draft.reference).body_text == "Recovered body"
        assert service.delete(duplicate.reference)
        return {
            "create_save_stale_write_rejected_duplicate_recovery_delete": True,
            "restart_readback": True,
        }

    check(
        "drafts.lifecycle",
        draft_lifecycle,
        "Production SQLite state, draft validation and recovery files; no transport",
    )

    def compose_lifecycle():
        service = ComposeService(gateway, state=context.state)
        expect(
            ConfigurationError,
            lambda: service.plan_send(
                DraftData("bad", to=[Address("broken")], subject="X", body_text="X")
            ),
        )
        draft = DraftData(
            "dft-audit-send",
            to=[Address("recipient@example.test")],
            subject="Audit send",
            body_text="Synthetic message.",
        )
        plan = service.plan_send(draft)
        expect(ConfirmationRequiredError, lambda: service.execute_send(plan))
        before = gateway.count("mail", folder="/Sent Items")
        assert not service.execute_send(plan, dry_run=True).sent
        assert service.execute_send(plan, confirmation_scope=plan.confirmation_scope).sent
        expect(
            ConflictError,
            lambda: service.execute_send(plan, confirmation_scope=plan.confirmation_scope),
        )
        assert gateway.count("mail", folder="/Sent Items") == before + 1
        # An uncertain result blocks a second submission, including after restart.
        gateway.faults["send"] = {"error": NetworkError("Synthetic transport timeout")}
        plan = service.plan_send(replace(draft, reference="dft-audit-uncertain"))
        expect(
            SendOutcomeUncertainError,
            lambda: service.execute_send(plan, confirmation_scope=plan.confirmation_scope),
        )
        restarted = ComposeService(gateway, state=context.state)
        expect(
            ConflictError,
            lambda: restarted.execute_send(plan, confirmation_scope=plan.confirmation_scope),
        )
        gateway.faults.clear()
        return {
            "validation_confirmation_dryrun": True,
            "single_persisted_simulated_send": True,
            "duplicate_and_uncertain_retry_blocked": True,
        }

    check("compose.delivery-state", compose_lifecycle)

    import typer.testing as testing
    from typer.main import get_command
    from typer.testing import CliRunner

    import gucmail.cli as cli

    original_build = cli.build_context
    original_get = testing._get_command
    cli.build_context = lambda **kwargs: context
    command = get_command(cli.app)
    testing._get_command = lambda app: app if hasattr(app, "main") else original_get(app)
    runner = CliRunner()
    cases = [
        ["version"],
        ["status"],
        ["config", "show"],
        ["config", "path"],
        ["cache", "status"],
        ["ews", "capabilities"],
        ["messages", "list", "--limit", "3"],
        ["messages", "show", "msg_0000002"],
        ["messages", "headers", "msg_0000002"],
        ["messages", "delete", "msg_0000002", "--dry-run"],
        ["messages", "move", "msg_0000002", "--to", "/Archive", "--dry-run"],
        ["folders", "list", "--limit", "3"],
        ["contacts", "list", "--limit", "3"],
        ["tasks", "list", "--limit", "3"],
        ["drafts", "list"],
        ["saved-searches", "list"],
        ["scheduled", "list"],
        ["index", "status"],
        ["notifications", "status"],
        ["webhooks", "list"],
        ["contact-groups", "list"],
    ]
    try:
        for args in cases:

            def invoke(args=args):
                context._gateway = gateway
                result = runner.invoke(command, ["--json", "--non-interactive", *args])
                if result.exit_code:
                    raise AssertionError(f"Exit {result.exit_code}: {result.output[:1400]}")
                value = json.loads(result.output)
                return {"exit_code": 0, "valid_json": True, "output_characters": len(result.output)}

            check(
                "cli." + ".".join(args),
                "invalid" if False else invoke,
                "Actual Typer entry point + production services + persistent synthetic transport",
            )
    finally:
        cli.build_context = original_build
        testing._get_command = original_get
    print(
        json.dumps(
            {
                "checks": len(rows),
                "passed": sum(row["outcome"] == "passed" for row in rows),
                "failed": sum(row["outcome"] != "passed" for row in rows),
            }
        )
    )


if __name__ == "__main__":
    main()
