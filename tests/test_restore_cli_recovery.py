"""Real CLI parsing/JSON/exit codes against persistent isolated restore records."""

import json

import pytest
from qa.synthetic.restore_target import OwnedRestoreTarget
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.bootstrap import build_context
from gucmail.services.backup import BackupItem, BackupService


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    for name in ("CONFIG", "DATA", "CACHE", "LOG"):
        monkeypatch.setenv(f"GUCMAIL_{name}_DIR", str(tmp_path / name))
    context = build_context()
    service = BackupService(context.paths.backup_dir, context.state)
    archive = service.create((
        BackupItem("msg-synthetic-cli", "message", {"subject": "Synthetic CLI restore"}),
    ), backup_id="synthetic-cli-recovery")
    target = OwnedRestoreTarget(tmp_path / "destination.sqlite", "accepted")
    calls = []

    def gateway(_ctx):
        calls.append(True)
        return target

    monkeypatch.setattr(cli.core, "gateway", gateway)
    return context, service, archive.backup_id, target, calls


def invoke(*args):
    return CliRunner().invoke(cli.app, ["--json", *args])


def test_cli_unknown_restore_plan_apply_and_status_never_initialize_a_provider(prepared):
    _context, service, backup_id, target, calls = prepared
    target.mode = "lost-ack"
    plan = service.plan_restore(backup_id)
    first = service.restore(backup_id, target=target, confirmed=True,
                            confirmation_token=plan.confirmation_token)
    assert target.count() == 1 and first.mutation_uncertain
    preview = invoke("backup", "restore-plan", backup_id, "--another-copy")
    assert preview.exit_code == 0, preview.output
    data = json.loads(preview.stdout)["data"]
    assert data["create_count"] == 0 and data["blocked_count"] == 1
    applied = invoke("--yes", "backup", "restore", backup_id, "--apply", "--another-copy")
    assert applied.exit_code == 10, applied.output
    assert not calls and target.count() == 1
    status = invoke("backup", "restore-status", first.operation_id)
    assert status.exit_code == 0, status.output
    data = json.loads(status.stdout)["data"]
    assert data["targets"][0]["status"] == "uncertain" and data["total_targets"] == 1
    assert not calls and target.count() == 1


def test_cli_completed_default_noop_and_explicit_extra_copy_use_same_service_policy(prepared):
    _context, service, backup_id, target, calls = prepared
    plan = service.plan_restore(backup_id)
    service.restore(backup_id, target=target, confirmed=True,
                    confirmation_token=plan.confirmation_token)
    noop = invoke("--yes", "backup", "restore", backup_id, "--apply")
    assert noop.exit_code == 0, noop.output
    assert not calls and target.count() == 1
    extra = invoke("--yes", "backup", "restore", backup_id, "--apply", "--another-copy")
    assert extra.exit_code == 0, extra.output
    data = json.loads(extra.stdout)["data"]
    assert data["restored_count"] == 1 and data["journal_available"]
    assert data["operation_id"] and target.count() == 2 and len(calls) == 1
    history = invoke("backup", "restore-status", "--limit", "1")
    assert history.exit_code == 0, history.output
    assert len(json.loads(history.stdout)["data"]) == 1


def test_cli_acknowledged_effect_and_failed_recovery_update_is_nonzero_partial(prepared):
    context, _service, backup_id, target, calls = prepared
    context.state.initialize()
    with context.state.transaction() as db:
        db.execute("CREATE TRIGGER synthetic_cli_ack_failure BEFORE UPDATE ON restore_targets "
                   "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'owned ack fault'); END")
    result = invoke("--yes", "backup", "restore", backup_id, "--apply")
    assert result.exit_code == 10, result.output
    data = json.loads(result.stdout)["data"]
    assert data["restored_count"] == 1 and data["failed_count"] == 0
    assert data["recovery_write_failed"] and target.count() == 1 and len(calls) == 1
    replay = invoke("--yes", "backup", "restore", backup_id, "--apply")
    assert replay.exit_code == 10 and target.count() == 1 and len(calls) == 1


@pytest.mark.parametrize("args", [("--limit", "201"), ("--offset", "-1")])
def test_cli_status_rejects_unbounded_or_negative_pages_without_provider(prepared, args):
    _context, _service, _backup_id, _target, calls = prepared
    result = invoke("backup", "restore-status", *args)
    assert result.exit_code == 2
    assert not calls
