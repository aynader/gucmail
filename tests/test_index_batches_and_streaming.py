"""Index migration, bounded atomic ingestion and stable streaming snapshots."""

import sqlite3
from threading import Event

import pytest

from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.services.index import IndexDocument, IndexRebuildCancelled, SearchIndex


def test_v1_migration_preserves_records_and_updates_the_correct_fts_row(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert_many([IndexDocument("z", subject="Old"), IndexDocument("a", subject="Keep")])
    with sqlite3.connect(index.path) as db:
        db.execute("DROP TABLE document_fts_ids")
        db.execute("UPDATE index_metadata SET value='1' WHERE key='schema_version'")
        db.execute("DELETE FROM documents_fts")
        # Deliberately unrelated rowids/order; migration must rebuild the mapping.
        db.execute("INSERT INTO documents_fts(rowid,reference,subject) VALUES (99,'z','Old')")
        db.execute("INSERT INTO documents_fts(rowid,reference,subject) VALUES (3,'a','Keep')")
    reopened = SearchIndex(index.path)
    assert reopened.verify().valid
    reopened.upsert(IndexDocument("z", subject="Replacement"))
    assert not reopened.search("Old")
    assert [v.reference for v in reopened.search("Replacement")] == ["z"]
    assert [v.reference for v in reopened.search("Keep")] == ["a"]
    # A VACUUM can change implicit document rowids. Explicit mapping IDs remain safe.
    with sqlite3.connect(index.path) as db:
        db.execute("VACUUM")
    reopened.upsert(IndexDocument("a", subject="AfterVacuum"))
    assert not reopened.search("Keep")
    assert [v.reference for v in reopened.search("AfterVacuum")] == ["a"]
    assert reopened.delete("z") and not reopened.search("Replacement")
    assert reopened.verify().valid


@pytest.mark.parametrize("fault", ["invalid", "sql", "cancel", "bytes"])
def test_ingestion_failure_rolls_back_records_fts_and_revision(tmp_path, monkeypatch, fault):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert(IndexDocument("old", subject="Retained"))
    before = index.revision()
    cancel = Event()
    if fault == "sql":
        with sqlite3.connect(index.path) as db:
            db.execute(
                "CREATE TRIGGER reject_second BEFORE INSERT ON documents "
                "WHEN NEW.reference='second' BEGIN SELECT RAISE(ABORT,'Synthetic'); END"
            )
    if fault == "cancel":
        original = index._write_row

        def write(db, row):
            original(db, row)
            cancel.set()

        monkeypatch.setattr(index, "_write_row", write)
    values = [
        IndexDocument("first", subject="New"),
        IndexDocument("" if fault == "invalid" else "second", subject="New"),
    ]
    with pytest.raises((ConfigurationError, LocalStateError, IndexRebuildCancelled)):
        index.upsert_many(
            values,
            cancel_event=cancel,
            max_prepared_bytes=1 if fault == "bytes" else 32 * 1024 * 1024,
        )
    assert index.revision() == before
    assert not index.search("New")
    assert [v.reference for v in index.search("Retained")] == ["old"]
    assert index.verify().valid


def test_batch_bound_does_not_consume_the_next_input(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    values = iter([IndexDocument(str(i)) for i in range(3)])
    assert index.upsert_many(values, limit=2) == 2
    assert next(values).reference == "2"


def test_stream_snapshot_order_filters_cancellation_and_early_close(tmp_path, monkeypatch):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert_many([IndexDocument(str(i), source="synthetic") for i in range(23)])
    expected = [v.reference for v in index.records(limit=30)]
    opened = []
    original = index._connect

    def connect():
        db = original()
        opened.append(db)
        return db

    monkeypatch.setattr(index, "_connect", connect)
    with index.stream_records(batch_size=3, source="synthetic") as records:
        first = next(records)
        other = SearchIndex(index.path)
        other.upsert(IndexDocument("added", source="synthetic"))
        actual = [first.reference, *(v.reference for v in records)]
        assert actual == expected and "added" not in actual
    cancel = Event()
    with index.stream_records(batch_size=2, cancel_event=cancel) as records:
        next(records)
        cancel.set()
        with pytest.raises(IndexRebuildCancelled):
            next(records)
    with index.stream_records(batch_size=2) as records:
        next(records)  # Intentional early stop still releases the snapshot.
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_future_schema_is_retained(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert(IndexDocument("old", subject="Retained"))
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE index_metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(LocalStateError, match="newer schema"):
        SearchIndex(index.path).initialize()
    with sqlite3.connect(index.path) as db:
        assert (
            db.execute("SELECT value FROM index_metadata WHERE key='schema_version'").fetchone()[0]
            == "999"
        )
        assert db.execute("SELECT subject FROM documents").fetchone()[0] == "Retained"
