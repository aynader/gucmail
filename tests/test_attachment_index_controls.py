"""Review, cancellation and persistent policy controls before attachment reads."""

import multiprocessing
import platform
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest
from tests.attachment_worker_fixtures import _slow_extraction_worker
from typer.testing import CliRunner

import gucmail.cli as cli
import gucmail.services.extractors as extractors
from gucmail.errors import ConfigurationError, ConflictError
from gucmail.services.attachment_indexing import AttachmentIndexPreferences, MailboxIndexProducer
from gucmail.services.backup import BackupService
from gucmail.services.extractors import AttachmentTextExtractor, ExtractionPolicy, ExtractionStatus
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.local_search import LocalSearchService
from gucmail.settings import SettingsContext, SettingsService
from gucmail.tui.diagnostics_screen import LocalDiagnosticsBackend
from test_attachment_index_producer import AttachmentGateway, ManyAttachments, producer
from test_diagnostics_tui_secondpass import _context


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"ocr_enabled": "true"},
        {"formats": None},
        {"formats": "txt"},
        {"formats": ("exe",)},
        {"max_input_bytes": True},
        {"max_input_bytes": 10 * 1024 * 1024 + 1},
        {"files_per_message": 0},
        {"files_per_run": 101},
        {"total_bytes": 100 * 1024 * 1024 + 1},
        {"output_chars": 100_001},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": 15.1},
        {"ocr_language": "invalid language"},
    ],
)
def test_invalid_attachment_policy_is_rejected_before_any_source(values):
    with pytest.raises(ConfigurationError):
        AttachmentIndexPreferences(**values)


def environment(root):
    return {
        f"GUCMAIL_{kind.upper()}_DIR": str(root / kind)
        for kind in ("config", "data", "cache", "log")
    }


@pytest.mark.parametrize("command", ["build", "rebuild"])
def test_enabled_cli_index_requires_confirmation_before_gateway(tmp_path, monkeypatch, command):
    context = _context(tmp_path)
    try:
        SettingsService(context.config_store).set("search.index_attachments", True)
        index = SearchIndex(context.paths.index_database)
        index.upsert(IndexDocument("retained", subject="Existing local record"))
        before = index.revision()

        def forbidden(_ctx):
            raise AssertionError("Unconfirmed attachment index opened the gateway")

        monkeypatch.setattr(cli.core, "gateway", forbidden)
        result = CliRunner().invoke(
            cli.app,
            ["--json", "--non-interactive", "index", command, "--limit", "1"],
            env=environment(tmp_path),
        )
        assert result.exit_code == 9, result.output
        assert "confirmation" in result.output.casefold()
        assert index.revision() == before
    finally:
        context.close()


@pytest.mark.parametrize("command", ["build", "rebuild"])
def test_cli_changed_policy_after_confirmation_prevents_download(tmp_path, monkeypatch, command):
    context = _context(tmp_path)
    try:
        SettingsService(context.config_store).set("search.index_attachments", True)
        original = cli.core.confirm

        def change_policy(ctx, *args, **kwargs):
            original(ctx, *args, **kwargs)
            SettingsService(context.config_store).set("search.attachment_files_per_run", 1)

        def forbidden(_ctx):
            raise AssertionError("Stale policy opened the gateway")

        monkeypatch.setattr(cli.core, "confirm", change_policy)
        monkeypatch.setattr(cli.core, "gateway", forbidden)
        result = CliRunner().invoke(
            cli.app,
            ["--json", "--non-interactive", "--yes", "index", command, "--limit", "1"],
            env=environment(tmp_path),
        )
        assert result.exit_code == 7, result.output
        assert "settings changed after review" in result.output
        assert SearchIndex(context.paths.index_database).stats().documents == 0
    finally:
        context.close()


def test_diagnostics_stale_policy_preserves_old_index_without_gateway(tmp_path):
    context = _context(tmp_path)
    try:
        index = SearchIndex(context.paths.index_database)
        index.upsert(IndexDocument("retained", subject="Existing local record"))
        before = index.revision()

        def forbidden():
            raise AssertionError("Stale Diagnostics policy opened the gateway")

        backend = LocalDiagnosticsBackend(context, gateway_factory=forbidden)
        plan = backend.plan_index_rebuild("inbox", 1, cancel=Event(), progress=lambda _: None)
        SettingsService(context.config_store).set("search.index_attachments", True)
        with pytest.raises(ConflictError, match="settings changed after preview"):
            backend.rebuild_index(
                plan,
                confirmation_scope=plan.confirmation_scope,
                cancel=Event(),
                progress=lambda _: None,
            )
        assert index.revision() == before and index.search("Existing")
    finally:
        context.close()


def test_diagnostics_saved_policy_downloads_extracts_and_replaces_atomically(tmp_path):
    context = _context(tmp_path)
    try:
        SettingsService(context.config_store).set("search.index_attachments", True)
        index = SearchIndex(context.paths.index_database)
        index.upsert(IndexDocument("retained", subject="Existing local record"))
        gateway = AttachmentGateway()
        backend = LocalDiagnosticsBackend(context, gateway_factory=lambda: gateway)
        plan = backend.plan_index_rebuild("inbox", 1, cancel=Event(), progress=lambda _: None)
        assert plan.attachment_preferences.enabled and not gateway.calls
        result = backend.rebuild_index(
            plan,
            confirmation_scope=plan.confirmation_scope,
            cancel=Event(),
            progress=lambda _: None,
        )
        assert result.removed == result.indexed == result.attachment_files == 1
        assert result.attachment_outcomes == (("success", 1),)
        assert index.search("uniquelysearchableattachment")[0].reference == "synthetic"
        with pytest.raises(ConfigurationError, match="not present"):
            index.record("retained")
        assert gateway.stream.closed and index.verify().valid
    finally:
        context.close()


def test_diagnostics_consumes_exact_machine_policy_saved_by_normal_settings(tmp_path):
    context = _context(tmp_path)
    try:
        settings = SettingsService(
            context.config_store,
            context=SettingsContext(
                account_id=context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )
        session = settings.begin()
        session.set("search.index_attachments", True)
        session.set("search.attachment_files_per_run", 1)
        session.apply()
        assert (
            not SettingsService(context.config_store)
            .value("search.index_attachments")
            .effective_value
        )
        backend = LocalDiagnosticsBackend(context)
        plan = backend.plan_index_rebuild("inbox", 1, cancel=Event(), progress=lambda _: None)
        assert plan.attachment_preferences.enabled
        assert plan.attachment_preferences.files_per_run == 1
        other = SettingsService(
            context.config_store, context=SettingsContext(machine_id="other-synthetic-machine")
        )
        assert not other.value("search.index_attachments").effective_value
    finally:
        context.close()




def test_running_owned_parser_cancels_and_leaves_no_child(tmp_path, monkeypatch):
    ready = tmp_path / "parser-ready.txt"
    cancel = Event()
    cancel_requested = []
    monkeypatch.setattr(extractors, "_worker_entry", _slow_extraction_worker)

    def cancel_started_child():
        # The audit bootstrap and spawn imports precede this controlled worker.
        # Keep its readiness allowance below the unchanged 12-second extraction
        # deadline, then measure cancellation itself independently of startup.
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            cancel.wait(0.01)
        cancel_requested.append(time.monotonic())
        cancel.set()

    observer = Thread(target=cancel_started_child)
    observer.start()
    start = time.monotonic()
    try:
        result = AttachmentTextExtractor(ExtractionPolicy(timeout_seconds=12)).extract(
            "synthetic.txt", str(ready).encode(), cancel_event=cancel
        )
    finally:
        observer.join(timeout=11)
    assert ready.exists(), (
        "Cancellation must exercise an actually running owned child; "
        f"result={result.status.value}, error={result.error}"
    )
    assert not observer.is_alive()
    assert result.status is ExtractionStatus.CANCELLED and not result.text
    assert time.monotonic() - start < 13
    assert len(cancel_requested) == 1 and time.monotonic() - cancel_requested[0] < 1
    pid = int(ready.read_text(encoding="utf-8"))
    assert pid not in {process.pid for process in multiprocessing.active_children()}


def test_pre_cancelled_parser_never_starts_worker(monkeypatch):
    cancel = Event()
    cancel.set()

    def forbidden(*args, **kwargs):
        raise AssertionError("A pre-cancelled extraction started a process")

    monkeypatch.setattr(extractors, "_extract_isolated", forbidden)
    result = AttachmentTextExtractor().extract("synthetic.txt", b"local", cancel_event=cancel)
    assert result.status is ExtractionStatus.CANCELLED


def test_html_alias_sanitized_aggregate_output_and_explicit_truncation():
    gateway = ManyAttachments(
        [
            ("page.htm", b"<p>Safe words</p><script>hostile()</script>", None),
            ("notes.txt", b"second document", None),
        ]
    )
    value = producer(gateway, enabled=True, formats=("htm", "txt"), output_chars=12)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.attachment_text == "Safe words\ns"
    assert document.payload["attachment_text_truncated"]
    assert "hostile" not in document.attachment_text
    assert document.payload["attachment_extractions"][0]["format"] == "html"
    assert all(stream.closed for stream in gateway.streams)


def test_unavailable_image_ocr_labels_failure_and_keeps_ordinary_text(tmp_path):
    class UnavailableOcr:
        def preview(self, path, **kwargs):
            assert Path(path).exists()
            raise ConfigurationError("Private provider detail should not enter the index")

    gateway = ManyAttachments(
        [
            ("image.png", b"not an image", None),
            ("notes.txt", b"Ordinary extraction available", None),
        ]
    )
    value = MailboxIndexProducer(
        gateway,
        AttachmentIndexPreferences(enabled=True, formats=("png", "txt"), ocr_enabled=True),
        extractor=AttachmentTextExtractor(ExtractionPolicy(isolated=False)),
        ocr_service=UnavailableOcr(),
        temporary_directory=tmp_path,
    )
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.attachment_text == "Ordinary extraction available"
    assert document.payload["attachment_extractions"][0]["status"] == "ocr_unavailable_or_invalid"
    assert "Private provider detail" not in str(document.payload)
    assert not tuple(tmp_path.iterdir())
    assert all(stream.closed for stream in gateway.streams)


@pytest.mark.parametrize("mode", ["index", "attachments"])
@pytest.mark.parametrize("fuzzy", [False, True])
def test_local_search_exposes_ocr_uncertainty_and_truncation_without_download(
    tmp_path, mode, fuzzy
):
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(
        IndexDocument(
            "synthetic",
            subject="Synthetic image",
            attachment_text="Recognized synthetic phrase",
            payload={
                "attachment_extractions": [
                    {
                        "label": "ocr_attachment_text",
                        "status": "success",
                        "chars": 27,
                        "truncated": True,
                    }
                ],
                "attachment_text_truncated": True,
            },
        )
    )
    report = LocalSearchService(index, BackupService(tmp_path / "backups")).search(
        "synthetic", mode=mode, fuzzy=fuzzy
    )
    assert len(report.results) == 1
    result = report.results[0]
    assert "OCR-derived attachment text" in result.explanation
    assert "recognition may be inaccurate" in result.explanation
    assert "truncated" in result.explanation


def test_local_search_exposes_real_ordinary_extraction_label_without_more_reads(tmp_path):
    gateway = AttachmentGateway()
    value = producer(gateway, enabled=True)
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.replace_many(value.documents(folder="inbox", limit=1))
    before = len(gateway.calls)
    report = LocalSearchService(index, BackupService(tmp_path / "backups")).search(
        "uniquelysearchableattachment", mode="attachments"
    )
    assert len(report.results) == 1
    assert "locally extracted attachment text" in report.results[0].explanation
    assert len(gateway.calls) == before
