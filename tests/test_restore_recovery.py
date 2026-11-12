"""Real archive/SQL restore effects, restart policy and failed ledger persistence."""

import sqlite3
from pathlib import Path
from threading import Event

import pytest
from qa.synthetic.restore_target import OwnedRestoreTarget

from gucmail.errors import ConfirmationRequiredError, LocalStateError, NetworkError
from gucmail.services.backup import BackupItem, BackupService
from gucmail.services.restore_journal import RestoreJournal
from gucmail.state import StateStore


class Target(OwnedRestoreTarget):
    def __init__(self, path, mode="accepted"):
        super().__init__(path, mode)
        self.calls = []

    def restore_item(self, item, destination_folder):
        self.calls.append(item.reference)
        return super().restore_item(item, destination_folder)


def prepared(tmp_path, count=1):
    state = StateStore(tmp_path / "state.sqlite", account_key="synthetic-restore")
    service = BackupService(tmp_path / "backups", state)
    backup = service.create((
        BackupItem(f"msg-{i}", "message", {"subject": f"Synthetic record {i}"},
                   folder_path="Synthetic/Inbox") for i in range(count)
    ), backup_id="synthetic-recovery")
    return state, service, backup.backup_id


def execute(service, backup_id, target, **kwargs):
    plan = service.plan_restore(backup_id, **kwargs)
    result = service.restore(backup_id, target=target, confirmed=True,
                             confirmation_token=plan.confirmation_token, **kwargs)
    return plan, result


def test_isolated_source_is_the_source_under_test():
    import gucmail.services.backup as module

    assert Path(module.__file__).resolve().is_relative_to(Path(__file__).parents[1] / "src")


def test_lost_reply_survives_restart_and_cannot_be_repeated_by_new_plan(tmp_path):
    state, service, backup_id = prepared(tmp_path)
    target = Target(tmp_path / "destination.sqlite", "lost-ack")
    original_plan, first = execute(service, backup_id, target)
    assert first.mutation_uncertain and first.restored_count == 0 and target.count() == 1
    assert first.journal_available and first.operation_id
    assert RestoreJournal(state).targets(first.operation_id)[0]["status"] == "uncertain"
    fresh = BackupService(service.root, StateStore(state.path, account_key=state.account_key))
    plan = fresh.plan_restore(backup_id)
    assert plan.create_count == 0 and plan.blocked_count == 1
    assert plan.entries[0].action == "review_required"
    assert first.operation_id in " ".join(plan.entries[0].warnings)
    with pytest.raises(ConfirmationRequiredError):
        fresh.restore(backup_id, target=target, confirmed=True,
                      confirmation_token=original_plan.confirmation_token)
    _, second = execute(fresh, backup_id, target)
    assert second.attempted_count == 0 and second.blocked_count == 1
    assert target.count() == 1 and len(target.calls) == 1
    override = fresh.plan_restore(backup_id, allow_completed_duplicates=True)
    assert override.create_count == 0 and override.blocked_count == 1


def test_definite_rejection_can_be_explicitly_retried_without_losing_old_history(tmp_path):
    state, service, backup_id = prepared(tmp_path)
    target = Target(tmp_path / "destination.sqlite", "rejected")
    _, first = execute(service, backup_id, target)
    assert not first.mutation_uncertain and target.count() == 0
    assert RestoreJournal(state).targets(first.operation_id)[0]["status"] == "rejected"
    target.mode = "accepted"
    _, second = execute(service, backup_id, target)
    assert second.restored_count == 1 and target.count() == 1
    assert first.operation_id != second.operation_id
    assert len(RestoreJournal(state).operations()) == 2


def test_default_skips_completed_but_intentional_copy_requires_new_exact_policy_review(tmp_path):
    state, service, backup_id = prepared(tmp_path)
    target = Target(tmp_path / "destination.sqlite")
    old_plan, first = execute(service, backup_id, target)
    normal = service.plan_restore(backup_id)
    assert normal.create_count == 0 and normal.duplicate_count == 1 and normal.blocked_count == 0
    assert first.operation_id in normal.entries[0].duplicate_reason
    with pytest.raises(ConfirmationRequiredError):
        service.restore(backup_id, target=target, confirmed=True,
                        confirmation_token=old_plan.confirmation_token,
                        allow_completed_duplicates=True)
    reviewed, second = execute(service, backup_id, target, allow_completed_duplicates=True)
    assert reviewed.allow_completed_duplicates and second.restored_count == 1
    assert target.count() == 2 and len(target.calls) == 2
    assert RestoreJournal(state).targets(second.operation_id)[0]["status"] == "completed"


def test_intent_failure_prevents_all_provider_calls(tmp_path):
    state, service, backup_id = prepared(tmp_path)
    target = Target(tmp_path / "destination.sqlite")
    with state.transaction() as db:
        db.execute("CREATE TRIGGER synthetic_reject_intent BEFORE INSERT ON restore_operations "
                   "BEGIN SELECT RAISE(ABORT,'owned intent fault'); END")
    with pytest.raises(LocalStateError, match="did not start"):
        execute(service, backup_id, target)
    assert not target.calls and target.count() == 0
    assert not RestoreJournal(state).operations()


def test_accepted_effect_with_failed_ack_write_is_completed_but_protected_and_stops_batch(tmp_path):
    state, service, backup_id = prepared(tmp_path, 3)
    target = Target(tmp_path / "destination.sqlite")
    with state.transaction() as db:
        db.execute("CREATE TRIGGER synthetic_reject_ack BEFORE UPDATE ON restore_targets "
                   "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'owned ack fault'); END")
    _, result = execute(service, backup_id, target)
    assert result.restored_count == 1 and result.failed_count == 0
    assert result.recovery_write_failed and not result.mutation_uncertain
    assert result.results[0].recovery_write_failed and result.stopped_early
    assert target.count() == len(target.calls) == 1
    rows = RestoreJournal(state).targets(result.operation_id)
    assert [row["status"] for row in rows] == ["dispatching", "not_started", "not_started"]
    fresh = service.plan_restore(backup_id)
    assert fresh.blocked_count == 1 and fresh.create_count == 2


def test_partial_restore_replans_only_unstarted_targets_and_never_replays_uncertainty(tmp_path):
    _state, service, backup_id = prepared(tmp_path, 3)

    class PartialTarget(Target):
        def restore_item(self, item, destination_folder):
            self.mode = "lost-ack" if len(self.calls) == 1 else "accepted"
            return super().restore_item(item, destination_folder)

    target = PartialTarget(tmp_path / "destination.sqlite")
    _, first = execute(service, backup_id, target)
    assert first.restored_count == 1 and first.failed_count == 1 and first.mutation_uncertain
    assert target.count() == 2 and len(target.calls) == 2
    fresh_plan = service.plan_restore(backup_id)
    counts = fresh_plan.create_count, fresh_plan.blocked_count, fresh_plan.duplicate_count
    assert counts == (1, 1, 1)
    _, last = execute(service, backup_id, target)
    assert last.restored_count == 1 and last.blocked_count == 1
    assert target.count() == len(target.calls) == 3
    assert len(set(target.calls)) == 3
    assert service.verify(backup_id).valid


def test_default_network_error_after_provider_entry_is_uncertain_not_safe_to_retry(tmp_path):
    state, service, backup_id = prepared(tmp_path)

    class UnknownTarget(Target):
        def restore_item(self, item, destination_folder):
            raise NetworkError("Synthetic unknown transport outcome")

    target = UnknownTarget(tmp_path / "destination.sqlite")
    _, result = execute(service, backup_id, target)
    assert result.mutation_uncertain
    assert RestoreJournal(state).targets(result.operation_id)[0]["status"] == "uncertain"


def test_missing_ledger_fails_before_any_provider_call(tmp_path):
    state, service, backup_id = prepared(tmp_path)
    target = Target(tmp_path / "destination.sqlite")
    state.initialize()
    with state.transaction() as db:
        db.execute("DROP TABLE restore_targets")
    with pytest.raises(sqlite3.OperationalError):
        execute(service, backup_id, target)
    assert target.count() == 0 and not target.calls


def test_cancel_during_claim_stops_before_provider_and_persists_cancelled_targets(
    tmp_path, monkeypatch
):
    state, service, backup_id = prepared(tmp_path, 3)
    target = Target(tmp_path / "destination.sqlite")
    cancel = Event()
    original = RestoreJournal.dispatch

    def claim_then_cancel(self, operation_id, ordinal):
        original(self, operation_id, ordinal)
        cancel.set()

    monkeypatch.setattr(RestoreJournal, "dispatch", claim_then_cancel)
    plan = service.plan_restore(backup_id)
    result = service.restore(
        backup_id, target=target, confirmed=True, confirmation_token=plan.confirmation_token,
        cancelled=cancel.is_set,
    )
    assert result.cancelled and not result.server_mutations_performed
    assert result.attempted_count == 0 and target.count() == 0 and not target.calls
    assert all(row["status"] == "cancelled"
               for row in RestoreJournal(state).targets(result.operation_id))
