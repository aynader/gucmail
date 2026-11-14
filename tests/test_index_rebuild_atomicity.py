import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError, ConflictError, LocalStateError
from gucmail.models import MessageSummary
from gucmail.services.index import IndexDocument, IndexRebuildCancelled, SearchIndex
from gucmail.tui.diagnostics_screen import LocalDiagnosticsBackend
from test_diagnostics_tui_secondpass import MetadataGateway, _context


def test_same_count_and_same_clock_timestamp_cannot_hide_a_concurrent_write(tmp_path, monkeypatch):
    import gucmail.services.index as module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    index, before = retained_index(tmp_path)
    SearchIndex(index.path).upsert(IndexDocument("retained", subject="Same-clock edit"))
    after = index.revision()
    assert after.documents == before.documents and after.updated_at == before.updated_at
    assert after.revision_id != before.revision_id
    with pytest.raises(ConflictError, match="changed"):
        index.replace_many([IndexDocument("new", subject="Replacement")], expected_revision=before)
    assert index.record("retained").subject == "Same-clock edit" and index.verify().valid


def test_clear_of_an_empty_index_invalidates_an_already_reviewed_replacement(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite3")
    before = index.revision()
    assert index.clear() == 0
    after = index.revision()
    assert after.documents == before.documents == 0 and after.updated_at == before.updated_at
    assert after.revision_id != before.revision_id
    with pytest.raises(ConflictError, match="changed"):
        index.replace_many([IndexDocument("new", subject="Replacement")], expected_revision=before)
    assert index.stats().documents == 0 and index.verify().valid


def test_cli_rebuild_read_failure_preserves_the_previous_usable_index(tmp_path, monkeypatch):
    env = {
        f"GUCMAIL_{kind.upper()}_DIR": str(tmp_path / kind)
        for kind in ("config", "data", "cache", "log")
    }
    index = SearchIndex(Path(env["GUCMAIL_DATA_DIR"]) / "index.sqlite3")
    index.upsert(IndexDocument("retained", subject="Existing preserved reference"))
    original = index.stats()
    summaries = tuple(MessageSummary(f"new-{i}", "Replacement", None, None) for i in range(2))

    class Gateway:
        def list_messages(self, **kwargs):
            return summaries

        def show_message(self, reference):
            if reference == "new-1":
                raise OSError("Synthetic interruption while loading the second body")
            return summaries[0]

    monkeypatch.setattr(cli.core, "gateway", lambda ctx: Gateway())
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "--yes", "index", "rebuild", "--include-bodies"],
        env=env,
    )
    assert result.exit_code != 0
    assert index.stats().documents == original.documents
    assert index.record("retained").subject == "Existing preserved reference"
    assert index.stats().updated_at == original.updated_at
    assert index.verify().valid


def retained_index(tmp_path):
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(IndexDocument("retained", subject="Preserved reference"))
    return index, index.revision()


def assert_retained(index, revision):
    assert index.revision() == revision
    assert index.record("retained").subject == "Preserved reference"
    assert index.verify().valid
    assert [hit.reference for hit in index.search("Preserved")] == ["retained"]


def test_atomic_replacement_commits_documents_fts_and_revision_together(tmp_path):
    index, before = retained_index(tmp_path)
    result = index.replace_many(
        [
            IndexDocument("first", subject="New uniquely searchable phrase"),
            IndexDocument("second", subject="Second replacement"),
        ],
        expected_revision=before,
    )
    assert (result.removed, result.indexed) == (1, 2)
    assert index.revision() != before
    assert index.verify().valid and index.stats().documents == 2
    assert not index.search("Preserved")
    assert [hit.reference for hit in index.search("uniquely")] == ["first"]


@pytest.mark.parametrize(
    "failure", ["read", "validation", "duplicate", "document_limit", "byte_limit"]
)
def test_preparation_failure_preserves_the_prior_index(tmp_path, failure):
    index, before = retained_index(tmp_path)

    def documents():
        yield IndexDocument("first", subject="New replacement")
        if failure == "read":
            raise OSError("Synthetic later read failure")
        yield IndexDocument(
            "" if failure == "validation" else "first" if failure == "duplicate" else "second",
            subject="x" * 1000 if failure == "byte_limit" else "Replacement data",
        )

    options = (
        {"limit": 1}
        if failure == "document_limit"
        else {"max_prepared_bytes": 400}
        if failure == "byte_limit"
        else {}
    )
    with pytest.raises((OSError, ConfigurationError)):
        index.replace_many(documents(), expected_revision=before, **options)
    assert_retained(index, before)


def test_sql_failure_after_first_insert_rolls_back_all_old_and_new_data(tmp_path):
    index, before = retained_index(tmp_path)
    with sqlite3.connect(index.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_second BEFORE INSERT ON documents "
            "WHEN NEW.reference='second' BEGIN "
            "SELECT RAISE(ABORT,'Synthetic insertion failure'); END"
        )
    with pytest.raises(LocalStateError, match="rolled back"):
        index.replace_many(
            [IndexDocument("first", subject="New"), IndexDocument("second", subject="New")],
            expected_revision=before,
        )
    assert_retained(index, before)
    assert not index.search("New")


@pytest.mark.parametrize("phase", ["before", "prepare", "write"])
def test_cancellation_retains_old_index_without_partial_fts_or_metadata(
    tmp_path, monkeypatch, phase
):
    index, before = retained_index(tmp_path)
    cancel = Event()
    if phase == "before":
        cancel.set()

    def values():
        yield IndexDocument("first", subject="New")
        if phase == "prepare":
            cancel.set()
        yield IndexDocument("second", subject="New")

    original = index._write_row

    def write(connection, row):
        original(connection, row)
        if phase == "write":
            cancel.set()

    monkeypatch.setattr(index, "_write_row", write)
    with pytest.raises(IndexRebuildCancelled) as error:
        index.replace_many(values(), expected_revision=before, cancel_event=cancel)
    assert error.value.exit_code == 130
    assert_retained(index, before)


@pytest.mark.parametrize("same_count", [False, True])
def test_another_index_handle_change_during_preparation_is_preserved(tmp_path, same_count):
    index, before = retained_index(tmp_path)
    other = SearchIndex(index.path)

    def values():
        yield IndexDocument("first", subject="Prepared replacement")
        other.upsert(
            IndexDocument("retained" if same_count else "another", subject="Concurrent update")
        )
        yield IndexDocument("second", subject="Prepared replacement")

    with pytest.raises(ConflictError, match="changed"):
        index.replace_many(values(), expected_revision=before)
    assert index.record("retained" if same_count else "another").subject == "Concurrent update"
    assert index.stats().documents == (1 if same_count else 2)
    assert not index.search("Prepared") and index.verify().valid


@pytest.mark.parametrize("limit", [True, 0, 501, 1.5, "2"])
def test_invalid_document_bounds_fail_before_reading_or_clearing(tmp_path, limit):
    index, before = retained_index(tmp_path)

    def forbidden():
        pytest.fail("Invalid limit consumed input")
        yield

    with pytest.raises(ConfigurationError):
        index.replace_many(forbidden(), limit=limit)
    assert_retained(index, before)


@pytest.mark.parametrize("maximum", [True, 0, 32 * 1024 * 1024 + 1, 1.5, "2"])
def test_invalid_byte_bounds_preserve_data(tmp_path, maximum):
    index, before = retained_index(tmp_path)
    with pytest.raises(ConfigurationError):
        index.replace_many([], max_prepared_bytes=maximum)
    assert_retained(index, before)


def test_explicit_empty_replacement_clears_atomically_and_changes_revision(tmp_path):
    index, before = retained_index(tmp_path)
    result = index.replace_many([], expected_revision=before)
    assert (result.removed, result.indexed) == (1, 0)
    assert index.revision().documents == 0 and index.revision().updated_at is not None
    assert index.revision() != before and index.verify().valid


def test_an_owned_second_process_update_cannot_be_erased_by_a_stale_rebuild(tmp_path):
    index, before = retained_index(tmp_path)
    source = Path(sys.modules[SearchIndex.__module__].__file__).parents[2]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in {"systemroot", "windir", "temp", "tmp", "path"}
    }
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from gucmail.services.index import SearchIndex,IndexDocument; "
        "SearchIndex(Path(sys.argv[2])).upsert(IndexDocument('retained',subject='Child update'))"
    )
    isolated_flags = ["-I"]
    if os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1":
        # This owned child still performs a real independent database write.
        # Preserve fail-closed sitecustomize rather than stripping its bootstrap
        # with -I. Ordinary CI retains interpreter isolation below.
        environment.update(
            (key, value)
            for key, value in os.environ.items()
            if key.startswith("GUCMAIL_")
            or key
            in {
                "PYTHONPATH",
                "USERPROFILE",
                "USERNAME",
                "APPDATA",
                "LOCALAPPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
            }
        )
        isolated_flags = []
        code = (
            "from qa.offline.guard import install_from_env; "
            "assert install_from_env() is not None; " + code
        )
    child = subprocess.run(  # noqa: S603 - fixed synthetic child; paths are positional data.
        [sys.executable, *isolated_flags, "-c", code, str(source), str(index.path)],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", errors="replace")
    with pytest.raises(ConflictError, match="changed"):
        index.replace_many(
            [IndexDocument("new", subject="Prepared replacement")], expected_revision=before
        )
    assert index.record("retained").subject == "Child update"
    assert index.stats().documents == 1 and index.verify().valid


def test_diagnostics_same_count_edit_invalidates_preview_before_gateway_access(tmp_path):
    context = _context(tmp_path)
    index = SearchIndex(context.paths.index_database)
    index.upsert(IndexDocument("retained", subject="Before preview"))
    backend = LocalDiagnosticsBackend(
        context, gateway_factory=lambda: pytest.fail("Stale plan resolved Exchange")
    )
    cancel = Event()
    try:
        plan = backend.plan_index_rebuild("inbox", 2, cancel=cancel, progress=lambda _: None)
        index.upsert(IndexDocument("retained", subject="Changed after preview"))
        with pytest.raises(ConflictError, match="changed"):
            backend.rebuild_index(
                plan,
                confirmation_scope=plan.confirmation_scope,
                cancel=cancel,
                progress=lambda _: None,
            )
        assert index.record("retained").subject == "Changed after preview" and index.verify().valid
    finally:
        context.close()


def test_diagnostics_cancellation_during_preparation_keeps_the_old_index(tmp_path):
    context = _context(tmp_path)
    index = SearchIndex(context.paths.index_database)
    index.upsert(IndexDocument("retained", subject="Preserved reference"))
    before = index.revision()
    cancel = Event()
    backend = LocalDiagnosticsBackend(context, gateway_factory=MetadataGateway)
    try:
        plan = backend.plan_index_rebuild("inbox", 2, cancel=cancel, progress=lambda _: None)

        def progress(value):
            if value.phase == "Preparing bounded index replacement" and value.completed == 2:
                cancel.set()

        with pytest.raises(IndexRebuildCancelled):
            backend.rebuild_index(
                plan, confirmation_scope=plan.confirmation_scope, cancel=cancel, progress=progress
            )
        assert_retained(index, before)
    finally:
        context.close()


def test_cli_unconfirmed_rebuild_preserves_data_without_resolving_exchange(tmp_path, monkeypatch):
    env = {
        f"GUCMAIL_{kind.upper()}_DIR": str(tmp_path / kind)
        for kind in ("config", "data", "cache", "log")
    }
    index = SearchIndex(Path(env["GUCMAIL_DATA_DIR"]) / "index.sqlite3")
    index.upsert(IndexDocument("retained", subject="Preserved reference"))
    before = index.revision()
    monkeypatch.setattr(
        cli.core, "gateway", lambda ctx: pytest.fail("Unconfirmed rebuild resolved Exchange")
    )
    result = CliRunner().invoke(
        cli.app, ["--json", "--non-interactive", "index", "rebuild"], env=env
    )
    assert result.exit_code == 9 and "--yes" in result.output
    assert_retained(index, before)


def test_index_operations_close_owned_sqlite_connections(tmp_path, monkeypatch):
    index = SearchIndex(tmp_path / "index.sqlite3")
    opened = []
    original = index._connect

    def connect():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(index, "_connect", connect)
    try:
        index.stats()
        assert len(opened) >= 2
        for connection in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()
