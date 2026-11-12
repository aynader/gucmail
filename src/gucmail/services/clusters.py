"""Deterministic, bounded, local-only mailbox clustering.

This module intentionally has no network or model-provider integration.  It accepts
small Exchange-independent records (or plain mappings), keeps only a bounded input
window, and returns immutable summaries containing message references rather than
message bodies.  Topic clustering uses fixed-seed feature hashing and an online
centroid algorithm, so it needs neither a global vocabulary nor a heavy ML runtime.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, time
from email.utils import parseaddr
from itertools import islice, pairwise
from pathlib import PurePath
from typing import Any

DEFAULT_MAX_ITEMS = 10_000
DEFAULT_MAX_CLUSTERS = 24
HARD_MAX_ITEMS = 100_000
HARD_MAX_CLUSTERS = 256

_MAX_REFERENCE_CHARS = 2_048
_MAX_SUBJECT_CHARS = 1_024
_MAX_BODY_CHARS = 16_384
_MAX_ADDRESS_CHARS = 512
_MAX_RECIPIENTS = 64
_MAX_ATTACHMENTS = 64
_MAX_TERMS_PER_ITEM = 96
_HASH_DIMENSIONS = 192
_TOP_VALUE_LIMIT = 8
_REPRESENTATIVE_LIMIT = 3
_TOPIC_SIMILARITY_THRESHOLD = 0.27

_WORD_RE = re.compile(r"[^\W_][\w'\N{RIGHT SINGLE QUOTATION MARK}-]{1,63}", re.UNICODE)
_REPLY_PREFIX_RE = re.compile(r"(?i)^\s*(?:(?:re|fw|fwd)\s*:\s*)+")
_SPACE_RE = re.compile(r"\s+")

# High-frequency mail prose has little topic value.  Keeping this compact and static
# is important: no user content is ever sent elsewhere to obtain stop words.
_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "dear",
        "did",
        "do",
        "does",
        "for",
        "forwarded",
        "from",
        "had",
        "has",
        "have",
        "hello",
        "hi",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "kind",
        "mail",
        "may",
        "me",
        "message",
        "more",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "regards",
        "re",
        "sent",
        "so",
        "some",
        "thank",
        "thanks",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_STRATEGY_ALIASES = {
    "topic": "topic",
    "topics": "topic",
    "similar_subject": "topic",
    "similar_subjects": "topic",
    "conversation": "conversation",
    "conversations": "conversation",
    "thread": "conversation",
    "sender": "sender",
    "senders": "sender",
    "from": "sender",
    "sender_domain": "sender_domain",
    "domain": "sender_domain",
    "organization": "sender_domain",
    "recipient": "recipient_group",
    "recipients": "recipient_group",
    "recipient_group": "recipient_group",
    "time": "time_period",
    "date": "time_period",
    "month": "time_period",
    "time_period": "time_period",
    "attachment": "attachment_type",
    "attachments": "attachment_type",
    "attachment_type": "attachment_type",
}


@dataclass(frozen=True, slots=True)
class ClusterItem:
    """A compact message record accepted by the local clusterer.

    ``from_value`` also accepts mappings, domain dataclasses, ``MessageSummary``
    instances, and ``MessageDetail``-like objects.  Text and collection fields are
    normalized and capped even when this class is constructed directly.
    """

    reference: str
    subject: str = ""
    body: str = ""
    text: str = ""
    sender: str = ""
    recipients: tuple[str, ...] = ()
    received_at: datetime | None = None
    is_read: bool = True
    attachment_types: tuple[str, ...] = ()
    folder_path: str = ""
    size_bytes: int = 0
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        reference = _bounded_identifier(self.reference, _MAX_REFERENCE_CHARS)
        if not reference:
            raise ValueError("Every cluster item must have a non-empty reference.")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "subject", _bounded_text(self.subject, _MAX_SUBJECT_CHARS))
        object.__setattr__(self, "body", _bounded_text(self.body, _MAX_BODY_CHARS))
        object.__setattr__(self, "text", _bounded_text(self.text, _MAX_BODY_CHARS))
        object.__setattr__(self, "sender", _address_text(self.sender))
        object.__setattr__(self, "recipients", _addresses(self.recipients))
        object.__setattr__(self, "received_at", _datetime(self.received_at))
        object.__setattr__(self, "is_read", _boolean(self.is_read, default=True))
        object.__setattr__(
            self,
            "attachment_types",
            _attachment_types(self.attachment_types, has_attachments=False),
        )
        object.__setattr__(self, "folder_path", _bounded_text(self.folder_path, 1_024))
        object.__setattr__(self, "size_bytes", _non_negative_int(self.size_bytes))
        conversation_id = (
            _bounded_identifier(self.conversation_id, _MAX_REFERENCE_CHARS)
            if self.conversation_id
            else None
        )
        object.__setattr__(self, "conversation_id", conversation_id)

    @classmethod
    def from_value(cls, value: object) -> ClusterItem:
        """Coerce a supported value without retaining the original object."""

        if isinstance(value, cls):
            return value
        raw = _mapping(value)
        summary_value = raw.get("summary")
        summary = _mapping(summary_value) if summary_value is not None else {}

        def choose(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in raw and raw[name] is not None:
                    return raw[name]
                if name in summary and summary[name] is not None:
                    return summary[name]
            return default

        reference = choose("reference", "message_reference", "item_reference", "local_ref", "id")
        if reference is None or not str(reference).strip():
            raise ValueError("Every cluster item must have a non-empty reference.")

        recipients: list[object] = []
        explicit_recipients = choose("recipients", "recipient", default=None)
        if explicit_recipients is not None:
            recipients.extend(_bounded_values(explicit_recipients, _MAX_RECIPIENTS))
        else:
            for field_name in ("to", "to_recipients", "cc", "cc_recipients", "bcc"):
                field_value = choose(field_name, default=None)
                if field_value is not None:
                    recipients.extend(
                        _bounded_values(
                            field_value,
                            max(0, _MAX_RECIPIENTS - len(recipients)),
                        )
                    )
                if len(recipients) >= _MAX_RECIPIENTS:
                    break

        raw_attachments = choose("attachments", default=()) or ()
        explicit_attachment_types = choose("attachment_types", default=()) or ()
        attachment_names = choose("attachment_names", "attachments_names", default=()) or ()
        combined_attachments = [
            *_bounded_values(explicit_attachment_types, _MAX_ATTACHMENTS),
            *_bounded_values(attachment_names, _MAX_ATTACHMENTS),
            *_bounded_values(raw_attachments, _MAX_ATTACHMENTS),
        ][:_MAX_ATTACHMENTS]

        return cls(
            reference=str(reference),
            subject=str(choose("subject", "title", default="") or ""),
            body=str(choose("body", "body_text", "preview", "snippet", default="") or ""),
            text=str(choose("text", "indexed_text", "attachment_text", default="") or ""),
            sender=_address_text(choose("sender", "from", "from_address", default="")),
            recipients=_addresses(recipients),
            received_at=_datetime(
                choose("received_at", "sent_at", "date", "timestamp", default=None)
            ),
            is_read=_boolean(choose("is_read", "read", default=True), default=True),
            attachment_types=_attachment_types(
                combined_attachments,
                has_attachments=_boolean(
                    choose("has_attachments", default=bool(combined_attachments)),
                    default=bool(combined_attachments),
                ),
            ),
            folder_path=str(choose("folder_path", "folder", default="") or ""),
            size_bytes=_non_negative_int(choose("size_bytes", "size", default=0)),
            conversation_id=(
                str(conversation_id)
                if (conversation_id := choose("conversation_id", "thread_id", default=None))
                else None
            ),
        )

    @property
    def id(self) -> str:
        """Compatibility alias for consumers that call local references IDs."""

        return self.reference


@dataclass(frozen=True, slots=True)
class MessageCluster:
    """An immutable, content-minimizing mailbox cluster summary."""

    cluster_id: str
    by: str
    key: str
    label: str
    explanation: str
    message_count: int
    unread_count: int
    start_at: datetime | None
    end_at: datetime | None
    top_senders: tuple[tuple[str, int], ...]
    top_recipients: tuple[tuple[str, int], ...]
    common_terms: tuple[tuple[str, int], ...]
    common_attachment_types: tuple[tuple[str, int], ...]
    representative_messages: tuple[str, ...]
    folder_distribution: tuple[tuple[str, int], ...]
    estimated_storage_bytes: int
    message_references: tuple[str, ...]

    @property
    def count(self) -> int:
        return self.message_count

    @property
    def references(self) -> tuple[str, ...]:
        return self.message_references

    @property
    def oldest_at(self) -> datetime | None:
        return self.start_at

    @property
    def newest_at(self) -> datetime | None:
        return self.end_at

    @property
    def storage_bytes(self) -> int:
        return self.estimated_storage_bytes

    def as_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""

        return {
            "cluster_id": self.cluster_id,
            "by": self.by,
            "key": self.key,
            "label": self.label,
            "explanation": self.explanation,
            "message_count": self.message_count,
            "unread_count": self.unread_count,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "top_senders": [{"value": value, "count": count} for value, count in self.top_senders],
            "top_recipients": [
                {"value": value, "count": count} for value, count in self.top_recipients
            ],
            "common_terms": [
                {"value": value, "count": count} for value, count in self.common_terms
            ],
            "common_attachment_types": [
                {"value": value, "count": count} for value, count in self.common_attachment_types
            ],
            "representative_messages": list(self.representative_messages),
            "folder_distribution": [
                {"value": value, "count": count} for value, count in self.folder_distribution
            ],
            "estimated_storage_bytes": self.estimated_storage_bytes,
            "message_references": list(self.message_references),
        }


@dataclass(slots=True)
class _TopicBucket:
    members: list[ClusterItem]
    centroid_sum: dict[int, float]
    empty_vector: bool = False


def cluster_messages(
    items: Iterable[object],
    *,
    by: str = "topic",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    seed: int = 0,
) -> tuple[MessageCluster, ...]:
    """Cluster a bounded input window without network access or global state.

    Supported strategies are ``topic``, ``conversation``, ``sender``,
    ``sender_domain``, ``recipient_group``, ``time_period``, and
    ``attachment_type``.  Strategy spelling is case-insensitive and accepts spaces
    or hyphens.  Topic results are stable for the same input window, limits, and
    seed.  Exact grouping strategies are independent of input order.
    """

    item_limit = _bounded_limit(max_items, "max_items", HARD_MAX_ITEMS)
    cluster_limit = _bounded_limit(max_clusters, "max_clusters", HARD_MAX_CLUSTERS)
    normalized_seed = _seed(seed)
    strategy = _strategy(by)
    records = _bounded_items(items, max_items=item_limit)
    if not records:
        return ()
    if strategy == "topic":
        buckets = _topic_buckets(records, max_clusters=cluster_limit, seed=normalized_seed)
        clusters = [
            _summarize_cluster(
                bucket.members,
                by=strategy,
                key="topic",
                seed=normalized_seed,
            )
            for bucket in buckets
        ]
    else:
        clusters = _exact_clusters(
            records,
            by=strategy,
            max_clusters=cluster_limit,
            seed=normalized_seed,
        )
    return tuple(
        sorted(
            clusters,
            key=lambda cluster: (
                -cluster.message_count,
                cluster.label.casefold(),
                cluster.cluster_id,
            ),
        )
    )


class MailboxClusterer:
    """Reusable configuration facade for CLI, TUI, and backup callers."""

    def __init__(
        self,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_clusters: int = DEFAULT_MAX_CLUSTERS,
        seed: int = 0,
    ) -> None:
        self.max_items = _bounded_limit(max_items, "max_items", HARD_MAX_ITEMS)
        self.max_clusters = _bounded_limit(
            max_clusters,
            "max_clusters",
            HARD_MAX_CLUSTERS,
        )
        self.seed = _seed(seed)

    def cluster(
        self,
        items: Iterable[object],
        *,
        by: str = "topic",
    ) -> tuple[MessageCluster, ...]:
        return cluster_messages(
            items,
            by=by,
            max_items=self.max_items,
            max_clusters=self.max_clusters,
            seed=self.seed,
        )

    def build(
        self,
        items: Iterable[object],
        *,
        by: str = "topic",
    ) -> tuple[MessageCluster, ...]:
        """Alias for ``cluster`` matching the user-facing build operation."""

        return self.cluster(items, by=by)


def _bounded_items(items: Iterable[object], *, max_items: int) -> list[ClusterItem]:
    # Keep at most one canonical value for a stable mailbox reference.  Selecting the
    # lexical fingerprint makes duplicate handling deterministic within the window.
    records: dict[str, ClusterItem] = {}
    fingerprints: dict[str, str] = {}
    for value in islice(items, max_items):
        item = ClusterItem.from_value(value)
        fingerprint = _item_fingerprint(item)
        previous = fingerprints.get(item.reference)
        if previous is None or fingerprint < previous:
            records[item.reference] = item
            fingerprints[item.reference] = fingerprint
    return sorted(records.values(), key=lambda item: item.reference.casefold())


def _topic_buckets(
    items: Sequence[ClusterItem],
    *,
    max_clusters: int,
    seed: int,
) -> list[_TopicBucket]:
    buckets: list[_TopicBucket] = []
    empty_bucket: _TopicBucket | None = None
    ordered = sorted(items, key=lambda item: (_seeded_rank(item.reference, seed), item.reference))

    for item in ordered:
        vector = _feature_vector(item)
        if not vector:
            if empty_bucket is None:
                if len(buckets) < max_clusters:
                    empty_bucket = _TopicBucket([item], {}, empty_vector=True)
                    buckets.append(empty_bucket)
                    continue
                empty_bucket = min(
                    buckets,
                    key=lambda bucket: (len(bucket.members), _bucket_tiebreak(bucket)),
                )
            empty_bucket.members.append(item)
            continue

        candidates = [bucket for bucket in buckets if not bucket.empty_vector]
        best: _TopicBucket | None = None
        best_score = -1.0
        for bucket in candidates:
            score = _cosine_to_sum(vector, bucket.centroid_sum)
            if score > best_score + 1e-12:
                best, best_score = bucket, score
            elif (
                abs(score - best_score) <= 1e-12
                and best is not None
                and _bucket_tiebreak(bucket) < _bucket_tiebreak(best)
            ):
                best = bucket

        if best is None or (
            best_score < _TOPIC_SIMILARITY_THRESHOLD and len(buckets) < max_clusters
        ):
            buckets.append(_TopicBucket([item], dict(vector)))
            continue
        if best is None:
            # ``max_clusters`` is validated as positive, so this is defensive only.
            best = buckets[0]
        best.members.append(item)
        for index, weight in vector.items():
            best.centroid_sum[index] = best.centroid_sum.get(index, 0.0) + weight

    return buckets


def _exact_clusters(
    items: Sequence[ClusterItem],
    *,
    by: str,
    max_clusters: int,
    seed: int,
) -> list[MessageCluster]:
    grouped: dict[str, list[ClusterItem]] = defaultdict(list)
    for item in items:
        grouped[_group_key(item, by)].append(item)

    ordered_groups = sorted(
        grouped.items(),
        key=lambda entry: (-len(entry[1]), entry[0].casefold(), entry[0]),
    )
    overflow = len(ordered_groups) > max_clusters
    if overflow:
        retained_count = max_clusters - 1
        retained = ordered_groups[:retained_count]
        remainder = [item for _, members in ordered_groups[retained_count:] for item in members]
        retained.append(("(other)", remainder))
        ordered_groups = retained

    return [
        _summarize_cluster(
            members,
            by=by,
            key=key,
            seed=seed,
            overflow=overflow and key == "(other)",
        )
        for key, members in ordered_groups
    ]


def _summarize_cluster(
    members: Sequence[ClusterItem],
    *,
    by: str,
    key: str,
    seed: int,
    overflow: bool = False,
) -> MessageCluster:
    ordered = sorted(members, key=lambda item: item.reference.casefold())
    dates = [item.received_at for item in ordered if item.received_at is not None]
    senders = _counts(item.sender for item in ordered if item.sender)
    recipients = _counts(
        recipient for item in ordered for recipient in set(item.recipients) if recipient
    )
    attachments = _counts(
        attachment for item in ordered for attachment in set(item.attachment_types) if attachment
    )
    folders = _counts(item.folder_path or "(unknown folder)" for item in ordered)
    terms = _common_terms(ordered)
    label = _cluster_label(by, key, ordered, terms, overflow=overflow)
    explanation = _cluster_explanation(by, key, len(ordered), label, overflow=overflow)
    references = tuple(item.reference for item in ordered)
    cluster_id = _cluster_id(by, key, references, seed)
    representatives = _representatives(ordered, terms, seed)
    return MessageCluster(
        cluster_id=cluster_id,
        by=by,
        key=key,
        label=label,
        explanation=explanation,
        message_count=len(ordered),
        unread_count=sum(not item.is_read for item in ordered),
        start_at=min(dates) if dates else None,
        end_at=max(dates) if dates else None,
        top_senders=senders,
        top_recipients=recipients,
        common_terms=terms,
        common_attachment_types=attachments,
        representative_messages=representatives,
        folder_distribution=folders,
        estimated_storage_bytes=sum(item.size_bytes for item in ordered),
        message_references=references,
    )


def _strategy(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("by must be a strategy name.")
    normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
    try:
        return _STRATEGY_ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(set(_STRATEGY_ALIASES.values())))
        raise ValueError(
            f"Unsupported cluster strategy {value!r}. Choose one of: {supported}."
        ) from exc


def _group_key(item: ClusterItem, by: str) -> str:
    if by == "conversation":
        return item.conversation_id or _normalized_subject(item.subject) or "(no conversation)"
    if by == "sender":
        return item.sender or "(unknown sender)"
    if by == "sender_domain":
        if "@" in item.sender:
            domain = item.sender.rsplit("@", 1)[1].strip(" >")
            return domain or "(unknown sender domain)"
        return "(unknown sender domain)"
    if by == "recipient_group":
        return " | ".join(item.recipients) if item.recipients else "(no recipients)"
    if by == "time_period":
        return item.received_at.strftime("%Y-%m") if item.received_at else "(unknown date)"
    if by == "attachment_type":
        if not item.attachment_types:
            return "(no attachment)"
        return " | ".join(item.attachment_types)
    raise AssertionError(f"Unhandled exact clustering strategy: {by}")


def _cluster_label(
    by: str,
    key: str,
    members: Sequence[ClusterItem],
    terms: tuple[tuple[str, int], ...],
    *,
    overflow: bool,
) -> str:
    if overflow:
        return f"Other {by.replace('_', ' ')} groups"
    if by == "topic":
        if terms:
            return "Topic: " + ", ".join(term for term, _ in terms[:3])
        subject = next((item.subject.strip() for item in members if item.subject.strip()), "")
        return f"Topic: {_bounded_text(subject, 80)}" if subject else "Topic: other messages"
    if by == "conversation":
        subject = next((item.subject.strip() for item in members if item.subject.strip()), "")
        return f"Conversation: {_bounded_text(_REPLY_PREFIX_RE.sub('', subject), 96) or key}"
    labels = {
        "sender": "Sender",
        "sender_domain": "Sender domain",
        "recipient_group": "Recipients",
        "time_period": "Time period",
        "attachment_type": "Attachment type",
    }
    return f"{labels[by]}: {_bounded_text(key, 128)}"


def _cluster_explanation(
    by: str,
    key: str,
    count: int,
    label: str,
    *,
    overflow: bool,
) -> str:
    noun = "message" if count == 1 else "messages"
    if overflow:
        return f"{count} {noun} from smaller groups combined to respect the cluster limit."
    if by == "topic":
        return f"{count} {noun} grouped by overlapping local text features ({label})."
    readable = by.replace("_", " ")
    return f"{count} {noun} with the same {readable}: {_bounded_text(key, 160)}."


def _common_terms(items: Sequence[ClusterItem]) -> tuple[tuple[str, int], ...]:
    document_frequency: Counter[str] = Counter()
    for item in items:
        document_frequency.update(set(_term_counts(item)))
    return tuple(
        sorted(document_frequency.items(), key=lambda pair: (-pair[1], pair[0]))[:_TOP_VALUE_LIMIT]
    )


def _counts(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    counts = Counter(values)
    return tuple(
        sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold(), pair[0]))[
            :_TOP_VALUE_LIMIT
        ]
    )


def _representatives(
    items: Sequence[ClusterItem],
    terms: tuple[tuple[str, int], ...],
    seed: int,
) -> tuple[str, ...]:
    important = {term for term, _ in terms[:5]}

    def score(item: ClusterItem) -> tuple[int, int, str, str]:
        overlap = len(important.intersection(_term_counts(item)))
        dated = item.received_at.isoformat() if item.received_at else ""
        return (-overlap, _seeded_rank(item.reference, seed), dated, item.reference)

    return tuple(item.reference for item in sorted(items, key=score)[:_REPRESENTATIVE_LIMIT])


def _term_counts(item: ClusterItem) -> Counter[str]:
    counts: Counter[str] = Counter()
    subject_tokens = _tokens(_REPLY_PREFIX_RE.sub("", item.subject), limit=48)
    for token in subject_tokens:
        counts[token] += 4
    for left, right in pairwise(subject_tokens):
        counts[f"{left} {right}"] += 2
    remaining = _MAX_TERMS_PER_ITEM - len(counts)
    if remaining > 0:
        for token in _tokens(f"{item.body}\n{item.text}", limit=remaining * 3):
            counts[token] += 1
    selected = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:_MAX_TERMS_PER_ITEM]
    return Counter(dict(selected))


def _tokens(value: str, *, limit: int) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    for match in _WORD_RE.finditer(normalized):
        token = match.group(0).strip("'-\N{RIGHT SINGLE QUOTATION MARK}")
        if len(token) < 2 or token in _STOP_WORDS or token.isdecimal():
            continue
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _feature_vector(item: ClusterItem) -> dict[int, float]:
    vector: dict[int, float] = {}
    for term, count in _term_counts(item).items():
        digest = hashlib.blake2b(
            term.encode("utf-8"),
            digest_size=8,
            person=b"gucmail-topic-v1",
        ).digest()
        index = int.from_bytes(digest, "big") % _HASH_DIMENSIONS
        vector[index] = vector.get(index, 0.0) + 1.0 + math.log1p(count)
    norm = math.sqrt(sum(weight * weight for weight in vector.values()))
    if norm:
        return {index: weight / norm for index, weight in vector.items()}
    return {}


def _cosine_to_sum(vector: Mapping[int, float], centroid_sum: Mapping[int, float]) -> float:
    dot = sum(weight * centroid_sum.get(index, 0.0) for index, weight in vector.items())
    norm = math.sqrt(sum(weight * weight for weight in centroid_sum.values()))
    return dot / norm if norm else 0.0


def _bucket_tiebreak(bucket: _TopicBucket) -> str:
    return min(item.reference.casefold() for item in bucket.members)


def _cluster_id(by: str, key: str, references: Sequence[str], seed: int) -> str:
    digest = hashlib.sha256()
    digest.update(b"gucmail-cluster-v1\0")
    digest.update(str(seed).encode("ascii"))
    digest.update(b"\0")
    digest.update(by.encode("utf-8"))
    digest.update(b"\0")
    digest.update(key.encode("utf-8"))
    for reference in references:
        digest.update(b"\0")
        digest.update(reference.encode("utf-8"))
    return f"cluster-{digest.hexdigest()[:16]}"


def _seeded_rank(reference: str, seed: int) -> int:
    digest = hashlib.blake2b(digest_size=8, person=b"gucmail-rank-v1")
    digest.update(str(seed).encode("ascii"))
    digest.update(b"\0")
    digest.update(reference.encode("utf-8"))
    return int.from_bytes(digest.digest(), "big")


def _item_fingerprint(item: ClusterItem) -> str:
    digest = hashlib.sha256()
    for value in (
        item.subject,
        item.body,
        item.text,
        item.sender,
        "\x1f".join(item.recipients),
        item.received_at.isoformat() if item.received_at else "",
        str(item.is_read),
        "\x1f".join(item.attachment_types),
        item.folder_path,
        str(item.size_bytes),
        item.conversation_id or "",
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (str, bytes, bytearray, int, float, bool)):
        return {}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    data: dict[str, Any] = {}
    for name in (
        "reference",
        "message_reference",
        "item_reference",
        "local_ref",
        "id",
        "subject",
        "title",
        "body",
        "body_text",
        "text",
        "indexed_text",
        "attachment_text",
        "preview",
        "snippet",
        "sender",
        "from_address",
        "recipients",
        "recipient",
        "to",
        "to_recipients",
        "cc",
        "cc_recipients",
        "bcc",
        "received_at",
        "sent_at",
        "date",
        "timestamp",
        "is_read",
        "read",
        "attachments",
        "attachment_types",
        "attachment_names",
        "has_attachments",
        "folder_path",
        "folder",
        "size_bytes",
        "size",
        "conversation_id",
        "thread_id",
        "summary",
        "email",
        "address",
        "name",
        "display_name",
        "filename",
        "content_type",
    ):
        if hasattr(value, name):
            data[name] = getattr(value, name)
    return data


def _addresses(values: object) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in _bounded_values(values, _MAX_RECIPIENTS):
        address = _address_text(value)
        if address and address not in seen:
            output.append(address)
            seen.add(address)
    return tuple(sorted(output, key=str.casefold))


def _address_text(value: object) -> str:
    if value is None:
        return ""
    raw = _mapping(value)
    if raw:
        email = raw.get("email") or raw.get("address")
        name = raw.get("name") or raw.get("display_name")
        candidate = str(email or name or "")
    else:
        candidate = str(value)
    _, parsed = parseaddr(candidate)
    result = (parsed or candidate).strip().casefold()
    return _bounded_identifier(result, _MAX_ADDRESS_CHARS)


def _attachment_types(values: object, *, has_attachments: bool) -> tuple[str, ...]:
    output: set[str] = set()
    for value in _bounded_values(values, _MAX_ATTACHMENTS):
        raw = _mapping(value)
        name = raw.get("name") or raw.get("filename") if raw else None
        content_type = raw.get("content_type") if raw else None
        candidate = str(name or content_type or value).strip()
        if not candidate:
            continue
        if candidate.casefold() in {
            "(no extension)",
            "(unknown attachment)",
        } or re.fullmatch(r"\.[a-zA-Z0-9][a-zA-Z0-9+_-]{0,31}", candidate):
            normalized = candidate.casefold()
        elif "/" in candidate and not name:
            normalized = candidate.casefold().split(";", 1)[0].strip()
        else:
            suffix = PurePath(candidate.replace("\\", "/")).suffix.casefold()
            normalized = suffix or "(no extension)"
        output.add(_bounded_text(normalized, 128))
    if has_attachments and not output:
        output.add("(unknown attachment)")
    return tuple(sorted(output))


def _bounded_values(value: object, limit: int) -> list[object]:
    if limit <= 0 or value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return [value]
    if isinstance(value, Iterable):
        output: list[object] = []
        for item in value:
            output.append(item)
            if len(output) >= limit:
                break
        return output
    return [value]


def _datetime(value: object) -> datetime | None:
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _boolean(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        return default
    return bool(value)


def _non_negative_int(value: object) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(number, 0), (1 << 63) - 1)


def _bounded_text(value: object, limit: int) -> str:
    text_value = str(value or "").replace("\x00", "")
    text_value = _SPACE_RE.sub(" ", text_value).strip()
    if len(text_value) <= limit:
        return text_value
    return (
        text_value[: limit - 17] + "~" + hashlib.sha256(text_value.encode("utf-8")).hexdigest()[:16]
    )


def _bounded_identifier(value: object, limit: int) -> str:
    return _bounded_text(value, limit)


def _normalized_subject(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _REPLY_PREFIX_RE.sub("", value)).casefold()
    return _SPACE_RE.sub(" ", normalized).strip()


def _bounded_limit(value: int, name: str, hard_limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return min(value, hard_limit)


def _seed(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("seed must be an integer.")
    if not -(1 << 63) <= value < (1 << 63):
        raise ValueError("seed must fit in a signed 64-bit integer.")
    return value


__all__ = [
    "DEFAULT_MAX_CLUSTERS",
    "DEFAULT_MAX_ITEMS",
    "HARD_MAX_CLUSTERS",
    "HARD_MAX_ITEMS",
    "ClusterItem",
    "MailboxClusterer",
    "MessageCluster",
    "cluster_messages",
]
