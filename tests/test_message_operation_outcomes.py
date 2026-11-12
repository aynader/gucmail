"""Batch errors must account for effects and retain recovery information."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import (
    ConfigurationError,
    ErrorContext,
    LocalStateError,
    MutationExecutionError,
    NetworkError,
)
from gucmail.services.messages import MessageService
from gucmail.state import StateStore
from test_cli import _environment
from test_mail_services import MessageFake


class CopyFault(MessageFake):
    def __init__(self, error: Exception, *, commit_first: bool = False):
        super().__init__()
        self.error = error
        self.commit_first = commit_first
        self.copied: list[str] = []

    def copy_message(self, reference: str, destination: str) -> str:
        if reference == "second":
            if self.commit_first:
                self.copied.append(reference)
            raise self.error
        self.copied.append(reference)
        return "new-" + reference


@pytest.mark.parametrize("lost_ack", [False, True])
def test_partial_copy_accounts_for_every_target_and_survives_restart(
    tmp_path: Path, lost_ack: bool
):
    error = (
        NetworkError(
            "Synthetic lost acknowledgment",
            context=ErrorContext(mutation_uncertain=True, retry_safe=False),
        )
        if lost_ack
        else ConfigurationError("Synthetic rejected write")
    )
    gateway = CopyFault(error, commit_first=lost_ack)
    path = tmp_path / "state.sqlite3"
    state = StateStore(path, account_key="synthetic")
    service = MessageService(gateway, state=state)
    plan = service.plan_copy(["first", "second", "third"], "Archive")
    with pytest.raises(MutationExecutionError) as caught:
        service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    failure = caught.value
    expected = ["completed", "uncertain" if lost_ack else "failed", "not_started"]
    assert [item["status"] for item in failure.outcomes] == expected
    assert failure.context.mutation_uncertain is lost_ack
    assert int(failure.exit_code) == 10
    assert gateway.copied == (["first", "second"] if lost_ack else ["first"])
    restarted = StateStore(path, account_key="synthetic")
    saved = restarted.get_message_operation(failure.operation_id)
    assert [item["status"] for item in saved["outcomes"]] == expected
    assert saved["outcomes"][0]["result_reference"] == "new-first"
    assert (
        StateStore(path, account_key="different-account").get_message_operation(
            failure.operation_id
        )
        is None
    )


def test_unclassified_transport_error_is_conservatively_uncertain(tmp_path: Path):
    service = MessageService(CopyFault(NetworkError("Synthetic connection lost")))
    with pytest.raises(MutationExecutionError) as caught:
        service.execute_mutation(service.plan_copy(["second"], "Archive"))
    assert caught.value.outcomes[0]["status"] == "uncertain"
    assert "No durable operation journal" in caught.value.context.suggestion


def test_journal_failure_after_ack_keeps_effect_and_does_not_send_next(tmp_path: Path, monkeypatch):
    state = StateStore(tmp_path / "state.sqlite3")
    original = state.record_message_target

    def fail_ack(operation_id, ordinal, status, **kwargs):
        if status == "completed":
            raise LocalStateError("Synthetic disk full after acknowledgment")
        return original(operation_id, ordinal, status, **kwargs)

    monkeypatch.setattr(state, "record_message_target", fail_ack)
    gateway = CopyFault(ConfigurationError("unused"))
    service = MessageService(gateway, state=state)
    plan = service.plan_copy(["first", "second"], "Archive")
    with pytest.raises(MutationExecutionError) as caught:
        service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    assert gateway.copied == ["first"]
    assert caught.value.outcomes[0]["status"] == "completed"
    assert "recovery journal" in caught.value.message
    saved = StateStore(state.path).get_message_operation(caught.value.operation_id)
    assert saved["outcomes"][0]["status"] == "uncertain"


def test_journal_failure_before_dispatch_and_dry_run_have_no_provider_effect(
    tmp_path: Path, monkeypatch
):
    state = StateStore(tmp_path / "state.sqlite3")
    gateway = MessageFake()
    service = MessageService(gateway, state=state)
    plan = service.plan_mark_read(["first", "second"])
    service.execute_mutation(plan, dry_run=True)
    assert state.list_message_operations() == []

    def fail_dispatch(*args, **kwargs):
        raise LocalStateError("Synthetic write lock")

    monkeypatch.setattr(state, "record_message_target", fail_dispatch)
    with pytest.raises(MutationExecutionError) as caught:
        service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    assert gateway.calls == []
    assert all(item["status"] == "not_started" for item in caught.value.outcomes)


def test_cli_operation_history_is_local_and_never_resolves_gateway(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Local operation history must not resolve a gateway")

    monkeypatch.setattr(cli, "_gateway", forbidden)
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["--json", "messages", "operations", "list"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0 and json.loads(result.output)["data"] == []
    missing = runner.invoke(
        cli.app, ["--json", "messages", "operations", "show", "missing"], env=_environment(tmp_path)
    )
    assert missing.exit_code == 6


def test_v12_migration_preserves_preferences_and_adds_recovery_tables(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    # Build a real v12 database with the production migration sequence rather
    # than dropping tables from the newest schema and leaving later markers.
    from gucmail.constants import SCHEMA_VERSION

    with monkeypatch.context() as legacy:
        legacy.setattr("gucmail.state.SCHEMA_VERSION", 12)
        state = StateStore(path)
        state.put_ui_preference("synthetic-preference", {"preserve": True})
        assert state.health()["schema_version"] == 12
    upgraded = StateStore(path)
    assert upgraded.get_ui_preference("synthetic-preference").value == {"preserve": True}
    assert upgraded.list_message_operations() == []
    assert upgraded.health()["schema_version"] == SCHEMA_VERSION
