"""Bounded local, backup, similarity, and duplicate search orchestration.

This service never initializes Exchange.  It composes the disposable SQLite FTS
index with immutable backup archives and labels every result with its actual source.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from rapidfuzz.fuzz import WRatio

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal
from gucmail.services.backup import BackupService, OfflineItem
from gucmail.services.index import IndexedRecord, IndexHit, SearchIndex
from gucmail.services.insights import DuplicateGroup, duplicate_insights
from gucmail.services.semantic_search import SemanticSearchService

MAX_LOCAL_RESULTS = 500
MAX_ANALYSIS_RECORDS = 5_000
MAX_BACKUP_ARCHIVES = 25


class LocalSearchMode(StrEnum):
    INDEX = "index"
    ATTACHMENTS = "attachments"
    BACKUPS = "backups"
    ALL = "all"
    SEMANTIC = "semantic"
    SIMILAR = "similar"
    DUPLICATES = "duplicates"
    NEAR_DUPLICATES = "near_duplicates"


@dataclass(frozen=True, slots=True)
class LocalSearchResult:
    result_id: str
    reference: str
    title: str
    source: str
    kind: str
    score: float
    folder: str = ""
    timestamp: str | None = None
    snippet: str = ""
    explanation: str = ""
    backup_id: str | None = None
    member_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalSearchReport:
    mode: LocalSearchMode
    query: str
    results: tuple[LocalSearchResult, ...]
    searched_sources: tuple[str, ...]
    scanned_count: int
    truncated: bool = False
    notices: tuple[str, ...] = ()


class LocalSearchService:
    """Execute private search and analysis against bounded local sources."""

    def __init__(
        self,
        index: SearchIndex,
        backups: BackupService,
        *,
        semantic_enabled: bool = False,
        semantic_model: str | None = None,
        semantic: SemanticSearchService | None = None,
    ) -> None:
        self.index = index
        self.backups = backups
        self.semantic_enabled = bool(semantic_enabled)
        self.semantic_model = semantic_model.strip() if semantic_model else None
        self.semantic = semantic

    def search(
        self,
        query: str,
        *,
        mode: LocalSearchMode | str = LocalSearchMode.ALL,
        fuzzy: bool = False,
        exact: bool = False,
        minimum_score: int = 75,
        limit: int = 100,
        reference: str | None = None,
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        attachment_extension: str | None = None,
        backup_id: str | None = None,
    ) -> LocalSearchReport:
        selected_mode = LocalSearchMode(mode)
        if not 1 <= limit <= MAX_LOCAL_RESULTS:
            raise ConfigurationError(
                f"Local search limit must be between 1 and {MAX_LOCAL_RESULTS}."
            )
        if not 0 <= minimum_score <= 100:
            raise ConfigurationError("Local search threshold must be between 0 and 100.")
        if fuzzy and exact:
            raise ConfigurationError("Choose fuzzy matching or exact phrase matching, not both.")
        normalized_query = sanitize_terminal(query, max_length=8_192).strip()
        selected_backup_id = sanitize_terminal(backup_id or "", max_length=300).strip() or None
        if (
            selected_mode
            in {
                LocalSearchMode.INDEX,
                LocalSearchMode.ATTACHMENTS,
                LocalSearchMode.BACKUPS,
                LocalSearchMode.ALL,
            }
            and not normalized_query
            and not (
                selected_mode is LocalSearchMode.ATTACHMENTS
                and any(
                    value is not None
                    for value in (
                        sender,
                        after,
                        before,
                        minimum_size,
                        maximum_size,
                        attachment_extension,
                    )
                )
            )
        ):
            raise ConfigurationError("Enter search text before running this search.")
        if selected_mode is LocalSearchMode.SEMANTIC:
            return self._semantic_search(normalized_query, limit=limit)
        if selected_mode is LocalSearchMode.SIMILAR:
            return self._similar(
                reference or normalized_query, limit=limit, threshold=minimum_score
            )
        if selected_mode in {LocalSearchMode.DUPLICATES, LocalSearchMode.NEAR_DUPLICATES}:
            return self._duplicates(selected_mode, limit=limit, threshold=minimum_score)

        index_results: tuple[LocalSearchResult, ...] = ()
        backup_results: tuple[LocalSearchResult, ...] = ()
        notices: list[str] = []
        scanned_count = 0
        if selected_mode in {
            LocalSearchMode.INDEX,
            LocalSearchMode.ATTACHMENTS,
            LocalSearchMode.ALL,
        }:
            hits = self.index.search(
                normalized_query,
                fuzzy=fuzzy,
                fuzzy_threshold=minimum_score,
                limit=limit,
                exact=exact,
                fields=("attachment_names", "attachment_text")
                if selected_mode is LocalSearchMode.ATTACHMENTS
                else (),
                sender=sender if selected_mode is LocalSearchMode.ATTACHMENTS else None,
                after=after if selected_mode is LocalSearchMode.ATTACHMENTS else None,
                before=before if selected_mode is LocalSearchMode.ATTACHMENTS else None,
                minimum_size=(
                    minimum_size if selected_mode is LocalSearchMode.ATTACHMENTS else None
                ),
                maximum_size=(
                    maximum_size if selected_mode is LocalSearchMode.ATTACHMENTS else None
                ),
                attachment_extension=(
                    attachment_extension if selected_mode is LocalSearchMode.ATTACHMENTS else None
                ),
            )
            if selected_mode is LocalSearchMode.ATTACHMENTS:
                index_results = tuple(
                    LocalSearchResult(
                        result_id=f"attachment:{hit.reference}",
                        reference=hit.reference,
                        title=hit.subject or "(untitled indexed message)",
                        source="local-index",
                        kind="attachment match",
                        score=hit.score,
                        folder=hit.folder_path,
                        timestamp=hit.received_at.isoformat() if hit.received_at else None,
                        snippet=hit.snippet,
                        explanation=(
                            "Matched an indexed attachment filename or safely extracted local "
                            "attachment text; no attachment was opened or downloaded."
                        )
                        + self._attachment_note(hit),
                    )
                    for hit in hits
                )
            else:
                index_results = tuple(self._index_result(hit) for hit in hits)
            scanned_count += self.index.stats().documents
        if selected_mode in {LocalSearchMode.BACKUPS, LocalSearchMode.ALL}:
            backup_results, backup_scanned, backup_notices = self._search_backups(
                normalized_query,
                fuzzy=fuzzy,
                minimum_score=minimum_score,
                limit=limit,
                backup_id=selected_backup_id,
            )
            scanned_count += backup_scanned
            notices.extend(backup_notices)
        combined = sorted(
            (*index_results, *backup_results),
            key=lambda item: (
                -item.score,
                item.source.casefold(),
                item.title.casefold(),
                item.result_id,
            ),
        )
        sources = (
            ("local-index", "backup")
            if selected_mode is LocalSearchMode.ALL
            else (
                ("local-index",)
                if selected_mode in {LocalSearchMode.INDEX, LocalSearchMode.ATTACHMENTS}
                else ((f"backup:{selected_backup_id}" if selected_backup_id else "backup"),)
            )
        )
        return LocalSearchReport(
            selected_mode,
            normalized_query,
            tuple(combined[:limit]),
            sources,
            scanned_count,
            truncated=len(combined) > limit,
            notices=tuple(notices),
        )

    def _search_backups(
        self,
        query: str,
        *,
        fuzzy: bool,
        minimum_score: int,
        limit: int,
        backup_id: str | None = None,
    ) -> tuple[tuple[LocalSearchResult, ...], int, tuple[str, ...]]:
        results: list[LocalSearchResult] = []
        notices: list[str] = []
        scanned = 0
        summaries = self.backups.list(limit=MAX_BACKUP_ARCHIVES)
        if backup_id is not None:
            summaries = tuple(summary for summary in summaries if summary.backup_id == backup_id)
            if not summaries:
                return (), 0, (f"Selected backup {backup_id} is not available.",)
        for summary in summaries:
            if summary.state != "complete":
                notices.append(f"Skipped {summary.backup_id}: state is {summary.state}.")
                continue
            if summary.encrypted:
                notices.append(
                    f"Skipped encrypted backup {summary.backup_id}; unlock it in Backups first."
                )
                continue
            try:
                archive = self.backups.open_offline(summary.backup_id, verify=False)
                scanned += summary.item_count
                remaining = max(1, limit - len(results))
                for hit in archive.search(
                    query,
                    fuzzy=fuzzy,
                    minimum_score=float(minimum_score),
                    limit=remaining,
                ):
                    results.append(self._backup_result(summary.backup_id, hit.item, hit.score))
                if len(results) >= limit:
                    break
            except Exception as exc:
                notices.append(
                    f"Skipped unreadable backup {summary.backup_id}: {type(exc).__name__}."
                )
        return tuple(results[:limit]), scanned, tuple(notices)

    def _similar(self, reference: str, *, limit: int, threshold: int) -> LocalSearchReport:
        target = self.index.record(reference)
        target_text = self._similarity_text(target)
        ranked: list[LocalSearchResult] = []
        records = self.index.records(limit=MAX_ANALYSIS_RECORDS)
        for record in records:
            if record.reference == target.reference:
                continue
            score = float(WRatio(target_text, self._similarity_text(record)))
            if score < threshold:
                continue
            ranked.append(
                LocalSearchResult(
                    result_id=f"similar:{record.reference}",
                    reference=record.reference,
                    title=record.subject or "(untitled indexed record)",
                    source="local-index",
                    kind="similar message",
                    score=score,
                    folder=record.folder_path,
                    timestamp=record.received_at.isoformat() if record.received_at else None,
                    snippet=self._safe_snippet(record.body),
                    explanation=(
                        f"Local normalized metadata/content similarity to {target.reference}."
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.reference))
        return LocalSearchReport(
            LocalSearchMode.SIMILAR,
            target.reference,
            tuple(ranked[:limit]),
            ("local-index",),
            len(records),
            truncated=len(records) >= MAX_ANALYSIS_RECORDS,
            notices=(
                "Similarity is bounded local lexical evidence, not proof that messages "
                "are duplicates.",
            ),
        )

    def _duplicates(
        self, mode: LocalSearchMode, *, limit: int, threshold: int
    ) -> LocalSearchReport:
        records = self.index.records(limit=MAX_ANALYSIS_RECORDS)
        report = duplicate_insights(
            (self._insight_value(record) for record in records),
            include_near=mode is LocalSearchMode.NEAR_DUPLICATES,
            similarity_threshold=threshold,
            max_items=MAX_ANALYSIS_RECORDS,
        )
        groups: tuple[DuplicateGroup, ...]
        if mode is LocalSearchMode.NEAR_DUPLICATES:
            groups = report.near_groups
        else:
            groups = (*report.exact_groups, *report.possible_groups)
        results = tuple(self._duplicate_result(group) for group in groups[:limit])
        notices = (
            "Exact groups require complete-content or raw-MIME digests; matching indexed "
            "text is labelled possible.",
            "No cleanup action is performed from Search.",
        )
        return LocalSearchReport(
            mode,
            "",
            results,
            ("local-index",),
            report.scanned_count,
            truncated=report.truncated or len(groups) > limit,
            notices=notices,
        )

    def _semantic_search(self, query: str, *, limit: int) -> LocalSearchReport:
        if self.semantic is not None and self.semantic.store.enabled:
            status = self.semantic.status()
            if not status["built"]:
                return LocalSearchReport(
                    LocalSearchMode.SEMANTIC,
                    query,
                    (),
                    (),
                    0,
                    notices=(
                        "No local topic model is built. Use Semantic setup to preview and confirm "
                        "a build from existing indexed text. Lexical search remains usable.",
                    ),
                )
            if not query:
                raise ConfigurationError("Enter search text for local topic ranking.")
            hits = self.semantic.search(query, limit=limit)
            return LocalSearchReport(
                LocalSearchMode.SEMANTIC,
                query,
                tuple(
                    LocalSearchResult(
                        result_id=f"semantic:{hit.reference}",
                        reference=hit.reference,
                        title=hit.subject,
                        source="local-semantic",
                        kind="indexed message",
                        score=100 * hit.score,
                        folder=hit.folder,
                        timestamp=hit.timestamp,
                        explanation=(
                            f"{hit.explanation} Topic similarity {hit.semantic_score:.3f}; "
                            f"lexical overlap {hit.lexical_score:.3f}; hybrid weight 0.8."
                        ),
                    )
                    for hit in hits
                ),
                ("local-semantic",),
                int(status["documents"]),
                truncated=bool(status.get("truncated")),
                notices=(
                    "Topics learned from the selected local corpus; no pretrained language model. "
                    "Subjects and optional indexed bodies only; no mailbox access or transmission.",
                ),
            )
        if not self.semantic_enabled:
            notice = (
                "Semantic search is optional and disabled. Enable it under Settings → Search & "
                "Indexing or use Semantic setup to review the private corpus-topic model."
            )
        elif not self.semantic_model:
            notice = (
                "Semantic search is enabled, but no supported local embedding model is configured. "
                "No mailbox text was transmitted or processed."
            )
        else:
            notice = (
                f"Configured model {sanitize_terminal(self.semantic_model, max_length=120)} is not "
                "available in this installation; use local FTS/fuzzy search meanwhile."
            )
        return LocalSearchReport(
            LocalSearchMode.SEMANTIC,
            query,
            (),
            (),
            0,
            notices=(notice,),
        )

    @staticmethod
    def _index_result(hit: IndexHit) -> LocalSearchResult:
        return LocalSearchResult(
            result_id=f"index:{hit.reference}",
            reference=hit.reference,
            title=hit.subject or "(untitled indexed record)",
            source="local-index",
            kind="indexed message",
            score=hit.score,
            folder=hit.folder_path,
            timestamp=hit.received_at.isoformat() if hit.received_at else None,
            snippet=hit.snippet,
            explanation=("SQLite FTS5" if "[" in hit.snippet else "Bounded fuzzy match")
            + LocalSearchService._attachment_note(hit),
        )

    @staticmethod
    def _attachment_note(hit: IndexHit) -> str:
        records = hit.payload.get("attachment_extractions", [])
        if not isinstance(records, (list, tuple)):
            return ""
        labels: set[str] = {
            str(record["label"])
            for record in records[:21]
            if isinstance(record, Mapping)
            and record.get("status") == "success"
            and isinstance(record.get("chars"), int)
            and record["chars"] > 0
            and isinstance(record.get("label"), str)
        }
        note = ""
        if any(
            label == "extracted_attachment_text" or label.startswith("extracted_attachment_text:")
            for label in labels
        ):
            note += " Includes locally extracted attachment text."
        if "ocr_attachment_text" in labels:
            note += " Includes OCR-derived attachment text; recognition may be inaccurate."
        if hit.payload.get("attachment_text_truncated") is True or any(
            isinstance(record, Mapping) and record.get("truncated") is True
            for record in records[:21]
        ):
            note += " Indexed attachment text was truncated."
        return note

    @classmethod
    def _backup_result(cls, backup_id: str, item: OfflineItem, score: float) -> LocalSearchResult:
        data = item.data if isinstance(item.data, Mapping) else {}
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        title = cls._first_text(data, metadata, names=("subject", "title", "name", "display_name"))
        timestamp = cls._first_text(
            data,
            metadata,
            names=("received_at", "sent_at", "start", "due_at", "modified_at"),
        )
        body = cls._first_text(data, metadata, names=("body", "body_text", "preview", "notes"))
        return LocalSearchResult(
            result_id=f"backup:{backup_id}:{item.item_key}",
            reference=item.reference,
            title=title or f"{item.kind} · {item.reference}",
            source=f"backup · {backup_id}",
            kind=item.kind,
            score=score,
            folder=item.folder_path or "",
            timestamp=timestamp or None,
            snippet=cls._safe_snippet(body),
            explanation="Immutable offline backup content match; result may be stale.",
            backup_id=backup_id,
        )

    @staticmethod
    def _first_text(*values: Mapping[str, Any], names: tuple[str, ...]) -> str:
        for name in names:
            for value in values:
                candidate = value.get(name)
                if candidate is not None and str(candidate).strip():
                    return sanitize_terminal(str(candidate), max_length=1_000)
        return ""

    @staticmethod
    def _similarity_text(record: IndexedRecord) -> str:
        return " ".join(
            (
                record.subject,
                record.sender,
                record.recipients,
                record.body,
                record.attachment_names,
                record.attachment_text,
            )
        ).casefold()

    @staticmethod
    def _safe_snippet(value: str) -> str:
        return sanitize_terminal(" ".join(value.split()), max_length=500)

    @staticmethod
    def _insight_value(record: IndexedRecord) -> Mapping[str, Any]:
        payload = dict(record.payload or {})
        payload.update(
            {
                "reference": record.reference,
                "subject": record.subject,
                "sender": record.sender,
                "body": record.body,
                "attachment_names": tuple(record.attachment_names.split()),
                "folder_path": record.folder_path,
                "received_at": record.received_at,
                "size": record.size,
            }
        )
        return payload

    @staticmethod
    def _duplicate_result(group: DuplicateGroup) -> LocalSearchResult:
        label = {
            "exact": "exact duplicate group",
            "possible": "possible duplicate group",
            "near": "near-duplicate group",
        }.get(group.kind, "duplicate group")
        return LocalSearchResult(
            result_id=f"duplicates:{group.kind}:{group.digest}",
            reference=group.references[0],
            title=group.representative_subject or "(untitled duplicate group)",
            source="local-index",
            kind=label,
            score=group.similarity,
            snippet=(
                f"{len(group.references)} records · reclaimable estimate "
                f"{group.reclaimable_bytes} bytes"
            ),
            explanation=group.explanation,
            member_references=group.references,
        )


__all__ = [
    "MAX_ANALYSIS_RECORDS",
    "MAX_BACKUP_ARCHIVES",
    "MAX_LOCAL_RESULTS",
    "LocalSearchMode",
    "LocalSearchReport",
    "LocalSearchResult",
    "LocalSearchService",
]
