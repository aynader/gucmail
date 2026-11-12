"""Durable restore claims, safe retries, exact review and bounded recovery reads."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from gucmail.errors import ConflictError, LocalStateError
from gucmail.services.restore_journal import RestoreJournal, RestoreTargetIntent
from gucmail.state import StateStore

CHAIN = "a" * 64
TARGET = RestoreTargetIntent("synthetic-item-key", "msg-synthetic", "Restored/Synthetic")


def journal(tmp_path, account="synthetic-a"):
    return RestoreJournal(StateStore(tmp_path / "state.sqlite", account_key=account))


def begin(store, operation="first", target=TARGET, **kwargs):
    store.begin(operation, "synthetic-backup", CHAIN, (target,), **kwargs)


@pytest.mark.parametrize("outcome", [None, "uncertain", "completed"])
def test_fresh_store_retains_dispatch_uncertainty_and_confirmed_replay_fence(tmp_path, outcome):
    store = journal(tmp_path)
    begin(store)
    store.dispatch("first", 0)
    if outcome:
        store.finish("first", 0, outcome, result_reference="msg-restored" if outcome == "completed"
                     else None)
    fresh = journal(tmp_path)
    previous = fresh.latest(CHAIN, TARGET.item_key, TARGET.destination)
    assert previous.status == (outcome or "dispatching")
    begin(fresh, "replay", RestoreTargetIntent(
        TARGET.item_key, TARGET.source_reference, TARGET.destination, previous.token,
    ))
    with pytest.raises(ConflictError):
        fresh.dispatch("replay", 0)
    assert fresh.targets("replay")[0]["status"] == "not_started"


def test_rejected_attempt_does_not_block_safe_retry_but_keeps_its_history(tmp_path):
    store = journal(tmp_path)
    begin(store)
    store.dispatch("first", 0)
    store.finish("first", 0, "rejected", error_type="ConflictError")
    assert store.latest(CHAIN, TARGET.item_key, TARGET.destination) is None
    begin(store, "retry")
    store.dispatch("retry", 0)
    store.finish("retry", 0, "completed", result_reference="msg-accepted")
    assert store.targets("first")[0]["status"] == "rejected"
    assert store.targets("retry")[0]["result_reference"] == "msg-accepted"
    assert [row["operation_id"] for row in store.operations()] == ["retry", "first"]


def test_completed_duplicate_needs_explicit_policy_and_exact_latest_review(tmp_path, monkeypatch):
    monkeypatch.setattr("gucmail.services.restore_journal.iso", lambda: "fixed-clock")
    store = journal(tmp_path)
    begin(store, "z-first")
    store.dispatch("z-first", 0)
    store.finish("z-first", 0, "completed", result_reference="first-result")
    previous = store.latest(CHAIN, TARGET.item_key, TARGET.destination)
    target = RestoreTargetIntent(TARGET.item_key, TARGET.source_reference, TARGET.destination,
                                 previous.token)
    begin(store, "a-duplicate", target, allow_completed_duplicates=True)
    store.dispatch("a-duplicate", 0)
    store.finish("a-duplicate", 0, "completed", result_reference="second-result")
    latest = store.latest(CHAIN, TARGET.item_key, TARGET.destination)
    assert latest.result_reference == "second-result"
    begin(store, "stale", target, allow_completed_duplicates=True)
    with pytest.raises(ConflictError, match="changed after review"):
        store.dispatch("stale", 0)
    assert [row["operation_id"] for row in store.operations()] == ["stale","a-duplicate","z-first"]


@pytest.mark.parametrize("outcome", [None, "uncertain"])
def test_duplicate_policy_never_overrides_an_unresolved_dispatch(tmp_path, outcome):
    store = journal(tmp_path)
    begin(store)
    store.dispatch("first", 0)
    if outcome:
        store.finish("first", 0, outcome)
    previous = store.latest(CHAIN, TARGET.item_key, TARGET.destination)
    begin(store, "duplicate", RestoreTargetIntent(
        TARGET.item_key, TARGET.source_reference, TARGET.destination, previous.token,
    ), allow_completed_duplicates=True)
    with pytest.raises(ConflictError):
        store.dispatch("duplicate", 0)


def test_two_writers_cannot_claim_the_same_reviewed_target(tmp_path):
    store = journal(tmp_path)
    begin(store, "one")
    begin(store, "two")

    def dispatch(operation):
        try:
            journal(tmp_path).dispatch(operation, 0)
        except ConflictError:
            return "blocked"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(dispatch, ("one", "two"))) == ["blocked", "claimed"]


def test_account_scope_and_cache_clear_preserve_protected_claims(tmp_path):
    first = journal(tmp_path, "synthetic-a")
    begin(first)
    first.dispatch("first", 0)
    first.state.clear_disposable_cache()
    assert first.latest(CHAIN, TARGET.item_key, TARGET.destination).status == "dispatching"
    second = journal(tmp_path, "synthetic-b")
    assert second.latest(CHAIN, TARGET.item_key, TARGET.destination) is None
    begin(second)
    second.dispatch("first", 0)
    assert second.latest(CHAIN, TARGET.item_key, TARGET.destination).status == "dispatching"


def test_rejected_ack_write_keeps_durable_dispatch_marker(tmp_path):
    store = journal(tmp_path)
    begin(store)
    store.dispatch("first", 0)
    with store.state.transaction() as db:
        db.execute("CREATE TRIGGER synthetic_reject_ack BEFORE UPDATE ON restore_targets "
                   "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'owned ack fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.finish("first", 0, "completed", result_reference="msg-accepted")
    latest = journal(tmp_path).latest(CHAIN, TARGET.item_key, TARGET.destination)
    assert latest.status == "dispatching"


def test_missing_ledger_is_not_silently_recreated(tmp_path):
    store = journal(tmp_path)
    begin(store)
    with store.state.transaction() as db:
        db.execute("DROP TABLE restore_targets")
    with pytest.raises(sqlite3.OperationalError):
        journal(tmp_path).latest(CHAIN, TARGET.item_key, TARGET.destination)


def test_history_paging_and_cancelled_unstarted_targets(tmp_path):
    store = journal(tmp_path)
    store.begin("many", "synthetic-backup", CHAIN, (
        RestoreTargetIntent(f"item-{i}", f"msg-{i}", "Restored/Synthetic") for i in range(501)
    ))
    store.dispatch("many", 0)
    store.cancel_unstarted("many")
    assert len(store.targets("many", limit=200)) == 200
    assert len(store.targets("many", limit=200, offset=400)) == 101
    assert store.targets("many", limit=1)[0]["status"] == "dispatching"
    assert store.targets("many", limit=1, offset=500)[0]["status"] == "cancelled"
    with pytest.raises(LocalStateError):
        store.targets("many", limit=201)
