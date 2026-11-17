"""A genuine v15 upgrade preserves user state; lookup and paging stay bounded."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import gucmail.state as state_module
from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import LocalStateError
from gucmail.models import Address, DraftData
from gucmail.services.drafts import DraftService
from gucmail.services.restore_journal import RestoreJournal, RestoreTargetIntent, replay_key
from gucmail.services.scheduler import SchedulerService
from gucmail.state import StateStore


def test_genuine_v15_upgrade_preserves_draft_recovery_scheduling_and_references(
    tmp_path, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(state_module, "SCHEMA_VERSION", 15)
        legacy = StateStore(tmp_path / "state.sqlite", account_key="synthetic-upgrade")
        drafts = DraftService(legacy, tmp_path / "recovery")
        draft = drafts.create(
            DraftData(
                "dft-upgrade",
                to=[Address("course@example.test")],
                subject="Synthetic جامعة",
                body_text="Exact unsent recovery\n[literal]",
            )
        )
        drafts.write_recovery(draft)
        scheduler = SchedulerService(legacy, drafts, lambda _draft: pytest.fail("No delivery"))
        job = scheduler.schedule_at(draft.reference, datetime.now(UTC) + timedelta(days=1))
        before = drafts.recovery_path(draft.reference).read_bytes()
        with legacy.connection() as db:
            assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 15
            old_draft = tuple(db.execute("SELECT * FROM local_drafts").fetchone())
            old_job = tuple(db.execute("SELECT * FROM scheduled_jobs").fetchone())
    fresh = StateStore(legacy.path, account_key=legacy.account_key)
    fresh.initialize()
    with fresh.connection() as db:
        assert (
            db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        )
        assert tuple(db.execute("SELECT * FROM local_drafts").fetchone()) == old_draft
        assert tuple(db.execute("SELECT * FROM scheduled_jobs").fetchone()) == old_job
        assert not db.execute("SELECT * FROM restore_operations").fetchall()
    restored_draft = DraftService(fresh, drafts.recovery_directory).get(draft.reference)
    assert restored_draft.body_text == draft.body_text
    assert drafts.recovery_path(draft.reference).read_bytes() == before
    assert fresh.blocking_job_for_draft(draft.reference)["reference"] == job.reference


def test_large_restore_history_uses_bounded_indexed_present_and_absent_lookups(tmp_path):
    state = StateStore(tmp_path / "state.sqlite")
    journal = RestoreJournal(state)
    chain = "c" * 64
    journal.begin(
        "large",
        "synthetic-backup",
        chain,
        (RestoreTargetIntent(f"item-{i}", f"msg-{i}", "Restored/Synthetic") for i in range(25_000)),
    )
    with state.transaction() as db:
        db.execute(
            "UPDATE restore_targets SET status='completed',result_reference='synthetic-result'"
        )
    with state.connection() as db:
        work = [0]

        def step():
            work[0] += 1
            return 0

        db.set_progress_handler(step, 1)
        found = journal._latest(db, replay_key(chain, "item-24999", "Restored/Synthetic"))
        absent = journal._latest(db, replay_key(chain, "missing", "Restored/Synthetic"))
        assert found and absent is None
        assert work[0] < 500, f"Lookup VM work scaled with history: {work[0]}"
    assert len(journal.targets("large", offset=24_900)) == 100


def test_failed_v16_ddl_rolls_back_and_can_be_retried_without_changing_v15_data(
    tmp_path, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(state_module, "SCHEMA_VERSION", 15)
        legacy = StateStore(tmp_path / "state.sqlite")
        legacy.initialize()
        old_preference = legacy.put_ui_preference(
            "synthetic-upgrade-marker", {"exact": "[literal] جامعة"}
        )
    fresh = StateStore(legacy.path)
    connect = fresh._connect

    def reject_index():
        db = connect()
        db.set_authorizer(
            lambda action, name, *_args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_INDEX and name == "idx_restore_replay_lookup"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    with monkeypatch.context() as patch:
        patch.setattr(fresh, "_connect", reject_index)
        with pytest.raises(LocalStateError, match="migration failed"):
            fresh.initialize()
    assert not fresh._initialized
    with fresh.connection() as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 15
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'restore_%'"
        ).fetchall()
    fresh.initialize()
    assert fresh.get_ui_preference("synthetic-upgrade-marker") == old_preference
    assert RestoreJournal(fresh).operations() == []


def test_restore_preview_reuses_one_read_connection_without_materializing_history(
    tmp_path, monkeypatch
):
    from gucmail.services.backup import BackupItem, BackupService

    state = StateStore(tmp_path / "state.sqlite")
    state.initialize()
    service = BackupService(tmp_path / "backups", state)
    archive = service.create(
        (BackupItem(f"msg-{i}", "message", {"subject": f"Synthetic {i}"}) for i in range(20)),
        backup_id="synthetic-connection-test",
    )
    original = state.connection
    calls = []

    def observed():
        calls.append(True)
        return original()

    monkeypatch.setattr(state, "connection", observed)
    plan = service.plan_restore(archive.backup_id)
    assert plan.create_count == 20
    assert len(calls) == 1
