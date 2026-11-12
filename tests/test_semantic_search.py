"""Actual synthetic model queries and bounded local lifecycle failure tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gucmail.errors import ConfirmationRequiredError, ConflictError, UnsupportedFeatureError
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.semantic import SemanticDocument, SemanticStateError, SemanticStore
from gucmail.services.semantic_model import (
    LocalModelError,
    LocalTopicModel,
    ModelBuildCancelled,
    fit_in_worker,
)
from gucmail.services.semantic_search import SemanticSearchService

FIXTURE = Path(__file__).parent / "fixtures/semantic-local-lsa-v1.json"
IDENTITY = hashlib.sha256(b"synthetic-account-only").hexdigest()
CHUNKS = (
    "car automobile motor engine vehicle road drive",
    "car engine road vehicle commute",
    "automobile motor vehicle drive transport",
    "bus train commute transport road",
    "cat kitten animal pet whiskers fur",
    "cat kitten pet animal paws",
    "dog puppy animal pet fur",
    "dog puppy pet walk animal",
)


def trained_model() -> LocalTopicModel:
    return LocalTopicModel.from_bytes(FIXTURE.read_bytes())


def prepared(path: Path, monkeypatch: pytest.MonkeyPatch) -> SemanticSearchService:
    from gucmail.services import semantic as module

    # Real numerical fixture; fitting isolation is verified by the separate owned-worker probe.
    monkeypatch.setattr(module, "fit_in_worker", lambda *args, **kwargs: trained_model())
    index = SearchIndex(path / "index.sqlite3")
    index.upsert_many(IndexDocument(f"m-{i}", subject=text) for i, text in enumerate(CHUNKS))
    store = SemanticStore(path / "semantic.sqlite3", identity=IDENTITY, enabled=True)
    service = SemanticSearchService(index, store)
    service.build(confirmed=True, dimensions=2)
    return service


def test_trained_model_query_roundtrip_is_nonlexical_and_has_no_numpy_import(monkeypatch):
    import gucmail.services.semantic_model as module

    def forbidden(*args, **kwargs):
        pytest.fail("Query attempted to import a fitting dependency")

    monkeypatch.setattr(module.importlib, "import_module", forbidden)
    model = trained_model()
    assert model.identity == "a76d900b37fe6274f02c28dbf691dbd927b5d7d531a3d92196267c8bbbab3728"
    assert model.similarity("car", CHUNKS[2]) > 0.9
    assert model.similarity("car", CHUNKS[4]) < 0.1
    assert LocalTopicModel.from_bytes(model.to_bytes()).embed("car") == model.embed("car")


@pytest.mark.parametrize("corruption", ["version", "idf", "nan", "basis", "provenance", "unknown"])
def test_corrupt_model_data_is_rejected(corruption):
    raw = json.loads(FIXTURE.read_bytes())
    if corruption == "version":
        raw["version"] = "unknown"
    elif corruption == "idf":
        raw["idf"][0] = True
    elif corruption == "nan":
        raw["components"][0][0] = float("nan")
    elif corruption == "basis":
        raw["components"][0] = [0] * len(raw["vocabulary"])
    elif corruption == "provenance":
        raw["training_digest"] = "not-a-digest"
    else:
        raw["executable"] = "never execute model content"
    with pytest.raises(LocalModelError):
        LocalTopicModel.from_bytes(json.dumps(raw).encode())


@pytest.mark.parametrize(
    "confirmed,enabled,error",
    [(False, True, ConfirmationRequiredError), (True, False, UnsupportedFeatureError)],
)
def test_absent_consent_does_not_create_data(tmp_path, confirmed, enabled, error):
    store = SemanticStore(tmp_path / "semantic.sqlite3", identity=IDENTITY, enabled=enabled)
    with pytest.raises(error):
        store.build(
            [SemanticDocument("a", CHUNKS[0]), SemanticDocument("b", CHUNKS[1])],
            confirmed=confirmed,
        )
    assert not store.path.exists()


def test_snapshot_selects_only_bounded_fields_and_never_initializes_absent_index(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite3")
    empty = index.semantic_snapshot(limit=2)
    assert not empty.documents and not index.path.exists()
    index.upsert_many(
        IndexDocument(
            f"m-{i}",
            subject="car" * 4000,
            body="private-body-" * 5000,
            sender="hidden@example.test",
            payload={"hidden": "must-not-load"},
        )
        for i in range(3)
    )
    subjects = index.semantic_snapshot(limit=2)
    assert len(subjects.documents) == 2 and subjects.total_documents == 3 and subjects.truncated
    assert all(len(d.subject) == 8192 and not d.body for d in subjects.documents)
    bodies = index.semantic_snapshot(limit=2, include_body=True)
    assert all(len(d.body) == 32768 for d in bodies.documents)
    assert bodies.source_digest != subjects.source_digest
    assert "hidden@example.test" not in repr(bodies) and "private-body" not in repr(bodies)


def test_index_query_restart_and_incremental_update_keep_exact_model(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    initial = service.store.status()
    hits = service.search("car")
    nonlexical = next(hit for hit in hits if hit.reference == "m-2")
    assert nonlexical.lexical_score == 0 and nonlexical.semantic_score > 0.9
    reopened = SemanticSearchService(
        service.index, SemanticStore(service.store.path, identity=IDENTITY, enabled=True)
    )
    assert reopened.search("car") == hits
    unchanged = reopened.build(confirmed=True)
    assert unchanged["unchanged_documents"] == 8 and unchanged["updated_documents"] == 0
    service.index.upsert_many([IndexDocument("m-2", subject=CHUNKS[2] + " road")])
    service.index.delete("m-7")
    assert reopened.status()["stale"]
    with pytest.raises(ConflictError, match="changed"):
        reopened.search("car")
    updated = reopened.build(confirmed=True)
    assert updated["updated_documents"] == 1 and updated["removed_documents"] == 1
    assert updated["unchanged_documents"] == 6 and not updated["retrained"]
    assert updated["model_bytes"] == initial["model_bytes"]
    assert not reopened.status()["stale"]
    assert all(hit.reference != "m-7" for hit in reopened.search("pet"))


def test_source_change_after_preview_or_during_fit_retains_existing_model(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    before = service.store.path.read_bytes()
    preview = service.preview()
    service.index.upsert_many([IndexDocument("m-8", subject="synthetic new car")])
    with pytest.raises(ConflictError, match="preview"):
        service.build(confirmed=True, expected_source_digest=preview["source_digest"])
    assert service.store.path.read_bytes() == before
    from gucmail.services import semantic as module

    def changed(*args, **kwargs):
        service.index.upsert_many([IndexDocument("m-9", subject="new synthetic animal")])
        return trained_model()

    monkeypatch.setattr(module, "fit_in_worker", changed)
    with pytest.raises(ConflictError, match="during fitting"):
        service.build(confirmed=True, rebuild=True)
    assert service.store.path.read_bytes() == before


def test_cancelled_build_and_changed_fields_preserve_existing_data(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    before = service.store.path.read_bytes()
    with pytest.raises(ModelBuildCancelled):
        service.build(confirmed=True, cancelled=lambda: True)
    with pytest.raises(ConflictError, match="rebuild"):
        service.build(confirmed=True, include_body=True)
    assert service.store.path.read_bytes() == before


def test_identity_conflict_and_unknown_schema_do_not_adopt_or_clear_data(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    before = service.store.path.read_bytes()
    foreign = SemanticStore(service.store.path, identity="b" * 64, enabled=True)
    with pytest.raises(SemanticStateError, match="another account"):
        foreign.clear(confirmed=True)
    assert service.store.path.read_bytes() == before
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES('synthetic preserved data')")
    before = other.read_bytes()
    with pytest.raises(SemanticStateError, match="schema"):
        SemanticStore(other, identity=IDENTITY).status()
    assert other.read_bytes() == before


def test_transaction_failure_rolls_back_and_does_not_expose_sqlite_error(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_write BEFORE UPDATE ON embeddings BEGIN "
            "SELECT RAISE(ABORT,'synthetic private body in provider failure'); END"
        )
    before = service.store.path.read_bytes()
    service.index.upsert_many([IndexDocument("m-2", subject=CHUNKS[2] + " road")])
    with pytest.raises(SemanticStateError) as caught:
        service.build(confirmed=True)
    assert "private body" not in str(caught.value)
    assert service.store.path.read_bytes() == before


def test_concurrent_clear_during_fit_prevents_resurrection(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    from gucmail.services import semantic as module

    def clear_while_fitting(*args, **kwargs):
        service.store.clear(confirmed=True)
        return trained_model()

    monkeypatch.setattr(module, "fit_in_worker", clear_while_fitting)
    with pytest.raises(ConflictError, match="Another"):
        service.build(confirmed=True, rebuild=True)
    assert service.store.status()["documents"] == 0
    assert not service.store.status()["built"]


def test_clear_requires_confirmation_compacts_derived_data_and_preserves_fts(tmp_path, monkeypatch):
    service = prepared(tmp_path, monkeypatch)
    before = service.store.path.read_bytes()
    lexical = service.index.search("car")
    with pytest.raises(ConfirmationRequiredError):
        service.clear(confirmed=False)
    assert service.store.path.read_bytes() == before
    result = service.clear(confirmed=True)
    assert result["cleared"] == 8 and result["mailbox_mutations"] == 0
    assert not service.store.status()["built"]
    assert b"automobile" not in service.store.path.read_bytes()
    assert service.index.search("car") == lexical


def test_body_selection_is_explicit_and_raw_bodies_are_not_persisted(tmp_path, monkeypatch):
    from gucmail.services import semantic as module

    calls = []

    def capture(chunks, **kwargs):
        calls.append(tuple(chunks))
        return trained_model()

    monkeypatch.setattr(module, "fit_in_worker", capture)
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert_many(
        [
            IndexDocument("a", subject=CHUNKS[0], body="private corpus body car"),
            IndexDocument("b", subject=CHUNKS[4], body="private corpus body animal"),
        ]
    )
    service = SemanticSearchService(
        index, SemanticStore(tmp_path / "semantic.sqlite3", identity=IDENTITY, enabled=True)
    )
    service.build(confirmed=True, dimensions=2)
    assert all("private corpus body" not in chunk for chunk in calls[0])
    service.build(confirmed=True, dimensions=2, include_body=True, rebuild=True)
    assert any("private corpus body" in chunk for chunk in calls[1])
    assert b"private corpus body" not in service.store.path.read_bytes()


def test_actual_optional_worker_degrades_without_numpy_or_fits_locally_when_installed():
    import importlib.util

    if importlib.util.find_spec("numpy") is None:
        with pytest.raises(UnsupportedFeatureError, match="optional semantic dependency"):
            fit_in_worker(CHUNKS, dimensions=2)
    else:
        model = fit_in_worker(CHUNKS, dimensions=2)
        assert model.similarity("car", CHUNKS[2]) > 0.9
    with pytest.raises(ModelBuildCancelled):
        fit_in_worker(CHUNKS, dimensions=2, cancelled=lambda: True)


@pytest.mark.parametrize("value", ["invalid-json", "wrong-dimensions", "unnormalized"])
def test_corrupt_embeddings_reject_query_instead_of_returning_scores(tmp_path, monkeypatch, value):
    service = prepared(tmp_path, monkeypatch)
    serialized = (
        "{"
        if value == "invalid-json"
        else json.dumps([[1]])
        if value == "wrong-dimensions"
        else json.dumps([[0.5, 0.5]])
    )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute("UPDATE embeddings SET vectors=? WHERE reference='m-0'", (serialized,))
    with pytest.raises(SemanticStateError, match="Corrupt"):
        service.search("car")
