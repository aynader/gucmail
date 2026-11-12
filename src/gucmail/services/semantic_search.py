"""Shared CLI/TUI orchestration for the opt-in local corpus-topic model."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

from gucmail.errors import ConfigurationError, ConflictError
from gucmail.services.index import SearchIndex
from gucmail.services.semantic import SemanticHit, SemanticSnapshot, SemanticStore


def _metadata_integer(value: object) -> int:
    if type(value) not in (int, str):
        raise ConfigurationError("Local semantic metadata contains an invalid integer.")
    try:
        return int(str(value))
    except ValueError as exc:
        raise ConfigurationError("Local semantic metadata contains an invalid integer.") from exc


class SemanticSearchService:
    def __init__(self, index: SearchIndex, store: SemanticStore) -> None:
        self.index = index
        self.store = store

    def status(self) -> dict[str, Any]:
        status = dict(self.store.status())
        status["fitting_dependency_available"] = importlib.util.find_spec("numpy") is not None
        status["query_requires_numpy"] = False
        status["model_downloads"] = False
        status["stale"] = False
        if status["built"]:
            snapshot = self.index.semantic_snapshot(
                limit=_metadata_integer(status["source_limit"]),
                include_body=bool(status["include_body"]),
            )
            status["stale"] = snapshot.source_digest != status["source_digest"]
            status["source_total_documents"] = snapshot.total_documents
            status["truncated"] = snapshot.truncated
        return status

    def _selection(
        self, *, limit: int | None, dimensions: int | None, include_body: bool | None
    ) -> tuple[SemanticSnapshot, int]:
        status = self.store.status()
        chosen_limit = (
            limit if limit is not None else _metadata_integer(status.get("source_limit", 0)) or 500
        )
        chosen_dimensions = (
            dimensions
            if dimensions is not None
            else _metadata_integer(status.get("requested_dimensions", 32))
        )
        if not 1 <= chosen_dimensions <= 96:
            raise ConfigurationError("Semantic dimensions must be between 1 and 96.")
        body = include_body if include_body is not None else bool(status.get("include_body", False))
        return self.index.semantic_snapshot(
            limit=chosen_limit, include_body=body
        ), chosen_dimensions

    def preview(
        self,
        *,
        limit: int | None = None,
        dimensions: int | None = None,
        include_body: bool | None = None,
        rebuild: bool = False,
    ) -> dict[str, Any]:
        snapshot, selected_dimensions = self._selection(
            limit=limit, dimensions=dimensions, include_body=include_body
        )
        return {
            "apply": False,
            "enabled": self.store.enabled,
            "model": "local-lsa-v1",
            "selected_documents": len(snapshot.documents),
            "total_local_documents": snapshot.total_documents,
            "limit": snapshot.selected_limit,
            "truncated": snapshot.truncated,
            "include_body": snapshot.include_body,
            "requested_dimensions": selected_dimensions,
            "rebuild": rebuild,
            "source_digest": snapshot.source_digest,
            "mailbox_requests": 0,
            "remote_transmission": False,
            "model_downloads": False,
            "privacy": (
                "Saves vocabulary, local topic basis, title/folder metadata, term overlap data and "
                "embeddings in a separate account-bound SQLite file. Body chunks are processed "
                "only when selected; they are not stored as raw text in the semantic file. "
                "Derived data still reveals content and must be protected."
            ),
            "bounds": (
                "2-2000 latest local records; subject 8192 characters; optional body 32768 per "
                "record; first 2048 chunks train at most 2048 terms/96 dimensions. "
                "Fitting has a 60-second child-process deadline; dense input is at most 32 MiB, "
                "and SVD working memory is additional. Model JSON <=8 MiB; SQLite <=64 MiB."
            ),
            "quality_limit": (
                "Corpus-trained latent semantic analysis; useful topic relationships require "
                "co-occurrence in this local selection. No pretrained or multilingual language "
                "understanding guarantee. Updates reuse the fitted basis; rebuild retrains it."
            ),
        }

    def build(
        self,
        *,
        confirmed: bool,
        limit: int | None = None,
        dimensions: int | None = None,
        include_body: bool | None = None,
        rebuild: bool = False,
        cancelled: Callable[[], bool] | None = None,
        expected_source_digest: str | None = None,
    ) -> dict[str, object]:
        snapshot, selected_dimensions = self._selection(
            limit=limit, dimensions=dimensions, include_body=include_body
        )
        if expected_source_digest is not None and snapshot.source_digest != expected_source_digest:
            raise ConflictError(
                "Local indexed text changed since the preview; review a new preview."
            )
        return self.store.build(
            snapshot.documents,
            confirmed=confirmed,
            dimensions=selected_dimensions,
            include_body=snapshot.include_body,
            rebuild=rebuild,
            cancelled=cancelled,
            source_digest=snapshot.source_digest,
            source_limit=snapshot.selected_limit,
            current_source=lambda: (
                self.index.semantic_snapshot(
                    limit=snapshot.selected_limit, include_body=snapshot.include_body
                ).source_digest
            ),
        )

    def search(
        self, query: str, *, limit: int = 50, semantic_weight: float = 0.8
    ) -> tuple[SemanticHit, ...]:
        status = self.store.status()
        digest = None
        if status["built"]:
            digest = self.index.semantic_snapshot(
                limit=_metadata_integer(status["source_limit"]),
                include_body=bool(status["include_body"]),
            ).source_digest
        return self.store.search(
            query, limit=limit, semantic_weight=semantic_weight, expected_source_digest=digest
        )

    def clear(self, *, confirmed: bool) -> dict[str, object]:
        return self.store.clear(confirmed=confirmed)
