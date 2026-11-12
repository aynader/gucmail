from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import LocalStateError
from gucmail.ews.mapping import make_local_reference
from gucmail.models import SendState
from gucmail.state import MIGRATIONS, StateStore


def add_original_scheduling_table(connection):
    # These focused legacy fixtures construct only their original subsystem.
    # Scheduling also existed in v1; include its genuine shape so the current
    # indexed composer lookup is exercised against a valid old database.
    statement = next(
        sql for sql in MIGRATIONS[1].split(";")
        if sql.strip().startswith("CREATE TABLE scheduled_jobs (")
    )
    connection.execute(statement)


def test_v15_index_migration_fails_closed_if_scheduling_table_is_missing(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 15):
            connection.executescript(MIGRATIONS[version])
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic')", (version,))
        connection.execute("""
            INSERT INTO local_drafts(reference,draft_json,revision,state,content_hash,
                created_at,updated_at) VALUES
            ('dft-kept','{"body_text":"Retained unsent text"}',1,'local_only','hash','now','now')
        """)
        connection.execute("DROP TABLE scheduled_jobs")
    with pytest.raises(LocalStateError, match="migration failed"):
        StateStore(path).initialize()
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 14
        assert connection.execute("SELECT draft_json FROM local_drafts").fetchone()[0] == (
            '{"body_text":"Retained unsent text"}'
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='scheduled_jobs'"
        ).fetchone() is None


def test_state_migration_and_saved_searches(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    assert store.health()["integrity"] == "ok"
    assert store.health()["schema_version"] == SCHEMA_VERSION
    store.save_search("Unread PDFs", {"is_read": False, "attachment_name": ".pdf"})
    assert store.get_search("unread pdfs") == {
        "attachment_name": ".pdf",
        "is_read": False,
    }
    assert len(store.list_searches()) == 1
    assert store.remove_search("UNREAD PDFS")


def test_notification_ledger_migrates_from_composite_v2_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        add_original_scheduling_table(connection)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'now'), (2, 'now');
            CREATE TABLE notification_history (
                event_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO notification_history(event_id, provider, state, detail, created_at)
            VALUES ('evt-legacy:console', 'console', 'delivered', NULL, '2026-08-05T00:00:00Z');
            CREATE TABLE item_references (
                local_ref TEXT PRIMARY KEY,
                item_kind TEXT NOT NULL,
                ews_id TEXT NOT NULL,
                change_key TEXT,
                folder_path TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_item_refs_ews
                ON item_references(item_kind, ews_id);
            """
        )

    store = StateStore(path)
    assert store.health()["schema_version"] == SCHEMA_VERSION
    with store.connection() as connection:
        row = connection.execute(
            "SELECT event_id, provider, attempts FROM notification_history"
        ).fetchone()
        claim_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='notification_delivery_claims'"
        ).fetchone()

    assert row is not None
    assert tuple(row) == ("evt-legacy", "console", 1)
    assert claim_table is not None


def test_first_run_migrations_are_serialized_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    stores = [StateStore(path) for _ in range(4)]

    with ThreadPoolExecutor(max_workers=4) as executor:
        health = list(executor.map(lambda store: store.health(), stores))

    assert all(item["integrity"] == "ok" for item in health)
    assert all(item["schema_version"] == SCHEMA_VERSION for item in health)


def test_event_deduplication(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    assert store.remember_event("evt-1", "m-1")
    assert not store.remember_event("evt-1", "m-1")


def test_scheduled_job_claim_is_exclusive(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    store.add_scheduled_job(
        reference="s-1",
        draft_reference="d-1",
        scheduled_for=now - timedelta(minutes=1),
        timezone="Africa/Cairo",
        original_expression="in 1 minute",
        idempotency_key="unique-key",
    )
    first = store.claim_due_jobs(worker_id="worker-a", now=now)
    second = store.claim_due_jobs(worker_id="worker-b", now=now)
    assert [job.reference for job in first] == ["s-1"]
    assert second == []
    assert len(store.list_scheduled_jobs(limit=1)) == 1


def test_stale_sending_job_requires_manual_review(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    store.add_scheduled_job(
        reference="s-1",
        draft_reference="d-1",
        scheduled_for=now - timedelta(minutes=1),
        timezone="UTC",
        original_expression="2026-08-05T11:59Z",
        idempotency_key="unique-key",
    )
    assert store.claim_due_jobs(worker_id="crashed", now=now, lease_seconds=1)
    assert store.recover_stale_jobs(now=now + timedelta(seconds=2)) == 1
    row = store.list_scheduled_jobs()[0]
    assert row["state"] == SendState.MANUAL_REVIEW.value


def test_reference_survives_move_deleted_items_relist_and_restore(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db", account_key="student@example.test")
    original_id = "synthetic-inbox-id"
    deleted_id = "synthetic-deleted-id"
    restored_id = "synthetic-restored-id"
    original_ref = make_local_reference("message", original_id)
    deleted_refresh_ref = make_local_reference("message", deleted_id)
    restored_refresh_ref = make_local_reference("message", restored_id)

    assert (
        store.put_reference(
            original_ref,
            "message",
            original_id,
            change_key="ck-inbox",
            folder_path="Inbox",
        )
        == original_ref
    )
    assert (
        store.put_reference(
            original_ref,
            "message",
            deleted_id,
            change_key="ck-deleted",
            folder_path="Deleted Items",
        )
        == original_ref
    )

    # This was the historical Deleted Items IntegrityError: the refreshed page
    # derives a second local ref for the replacement EWS ID.
    assert (
        store.put_reference(
            deleted_refresh_ref,
            "message",
            deleted_id,
            change_key="ck-deleted",
            folder_path="Deleted Items",
        )
        == original_ref
    )
    deleted = store.resolve_reference(deleted_refresh_ref, "message")
    assert deleted is not None
    assert deleted["canonical_ref"] == original_ref
    assert deleted["ews_id"] == deleted_id
    assert deleted["folder_path"] == "Deleted Items"
    assert deleted["is_alias"] is True
    assert len(list(store.iter_rows("item_references"))) == 1

    # Restore through the relist alias, then encounter the restored item through
    # another freshly derived reference. The original public identity still wins.
    assert (
        store.put_reference(
            deleted_refresh_ref,
            "message",
            restored_id,
            change_key="ck-restored",
            folder_path="Inbox",
        )
        == original_ref
    )
    assert (
        store.put_reference(
            restored_refresh_ref,
            "message",
            restored_id,
            change_key="ck-restored",
            folder_path="Inbox",
        )
        == original_ref
    )
    assert store.resolve_reference(original_ref, "message")["ews_id"] == restored_id  # type: ignore[index]
    assert store.resolve_reference(deleted_refresh_ref, "message")["ews_id"] == restored_id  # type: ignore[index]
    assert store.resolve_reference(restored_refresh_ref, "message")["ews_id"] == restored_id  # type: ignore[index]
    history = store.reference_history(original_ref, "message")
    assert {item["ews_id"] for item in history} == {original_id, deleted_id, restored_id}
    assert [item["ews_id"] for item in history if item["is_current"]] == [restored_id]
    assert store.reference_health()["ok"] is True


def test_refresh_before_move_is_merged_into_existing_stable_identity(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    stable_ref = "msg_stable"
    refresh_ref = "msg_refresh"
    store.put_reference(stable_ref, "message", "ews-before-move")

    # A destination refresh may win the race and cache the replacement ID first.
    store.put_reference(refresh_ref, "message", "ews-after-move")
    assert len(list(store.iter_rows("item_references"))) == 2

    # The move completion carries the known stable ref. It deterministically wins,
    # while the prematurely cached ref becomes a redirect instead of a duplicate.
    assert store.put_reference(stable_ref, "message", "ews-after-move") == stable_ref
    assert len(list(store.iter_rows("item_references"))) == 1
    assert store.resolve_reference(refresh_ref, "message")["canonical_ref"] == stable_ref  # type: ignore[index]


def test_concurrent_relists_share_one_canonical_reference(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    stable = StateStore(path)
    stable.put_reference("msg_original", "message", "ews-original")
    stable.put_reference("msg_original", "message", "ews-replacement")
    aliases = [f"msg_refresh_{index}" for index in range(24)]

    def remember(alias: str) -> str:
        return StateStore(path).put_reference(alias, "message", "ews-replacement")

    with ThreadPoolExecutor(max_workers=8) as executor:
        canonical = list(executor.map(remember, aliases))

    assert canonical == ["msg_original"] * len(aliases)
    assert len(list(stable.iter_rows("item_references"))) == 1
    assert len(list(stable.iter_rows("item_reference_aliases"))) == len(aliases)
    assert all(
        stable.resolve_reference(alias, "message")["canonical_ref"] == "msg_original"  # type: ignore[index]
        for alias in aliases
    )
    assert stable.reference_health()["ok"] is True


def test_reference_identity_is_account_scoped(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = StateStore(path, account_key="First@Example.Test")
    second = StateStore(path, account_key="second@example.test")

    first.put_reference("msg_shared", "message", "same-mailbox-local-id", change_key="a")
    second.put_reference("msg_shared", "message", "same-mailbox-local-id", change_key="b")

    assert first.resolve_reference("msg_shared", "message")["change_key"] == "a"  # type: ignore[index]
    assert second.resolve_reference("msg_shared", "message")["change_key"] == "b"  # type: ignore[index]
    rows = list(first.iter_rows("item_references"))
    assert {row["account_key"] for row in rows} == {
        "first@example.test",
        "second@example.test",
    }


def test_local_reference_factory_can_namespace_accounts_without_changing_legacy_refs() -> None:
    legacy = make_local_reference("message", "remote-id")
    assert legacy == make_local_reference("message", "remote-id")
    assert make_local_reference(
        "message", "remote-id", account_key="FIRST@EXAMPLE.TEST"
    ) == make_local_reference("message", "remote-id", account_key="first@example.test")
    assert legacy != make_local_reference("message", "remote-id", account_key="first@example.test")
    assert make_local_reference(
        "message", "remote-id", account_key="first@example.test"
    ) != make_local_reference("message", "remote-id", account_key="second@example.test")


def test_v3_duplicate_reference_migration_repairs_collisions_and_preserves_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        add_original_scheduling_table(connection)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, 'v1'), (2, 'v2'), (3, 'v3');
            CREATE TABLE item_references (
                local_ref TEXT PRIMARY KEY,
                item_kind TEXT NOT NULL,
                ews_id TEXT NOT NULL,
                change_key TEXT,
                folder_path TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO item_references VALUES
                ('msg_stable', 'message', 'duplicate-ews-id', 'stable-ck', 'Deleted Items',
                 '2026-08-01T00:00:00+00:00'),
                ('msg_generated', 'message', 'duplicate-ews-id', 'new-ck', 'Deleted Items',
                 '2026-08-02T00:00:00+00:00'),
                ('msg_other', 'message', 'other-ews-id', 'other-ck', 'Inbox',
                 '2026-08-03T00:00:00+00:00');
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('scheduler.paused', '1');
            CREATE TABLE saved_searches (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                query_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO saved_searches VALUES
                ('Keep me', '{"is_read": false}', 'created', 'updated');
            """
        )

    store = StateStore(path)
    health = store.health()
    assert health["schema_version"] == SCHEMA_VERSION
    assert health["integrity"] == "ok"
    assert health["foreign_key_errors"] == 0
    assert health["reference_identity"]["ok"] is True
    assert len(list(store.iter_rows("item_references"))) == 2
    assert store.resolve_reference("msg_stable", "message")["canonical_ref"] == "msg_stable"  # type: ignore[index]
    assert store.resolve_reference("msg_generated", "message")["canonical_ref"] == "msg_stable"  # type: ignore[index]
    assert store.resolve_reference("msg_other", "message")["ews_id"] == "other-ews-id"  # type: ignore[index]
    assert store.get_search("keep me") == {"is_read": False}
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key='scheduler.paused'"
            ).fetchone()[0]
            == "1"
        )


def test_disposable_cache_clear_preserves_monitor_continuity(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.put_reference("msg_cached", "message", "ews-cached")
    store.checkpoint("watch:inbox", sync_state="opaque-token", succeeded=True)
    assert store.remember_event("event-once", "msg_cached") is True

    cleared = store.clear_disposable_cache()

    assert cleared["item_references"] == 1
    assert "sync_checkpoints" not in cleared
    assert "seen_items" not in cleared
    assert store.remember_event("event-once", "msg_cached") is False
    with store.connection() as connection:
        checkpoint = connection.execute(
            "SELECT sync_state, last_success_at FROM sync_checkpoints WHERE scope='watch:inbox'"
        ).fetchone()
    assert checkpoint is not None
    assert checkpoint["sync_state"] == "opaque-token"
    assert checkpoint["last_success_at"] is not None
