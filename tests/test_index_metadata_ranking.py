"""Narrow ranking metadata preserves exact filtering, ordering and durable writes."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.services.index import INDEX_SCHEMA_VERSION, IndexDocument, SearchIndex


def documents():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        IndexDocument(
            f"doc-{i:04d}",
            subject=f"Academic {i} [red] 研究",
            sender="Alice@example.test" if i % 2 else "bob@example.test",
            body="Academic notes " * (i % 7 + 1),
            attachment_names="notes.PDF" if i % 3 else "",
            attachment_text="optics" if i % 3 == 0 else "",
            folder_path="/Inbox/_%[x]" if i % 2 else "/Archive/研究",
            received_at=None if i % 11 == 0 else start + timedelta(hours=i // 2),
            size=None if i % 13 == 0 else i * 100,
            payload={"synthetic": i},
        )
        for i in range(300)
    ]


def records(index):
    with index.stream_records() as values:
        return list(values)


def test_oversized_offset_is_a_validation_error(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    with pytest.raises(ConfigurationError, match="bounds"):
        index.search("Academic", offset=1 << 70)


def prepare_v2_mapping(index):
    with sqlite3.connect(index.path) as connection:
        identities = connection.execute(
            "SELECT fts_rowid,reference FROM document_fts_ids ORDER BY reference"
        ).fetchall()
        # Recreate the actual v2 mapping shape; do not leave v3 columns under an
        # older version marker or rely on mocking the new version constant.
        connection.execute("DROP TABLE document_fts_ids")
        connection.execute(
            "CREATE TABLE document_fts_ids(fts_rowid INTEGER PRIMARY KEY, "
            "reference TEXT NOT NULL UNIQUE REFERENCES documents(reference) ON DELETE CASCADE)"
        )
        connection.executemany("INSERT INTO document_fts_ids VALUES (?,?)", identities)
        connection.execute("UPDATE index_metadata SET value='2' WHERE key='schema_version'")
    return identities


def test_v2_upgrade_preserves_ids_records_and_revision(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert_many(documents(), limit=500)
    before, revision = records(index), index.revision()
    identities = prepare_v2_mapping(index)
    reopened = SearchIndex(index.path)
    assert reopened.verify().valid and reopened.verify().schema_version == INDEX_SCHEMA_VERSION
    assert records(reopened) == before and reopened.revision() == revision
    with reopened._connection() as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT fts_rowid,reference FROM document_fts_ids ORDER BY reference"
            )
        ]
    assert after == identities


def test_failed_v2_metadata_backfill_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert_many(documents()[:3])
    before, revision = records(index), index.revision()
    prepare_v2_mapping(index)
    original = index._connect

    def rejected_backfill():
        connection = original()
        connection.set_authorizer(
            lambda action, table, column, database, trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == "document_fts_ids"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    with monkeypatch.context() as failure:
        failure.setattr(index, "_connect", rejected_backfill)
        with pytest.raises(LocalStateError, match="initialize"):
            index.initialize()
    with sqlite3.connect(index.path) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(document_fts_ids)")] == [
            "fts_rowid",
            "reference",
        ]
        assert (
            connection.execute(
                "SELECT value FROM index_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "2"
        )
    reopened = SearchIndex(index.path)
    assert reopened.verify().valid and reopened.revision() == revision
    assert records(reopened) == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("received_at", "broken"),
        ("folder_path", "wrong"),
        ("sender", "wrong"),
        ("size", -1),
        ("attachment_names", "wrong"),
    ],
)
def test_integrity_check_detects_stale_filter_metadata(tmp_path, column, value):
    index = SearchIndex(tmp_path / "index.sqlite")
    index.upsert_many(documents()[:2])
    with index._connection() as connection:
        connection.execute(
            f"UPDATE document_fts_ids SET {column}=? WHERE reference='doc-0001'",  # noqa: S608
            (value,),
        )
    result = index.verify()
    assert result.integrity == "ok" and not result.valid
    assert result.documents == result.full_text_rows == 2


def test_metadata_write_failure_rolls_back_document_fts_and_revision(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite")
    values = documents()[:2]
    index.upsert_many(values)
    before, revision = records(index), index.revision()
    with index._connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_metadata BEFORE UPDATE ON document_fts_ids "
            "WHEN NEW.reference='doc-0001' BEGIN SELECT RAISE(ABORT,'Synthetic rejection'); END"
        )
    with pytest.raises(LocalStateError, match="rolled back"):
        index.upsert_many(
            [replace(item, sender="changed@example.test", size=42) for item in values]
        )
    assert records(index) == before and index.revision() == revision and index.verify().valid
    assert not index.search("changed", fields=("sender",))


def legacy(index, term, options):
    params = [index._match_expression(term, fields=options.get("fields", ()))]
    predicates = ["documents_fts MATCH ?"]
    for name, expression in (
        ("folder", "d.folder_path LIKE ? ESCAPE '\\'"),
        ("sender", "LOWER(d.sender) LIKE ?"),
        ("after", "d.received_at >= ?"),
        ("before", "d.received_at < ?"),
        ("minimum_size", "d.size >= ?"),
        ("maximum_size", "d.size <= ?"),
        ("attachment_extension", "LOWER(d.attachment_names) LIKE ?"),
    ):
        if name in options:
            value = options[name]
            if name == "folder":
                value = (
                    "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
            elif name in {"sender", "attachment_extension"}:
                value = "%" + value.casefold() + "%"
            elif name in {"after", "before"}:
                value = value.isoformat()
            predicates.append(expression)
            params.append(value)
    where = " AND ".join(predicates)
    with index._connection() as connection:
        rows = connection.execute(
            "SELECT d.reference,bm25(documents_fts) AS rank,"
            "snippet(documents_fts,-1,'[',']',' … ',24) AS sample "
            "FROM documents_fts JOIN documents d USING(reference) WHERE "
            + where
            + " ORDER BY rank,d.received_at DESC,d.reference LIMIT 25 OFFSET ?",
            (*params, options.get("offset", 0)),
        ).fetchall()
    return [(row[0], round(max(0.0, 100.0 + row[1]), 4), row[2]) for row in rows]


@pytest.mark.parametrize(
    "term,options",
    [
        ("Academic", {}),
        ("Academic", {"offset": 200}),
        ("Academic", {"folder": "Inbox/_%[x]"}),
        ("Academic", {"sender": "ALICE"}),
        ("Academic", {"after": datetime(2026, 10, 3, tzinfo=UTC)}),
        ("Academic", {"before": datetime(2026, 10, 3, tzinfo=UTC)}),
        ("Academic", {"minimum_size": 0, "maximum_size": 15_000}),
        ("notes", {"fields": ("attachment_names",)}),
        ("optics", {"fields": ("attachment_text",)}),
        ("Academic", {"attachment_extension": "PDF"}),
        ("Academic", {"sender": "alice", "minimum_size": 9000, "folder": "Inbox"}),
    ],
)
def test_narrow_ranking_matches_legacy_full_document_query(tmp_path, term, options):
    index = SearchIndex(tmp_path / "index.sqlite")
    values = documents()
    index.upsert_many(values, limit=500)
    expected = legacy(index, term, options)
    assert [
        (hit.reference, hit.score, hit.snippet)
        for hit in index.search(
            term,
            limit=25,
            **options,
        )
    ] == expected
    # Update filter/order fields after ingestion, retaining the same FTS identity.
    index.upsert(
        replace(
            values[1],
            sender="changed@example.test",
            size=1,
            received_at=datetime(2026, 12, 1, tzinfo=UTC),
            folder_path="/Other",
        )
    )
    assert index.verify().valid
    assert [
        (hit.reference, hit.score, hit.snippet)
        for hit in index.search(
            term,
            limit=25,
            **options,
        )
    ] == legacy(index, term, options)
