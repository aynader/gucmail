"""Bounded, local-only mailbox storage, sender, and duplicate insights.

The functions in this module deliberately operate on Exchange-independent domain
objects or plain mappings.  They never make network requests and never mutate a
mailbox.  Input is capped so an accidental iterator over a very large backup cannot
consume unbounded memory.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from rapidfuzz import fuzz

DEFAULT_MAX_ITEMS = 50_000
DEFAULT_LARGEST_LIMIT = 20
DEFAULT_NEAR_DUPLICATE_COMPARISONS = 50_000
_SPACE_RE = re.compile(r"\s+")
_SUBJECT_PREFIX_RE = re.compile(r"(?i)^\s*(?:(?:re|fw|fwd)\s*:\s*)+")


@dataclass(frozen=True, slots=True)
class InsightMessage:
    """Small normalized record used by the local analyzers."""

    reference: str
    subject: str = ""
    sender: str = ""
    received_at: datetime | None = None
    folder_path: str = ""
    size_bytes: int = 0
    is_read: bool = True
    has_attachments: bool = False
    body: str = ""
    attachment_names: tuple[str, ...] = ()
    attachment_bytes: int = 0
    internet_message_id: str | None = None
    content_sha256: str | None = None
    mime_sha256: str | None = None

    @classmethod
    def from_value(cls, value: object) -> InsightMessage:
        """Coerce a domain message, dataclass, or mapping without retaining it."""

        raw = _mapping(value)
        summary = _mapping(raw.get("summary")) if raw.get("summary") is not None else {}

        def choose(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in raw and raw[name] is not None:
                    return raw[name]
                if name in summary and summary[name] is not None:
                    return summary[name]
            return default

        reference = str(choose("reference", "id", "item_reference", default="")).strip()
        if not reference:
            raise ValueError("Every insight message must have a non-empty reference.")

        sender = _address_text(choose("sender", "from", "from_address", default=""))
        attachments = choose("attachments", default=()) or ()
        attachment_names: list[str] = []
        attachment_bytes = _non_negative_int(choose("attachment_bytes", default=0))
        for attachment in attachments if isinstance(attachments, Iterable) else ():
            item = _mapping(attachment)
            name = item.get("name") or item.get("filename")
            if name:
                attachment_names.append(str(name))
            attachment_bytes += _non_negative_int(item.get("size", item.get("size_bytes", 0)))
        explicit_names = choose("attachment_names", "attachments_names", default=()) or ()
        if isinstance(explicit_names, str):
            attachment_names.append(explicit_names)
        elif isinstance(explicit_names, Iterable):
            attachment_names.extend(str(name) for name in explicit_names if name)

        return cls(
            reference=reference,
            subject=str(choose("subject", default="") or ""),
            sender=sender,
            received_at=_datetime(choose("received_at", "sent_at", "date")),
            folder_path=str(choose("folder_path", "folder", default="") or ""),
            size_bytes=_non_negative_int(choose("size_bytes", "size", default=0)),
            is_read=bool(choose("is_read", "read", default=True)),
            has_attachments=bool(
                choose(
                    "has_attachments",
                    default=bool(attachment_names or attachments),
                )
            ),
            body=str(choose("body", "body_text", "preview", default="") or ""),
            attachment_names=tuple(dict.fromkeys(attachment_names)),
            attachment_bytes=attachment_bytes,
            internet_message_id=(
                str(message_id)
                if (message_id := choose("internet_message_id", "message_id"))
                else None
            ),
            content_sha256=_sha256_digest(
                choose("content_sha256", "body_sha256", "content_digest")
            ),
            mime_sha256=_sha256_digest(choose("mime_sha256", "raw_mime_sha256", "mime_digest")),
        )


@dataclass(frozen=True, slots=True)
class StorageBucket:
    label: str
    item_count: int
    total_bytes: int
    unread_count: int = 0
    attachment_bytes: int = 0


@dataclass(frozen=True, slots=True)
class LargestItem:
    reference: str
    subject: str
    sender: str
    folder_path: str
    size_bytes: int
    received_at: datetime | None


@dataclass(frozen=True, slots=True)
class StorageReport:
    scanned_count: int
    truncated: bool
    total_bytes: int
    attachment_bytes: int
    largest_messages: tuple[LargestItem, ...]
    by_folder: tuple[StorageBucket, ...]
    by_sender: tuple[StorageBucket, ...]
    by_period: tuple[StorageBucket, ...]
    by_attachment_type: tuple[StorageBucket, ...]


@dataclass(frozen=True, slots=True)
class SenderInsight:
    sender: str
    message_count: int
    unread_count: int
    total_bytes: int
    attachment_count: int
    first_message_at: datetime | None
    last_message_at: datetime | None
    folders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SenderReport:
    scanned_count: int
    truncated: bool
    senders: tuple[SenderInsight, ...]


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """A set of exact or approximate duplicate messages."""

    kind: str
    references: tuple[str, ...]
    digest: str
    representative_subject: str
    total_bytes: int
    reclaimable_bytes: int
    similarity: float = 100.0
    explanation: str = "Normalized message content is identical."


@dataclass(frozen=True, slots=True)
class DuplicateAttachment:
    digest: str
    names: tuple[str, ...]
    message_references: tuple[str, ...]
    copies: int
    total_bytes: int
    reclaimable_bytes: int


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    scanned_count: int
    truncated: bool
    exact_groups: tuple[DuplicateGroup, ...]
    near_groups: tuple[DuplicateGroup, ...] = ()
    attachment_groups: tuple[DuplicateAttachment, ...] = ()
    comparisons: int = 0
    possible_groups: tuple[DuplicateGroup, ...] = ()

    @property
    def estimated_reclaimable_bytes(self) -> int:
        return sum(group.reclaimable_bytes for group in self.exact_groups) + sum(
            group.reclaimable_bytes for group in self.attachment_groups
        )


@dataclass(frozen=True, slots=True)
class MailboxInsightReport:
    storage: StorageReport
    senders: SenderReport
    duplicates: DuplicateReport


def storage_insights(
    messages: Iterable[object],
    *,
    largest_limit: int = DEFAULT_LARGEST_LIMIT,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> StorageReport:
    """Calculate deterministic storage summaries from at most ``max_items`` messages."""

    records, truncated = _bounded_messages(messages, max_items=max_items)
    largest_limit = _bounded_limit(largest_limit, maximum=1_000)
    folder_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    sender_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    period_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    type_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])

    for item in records:
        _add_storage(folder_stats[item.folder_path or "(unknown folder)"], item)
        _add_storage(sender_stats[item.sender or "(unknown sender)"], item)
        period = item.received_at.strftime("%Y-%m") if item.received_at else "(unknown date)"
        _add_storage(period_stats[period], item)
        if item.attachment_names:
            per_file = item.attachment_bytes // max(len(item.attachment_names), 1)
            for name in item.attachment_names:
                suffix = _attachment_type(name)
                values = type_stats[suffix]
                values[0] += 1
                values[1] += per_file
                values[3] += per_file

    largest = sorted(
        records,
        key=lambda item: (-item.size_bytes, _date_key(item.received_at), item.reference),
    )[:largest_limit]
    return StorageReport(
        scanned_count=len(records),
        truncated=truncated,
        total_bytes=sum(item.size_bytes for item in records),
        attachment_bytes=sum(item.attachment_bytes for item in records),
        largest_messages=tuple(
            LargestItem(
                reference=item.reference,
                subject=item.subject,
                sender=item.sender,
                folder_path=item.folder_path,
                size_bytes=item.size_bytes,
                received_at=item.received_at,
            )
            for item in largest
        ),
        by_folder=_buckets(folder_stats),
        by_sender=_buckets(sender_stats),
        by_period=_buckets(period_stats, chronological=True),
        by_attachment_type=_buckets(type_stats),
    )


def sender_insights(
    messages: Iterable[object],
    *,
    limit: int = 100,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> SenderReport:
    """Group message volume and storage by normalized sender address."""

    records, truncated = _bounded_messages(messages, max_items=max_items)
    limit = _bounded_limit(limit, maximum=10_000)
    grouped: dict[str, list[InsightMessage]] = defaultdict(list)
    for item in records:
        grouped[item.sender or "(unknown sender)"].append(item)
    output: list[SenderInsight] = []
    for sender, items in grouped.items():
        dates = [item.received_at for item in items if item.received_at is not None]
        output.append(
            SenderInsight(
                sender=sender,
                message_count=len(items),
                unread_count=sum(not item.is_read for item in items),
                total_bytes=sum(item.size_bytes for item in items),
                attachment_count=sum(len(item.attachment_names) for item in items),
                first_message_at=min(dates) if dates else None,
                last_message_at=max(dates) if dates else None,
                folders=tuple(
                    sorted(
                        {item.folder_path for item in items if item.folder_path},
                        key=str.casefold,
                    )
                ),
            )
        )
    output.sort(key=lambda item: (-item.total_bytes, -item.message_count, item.sender.casefold()))
    return SenderReport(len(records), truncated, tuple(output[:limit]))


def duplicate_insights(
    messages: Iterable[object],
    *,
    include_near: bool = False,
    similarity_threshold: int = 92,
    max_items: int = 10_000,
    max_pair_comparisons: int = DEFAULT_NEAR_DUPLICATE_COMPARISONS,
) -> DuplicateReport:
    """Find exact duplicates and optionally bounded near-duplicates.

    Exact detection requires a caller-supplied SHA-256 digest of complete content or
    raw MIME.  Normalized subject/body/attachment metadata is useful evidence but is
    not proof: matching shallow records are reported as ``possible_groups`` and are
    intentionally excluded from executable cleanup plans.  Near-duplicate comparison
    is deliberately bounded and only compares messages sharing a coarse subject token,
    preventing quadratic work on a large mailbox.
    """

    records, truncated = _bounded_messages(messages, max_items=max_items)
    similarity_threshold = max(0, min(int(similarity_threshold), 100))
    max_pair_comparisons = max(0, min(int(max_pair_comparisons), 1_000_000))
    exact: dict[str, list[InsightMessage]] = defaultdict(list)
    shallow: dict[str, list[InsightMessage]] = defaultdict(list)
    normalized: dict[str, str] = {}
    for item in records:
        canonical = _canonical_content(item)
        normalized[item.reference] = canonical
        shallow[hashlib.sha256(canonical.encode("utf-8")).hexdigest()].append(item)
        if strong_key := _strong_content_key(item):
            exact[hashlib.sha256(strong_key.encode("ascii")).hexdigest()].append(item)

    exact_groups = tuple(
        sorted(
            (
                _duplicate_group(
                    "exact",
                    digest,
                    items,
                    explanation="A complete content or raw-MIME SHA-256 digest is identical.",
                )
                for digest, items in exact.items()
                if len(items) > 1
            ),
            key=lambda group: (-group.reclaimable_bytes, group.references),
        )
    )
    exact_references = {reference for group in exact_groups for reference in group.references}
    possible_groups = tuple(
        sorted(
            (
                _duplicate_group(
                    "possible",
                    digest,
                    items,
                    explanation=(
                        "Normalized subject/body/attachment metadata matches, but no "
                        "identical complete-content or raw-MIME digest proves duplication."
                    ),
                )
                for digest, items in shallow.items()
                if len(items) > 1 and not all(item.reference in exact_references for item in items)
            ),
            key=lambda group: (-group.similarity, -group.reclaimable_bytes, group.references),
        )
    )
    possible_references = {reference for group in possible_groups for reference in group.references}

    near_groups: list[DuplicateGroup] = []
    comparisons = 0
    if include_near and max_pair_comparisons:
        coarse: dict[str, list[InsightMessage]] = defaultdict(list)
        for item in records:
            if item.reference not in exact_references | possible_references:
                coarse[_coarse_key(item)].append(item)
        paired: set[str] = set()
        for bucket_name in sorted(coarse):
            bucket = sorted(coarse[bucket_name], key=lambda item: item.reference)
            for left_index, left in enumerate(bucket):
                if left.reference in paired:
                    continue
                matches = [left]
                for right in bucket[left_index + 1 :]:
                    if comparisons >= max_pair_comparisons:
                        break
                    comparisons += 1
                    if right.reference in paired:
                        continue
                    score = float(
                        fuzz.ratio(normalized[left.reference], normalized[right.reference])
                    )
                    if score >= similarity_threshold:
                        matches.append(right)
                if len(matches) > 1:
                    paired.update(item.reference for item in matches)
                    scores = [
                        float(
                            fuzz.ratio(
                                normalized[matches[0].reference],
                                normalized[item.reference],
                            )
                        )
                        for item in matches[1:]
                    ]
                    digest = hashlib.sha256(
                        "\0".join(item.reference for item in matches).encode("utf-8")
                    ).hexdigest()
                    near_groups.append(
                        _duplicate_group(
                            "near",
                            digest,
                            matches,
                            similarity=min(scores),
                            explanation=(
                                f"Normalized content similarity is at least {min(scores):.1f}%."
                            ),
                        )
                    )
                if comparisons >= max_pair_comparisons:
                    break
            if comparisons >= max_pair_comparisons:
                break

    near_groups.sort(
        key=lambda group: (-group.similarity, -group.reclaimable_bytes, group.references)
    )
    return DuplicateReport(
        scanned_count=len(records),
        truncated=truncated,
        exact_groups=exact_groups,
        near_groups=tuple(near_groups),
        comparisons=comparisons,
        possible_groups=possible_groups,
    )


def duplicate_attachment_insights(
    attachments: Iterable[object],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> tuple[DuplicateAttachment, ...]:
    """Group attachment metadata that already contains a trustworthy SHA-256 digest.

    Content is never downloaded or opened.  Entries without a supplied digest are
    skipped, because filename and size alone are not proof of duplication.
    """

    if max_items < 1:
        raise ValueError("max_items must be at least 1.")
    grouped: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for position, value in enumerate(attachments):
        if position >= max_items:
            break
        item = _mapping(value)
        digest = str(item.get("sha256") or item.get("digest") or "").lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        grouped[digest].append(
            (
                str(item.get("message_reference") or item.get("reference") or ""),
                str(item.get("name") or item.get("filename") or "attachment"),
                _non_negative_int(item.get("size_bytes", item.get("size", 0))),
            )
        )
    groups: list[DuplicateAttachment] = []
    for digest, copies in grouped.items():
        if len(copies) < 2:
            continue
        sizes = [copy[2] for copy in copies]
        groups.append(
            DuplicateAttachment(
                digest=digest,
                names=tuple(copy[1] for copy in copies),
                message_references=tuple(copy[0] for copy in copies),
                copies=len(copies),
                total_bytes=sum(sizes),
                reclaimable_bytes=sum(sizes) - min(sizes),
            )
        )
    return tuple(sorted(groups, key=lambda group: (-group.reclaimable_bytes, group.digest)))


def analyze_mailbox(
    messages: Iterable[object],
    *,
    include_near_duplicates: bool = False,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> MailboxInsightReport:
    """Create all core reports with a single bounded materialization of input."""

    records, _ = _bounded_messages(messages, max_items=max_items)
    return MailboxInsightReport(
        storage=storage_insights(records, max_items=max_items),
        senders=sender_insights(records, max_items=max_items),
        duplicates=duplicate_insights(
            records,
            include_near=include_near_duplicates,
            max_items=max_items,
        ),
    )


class InsightAnalyzer:
    """Configurable facade for presentation layers and command handlers."""

    def __init__(self, *, max_items: int = DEFAULT_MAX_ITEMS) -> None:
        if max_items < 1:
            raise ValueError("max_items must be at least 1.")
        self.max_items = max_items

    def storage(self, messages: Iterable[object], *, largest_limit: int = 20) -> StorageReport:
        return storage_insights(messages, largest_limit=largest_limit, max_items=self.max_items)

    def senders(self, messages: Iterable[object], *, limit: int = 100) -> SenderReport:
        return sender_insights(messages, limit=limit, max_items=self.max_items)

    def duplicates(
        self,
        messages: Iterable[object],
        *,
        include_near: bool = False,
        similarity_threshold: int = 92,
    ) -> DuplicateReport:
        return duplicate_insights(
            messages,
            include_near=include_near,
            similarity_threshold=similarity_threshold,
            max_items=self.max_items,
        )


# Compatibility names kept intentionally simple for CLI and TUI callers.
analyze_storage = storage_insights
analyze_senders = sender_insights
find_duplicate_messages = duplicate_insights
find_duplicate_attachments = duplicate_attachment_insights


def _mapping(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    data: dict[str, Any] = {}
    for name in (
        "reference",
        "id",
        "subject",
        "sender",
        "received_at",
        "sent_at",
        "folder_path",
        "size",
        "size_bytes",
        "is_read",
        "has_attachments",
        "preview",
        "body",
        "body_text",
        "attachments",
        "attachment_names",
        "internet_message_id",
        "summary",
        "name",
        "filename",
        "sha256",
        "digest",
        "message_reference",
    ):
        if hasattr(value, name):
            data[name] = getattr(value, name)
    return data


def _address_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        email = value.get("email") or value.get("address") or ""
        name = value.get("name") or value.get("display_name") or ""
        return str(email or name).strip().casefold()
    email = getattr(value, "email", None)
    if email:
        return str(email).strip().casefold()
    _, parsed = parseaddr(str(value))
    return (parsed or str(value)).strip().casefold()


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(str(value or 0).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0


def _sha256_digest(value: object) -> str | None:
    """Return only explicitly supplied, syntactically valid SHA-256 evidence."""

    if value is None:
        return None
    digest = str(value).strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _bounded_messages(
    messages: Iterable[object], *, max_items: int
) -> tuple[list[InsightMessage], bool]:
    if max_items < 1:
        raise ValueError("max_items must be at least 1.")
    records: list[InsightMessage] = []
    truncated = False
    for position, value in enumerate(messages):
        if position >= max_items:
            truncated = True
            break
        records.append(
            value if isinstance(value, InsightMessage) else InsightMessage.from_value(value)
        )
    return records, truncated


def _bounded_limit(value: int, *, maximum: int) -> int:
    if value < 0:
        raise ValueError("limit may not be negative.")
    return min(int(value), maximum)


def _add_storage(values: list[int], item: InsightMessage) -> None:
    values[0] += 1
    values[1] += item.size_bytes
    values[2] += int(not item.is_read)
    values[3] += item.attachment_bytes


def _buckets(
    grouped: Mapping[str, Sequence[int]], *, chronological: bool = False
) -> tuple[StorageBucket, ...]:
    buckets = [
        StorageBucket(
            label=label,
            item_count=values[0],
            total_bytes=values[1],
            unread_count=values[2],
            attachment_bytes=values[3],
        )
        for label, values in grouped.items()
    ]
    if chronological:
        buckets.sort(key=lambda item: item.label)
    else:
        buckets.sort(key=lambda item: (-item.total_bytes, item.label.casefold()))
    return tuple(buckets)


def _attachment_type(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else "(no extension)"
    return f".{suffix}" if suffix != "(no extension)" else suffix


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = _SPACE_RE.sub(" ", value)
    return value.strip()


def _canonical_content(item: InsightMessage) -> str:
    subject = _normalize_text(_SUBJECT_PREFIX_RE.sub("", item.subject))
    body = _normalize_text(item.body)
    attachments = "\x1f".join(sorted(_normalize_text(name) for name in item.attachment_names))
    return "\x1e".join((subject, body, attachments))


def _strong_content_key(item: InsightMessage) -> str | None:
    if item.mime_sha256:
        return f"mime:{item.mime_sha256}"
    if item.content_sha256:
        return f"content:{item.content_sha256}"
    return None


def _coarse_key(item: InsightMessage) -> str:
    subject = _normalize_text(_SUBJECT_PREFIX_RE.sub("", item.subject))
    tokens = re.findall(r"\w+", subject, flags=re.UNICODE)
    return " ".join(tokens[:3]) or "(no subject)"


def _duplicate_group(
    kind: str,
    digest: str,
    items: Sequence[InsightMessage],
    *,
    similarity: float = 100.0,
    explanation: str = "Normalized message content is identical.",
) -> DuplicateGroup:
    ordered = sorted(items, key=lambda item: (_date_key(item.received_at), item.reference))
    sizes = [item.size_bytes for item in ordered]
    return DuplicateGroup(
        kind=kind,
        references=tuple(item.reference for item in ordered),
        digest=digest,
        representative_subject=ordered[0].subject,
        total_bytes=sum(sizes),
        reclaimable_bytes=sum(sizes[1:]),
        similarity=similarity,
        explanation=explanation,
    )


def _date_key(value: datetime | None) -> tuple[int, int, int, int, int, int, int]:
    if value is None:
        return (0, 0, 0, 0, 0, 0, 0)
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return (
        normalized.year,
        normalized.month,
        normalized.day,
        normalized.hour,
        normalized.minute,
        normalized.second,
        normalized.microsecond,
    )


__all__ = [
    "DEFAULT_MAX_ITEMS",
    "DuplicateAttachment",
    "DuplicateGroup",
    "DuplicateReport",
    "InsightAnalyzer",
    "InsightMessage",
    "LargestItem",
    "MailboxInsightReport",
    "SenderInsight",
    "SenderReport",
    "StorageBucket",
    "StorageReport",
    "analyze_mailbox",
    "analyze_senders",
    "analyze_storage",
    "duplicate_attachment_insights",
    "duplicate_insights",
    "find_duplicate_attachments",
    "find_duplicate_messages",
    "sender_insights",
    "storage_insights",
]
