"""Saved attachment-index consent must reach a bounded shared producer."""

import io
import json
from threading import Event

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError
from gucmail.models import AttachmentInfo, MessageSummary
from gucmail.services.attachment_indexing import (
    AttachmentIndexPreferences,
    MailboxIndexProducer,
    attachment_index_preferences,
)
from gucmail.services.extractors import AttachmentTextExtractor, ExtractionPolicy
from gucmail.services.index import IndexDocument, IndexRebuildCancelled, SearchIndex
from gucmail.settings import SettingsService
from test_diagnostics_tui_secondpass import _context
from test_local_ocr import setup_image


class AttachmentGateway:
    def __init__(self):
        self.calls = []
        self.stream = None

    def list_messages(self, **kwargs):
        self.calls.append(("messages", kwargs))
        return (
            MessageSummary("synthetic", "A synthetic attachment", None, None, has_attachments=True),
        )

    def show_message(self, reference):
        raise AssertionError("Metadata index unexpectedly fetched a body")

    def list_attachments(self, reference, limit=100, offset=0):
        self.calls.append(("metadata", reference, limit, offset))
        return (AttachmentInfo("file", "note.txt", 30, "text/plain"),)

    def open_attachment(self, reference, attachment):
        self.calls.append(("content", reference, attachment))
        self.stream = io.BytesIO(b"uniquelysearchableattachment")
        return self.stream


def test_saved_attachment_index_preference_populates_searchable_text_without_body_reads(
    tmp_path, monkeypatch
):
    gateway = AttachmentGateway()
    monkeypatch.setattr(cli, "_gateway", lambda ctx: gateway)
    env = {
        f"GUCMAIL_{kind.upper()}_DIR": str(tmp_path / kind)
        for kind in ("config", "data", "cache", "log")
    }
    runner = CliRunner()
    changed = runner.invoke(
        cli.app,
        ["--json", "--non-interactive", "config", "set", "index_attachments", "true"],
        env=env,
    )
    assert changed.exit_code == 0, changed.output
    result = runner.invoke(
        cli.app, ["--json", "--non-interactive", "--yes", "index", "build", "--limit", "1"], env=env
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["indexed"] == 1
    index = SearchIndex(tmp_path / "data/index.sqlite3")
    assert index.record("synthetic").attachment_text == "uniquelysearchableattachment"
    assert index.search("uniquelysearchableattachment")[0].reference == "synthetic"
    assert gateway.stream.closed
    assert [row[0] for row in gateway.calls] == ["messages", "metadata", "content"]


def producer(gateway, **preferences):
    return MailboxIndexProducer(
        gateway,
        AttachmentIndexPreferences(**preferences),
        extractor=AttachmentTextExtractor(ExtractionPolicy(isolated=False)),
    )


def test_disabled_setting_avoids_attachment_metadata_and_content_reads():
    gateway = AttachmentGateway()
    result = tuple(producer(gateway).documents(folder="inbox", limit=1))
    assert len(result) == 1 and not result[0].attachment_text
    assert [r[0] for r in gateway.calls] == ["messages"]


@pytest.mark.parametrize(
    "option, outcome",
    [
        ({"formats": ("pdf",)}, "disabled"),
        ({"max_input_bytes": 20}, "too_large"),
        ({"formats": ("png",)}, "disabled"),
    ],
)
def test_preflight_disabled_format_or_known_size_never_opens_content(option, outcome):
    gateway = AttachmentGateway()
    value = producer(gateway, enabled=True, **option)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.payload["attachment_extractions"][0]["status"] == outcome
    assert not any(r[0] == "content" for r in gateway.calls)


class ManyAttachments(AttachmentGateway):
    def __init__(self, files):
        super().__init__()
        self.files = files
        self.streams = []

    def list_attachments(self, reference, limit=100, offset=0):
        self.calls.append(("metadata", reference, limit, offset))
        return tuple(
            AttachmentInfo(str(i), name, size, None)
            for i, (name, content, size) in enumerate(self.files)
        )[offset : offset + limit]

    def open_attachment(self, reference, attachment):
        self.calls.append(("content", reference, attachment))
        stream = io.BytesIO(self.files[int(attachment)][1])
        self.streams.append(stream)
        return stream


def test_unknown_size_is_bounded_and_stream_is_closed_without_indexing_partial_text():
    gateway = ManyAttachments([("note.txt", b"X" * 100, None)])
    value = producer(gateway, enabled=True, max_input_bytes=20, total_bytes=50)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.attachment_text == ""
    assert value.report.bytes_downloaded == 21
    assert gateway.streams[0].closed
    assert document.payload["attachment_extractions"][0]["status"] == "too_large"


def test_total_budget_counts_probe_bytes_and_never_exceeds_reviewed_ceiling():
    gateway = ManyAttachments([("a.txt", b"first text", None), ("b.txt", b"SECOND" * 20, None)])
    value = producer(gateway, enabled=True, total_bytes=20)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.attachment_text == "first text"
    assert value.report.bytes_downloaded == 20
    assert all(s.closed for s in gateway.streams)
    assert [r["status"] for r in document.payload["attachment_extractions"]] == [
        "success",
        "too_large",
    ]


@pytest.mark.parametrize(
    "bounds,expected",
    [
        ({"files_per_message": 1}, ["success", "message_limit"]),
        ({"files_per_run": 1}, ["success", "run_limit", "run_limit"]),
    ],
)
def test_per_message_and_per_run_download_counts_are_enforced(bounds, expected):
    gateway = ManyAttachments([("a.txt", b"one", 3), ("b.txt", b"two", 3), ("c.txt", b"three", 5)])
    value = producer(gateway, enabled=True, **bounds)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert [r["status"] for r in document.payload["attachment_extractions"]] == expected
    assert len(gateway.streams) == 1
    assert value.report.files_downloaded == 1


def test_parser_failure_is_labelled_and_does_not_stop_an_independent_safe_file():
    gateway = ManyAttachments([("bad.json", b"{broken", 7), ("safe.txt", b"retained text", 13)])
    value = producer(gateway, enabled=True)
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    records = document.payload["attachment_extractions"]
    assert [r["status"] for r in records] == ["failed", "success"]
    assert records[1]["label"] == "extracted_attachment_text:txt"
    assert document.attachment_text == "retained text"
    assert value.report.outcomes == {"failed": 1, "success": 1}


def test_later_download_error_preserves_prior_atomic_index_and_closes_streams(tmp_path):
    class Failure(ManyAttachments):
        def open_attachment(self, reference, attachment):
            if attachment == "1":
                raise OSError("Synthetic unavailable stream")
            return super().open_attachment(reference, attachment)

    gateway = Failure([("a.txt", b"one", 3), ("b.txt", b"two", 3)])
    value = producer(gateway, enabled=True)
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(IndexDocument("old", subject="Preserved"))
    before = index.revision()
    with pytest.raises(OSError, match="Synthetic"):
        index.replace_many(value.documents(folder="inbox", limit=1), expected_revision=before)
    assert index.revision() == before and index.verify().valid
    assert index.record("old").subject == "Preserved" and gateway.streams[0].closed


def test_cancel_during_bounded_read_preserves_prior_index_and_closes_stream(tmp_path):
    cancel = Event()

    class Cancelling(io.BytesIO):
        def read(self, size):
            result = super().read(size)
            cancel.set()
            return result

    gateway = AttachmentGateway()
    stream = Cancelling(b"one")
    gateway.open_attachment = lambda *args: stream
    value = producer(gateway, enabled=True)
    value.cancel_event = cancel
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(IndexDocument("old", subject="Preserved"))
    before = index.revision()
    with pytest.raises(IndexRebuildCancelled):
        index.replace_many(
            value.documents(folder="inbox", limit=1), expected_revision=before, cancel_event=cancel
        )
    assert stream.closed and index.revision() == before


def test_image_ocr_requires_both_format_and_separate_saved_consent(tmp_path):
    image, backend, service = setup_image(tmp_path)
    gateway = ManyAttachments([("image.png", image.read_bytes(), image.stat().st_size)])
    disabled = producer(gateway, enabled=True, formats=("png",))
    (document,) = tuple(disabled.documents(folder="inbox", limit=1))
    assert document.payload["attachment_extractions"][0]["status"] == "ocr_disabled"
    assert not gateway.streams and not backend.calls
    value = producer(gateway, enabled=True, formats=("png",), ocr_enabled=True)
    value.ocr_service = service
    (document,) = tuple(value.documents(folder="inbox", limit=1))
    assert document.attachment_text == "Recognized synthetic image"
    record = document.payload["attachment_extractions"][0]
    assert record["label"] == "ocr_attachment_text" and record["potentially_inaccurate"]
    assert record["language"] == "en-US" and gateway.streams[0].closed
    assert backend.path is not None and not backend.path.exists()


def test_staged_attachment_preferences_cancel_apply_restart_reset_and_environment_consumer(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    try:
        settings = SettingsService(context.config_store)
        assert not attachment_index_preferences(settings).enabled
        session = settings.begin()
        session.set("search.index_attachments", True)
        session.set("search.attachment_formats", ["TXT", ".CSV", "JPEG"])
        assert not attachment_index_preferences(settings).enabled
        session.cancel()
        assert not attachment_index_preferences(settings).enabled
        session = settings.begin()
        session.set("search.index_attachments", True)
        session.set("search.attachment_formats", ["TXT", ".CSV", "JPEG"])
        session.apply()
        saved = attachment_index_preferences(SettingsService(context.config_store))
        assert saved.enabled and saved.formats == ("csv", "jpg", "txt")
        settings.reset("search.attachment_formats")
        assert attachment_index_preferences(settings).formats == tuple(
            sorted(("csv", "html", "json", "txt", "xml"))
        )
        monkeypatch.setenv("GUCMAIL_SETTING_SEARCH__ATTACHMENT_FILES_PER_RUN", "1")
        assert attachment_index_preferences(settings).files_per_run == 1
        with pytest.raises(ConfigurationError, match="overridden"):
            settings.set("search.attachment_files_per_run", 2)
    finally:
        context.close()
