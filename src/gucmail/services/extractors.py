"""Bounded, offline text extraction for untrusted email attachments.

The public extractor never opens links, executes active content, or writes an
attachment to disk.  By default only simple text formats are enabled.  PDF and
OOXML support is opt-in and uses the optional ``attachments`` dependencies.
Hostile parsers run in a short-lived worker process unless isolation is
explicitly disabled by a trusted caller.
"""

from __future__ import annotations

import csv
import html
import importlib
import io
import json
import math
import multiprocessing
import os
import re
import socket
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from html.parser import HTMLParser
from multiprocessing.connection import Connection
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any, Final, Protocol
from xml.etree import ElementTree

CORE_FORMATS: Final = frozenset({"txt", "csv", "json", "xml", "html"})
OPTIONAL_FORMATS: Final = frozenset({"pdf", "docx", "pptx", "xlsx"})
SUPPORTED_FORMATS: Final = CORE_FORMATS | OPTIONAL_FORMATS

_FORMAT_BY_SUFFIX: Final = {
    ".txt": "txt",
    ".csv": "csv",
    ".json": "json",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
}
_FORMAT_BY_MEDIA_TYPE: Final = {
    "text/plain": "txt",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/json": "json",
    "text/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
_MACRO_SUFFIXES: Final = frozenset(
    {
        ".docm",
        ".dotm",
        ".xlsm",
        ".xltm",
        ".xlam",
        ".pptm",
        ".potm",
        ".ppam",
        ".ppsm",
        ".sldm",
    }
)
_MACRO_MEDIA_MARKER: Final = "macroenabled"
_ACTIVE_ARCHIVE_PARTS: Final = (
    "vbaproject.bin",
    "vbadata.xml",
    "/activex/",
    "/macrosheets/",
    "/dialogsheets/",
)
_EXPECTED_OOXML_PART: Final = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}
_DEPENDENCY_BY_FORMAT: Final = {
    "pdf": "pypdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "xlsx": "openpyxl",
}
_ANSI_ESCAPE_RE: Final = re.compile(r"(?:\x1B[@-_][0-?]*[ -/]*[@-~]|\x9B[0-?]*[ -/]*[@-~])")
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DTD_RE: Final = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_EXTERNAL_REL_RE: Final = re.compile(rb"\bTargetMode\s*=\s*(['\"])External\1", re.IGNORECASE)
_OLE_COMPOUND_MAGIC: Final = bytes.fromhex("D0CF11E0A1B11AE1")


class ExtractionStatus(StrEnum):
    """Stable outcomes suitable for persistence in the search index."""

    SUCCESS = "success"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    UNSAFE = "unsafe"
    ENCRYPTED = "encrypted"
    DEPENDENCY_MISSING = "dependency_missing"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    """Limits and per-format controls for one extraction operation."""

    enabled_formats: frozenset[str] = CORE_FORMATS
    max_input_bytes: int = 10 * 1024 * 1024
    max_output_chars: int = 1_000_000
    timeout_seconds: float = 8.0
    max_archive_uncompressed_bytes: int = 50 * 1024 * 1024
    max_archive_members: int = 5_000
    max_compression_ratio: float = 150.0
    max_pages: int = 200
    max_rows: int = 20_000
    max_cells: int = 100_000
    worker_memory_bytes: int = 512 * 1024 * 1024
    isolated: bool = True

    def __post_init__(self) -> None:
        normalized = frozenset(str(item).lower().lstrip(".") for item in self.enabled_formats)
        unknown = normalized - SUPPORTED_FORMATS
        if unknown:
            raise ValueError(f"Unsupported enabled format(s): {', '.join(sorted(unknown))}")
        object.__setattr__(self, "enabled_formats", normalized)
        integer_limits = {
            "max_input_bytes": self.max_input_bytes,
            "max_output_chars": self.max_output_chars,
            "max_archive_uncompressed_bytes": self.max_archive_uncompressed_bytes,
            "max_archive_members": self.max_archive_members,
            "max_pages": self.max_pages,
            "max_rows": self.max_rows,
            "max_cells": self.max_cells,
            "worker_memory_bytes": self.worker_memory_bytes,
        }
        for name, value in integer_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(self.max_compression_ratio, bool) or self.max_compression_ratio < 1:
            raise ValueError("max_compression_ratio must be at least 1")

    @classmethod
    def all_supported(cls, **overrides: Any) -> ExtractionPolicy:
        """Build an explicit opt-in policy for every supported format."""

        return cls(enabled_formats=SUPPORTED_FORMATS, **overrides)


MetadataValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """A non-throwing, persistence-friendly extraction result."""

    status: ExtractionStatus
    format: str | None
    text: str = ""
    error: str | None = None
    truncated: bool = False
    char_count: int = 0
    label: str = "extracted_attachment_text"
    metadata: Mapping[str, MetadataValue] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ExtractionStatus.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class _ExtractionProblem(Exception):
    status = ExtractionStatus.FAILED


class _UnsafeDocument(_ExtractionProblem):
    status = ExtractionStatus.UNSAFE


class _EncryptedDocument(_ExtractionProblem):
    status = ExtractionStatus.ENCRYPTED


class _DependencyUnavailable(_ExtractionProblem):
    status = ExtractionStatus.DEPENDENCY_MISSING


class _ExtractionTimedOut(_ExtractionProblem):
    status = ExtractionStatus.TIMEOUT


class _InvalidDocument(_ExtractionProblem):
    status = ExtractionStatus.FAILED


@dataclass(frozen=True, slots=True)
class _ArchiveInspection:
    members: int
    uncompressed_bytes: int
    external_relationships: int


class _WorkerProcess(Protocol):
    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def close(self) -> None: ...


class _TextAccumulator:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def add(self, value: object, *, separator: str = "\n") -> bool:
        if value is None:
            return True
        cleaned = _sanitize_text(str(value)).strip()
        if not cleaned:
            return True
        prefix = separator if self.parts else ""
        remaining = self.limit - self.length
        if remaining <= 0:
            self.truncated = True
            return False
        combined = prefix + cleaned
        if len(combined) > remaining:
            self.parts.append(combined[:remaining])
            self.length = self.limit
            self.truncated = True
            return False
        self.parts.append(combined)
        self.length += len(combined)
        return True

    def render(self) -> str:
        return "".join(self.parts)


class _SafeHTMLTextParser(HTMLParser):
    _IGNORED_TAGS = frozenset(
        {"script", "style", "noscript", "template", "iframe", "object", "embed", "svg"}
    )

    def __init__(self, output: _TextAccumulator, deadline: float) -> None:
        super().__init__(convert_charrefs=True)
        self.output = output
        self.deadline = deadline
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self._IGNORED_TAGS:
            self.ignored_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        _check_deadline(self.deadline)
        if not self.ignored_depth:
            self.output.add(html.unescape(data))


class AttachmentTextExtractor:
    """Extract searchable text from bytes without raising on hostile input."""

    def __init__(self, policy: ExtractionPolicy | None = None) -> None:
        self.policy = policy or ExtractionPolicy()

    def extract(
        self,
        filename: str,
        content: bytes | bytearray | memoryview,
        *,
        content_type: str | None = None,
        cancel_event: Event | None = None,
    ) -> ExtractionResult:
        if cancel_event is not None and cancel_event.is_set():
            return _result(
                ExtractionStatus.CANCELLED, None, error="Attachment extraction was cancelled."
            )
        suffix = _filename_suffix(filename)
        normalized_media_type = _normalize_media_type(content_type)
        if suffix in _MACRO_SUFFIXES or (
            normalized_media_type and _MACRO_MEDIA_MARKER in normalized_media_type
        ):
            return _result(
                ExtractionStatus.UNSAFE,
                None,
                error="Macro-enabled Office documents are not processed.",
            )

        format_name = _detect_format(filename, normalized_media_type)
        if format_name is None:
            return _result(
                ExtractionStatus.UNSUPPORTED,
                None,
                error="Attachment format is not supported for text extraction.",
            )
        if format_name not in self.policy.enabled_formats:
            return _result(
                ExtractionStatus.DISABLED,
                format_name,
                error="Text extraction for this attachment format is disabled.",
            )

        try:
            content_size = content.nbytes if isinstance(content, memoryview) else len(content)
        except (TypeError, AttributeError):
            return _result(
                ExtractionStatus.FAILED,
                format_name,
                error="Attachment content must be bytes.",
            )
        if content_size > self.policy.max_input_bytes:
            return _result(
                ExtractionStatus.TOO_LARGE,
                format_name,
                error="Attachment exceeds the configured extraction size limit.",
                metadata={"input_bytes": content_size},
            )
        data = bytes(content)
        if len(data) > self.policy.max_input_bytes:
            return _result(
                ExtractionStatus.TOO_LARGE,
                format_name,
                error="Attachment exceeds the configured extraction size limit.",
                metadata={"input_bytes": len(data)},
            )
        if not data:
            return _result(
                ExtractionStatus.SUCCESS,
                format_name,
                metadata={"input_bytes": 0},
            )

        if self.policy.isolated:
            return _extract_isolated(format_name, data, self.policy, cancel_event=cancel_event)
        return _extract_in_current_process(format_name, data, self.policy)

    def extract_path(
        self,
        path: Path | str,
        *,
        content_type: str | None = None,
    ) -> ExtractionResult:
        source = Path(path)
        format_name = _detect_format(source.name, _normalize_media_type(content_type))
        try:
            with source.open("rb") as handle:
                content = handle.read(self.policy.max_input_bytes + 1)
        except OSError:
            return _result(
                ExtractionStatus.FAILED,
                format_name,
                error="Attachment could not be read for text extraction.",
            )
        return self.extract(source.name, content, content_type=content_type)


SafeAttachmentExtractor = AttachmentTextExtractor


def extract_attachment_text(
    filename: str | Path,
    content: bytes | bytearray | memoryview | None = None,
    *,
    content_type: str | None = None,
    policy: ExtractionPolicy | None = None,
) -> ExtractionResult:
    """Convenience wrapper for byte content, or for a path when content is omitted."""

    extractor = AttachmentTextExtractor(policy)
    if content is None:
        return extractor.extract_path(filename, content_type=content_type)
    return extractor.extract(str(filename), content, content_type=content_type)


extract_text = extract_attachment_text

_TRACKER_LOCK = threading.Lock()


def ensure_resource_tracker() -> None:
    """Start multiprocessing's helper process with a usable stderr.

    On Linux and macOS the helper inherits ``sys.stderr.fileno()``. Inside the TUI, Textual
    replaces stderr with an object whose fileno() is -1, and every worker start then fails.
    """

    if os.name == "nt":
        return
    try:
        if sys.stderr is not None and sys.stderr.fileno() >= 0:
            return
    except (AttributeError, OSError, ValueError):
        pass
    from multiprocessing import resource_tracker

    with _TRACKER_LOCK, open(os.devnull, "w", encoding="utf-8") as sink:
        previous = sys.stderr
        sys.stderr = sink
        try:
            resource_tracker.ensure_running()
        finally:
            sys.stderr = previous


def _extract_isolated(
    format_name: str,
    data: bytes,
    policy: ExtractionPolicy,
    *,
    cancel_event: Event | None = None,
) -> ExtractionResult:
    ensure_resource_tracker()
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_entry,
        args=(send_connection, format_name, data, policy),
        name=f"gucmail-extract-{format_name}",
        daemon=True,
    )
    try:
        process.start()
    except (OSError, RuntimeError):
        receive_connection.close()
        send_connection.close()
        return _result(
            ExtractionStatus.FAILED,
            format_name,
            error="The isolated extraction worker could not be started.",
        )
    send_connection.close()
    try:
        deadline = time.monotonic() + policy.timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return _result(
                    ExtractionStatus.CANCELLED,
                    format_name,
                    error="Attachment extraction was cancelled.",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _result(
                    ExtractionStatus.TIMEOUT,
                    format_name,
                    error="Attachment text extraction exceeded the configured time limit.",
                )
            if receive_connection.poll(
                min(remaining, 0.05) if cancel_event is not None else remaining
            ):
                break
        try:
            result = receive_connection.recv()
        except (EOFError, OSError):
            result = None
        process.join(timeout=0.5)
        if process.is_alive():
            _stop_process(process)
        if isinstance(result, ExtractionResult):
            return result
        return _result(
            ExtractionStatus.FAILED,
            format_name,
            error="The isolated extraction worker ended unexpectedly.",
        )
    finally:
        receive_connection.close()
        _stop_process(process)


def _worker_entry(
    connection: Connection,
    format_name: str,
    data: bytes,
    policy: ExtractionPolicy,
) -> None:
    try:
        _apply_worker_resource_limits(policy)
        _disable_network_access()
        result = _extract_in_current_process(format_name, data, policy)
    except BaseException as exc:
        result = _result(
            ExtractionStatus.FAILED,
            format_name,
            error=f"The extraction worker failed safely ({type(exc).__name__}).",
        )
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def _stop_process(process: _WorkerProcess) -> None:
    try:
        alive = process.is_alive()
    except ValueError:
        return
    if alive:
        process.terminate()
        process.join(timeout=0.5)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.5)
    process.close()


def _apply_worker_resource_limits(policy: ExtractionPolicy) -> None:
    if os.name != "posix":
        return
    try:
        resource: Any = importlib.import_module("resource")

        _, hard_limit = resource.getrlimit(resource.RLIMIT_AS)
        desired = policy.worker_memory_bytes
        if hard_limit != resource.RLIM_INFINITY:
            desired = min(desired, hard_limit)
        resource.setrlimit(resource.RLIMIT_AS, (desired, desired))
        cpu_seconds = max(1, math.ceil(policy.timeout_seconds) + 1)
        _, cpu_hard_limit = resource.getrlimit(resource.RLIMIT_CPU)
        if cpu_hard_limit != resource.RLIM_INFINITY:
            cpu_seconds = min(cpu_seconds, cpu_hard_limit)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ImportError, OSError, ValueError):
        # The parent-enforced wall clock timeout remains the portable boundary.
        return


def _disable_network_access() -> None:
    """Deny common Python network paths inside the disposable parser worker."""

    import http.client
    import ssl
    import urllib.request

    original_socket = socket.socket

    class BlockedSocket(original_socket):  # type: ignore[misc,valid-type]
        def connect(self, address: object) -> None:
            del address
            raise PermissionError("Network access is disabled during attachment extraction.")

        def connect_ex(self, address: object) -> int:
            del address
            raise PermissionError("Network access is disabled during attachment extraction.")

        def sendto(self, *args: object, **kwargs: object) -> int:
            del args, kwargs
            raise PermissionError("Network access is disabled during attachment extraction.")

    def blocked(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise PermissionError("Network access is disabled during attachment extraction.")

    setattr(socket, "socket", BlockedSocket)  # noqa: B010
    setattr(socket, "create_connection", blocked)  # noqa: B010
    setattr(socket, "getaddrinfo", blocked)  # noqa: B010
    setattr(urllib.request, "urlopen", blocked)  # noqa: B010
    setattr(http.client.HTTPConnection, "connect", blocked)  # noqa: B010
    setattr(http.client.HTTPSConnection, "connect", blocked)  # noqa: B010
    setattr(ssl.SSLSocket, "connect", blocked)  # noqa: B010


def _extract_in_current_process(
    format_name: str,
    data: bytes,
    policy: ExtractionPolicy,
) -> ExtractionResult:
    deadline = time.monotonic() + policy.timeout_seconds
    try:
        if format_name == "txt":
            text, truncated, metadata = _extract_plain_text(data, policy)
        elif format_name == "csv":
            text, truncated, metadata = _extract_csv(data, policy, deadline)
        elif format_name == "json":
            text, truncated, metadata = _extract_json(data, policy)
        elif format_name == "xml":
            text, truncated, metadata = _extract_xml(data, policy, deadline)
        elif format_name == "html":
            text, truncated, metadata = _extract_html(data, policy, deadline)
        elif format_name == "pdf":
            text, truncated, metadata = _extract_pdf(data, policy, deadline)
        elif format_name == "docx":
            text, truncated, metadata = _extract_docx(data, policy, deadline)
        elif format_name == "pptx":
            text, truncated, metadata = _extract_pptx(data, policy, deadline)
        elif format_name == "xlsx":
            text, truncated, metadata = _extract_xlsx(data, policy, deadline)
        else:
            return _result(
                ExtractionStatus.UNSUPPORTED,
                format_name,
                error="Attachment format is not supported for text extraction.",
            )
        metadata = {"input_bytes": len(data), **metadata}
        return _result(
            ExtractionStatus.SUCCESS,
            format_name,
            text=text,
            truncated=truncated,
            metadata=metadata,
        )
    except _ExtractionProblem as exc:
        return _result(exc.status, format_name, error=str(exc))
    except (MemoryError, RecursionError):
        return _result(
            ExtractionStatus.FAILED,
            format_name,
            error="Attachment structure exceeded safe extraction resources.",
        )
    except Exception as exc:
        return _result(
            ExtractionStatus.FAILED,
            format_name,
            error=f"Attachment text extraction failed safely ({type(exc).__name__}).",
        )


def _extract_plain_text(
    data: bytes, policy: ExtractionPolicy
) -> tuple[str, bool, dict[str, MetadataValue]]:
    decoded, encoding = _decode_text(data)
    output = _TextAccumulator(policy.max_output_chars)
    output.add(decoded, separator="")
    return output.render(), output.truncated, {"encoding": encoding}


def _extract_csv(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    decoded, encoding = _decode_text(data)
    output = _TextAccumulator(policy.max_output_chars)
    old_field_limit = csv.field_size_limit()
    csv.field_size_limit(min(policy.max_input_bytes, 1_000_000))
    row_count = 0
    try:
        reader = csv.reader(io.StringIO(decoded, newline=""))
        for row in reader:
            _check_deadline(deadline)
            if row_count >= policy.max_rows:
                output.truncated = True
                break
            row_count += 1
            if not output.add("\t".join(row)):
                break
    except csv.Error as exc:
        raise _InvalidDocument("CSV content could not be parsed safely.") from exc
    finally:
        csv.field_size_limit(old_field_limit)
    return output.render(), output.truncated, {"encoding": encoding, "rows": row_count}


def _extract_json(
    data: bytes, policy: ExtractionPolicy
) -> tuple[str, bool, dict[str, MetadataValue]]:
    try:
        parsed = json.loads(data, parse_constant=_reject_json_constant)
        rendered = json.dumps(parsed, ensure_ascii=False, indent=2, allow_nan=False)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _InvalidDocument("JSON content could not be parsed safely.") from exc
    output = _TextAccumulator(policy.max_output_chars)
    output.add(rendered, separator="")
    return output.render(), output.truncated, {"root_type": type(parsed).__name__}


def _extract_xml(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    if _contains_dtd(data):
        raise _UnsafeDocument("XML document declarations and entities are not processed.")
    try:
        root = ElementTree.fromstring(data)  # noqa: S314 - DTD/entity input is rejected above.
    except ElementTree.ParseError as exc:
        raise _InvalidDocument("XML content could not be parsed safely.") from exc
    output = _TextAccumulator(policy.max_output_chars)
    element_count = 0
    for element in root.iter():
        _check_deadline(deadline)
        element_count += 1
        if element.text and not output.add(element.text):
            break
        for value in element.attrib.values():
            if not output.add(value):
                break
        if element.tail and not output.add(element.tail):
            break
    return output.render(), output.truncated, {"elements": element_count}


def _extract_html(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    decoded, encoding = _decode_text(data)
    output = _TextAccumulator(policy.max_output_chars)
    parser = _SafeHTMLTextParser(output, deadline)
    try:
        parser.feed(decoded)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise _InvalidDocument("HTML content could not be parsed safely.") from exc
    return output.render(), output.truncated, {"encoding": encoding}


def _extract_pdf(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    if not data.lstrip().startswith(b"%PDF-"):
        raise _InvalidDocument("PDF content has an invalid file signature.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise _DependencyUnavailable(_dependency_message("pdf")) from exc

    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
    except Exception as exc:
        raise _InvalidDocument("PDF content could not be parsed safely.") from exc
    if reader.is_encrypted:
        raise _EncryptedDocument("Password-protected PDF files require explicit user action.")
    output = _TextAccumulator(policy.max_output_chars)
    pages_processed = 0
    for page in reader.pages:
        _check_deadline(deadline)
        if pages_processed >= policy.max_pages:
            output.truncated = True
            break
        pages_processed += 1
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise _InvalidDocument("A PDF page could not be parsed safely.") from exc
        if not output.add(page_text):
            break
    return output.render(), output.truncated, {"pages_processed": pages_processed}


def _extract_docx(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    inspection = _inspect_ooxml(data, "docx", policy)
    try:
        from docx import Document
    except ImportError as exc:
        raise _DependencyUnavailable(_dependency_message("docx")) from exc
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        raise _InvalidDocument("DOCX content could not be parsed safely.") from exc

    output = _TextAccumulator(policy.max_output_chars)
    paragraphs = 0
    for paragraph in document.paragraphs:
        _check_deadline(deadline)
        paragraphs += 1
        if not output.add(paragraph.text):
            break
    if not output.truncated:
        for table in document.tables:
            for row in table.rows:
                _check_deadline(deadline)
                if not output.add("\t".join(cell.text for cell in row.cells)):
                    break
            if output.truncated:
                break
    metadata: dict[str, MetadataValue] = {
        "paragraphs": paragraphs,
        "archive_members": inspection.members,
        "archive_uncompressed_bytes": inspection.uncompressed_bytes,
        "external_links_ignored": inspection.external_relationships,
    }
    return output.render(), output.truncated, metadata


def _extract_pptx(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    inspection = _inspect_ooxml(data, "pptx", policy)
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise _DependencyUnavailable(_dependency_message("pptx")) from exc
    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise _InvalidDocument("PPTX content could not be parsed safely.") from exc

    output = _TextAccumulator(policy.max_output_chars)
    slides_processed = 0
    shapes_processed = 0
    for slide in presentation.slides:
        _check_deadline(deadline)
        if slides_processed >= policy.max_pages:
            output.truncated = True
            break
        slides_processed += 1
        for shape in slide.shapes:
            _check_deadline(deadline)
            shapes_processed += 1
            if shapes_processed > policy.max_cells:
                output.truncated = True
                break
            text_value = getattr(shape, "text", None)
            if text_value and not output.add(text_value):
                break
        if output.truncated:
            break
    metadata: dict[str, MetadataValue] = {
        "slides_processed": slides_processed,
        "shapes_processed": shapes_processed,
        "archive_members": inspection.members,
        "archive_uncompressed_bytes": inspection.uncompressed_bytes,
        "external_links_ignored": inspection.external_relationships,
    }
    return output.render(), output.truncated, metadata


def _extract_xlsx(
    data: bytes,
    policy: ExtractionPolicy,
    deadline: float,
) -> tuple[str, bool, dict[str, MetadataValue]]:
    inspection = _inspect_ooxml(data, "xlsx", policy)
    try:
        load_workbook = importlib.import_module("openpyxl").load_workbook
    except ImportError as exc:
        raise _DependencyUnavailable(_dependency_message("xlsx")) from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise _InvalidDocument("XLSX content could not be parsed safely.") from exc

    output = _TextAccumulator(policy.max_output_chars)
    rows_processed = 0
    cells_processed = 0
    sheets_processed = 0
    try:
        for worksheet in workbook.worksheets:
            _check_deadline(deadline)
            sheets_processed += 1
            if not output.add(worksheet.title):
                break
            for row in worksheet.iter_rows(values_only=True):
                _check_deadline(deadline)
                if rows_processed >= policy.max_rows or cells_processed >= policy.max_cells:
                    output.truncated = True
                    break
                rows_processed += 1
                values: list[str] = []
                for cell in row:
                    cells_processed += 1
                    if cells_processed > policy.max_cells:
                        output.truncated = True
                        break
                    if cell is not None:
                        values.append(_spreadsheet_value(cell))
                if values and not output.add("\t".join(values)):
                    break
            if output.truncated:
                break
    finally:
        workbook.close()
    metadata: dict[str, MetadataValue] = {
        "sheets_processed": sheets_processed,
        "rows_processed": rows_processed,
        "cells_processed": min(cells_processed, policy.max_cells),
        "archive_members": inspection.members,
        "archive_uncompressed_bytes": inspection.uncompressed_bytes,
        "external_links_ignored": inspection.external_relationships,
    }
    return output.render(), output.truncated, metadata


def _inspect_ooxml(
    data: bytes,
    format_name: str,
    policy: ExtractionPolicy,
) -> _ArchiveInspection:
    if data.startswith(_OLE_COMPOUND_MAGIC):
        raise _EncryptedDocument(
            "Encrypted Office documents and legacy compound files require explicit user action."
        )
    if not data.startswith(b"PK"):
        raise _InvalidDocument("Office document has an invalid file signature.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > policy.max_archive_members:
                raise _UnsafeDocument("Office document contains too many archive members.")
            total_uncompressed = 0
            total_compressed = 0
            names: set[str] = set()
            expected_part_found = False
            external_relationships = 0
            for info in infos:
                normalized = info.filename.replace("\\", "/")
                folded = normalized.casefold()
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts or "\x00" in normalized:
                    raise _UnsafeDocument("Office document contains an unsafe archive path.")
                if folded in names:
                    raise _UnsafeDocument("Office document contains duplicate archive members.")
                names.add(folded)
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == stat.S_IFLNK:
                    raise _UnsafeDocument("Office document contains an archive link.")
                if info.flag_bits & 0x1:
                    raise _EncryptedDocument(
                        "Password-protected Office documents require explicit user action."
                    )
                if any(marker in f"/{folded}" for marker in _ACTIVE_ARCHIVE_PARTS):
                    raise _UnsafeDocument(
                        "Office document contains macros or active embedded content."
                    )
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > policy.max_archive_uncompressed_bytes:
                    raise _UnsafeDocument(
                        "Office document expands beyond the configured archive size limit."
                    )
                if info.file_size and not info.compress_size:
                    raise _UnsafeDocument("Office document has an unsafe compression ratio.")
                if info.compress_size and (
                    info.file_size / info.compress_size > policy.max_compression_ratio
                ):
                    raise _UnsafeDocument("Office document has an unsafe compression ratio.")
                if folded == _EXPECTED_OOXML_PART[format_name]:
                    expected_part_found = True
                if folded.endswith((".xml", ".rels")) and info.file_size:
                    member = archive.read(info)
                    if _contains_dtd(member):
                        raise _UnsafeDocument("Office document contains an unsafe XML declaration.")
                    if folded.endswith(".rels"):
                        external_relationships += len(_EXTERNAL_REL_RE.findall(member))
            if total_uncompressed and (
                total_uncompressed / max(total_compressed, 1) > policy.max_compression_ratio
            ):
                raise _UnsafeDocument("Office document has an unsafe compression ratio.")
            if not expected_part_found:
                raise _InvalidDocument("Office document does not match its declared format.")
    except zipfile.BadZipFile as exc:
        raise _InvalidDocument("Office document archive could not be parsed safely.") from exc
    return _ArchiveInspection(
        members=len(infos),
        uncompressed_bytes=total_uncompressed,
        external_relationships=external_relationships,
    )


def _decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        candidates: tuple[str, ...] = ("utf-8-sig",)
    elif data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        candidates = ("utf-32",)
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        candidates = ("utf-8", "cp1252")
    for encoding in candidates:
        try:
            decoded = data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if "\x00" in decoded and not encoding.startswith(("utf-16", "utf-32")):
            raise _InvalidDocument("Text attachment appears to contain binary data.")
        return decoded, encoding
    raise _InvalidDocument("Text attachment uses an unsupported character encoding.")


def _sanitize_text(value: str) -> str:
    value = _ANSI_ESCAPE_RE.sub("", value)
    value = _CONTROL_RE.sub("", value)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _contains_dtd(data: bytes) -> bool:
    # Removing NULs also catches UTF-16/32 spellings of declaration keywords.
    return bool(_DTD_RE.search(data.replace(b"\x00", b"")))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _spreadsheet_value(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise _ExtractionTimedOut("Attachment text extraction exceeded the configured time limit.")


def _filename_suffix(filename: str) -> str:
    leaf = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    return Path(leaf).suffix.casefold()


def _normalize_media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.partition(";")[0].strip().casefold() or None


def _detect_format(filename: str, content_type: str | None) -> str | None:
    suffix_format = _FORMAT_BY_SUFFIX.get(_filename_suffix(filename))
    if suffix_format:
        return suffix_format
    return _FORMAT_BY_MEDIA_TYPE.get(content_type or "")


def _dependency_message(format_name: str) -> str:
    dependency = _DEPENDENCY_BY_FORMAT[format_name]
    return (
        f"{format_name.upper()} extraction requires the optional {dependency} dependency "
        "from the attachments extra."
    )


def _result(
    status: ExtractionStatus,
    format_name: str | None,
    *,
    text: str = "",
    error: str | None = None,
    truncated: bool = False,
    metadata: Mapping[str, MetadataValue] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        status=status,
        format=format_name,
        text=text,
        error=error,
        truncated=truncated,
        char_count=len(text),
        label=f"extracted_attachment_text:{format_name}" if format_name else "attachment_text",
        metadata=dict(metadata or {}),
    )


__all__ = [
    "CORE_FORMATS",
    "OPTIONAL_FORMATS",
    "SUPPORTED_FORMATS",
    "AttachmentTextExtractor",
    "ExtractionPolicy",
    "ExtractionResult",
    "ExtractionStatus",
    "SafeAttachmentExtractor",
    "extract_attachment_text",
    "extract_text",
]
