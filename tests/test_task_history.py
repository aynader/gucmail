"""Local observed Task links with owned state and synthetic native responses."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from exchangelib import Account, Task
from typer.testing import CliRunner

from gucmail import cli
from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import (
    ConfigurationError,
    GUCmailError,
    LocalStateError,
    MutationResultUnavailableError,
)
from gucmail.models import TaskInfo, to_primitive
from gucmail.services.task_history import TaskHistory, history_text
from gucmail.services.tasks import TaskPatch, TaskService
from gucmail.state import StateStore
from test_acceptance_ews_regressions import projected_gateway as projected_gateway


@pytest.fixture(params=[False, True], ids=["session", "sqlite"])
def history(request, tmp_path):
    state = StateStore(tmp_path / "history.sqlite3") if request.param else None
    return TaskHistory(state)


def test_observed_family_links_follow_actual_births_and_reopen_without_guessing(history):
    first = history.accepted(
        "task_series", "task_instance_one", action="complete", distinct_instance=True
    )
    history.finish(first.operation_id, verified=True)
    second = history.accepted(
        "task_series", "task_instance_two", action="complete", distinct_instance=True
    )
    history.finish(second.operation_id, verified=False)
    reopened = history.accepted(
        "task_instance_one", "task_instance_one", action="reopen", distinct_instance=False
    )
    history.finish(reopened.operation_id, verified=None)
    for reference in ("task_series", "task_instance_one", "task_instance_two"):
        page = history.page(reference)
        assert page.family_reference == "task_series"
        assert [r.operation_id for r in page.observations] == [
            reopened.operation_id,
            second.operation_id,
            first.operation_id,
        ]
        assert [r.outcome for r in page.observations] == [
            "readback_not_requested",
            "readback_unavailable",
            "readback_verified",
        ]
        assert [r.distinct_instance for r in page.observations] == [False, True, True]
        assert "unseen server history is unavailable" in history_text(page)
        assert page.next_before is None
    assert history.page("task_same_subject_but_unrelated").observations == ()


def test_keyset_pages_remain_ordered_when_a_new_update_arrives(history):
    for _ in range(5):
        history.accepted("task_one", "task_one", action="edit", distinct_instance=False)
    first = history.page("task_one", limit=2)
    history.accepted("task_one", "task_one", action="edit", distinct_instance=False)
    second = history.page("task_one", limit=2, before=first.next_before)
    third = history.page("task_one", limit=2, before=second.next_before)
    assert [r.sequence for p in (first, second, third) for r in p.observations] == [5, 4, 3, 2, 1]
    assert third.next_before is None
    assert history.page("task_one", limit=1).observations[0].sequence == 6


def test_opaque_operation_id_is_idempotent_and_conflicting_reuse_is_atomic(history):
    identifier = uuid4().hex
    first = history.accepted(
        "task_original",
        "task_result",
        action="complete",
        distinct_instance=True,
        operation_id=identifier,
    )
    again = history.accepted(
        "task_original",
        "task_result",
        action="complete",
        distinct_instance=True,
        operation_id=identifier,
    )
    assert again == first
    with pytest.raises(GUCmailError, match="reused"):
        history.accepted(
            "task_original",
            "task_result",
            action="edit",
            distinct_instance=True,
            operation_id=identifier,
        )
    assert history.page("task_result").observations == (first,)
    history.finish(identifier, verified=True)
    with pytest.raises(GUCmailError, match="outcome"):
        history.finish(identifier, verified=False)
    assert history.page("task_original").observations[0].outcome == "readback_verified"


@pytest.mark.parametrize(
    "reference", ["", "ews:synthetic-remote", "bad\x1b[2J", "two\nlines", "tab\tref", "x" * 1025]
)
def test_invalid_history_reference_rejected_before_recording(history, reference):
    with pytest.raises(ConfigurationError):
        history.accepted(reference, "task_result", action="edit", distinct_instance=True)
    assert history.page("task_result").observations == ()


@pytest.mark.parametrize(
    "limit,before", [(0, None), (True, None), (201, None), (1, 0), (1, True), (1, -1)]
)
def test_history_query_bounds_are_strict(history, limit, before):
    with pytest.raises(ConfigurationError):
        history.page("task_one", limit=limit, before=before)


def test_retention_is_bounded_without_inferring_lost_past(tmp_path):
    for persistent in (False, True):
        current = TaskHistory(
            StateStore(tmp_path / "bounded.db") if persistent else None, retained_limit=2
        )
        for index in range(4):
            current.accepted(
                "task_series", f"task_result_{index}", action="complete", distinct_instance=True
            )
        page = current.page("task_series")
        assert [r.result_reference for r in page.observations] == ["task_result_3", "task_result_2"]
        assert current.page("task_result_0").observations == ()
        assert page.retained_limit == 2 and page.next_before is None
        if persistent:
            with current.state.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM task_observations").fetchone()[0] == 2
                )
                assert (
                    connection.execute("SELECT COUNT(*) FROM task_history_members").fetchone()[0]
                    == 3
                )


def test_schema10_upgrade_preserves_realistic_existing_records_and_links_survive_cache_clear(
    tmp_path, monkeypatch
):
    import gucmail.state as state_module

    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(state_module, "SCHEMA_VERSION", 10)
    old = StateStore(path)
    old.put_reference("task_series", "task", "synthetic-remote-series", change_key="old")
    old.put_reference("task_instance", "task", "synthetic-remote-instance", change_key="fresh")
    old.put_reference("task_alias", "task", "synthetic-remote-series")
    old.put_reference("task_unobserved", "task", "synthetic-unobserved")
    old.put_reference("msg_disposable", "message", "synthetic-message")
    old.save_search("Existing", {"subject": "synthetic"})
    assert old.health()["schema_version"] == 10
    monkeypatch.setattr(state_module, "SCHEMA_VERSION", SCHEMA_VERSION)
    current = StateStore(path)
    history = TaskHistory(current)
    recorded = history.accepted(
        "task_series", "task_instance", action="complete", distinct_instance=True
    )
    history.finish(recorded.operation_id, verified=True)
    assert current.health()["schema_version"] == SCHEMA_VERSION
    preview = current.disposable_cache_counts()
    cleared = current.clear_disposable_cache()
    assert cleared["item_references"] == preview["item_references"] == 2
    assert current.resolve_reference("task_unobserved") is None
    assert current.resolve_reference("msg_disposable") is None
    reopened = StateStore(path)
    assert reopened.resolve_reference("task_series", "task")["ews_id"] == "synthetic-remote-series"
    assert reopened.resolve_reference("task_alias", "task")["canonical_ref"] == "task_series"
    assert (
        reopened.resolve_reference("task_instance", "task")["ews_id"] == "synthetic-remote-instance"
    )
    assert reopened.get_search("Existing")
    page = TaskHistory(reopened).page("task_alias")
    assert (
        page.family_reference == "task_series"
        and page.observations[0].operation_id == recorded.operation_id
    )
    serialized = json.dumps(to_primitive(page))
    assert "synthetic-remote" not in serialized and "change_key" not in serialized


def test_account_isolation_concurrent_writers_and_conflicting_family_links(tmp_path):
    path = tmp_path / "concurrent.db"
    one = TaskHistory(StateStore(path, account_key="ACCOUNT-ONE"))
    two = TaskHistory(StateStore(path, account_key="account-two"))
    one.accepted("task_series", "task_result", action="complete", distinct_instance=True)
    assert two.page("task_result").observations == ()
    two.accepted("task_other", "task_result", action="edit", distinct_instance=True)
    assert two.page("task_result").family_reference == "task_other"
    with pytest.raises(GUCmailError, match="another observed family"):
        one.accepted("task_unrelated", "task_result", action="edit", distinct_instance=True)

    def writer(_):
        current = TaskHistory(StateStore(path, account_key="account-one"))
        return current.accepted(
            "task_series", "task_series", action="edit", distinct_instance=False
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        observations = list(pool.map(writer, range(12)))
    page = one.page("task_series")
    assert len(page.observations) == 13
    assert (
        len({r.operation_id for r in observations}) == len({r.sequence for r in observations}) == 12
    )
    assert [r.sequence for r in page.observations] == sorted(
        (r.sequence for r in page.observations), reverse=True
    )
    assert len(two.page("task_result").observations) == 1


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "readback",
        "record",
        "finish",
        "invalid_result",
        "missing_id",
        "wrong_type",
        "wrong_id",
        "multiple",
        "history",
    ],
)
def test_native_accepted_completion_records_identity_and_uncertainty_without_replay(
    projected_gateway, monkeypatch, persistent, failure
):
    gateway = projected_gateway
    if not persistent:
        gateway.state = None
    item = Task(
        account=gateway.account,
        id="synthetic-series",
        changekey="before",
        subject="Synthetic valid Task",
        status="NotStarted",
        percent_complete=Decimal(0),
    )
    reference = gateway._remember("task", item)
    writes, reads = [], []

    def save(value, *, update_fields):
        writes.append((value.id, tuple(update_fields)))
        value.id = None if failure == "missing_id" else "synthetic-completed"
        value.changekey = "accepted"

    def fetch(account, *, ids, only_fields):
        reads.append(ids[0][0])
        if failure == "readback":
            raise TimeoutError("Synthetic post-update readback unavailable")
        if failure == "wrong_type":
            from exchangelib import CalendarItem

            return [CalendarItem(account=account, id="synthetic-completed", changekey="wrong-type")]
        values = [
            Task(
                account=account,
                id="synthetic-unrequested" if failure == "wrong_id" else "synthetic-completed",
                changekey="fresh",
                subject="" if failure == "invalid_result" else "Synthetic valid Task",
                status="Completed",
                percent_complete=Decimal(100),
                complete_date=datetime(2030, 1, 1, tzinfo=UTC),
            )
        ]
        return values * 2 if failure == "multiple" else values

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    history = gateway._local_task_history()
    history_reader = gateway._local_task_history
    if failure == "history":
        monkeypatch.setattr(
            gateway,
            "_local_task_history",
            lambda: (_ for _ in ()).throw(RuntimeError("Synthetic local initialization failure")),
        )
    if failure == "record":
        monkeypatch.setattr(
            history,
            "accepted",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Synthetic local record failure")),
        )
    if failure == "finish":
        monkeypatch.setattr(
            history,
            "finish",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Synthetic local outcome failure")),
        )
    if failure:
        with pytest.raises(MutationResultUnavailableError) as caught:
            TaskService(gateway).complete(reference)
        assert caught.value.as_dict()["retry_safe"] is False
        assert caught.value.as_dict()["mutation_uncertain"] is True
    else:
        result = TaskService(gateway).complete(reference)
        assert result.reference != reference
    if failure == "history":
        monkeypatch.setattr(gateway, "_local_task_history", history_reader)
    page = gateway.get_task_history(reference)
    assert writes == [("synthetic-series", ("status", "percent_complete"))]
    assert reads == (
        [] if failure in {"record", "missing_id", "history"} else ["synthetic-completed"]
    )
    assert len(page.observations) == (0 if failure in {"record", "missing_id", "history"} else 1)
    if page.observations:
        entry = page.observations[0]
        assert entry.original_reference == reference and entry.result_reference != reference
        assert entry.action == "complete" and entry.distinct_instance
        assert (
            entry.outcome
            == {
                None: "readback_verified",
                "readback": "readback_unavailable",
                "invalid_result": "readback_unavailable",
                "wrong_type": "readback_unavailable",
                "wrong_id": "readback_unavailable",
                "multiple": "readback_unavailable",
                "finish": "accepted",
            }[failure]
        )
        assert "synthetic-series" not in json.dumps(to_primitive(page))
        assert gateway._resolve_item(reference, "task")[1].id == "synthetic-series"
    assert len(writes) == 1 and len(reads) <= 1


def test_task_history_canonicalizes_aliases_and_preserves_their_identity_on_cache_clear(tmp_path):
    state = StateStore(tmp_path / "aliases.db")
    state.put_reference("task_canonical", "task", "synthetic-remote-task")
    state.put_reference("task_alias", "task", "synthetic-remote-task")
    history = TaskHistory(state)
    entry = history.accepted("task_alias", "task_canonical", action="edit", distinct_instance=False)
    assert (
        entry.original_reference
        == entry.result_reference
        == entry.family_reference
        == "task_canonical"
    )
    state.clear_disposable_cache()
    assert state.resolve_reference("task_alias")["canonical_ref"] == "task_canonical"
    assert history.page("task_alias").observations == (entry,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "invalid-operation-id"),
        ("observed_at", "invalid-time"),
        ("original_reference", "ews:synthetic-remote"),
        ("distinct_instance", 1),
    ],
)
def test_corrupt_observed_metadata_fails_locally_and_preserves_existing_state(
    tmp_path, field, value
):
    state = StateStore(tmp_path / "corrupt-observation.db")
    history = TaskHistory(state)
    recorded = history.accepted("task_one", "task_one", action="edit", distinct_instance=False)
    with state.transaction(immediate=True) as connection:
        connection.execute(
            f"UPDATE task_observations SET {field}=? WHERE sequence=?",  # noqa: S608 - test allowlist
            (value, recorded.sequence),
        )
    with pytest.raises(LocalStateError, match="existing state was preserved"):
        history.page("task_one")
    with state.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_observations").fetchone()[0] == 1


def test_failed_schema11_migration_rolls_back_all_changes_and_can_retry_without_reset(
    tmp_path, monkeypatch
):
    import gucmail.state as state_module

    path = tmp_path / "migration.db"
    with monkeypatch.context() as old_version:
        old_version.setattr(state_module, "SCHEMA_VERSION", 9)
        old = StateStore(path)
        old.put_reference("task_existing", "task", "synthetic-retained")
        old.save_search("Keep", {"subject": "synthetic"})
    with monkeypatch.context() as broken:
        broken.setitem(
            state_module.MIGRATIONS, 11, "CREATE TABLE partial_history(value TEXT);\nINVALID SQL;\n"
        )
        with pytest.raises(GUCmailError):
            StateStore(path).initialize()
    with old.connection() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 9
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {"partial_history", "task_observations", "contact_history"}.intersection(tables)
    recovered = StateStore(path)
    recovered.initialize()
    assert recovered.health()["schema_version"] == SCHEMA_VERSION
    assert recovered.resolve_reference("task_existing")["ews_id"] == "synthetic-retained"
    assert recovered.get_search("Keep")
    assert TaskHistory(recovered).page("task_existing").observations == ()


@pytest.mark.parametrize("action", ["create", "update", "complete", "reopen"])
def test_accepted_but_invalid_task_result_is_uncertain_for_every_mutation(action):
    class Accepted:
        def __init__(self):
            self.writes = []

        def create_task(self, plan):
            self.writes.append(plan)
            return TaskInfo("task_result", "", "not_started")

        def update_task(self, plan):
            self.writes.append(plan)
            return TaskInfo("task_result", "", "not_started")

    provider = Accepted()
    service = TaskService(provider)
    current = TaskInfo("task_original", "Valid before operation", "not_started")
    with pytest.raises(MutationResultUnavailableError) as caught:
        if action == "create":
            service.create(current)
        elif action == "update":
            service.update(
                current.reference, TaskPatch(subject="New valid subject"), current=current
            )
        else:
            getattr(service, action)(current.reference)
    assert len(provider.writes) == 1
    assert caught.value.as_dict()["retry_safe"] is False
    assert caught.value.as_dict()["mutation_uncertain"] is True


def test_cli_history_reads_owned_state_without_credentials_or_gateway(tmp_path, monkeypatch):
    from gucmail.credentials import CredentialResolver
    from test_task_regeneration import _context

    context = _context(tmp_path)
    history = TaskHistory(context.state)
    recorded = history.accepted(
        "task_series", "task_result", action="complete", distinct_instance=True
    )
    history.finish(recorded.operation_id, verified=True)
    for name in ("CONFIG", "DATA", "CACHE", "LOG"):
        monkeypatch.setenv(
            "GUCMAIL_" + name + "_DIR", str(getattr(context.paths, name.lower() + "_dir"))
        )
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Local history resolved Exchange")
    )
    monkeypatch.setattr(
        CredentialResolver,
        "resolve",
        lambda *_a, **_k: pytest.fail("Local history resolved credentials"),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--config",
            str(context.paths.config_file),
            "--json",
            "--non-interactive",
            "tasks",
            "history",
            "task_result",
            "--limit",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    page = json.loads(result.output)["data"]
    assert page["family_reference"] == "task_series"
    assert page["observations"][0]["operation_id"] == recorded.operation_id
    assert page["persistence"] == "persistent local state"
    assert page["next_before"] is None
    assert "body" not in page and "ews_id" not in page
