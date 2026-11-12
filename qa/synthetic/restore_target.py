"""Diagnose restore restart/replay using actual services and an owned SQL target.

This is a fault/recovery probe, not complete Backup Center acceptance, a benchmark
or server validation. The guard must already be active. A committed lost reply
and a known pre-effect rejection exercise different outcomes without any Exchange.
"""

import json
import sqlite3
from threading import Event
from uuid import uuid4

from gucmail.errors import ConflictError, ErrorContext, NetworkError
from gucmail.services.backup import BackupItem, BackupService
from gucmail.state import StateStore
from gucmail.tui.area_backends import BackupAreaBackend
from qa.offline.guard import install_from_env
from qa.synthetic.context import capture_source_fingerprint


class OwnedRestoreTarget:
    backup_source_identity = "synthetic-restore-replay-target"

    def __init__(self, path, mode):
        self.path, self.mode = path, mode
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS restored "
                "(id TEXT PRIMARY KEY, source TEXT, destination TEXT, payload TEXT)"
            )

    def iter_backup_items(self, request, completed_item_keys):
        raise AssertionError("Restore must not enumerate its destination for backup")

    def restore_item(self, item, destination_folder):
        if self.mode == "rejected":
            raise ConflictError(
                "Synthetic rejection before any effect",
                context=ErrorContext(mutation_uncertain=False),
            )
        reference = "msg-restored-" + uuid4().hex
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO restored VALUES (?,?,?,?)",
                (reference, item.reference, destination_folder, json.dumps(item.data)),
            )
        if self.mode == "lost-ack":
            raise NetworkError(
                "Synthetic reply lost after committed restore",
                context=ErrorContext(mutation_uncertain=True, retry_safe=False),
            )
        return reference

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT count(*) FROM restored").fetchone()[0]


def main():
    guard = install_from_env()
    root = guard.private / ("restore-replay-probe-" + uuid4().hex)
    root.mkdir()
    observations = []
    for mode in ("lost-ack", "rejected"):
        owned = root / mode
        owned.mkdir()
        state = StateStore(owned / "state.sqlite")
        backup_root = owned / "backups"
        backup = BackupService(backup_root, state=state).create(
            (
                BackupItem(
                    "msg-synthetic-restore-source",
                    "message",
                    {"subject": "Synthetic restore recovery", "body_text": "Owned test content"},
                    folder_path="Synthetic/Inbox",
                ),
            ),
            backup_id="synthetic-replay-" + mode,
        )
        target = OwnedRestoreTarget(owned / "destination.sqlite", mode)
        backend = BackupAreaBackend(
            backup_root,
            state=state,
            source_factory=lambda target=target: target,
        )
        plan = backend.create_restore_plan(
            backup.backup_id,
            (),
            cancel=Event(),
            progress=lambda _progress: None,
        )
        first = backend.execute_restore(
            plan,
            confirmed=True,
            cancel=Event(),
            progress=lambda _progress: None,
        )
        after_first = target.count()
        # A new state object, backend and target reopen the same persisted files.
        # This simulates restart of these services, not an OS/process-kill test.
        restarted_target = OwnedRestoreTarget(target.path, "accepted")
        restarted = BackupAreaBackend(
            backup_root,
            state=StateStore(state.path),
            source_factory=lambda target=restarted_target: target,
        )
        fresh_plan = restarted.create_restore_plan(
            backup.backup_id,
            (),
            cancel=Event(),
            progress=lambda _progress: None,
        )
        second = restarted.execute_restore(
            fresh_plan,
            confirmed=True,
            cancel=Event(),
            progress=lambda _progress: None,
        )
        observations.append(
            {
                "mode": mode,
                "first_attempted": first.attempted_count,
                "first_reported_restored": first.restored_count,
                "first_mutation_uncertain": first.mutation_uncertain,
                "durable_rows_after_first": after_first,
                "fresh_plan_create_count": fresh_plan.create_count,
                "same_confirmation_token": fresh_plan.confirmation_token == plan.confirmation_token,
                "second_reported_restored": second.restored_count,
                "durable_rows_after_second": restarted_target.count(),
            }
        )
    result = {
        "diagnostic_only": True,
        "source_fingerprint": capture_source_fingerprint(),
        "scenarios": observations,
        "boundary": "Actual BackupAreaBackend/BackupService plan and execution with owned "
        "persistent SQL target and fresh service objects. Two explicit confirmed executions; "
        "no automatic retry, real process kill, UI interaction, Exchange or delivery. Provider "
        "attempt flags describe the synthetic target, never live server mutations.",
    }
    (guard.output / "restore-replay-probe.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
