"""Explicitly enabled, bounded mailbox attachment text for the disposable index."""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO, Protocol

from gucmail.errors import ConfigurationError
from gucmail.models import AttachmentInfo, MessageDetail, MessageSummary
from gucmail.security import safe_filename, sanitize_terminal
from gucmail.services.extractors import (
    CORE_FORMATS,
    SUPPORTED_FORMATS,
    AttachmentTextExtractor,
    ExtractionPolicy,
)
from gucmail.services.index import IndexDocument, IndexRebuildCancelled
from gucmail.services.message_scan import scan_message_summaries
from gucmail.services.ocr import OcrService

IMAGE_FORMATS = frozenset({"png", "jpg"})
INDEX_FORMATS = SUPPORTED_FORMATS | IMAGE_FORMATS
ATTACHMENT_INDEX_SETTINGS = (
    "search.index_attachments",
    "search.attachment_formats",
    "search.attachment_max_bytes",
    "search.attachment_timeout",
    "search.attachment_files_per_message",
    "search.attachment_files_per_run",
    "search.attachment_total_bytes",
    "search.attachment_output_chars",
    "search.attachment_ocr_enabled",
    "search.attachment_ocr_language",
)


def attachment_index_preferences(source: Any) -> AttachmentIndexPreferences:
    snapshot = source.snapshot(ATTACHMENT_INDEX_SETTINGS)
    return AttachmentIndexPreferences.from_values(
        {v.spec.setting_id: v.effective_value for v in snapshot.values}
    )


def normalize_formats(value: Sequence[str]) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) > len(INDEX_FORMATS) + 2
    ):
        raise ConfigurationError("Choose a bounded list of supported attachment formats.")
    normalized = tuple(
        sorted({_canonical_format(str(item).strip().casefold().lstrip(".")) for item in value})
    )
    if set(normalized) - INDEX_FORMATS:
        raise ConfigurationError(
            "Supported attachment index formats: " + ", ".join(sorted(INDEX_FORMATS))
        )
    return normalized


def _canonical_format(value: str) -> str:
    return {"jpeg": "jpg", "htm": "html"}.get(value, value)


def normalize_language(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){1,3}", value
    ):
        raise ConfigurationError("Choose an OCR language tag, such as en-US or ar-SA.")
    return value


@dataclass(frozen=True, slots=True)
class AttachmentIndexPreferences:
    enabled: bool = False
    formats: tuple[str, ...] = tuple(sorted(CORE_FORMATS))
    max_input_bytes: int = 5 * 1024 * 1024
    timeout_seconds: float = 8.0
    files_per_message: int = 5
    files_per_run: int = 25
    total_bytes: int = 20 * 1024 * 1024
    output_chars: int = 50_000
    ocr_enabled: bool = False
    ocr_language: str = "en-US"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.ocr_enabled, bool):
            raise ConfigurationError("Attachment indexing and OCR consent must be boolean.")
        object.__setattr__(self, "formats", normalize_formats(self.formats))
        normalize_language(self.ocr_language)
        for name, ceiling in {
            "max_input_bytes": 10 * 1024 * 1024,
            "files_per_message": 20,
            "files_per_run": 100,
            "total_bytes": 100 * 1024 * 1024,
            "output_chars": 100_000,
        }.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ConfigurationError(f"{name} must be between 1 and {ceiling}.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 1 <= self.timeout_seconds <= 15
        ):
            raise ConfigurationError(
                "Attachment processing timeout must be between 1 and 15 seconds."
            )

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> AttachmentIndexPreferences:
        return cls(
            **dict(
                zip(
                    (
                        "enabled",
                        "formats",
                        "max_input_bytes",
                        "timeout_seconds",
                        "files_per_message",
                        "files_per_run",
                        "total_bytes",
                        "output_chars",
                        "ocr_enabled",
                        "ocr_language",
                    ),
                    (values[k] for k in ATTACHMENT_INDEX_SETTINGS),
                    strict=True,
                )
            )
        )

    @property
    def fingerprint(self) -> str:
        import json

        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def review(self) -> str:
        if not self.enabled:
            return "Attachment content indexing is disabled; attachment content is not downloaded."
        return (
            "Attachment content indexing is enabled for "
            f"{', '.join(self.formats) or 'no formats'}. "
            f"At most {self.files_per_message} files per message and {self.files_per_run} per run; "
            f"{self.max_input_bytes} bytes per file and "
            f"{self.total_bytes} downloaded bytes per run. "
            f"Local parser timeout: {self.timeout_seconds:g}s. "
            f"At most {self.output_chars} extracted characters retained per message. "
            + (
                f"Image OCR is explicitly enabled in {self.ocr_language}; "
                "recognized text may be inaccurate. "
                if self.ocr_enabled
                else "Image OCR is disabled. "
            )
            + "Extracted text and labelled outcomes are private local index data. "
            "No attachment is opened or executed."
        )


class IndexGateway(Protocol):
    def list_messages(self, **kwargs: Any) -> Sequence[MessageSummary]: ...
    def show_message(self, reference: str) -> MessageDetail: ...
    def list_attachments(
        self, reference: str, limit: int = 100, offset: int = 0
    ) -> Sequence[AttachmentInfo]: ...
    def open_attachment(
        self, reference: str, attachment: str
    ) -> BinaryIO | contextlib.AbstractContextManager[BinaryIO]: ...


@dataclass(slots=True)
class IndexProducerReport:
    documents: int = 0
    files_downloaded: int = 0
    bytes_downloaded: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)

    def summary(self) -> dict[str, object]:
        return {
            "documents": self.documents,
            "files_downloaded": self.files_downloaded,
            "bytes_downloaded": self.bytes_downloaded,
            "outcomes": dict(self.outcomes),
        }


class MailboxIndexProducer:
    def __init__(
        self,
        gateway: IndexGateway,
        preferences: AttachmentIndexPreferences,
        *,
        cancel_event: Event | None = None,
        extractor: AttachmentTextExtractor | None = None,
        ocr_service: OcrService | None = None,
        temporary_directory: Path | None = None,
    ) -> None:
        self.gateway = gateway
        self.preferences = preferences
        self.cancel_event = cancel_event or Event()
        self.extractor = extractor or AttachmentTextExtractor(
            ExtractionPolicy(
                enabled_formats=frozenset(preferences.formats) & SUPPORTED_FORMATS,
                max_input_bytes=preferences.max_input_bytes,
                max_output_chars=preferences.output_chars,
                timeout_seconds=preferences.timeout_seconds,
            )
        )
        self.ocr_service = ocr_service
        self.temporary_directory = temporary_directory
        self.report = IndexProducerReport()

    def _cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise IndexRebuildCancelled(
                "Index construction was cancelled; no further attachment is fetched."
            )

    def documents(
        self,
        *,
        folder: str,
        limit: int,
        include_bodies: bool = False,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[IndexDocument]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ConfigurationError("Index page limit must be between 1 and 500.")
        self._cancelled()
        summaries = scan_message_summaries(self.gateway, folder=folder, limit=limit)
        for position, summary in enumerate(summaries):
            if position >= limit:
                break
            self._cancelled()
            value = self.gateway.show_message(summary.reference) if include_bodies else summary
            document = IndexDocument.from_value(value)
            if self.preferences.enabled and summary.has_attachments:
                document = self._attachments(document)
            self._cancelled()
            self.report.documents += 1
            if progress is not None:
                progress(self.report.documents)
            yield document

    def _attachments(self, document: IndexDocument) -> IndexDocument:
        policy = self.preferences
        metadata = self.gateway.list_attachments(
            document.reference, limit=policy.files_per_message + 1
        )
        records: list[dict[str, object]] = []
        texts: list[str] = []
        names: list[str] = []
        for position, info in enumerate(metadata):
            self._cancelled()
            if position > policy.files_per_message:
                break
            name = sanitize_terminal(safe_filename(info.name), max_length=240)
            names.append(name)
            format_name = _canonical_format(Path(name).suffix.casefold().lstrip("."))
            record: dict[str, object] = {
                "reference": sanitize_terminal(info.reference, max_length=300),
                "name": name,
                "format": format_name,
                "label": "extracted_attachment_text",
                "status": "unsupported",
            }
            if position == policy.files_per_message:
                record["status"] = "message_limit"
            elif info.is_item_attachment or info.dangerous:
                record["status"] = "unsafe"
            elif format_name not in policy.formats:
                record["status"] = "disabled" if format_name in INDEX_FORMATS else "unsupported"
            elif format_name in IMAGE_FORMATS and not policy.ocr_enabled:
                record["status"] = "ocr_disabled"
            elif info.size is not None and (
                info.size < 0
                or info.size
                > min(policy.max_input_bytes, policy.total_bytes - self.report.bytes_downloaded)
            ):
                record["status"] = "too_large"
            elif (
                self.report.files_downloaded >= policy.files_per_run
                or self.report.bytes_downloaded >= policy.total_bytes - 1
            ):
                record["status"] = "run_limit"
            else:
                # Reserve the size probe byte inside the total download budget.
                remaining = min(
                    policy.max_input_bytes, policy.total_bytes - self.report.bytes_downloaded - 1
                )
                content = self._read(document.reference, info.reference, remaining)
                if len(content) > remaining:
                    record["status"] = "too_large"
                else:
                    text, result = self._extract(name, format_name, content)
                    record.update(result)
                    if text:
                        texts.append(text)
            self.report.outcomes[str(record["status"])] += 1
            records.append(record)
        payload = dict(document.payload or {})
        payload["attachment_extractions"] = records
        text = "\n".join(texts)
        payload["attachment_text_truncated"] = len(text) > policy.output_chars
        return replace(
            document,
            attachment_names=" ".join(names),
            attachment_text=text[: policy.output_chars],
            payload=payload,
        )

    def _read(self, message: str, attachment: str, maximum: int) -> bytes:
        self._cancelled()
        self.report.files_downloaded += 1
        raw = self.gateway.open_attachment(message, attachment)
        chunks: list[bytes] = []
        total = 0
        manager = (
            raw if isinstance(raw, contextlib.AbstractContextManager) else contextlib.closing(raw)
        )
        with manager as stream:
            while total <= maximum:
                self._cancelled()
                chunk = stream.read(min(64 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ConfigurationError("Attachment stream returned invalid byte content.")
                if len(chunk) > maximum + 1 - total:
                    raise ConfigurationError(
                        "Attachment stream exceeded the requested bounded read."
                    )
                chunks.append(chunk)
                total += len(chunk)
                self.report.bytes_downloaded += len(chunk)
        self._cancelled()
        return b"".join(chunks)

    def _extract(
        self, name: str, format_name: str, content: bytes
    ) -> tuple[str, dict[str, object]]:
        if format_name not in IMAGE_FORMATS:
            result = self.extractor.extract(name, content, cancel_event=self.cancel_event)
            self._cancelled()
            return result.text, {
                "status": result.status.value,
                "label": result.label,
                "truncated": result.truncated,
                "chars": result.char_count,
            }
        policy = self.preferences
        service = self.ocr_service or OcrService()
        try:
            with tempfile.TemporaryDirectory(
                prefix="gucmail-index-ocr-", dir=self.temporary_directory
            ) as directory:
                path = Path(directory) / ("image." + format_name)
                path.write_bytes(content)
                self._cancelled()
                plan = service.preview(
                    path,
                    language=policy.ocr_language,
                    timeout_seconds=policy.timeout_seconds,
                    max_output_chars=policy.output_chars,
                    cancel_event=self.cancel_event,
                )
                recognized = service.extract(plan, enabled=True, cancel_event=self.cancel_event)
                self._cancelled()
                return recognized.text, {
                    "status": recognized.status,
                    "label": "ocr_attachment_text",
                    "language": policy.ocr_language,
                    "potentially_inaccurate": True,
                    "truncated": recognized.truncated,
                    "chars": len(recognized.text),
                }
        except ConfigurationError:
            self._cancelled()
            return "", {
                "status": "ocr_unavailable_or_invalid",
                "label": "ocr_attachment_text",
                "language": policy.ocr_language,
                "potentially_inaccurate": True,
            }
