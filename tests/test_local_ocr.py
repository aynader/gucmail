from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from threading import Event

import pytest

from gucmail.errors import ConfigurationError
from gucmail.services.extractors import ExtractionPolicy, extract_attachment_text
from gucmail.services.ocr import MAX_INPUT_BYTES, OcrService, WindowsOcrBackend


def png_header(width=20, height=10):
    header = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + header
        + struct.pack(">I", zlib.crc32(header))
    )


class Backend:
    def __init__(self):
        self.calls = []
        self.languages = ["en-US", "ar-SA"]
        self.result = None
        self.path = None
        self.raise_on_extract = False

    def invoke(self, options, *, timeout, cancel_event):
        self.calls.append(dict(options))
        if options["mode"] == "status":
            return {"status": "available", "languages": self.languages}
        self.path = Path(options["path"])
        assert self.path.is_file() and self.path.name.startswith("gucmail-ocr-")
        assert self.path.read_bytes() == png_header()
        if self.raise_on_extract:
            raise RuntimeError("Untrusted worker error with secret-placeholder")
        return (
            self.result
            if self.result is not None
            else {
                "status": "success",
                "text": "Recognized synthetic image",
                "truncated": False,
                "language": options["language"],
                "width": options["width"],
                "height": options["height"],
            }
        )


def setup_image(tmp_path):
    source = tmp_path / "selected image.png"
    source.write_bytes(png_header())
    backend = Backend()
    return source, backend, OcrService(backend=backend)


def test_preview_and_disabled_recognition_never_start_image_worker(tmp_path):
    source, backend, service = setup_image(tmp_path)
    status = service.status()
    assert not status["enabled_by_default"] and not status["remote_transmission"]
    assert not status["automatic_indexing"] and status["languages"] == ["ar-SA", "en-US"]
    plan = service.preview(source, language="en-US")
    assert plan.can_recognize and plan.width == 20 and plan.height == 10
    preview = plan.as_dict()
    assert preview["apply"] is False and "No mailbox fetch" in preview["privacy"]
    before = len(backend.calls)
    assert service.extract(plan).status == "disabled"
    assert len(backend.calls) == before and all(c["mode"] == "status" for c in backend.calls)


def test_explicit_recognition_copies_only_reviewed_bytes_and_removes_temporary_file(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    result = service.extract(plan, enabled=True)
    assert result.status == "success" and result.text == "Recognized synthetic image"
    assert result.sha256 == plan.sha256 and result.label == "ocr_attachment_text"
    assert backend.path is not None and not backend.path.exists() and backend.path != source
    assert source.read_bytes() == png_header()
    assert json.loads(json.dumps(result.as_dict()))["text"] == result.text


@pytest.mark.parametrize("changed", ["image", "language"])
def test_source_or_installed_language_change_invalidates_reviewed_plan(tmp_path, changed):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    if changed == "image":
        source.write_bytes(png_header(21, 10))
    else:
        backend.languages = ["ar-SA"]
    with pytest.raises(ConfigurationError, match="changed"):
        service.extract(plan, enabled=True)
    assert all(c["mode"] == "status" for c in backend.calls)


def test_missing_language_is_reported_without_installing_or_recognizing(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="zz-ZZ")
    assert not plan.can_recognize
    assert service.extract(plan, enabled=True).status == "unavailable"
    assert all(c["mode"] == "status" for c in backend.calls)


def test_cancelled_operation_does_not_read_image_again_or_start_worker(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    source.unlink()
    cancelled = Event()
    cancelled.set()
    before = len(backend.calls)
    assert service.extract(plan, enabled=True, cancel_event=cancelled).status == "cancelled"
    assert len(backend.calls) == before


@pytest.mark.parametrize(
    "outcome",
    [
        "failed",
        "timeout",
        "cancelled",
        "invalid_image",
        "language_unavailable",
        "untrusted",
        ["bad"],
    ],
)
def test_worker_failure_is_bounded_secret_safe_and_removes_owned_copy(tmp_path, outcome):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    backend.result = {
        "status": outcome,
        "error": "secret-placeholder",
        "text": "private-placeholder",
    }
    result = service.extract(plan, enabled=True)
    assert result.status != "success" and result.text == ""
    assert "placeholder" not in str(result.as_dict())
    assert backend.path is not None and not backend.path.exists()


def test_worker_exception_does_not_escape_or_leave_a_temporary_image(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    backend.raise_on_extract = True
    result = service.extract(plan, enabled=True)
    assert result.status == "failed" and "placeholder" not in str(result)
    assert backend.path is not None and not backend.path.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "x" * 11),
        ("language", "ar-SA"),
        ("width", 3000),
        ("height", 999),
        ("truncated", "true"),
        ("text", ["bad"]),
    ],
)
def test_invalid_worker_results_are_rejected(tmp_path, field, value):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US", max_output_chars=10)
    backend.result = {
        "status": "success",
        "text": "hello",
        "language": "en-US",
        "width": 20,
        "height": 10,
        "truncated": False,
        field: value,
    }
    assert service.extract(plan, enabled=True).status == "failed"


def test_recognized_text_is_terminal_safe_and_truncation_is_exposed(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    backend.result = {
        "status": "success",
        "text": "Arabic العربية\x1b[31m\x00",
        "language": "en-US",
        "width": 20,
        "height": 10,
        "truncated": True,
    }
    result = service.extract(plan, enabled=True)
    assert result.truncated and "العربية" in result.text
    assert "\x1b" not in result.text and "\x00" not in result.text


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not an image",
        png_header()[:-1],
        b"\xff\xd8\xff\xc0\x00\xff",
        png_header(0, 10),
        png_header(2049, 1),
        png_header(1, 2049),
    ],
)
def test_malformed_or_oversized_pixel_headers_fail_before_any_backend_operation(tmp_path, content):
    source, backend, service = setup_image(tmp_path)
    source.write_bytes(content)
    with pytest.raises(ConfigurationError):
        service.preview(source, language="en-US")
    assert not backend.calls


def test_byte_limit_is_checked_before_native_decode(tmp_path):
    source, backend, service = setup_image(tmp_path)
    with source.open("wb") as handle:
        handle.truncate(MAX_INPUT_BYTES + 1)
    with pytest.raises(ConfigurationError, match="8 MiB"):
        service.preview(source, language="en-US")
    assert not backend.calls


@pytest.mark.parametrize(
    "path",
    [
        "https://example.test/image.png",
        "//server/share/image.png",
        r"\\server\share\image.png",
        "document.svg",
        "page.pdf",
    ],
)
def test_network_and_unsupported_paths_are_rejected_before_open(tmp_path, path):
    backend = Backend()
    with pytest.raises(ConfigurationError):
        OcrService(backend=backend).preview(Path(path), language="en-US")
    assert not backend.calls


@pytest.mark.parametrize("language", ["en-US; Start-Process malicious", "", "\x00", "en_US", None])
def test_language_is_data_and_requires_a_valid_installed_tag(tmp_path, language):
    source, backend, service = setup_image(tmp_path)
    with pytest.raises(ConfigurationError):
        service.preview(source, language=language)
    assert not backend.calls


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 16},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": "eight"},
        {"max_output_chars": 0},
        {"max_output_chars": 100001},
        {"max_output_chars": True},
    ],
)
def test_resource_limits_validate_before_backend_use(tmp_path, options):
    source, backend, service = setup_image(tmp_path)
    with pytest.raises(ConfigurationError):
        service.preview(source, language="en-US", **options)
    assert not backend.calls


def test_unsupported_platform_does_not_launch_native_child(monkeypatch):
    from gucmail.services import ocr

    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(
        ocr.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unexpected native child")
    )
    status = OcrService(backend=WindowsOcrBackend()).status()
    assert not status["available"] and status["languages"] == []


def test_ordinary_text_extraction_remains_independent_of_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WindowsOcrBackend,
        "invoke",
        lambda *args, **kwargs: pytest.fail("Ordinary extraction invoked OCR"),
    )
    result = extract_attachment_text(
        "notes.txt", b"ordinary text", policy=ExtractionPolicy(isolated=False)
    )
    assert result.ok and result.text == "ordinary text"


@pytest.mark.parametrize("marker", [0xC0, 0xC1, 0xC2])
def test_common_jpeg_frame_headers_are_bounded_before_decoding(tmp_path, marker):
    source, _, service = setup_image(tmp_path)
    path = source.with_suffix(".jpg")
    path.write_bytes(b"\xff\xd8\xff" + bytes([marker]) + b"\x00\x08\x08\x00\x0a\x00\x14\x01")
    preview = service.preview(path, language="en-US")
    assert preview.format == "jpg" and preview.width == 20 and preview.height == 10


def test_symbolic_local_image_path_is_rejected_without_backend_use(tmp_path, monkeypatch):
    source, backend, service = setup_image(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == source or original(path))
    with pytest.raises(ConfigurationError, match="symbolic"):
        service.preview(source, language="en-US")
    assert not backend.calls


def test_mapped_remote_drive_is_rejected_before_file_content_read(tmp_path, monkeypatch):
    from gucmail.services import ocr

    source, backend, service = setup_image(tmp_path)
    monkeypatch.setattr(ocr, "_remote_drive", lambda path: True)
    monkeypatch.setattr(ocr, "_read_image", lambda path: pytest.fail("Read mapped remote input"))
    with pytest.raises(ConfigurationError, match="mapped network"):
        service.preview(source, language="en-US")
    assert not backend.calls


def test_cancellation_during_language_recheck_is_not_reported_as_changed_consent(tmp_path):
    source, backend, service = setup_image(tmp_path)
    plan = service.preview(source, language="en-US")
    cancelled = Event()
    original = backend.invoke

    def cancel_status(options, *, timeout, cancel_event):
        if options["mode"] == "status" and cancel_event is not None:
            cancel_event.set()
            return {"status": "cancelled"}
        return original(options, timeout=timeout, cancel_event=cancel_event)

    backend.invoke = cancel_status
    result = service.extract(plan, enabled=True, cancel_event=cancelled)
    assert result.status == "cancelled" and result.text == ""
    assert backend.path is None and all(c["mode"] == "status" for c in backend.calls)
