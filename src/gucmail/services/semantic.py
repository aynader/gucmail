"""Account-bound optional local semantic index; never fetches mailbox content."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from gucmail.errors import (
    ConfirmationRequiredError,
    ConflictError,
    LocalStateError,
    UnsupportedFeatureError,
)
from gucmail.services.semantic_model import (
    MAX_CHUNK_CHARACTERS,
    MAX_COMPONENTS,
    LocalTopicModel,
    ModelBuildCancelled,
    fit_in_worker,
    tokens,
)

SCHEMA = 1
MAX_DOCUMENTS = 2_000
MAX_BODY_CHARACTERS = 32_768
MAX_DATABASE_BYTES = 64 * 1024 * 1024


class SemanticStateError(LocalStateError):
    pass


class SemanticConflictError(ConflictError):
    pass


@dataclass(frozen=True, slots=True)
class SemanticDocument:
    reference: str
    subject: str
    body: str = field(default="", repr=False)
    folder: str = ""
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticSnapshot:
    documents: tuple[SemanticDocument, ...]
    source_digest: str
    total_documents: int
    selected_limit: int
    include_body: bool

    @property
    def truncated(self) -> bool:
        return self.total_documents > len(self.documents)


def snapshot_digest(
    documents: Sequence[SemanticDocument], *, total: int, limit: int, include_body: bool
) -> str:
    return hashlib.sha256(
        json.dumps(
            [total, limit, include_body, [_digest(d, _chunks(d, include_body)) for d in documents]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def account_identity(email: str, endpoint: str) -> str:
    return hashlib.sha256((email.strip().casefold() + "\0" + endpoint.strip()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticHit:
    reference: str
    subject: str
    folder: str
    timestamp: str | None
    score: float
    semantic_score: float
    lexical_score: float
    explanation: str = "Local corpus-topic projection plus lexical term overlap."


def _clean_reference(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 8192
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SemanticStateError("Semantic documents need a usable stable reference.")


def _chunks(document: SemanticDocument, include_body: bool) -> tuple[str, ...]:
    subject = document.subject[:MAX_CHUNK_CHARACTERS]
    body = document.body[:MAX_BODY_CHARACTERS] if include_body else ""
    return tuple(
        chunk
        for chunk in (
            subject,
            *(
                body[index : index + MAX_CHUNK_CHARACTERS]
                for index in range(0, len(body), MAX_CHUNK_CHARACTERS)
            ),
        )
        if tokens(chunk)
    )


def _digest(document: SemanticDocument, chunks: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                document.reference,
                document.subject[:1024],
                document.folder[:2048],
                document.timestamp,
                chunks,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class SemanticStore:
    def __init__(self, path: Path, *, identity: str, enabled: bool = False) -> None:
        if not re.fullmatch("[0-9a-f]{64}", identity):
            raise SemanticStateError("Semantic storage needs an explicit account identity hash.")
        self.path = Path(path)
        self.identity = identity
        self.enabled = bool(enabled)

    def _open_connection(self, *, readonly: bool = False) -> sqlite3.Connection:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise SemanticStateError("Semantic store cannot use symbolic links.")
        if self.path.exists() and self.path.stat().st_size > MAX_DATABASE_BYTES:
            raise SemanticStateError("Semantic store exceeds the bounded storage budget.")
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = (
            sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            if readonly
            else sqlite3.connect(self.path, timeout=5)
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if version not in (0, SCHEMA) or (version == 0 and (readonly or tables)):
                raise SemanticStateError(
                    "Unsupported semantic store schema; preserve the file for inspection."
                )
            if readonly:
                connection.execute("PRAGMA query_only=ON")
                return connection
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS embeddings(reference TEXT PRIMARY KEY,"
                "subject TEXT NOT NULL,folder TEXT NOT NULL,timestamp TEXT,"
                "digest TEXT NOT NULL,vectors TEXT NOT NULL,terms TEXT NOT NULL)"
            )
            if version == 0:
                connection.execute("PRAGMA user_version=1")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def _connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = self._open_connection(readonly=readonly)
            with connection:
                yield connection
        except sqlite3.Error:
            raise SemanticStateError(
                "Local semantic storage is locked, damaged or unavailable; preserve the file "
                "and retry after closing other builds. No mailbox mutation occurred."
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        if metadata and metadata.get("identity") != self.identity:
            raise SemanticStateError(
                "Semantic data belongs to another account; use its own data directory."
            )
        return metadata

    def status(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "enabled": self.enabled,
                "model": "local-lsa-v1",
                "documents": 0,
                "dimensions": 0,
                "model_bytes": 0,
                "database_bytes": 0,
                "built": False,
                "remote_transmission": False,
            }
        with self._connect(readonly=True) as connection:
            metadata = self._metadata(connection)
            count = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
        try:
            dimensions = int(metadata.get("dimensions", "0"))
            source_limit = int(metadata.get("source_limit", "0"))
        except ValueError as exc:
            raise SemanticStateError("Corrupt semantic metadata; preserve the file.") from exc
        return {
            "enabled": self.enabled,
            "model": "local-lsa-v1",
            "documents": count,
            "dimensions": dimensions,
            "requested_dimensions": metadata.get("requested_dimensions", "32"),
            "model_bytes": len(metadata.get("model", "").encode()),
            "database_bytes": self.path.stat().st_size,
            "built": bool(metadata.get("model")),
            "include_body": metadata.get("include_body") == "true",
            "updated_at": metadata.get("updated_at"),
            "revision": metadata.get("revision"),
            "source_digest": metadata.get("source_digest"),
            "source_limit": source_limit,
            "remote_transmission": False,
            "quality_limit": (
                "Topics learned from the selected local corpus; no pretrained language model."
            ),
        }

    def build(
        self,
        documents: Sequence[SemanticDocument],
        *,
        confirmed: bool,
        include_body: bool = False,
        dimensions: int = 32,
        rebuild: bool = False,
        cancelled: Callable[[], bool] | None = None,
        fit: Callable[..., LocalTopicModel] | None = None,
        source_digest: str | None = None,
        source_limit: int | None = None,
        current_source: Callable[[], str] | None = None,
    ) -> dict[str, object]:
        if not self.enabled:
            raise UnsupportedFeatureError(
                "Semantic search is disabled; lexical search remains usable."
            )
        if not confirmed:
            raise ConfirmationRequiredError(
                "Confirm the selected fields and local model before building."
            )
        if not 2 <= len(documents) <= MAX_DOCUMENTS:
            raise SemanticStateError(
                "A semantic build needs 2-2000 explicitly selected indexed records."
            )
        if type(dimensions) is not int or not 1 <= dimensions <= MAX_COMPONENTS:
            raise SemanticStateError("Semantic dimensions must be an integer from 1 to 96.")
        source_limit = len(documents) if source_limit is None else source_limit
        if type(source_limit) is not int or not len(documents) <= source_limit <= MAX_DOCUMENTS:
            raise SemanticStateError("Semantic source selection exceeds the document bound.")
        source_digest = source_digest or snapshot_digest(
            documents, total=len(documents), limit=source_limit, include_body=include_body
        )
        if not re.fullmatch("[0-9a-f]{64}", source_digest):
            raise SemanticStateError("Semantic source digest must be an exact SHA-256 value.")
        refs = [document.reference for document in documents]
        for reference in refs:
            _clean_reference(reference)
        if len(set(refs)) != len(refs):
            raise SemanticStateError("A semantic build cannot contain duplicate references.")

        def check_stop() -> None:
            if cancelled and cancelled():
                raise ModelBuildCancelled(
                    "Semantic build cancelled; preceding model and embeddings are retained."
                )

        check_stop()
        with self._connect() as connection:
            metadata = self._metadata(connection)
            previous = {
                row["reference"]: row["digest"]
                for row in connection.execute("SELECT reference,digest FROM embeddings")
            }
        revision = metadata.get("revision", "")
        chosen_chunks = [(document, _chunks(document, include_body)) for document in documents]
        if not rebuild and metadata.get("model"):
            if metadata.get("include_body") != str(include_body).lower() or metadata.get(
                "requested_dimensions"
            ) != str(dimensions):
                raise SemanticConflictError(
                    "Model fields/dimensions changed; explicitly rebuild the model."
                )
            model = LocalTopicModel.from_bytes(metadata["model"].encode())
        else:
            training = [chunk for _, chunks in chosen_chunks for chunk in chunks][:2048]
            if len(training) < 2:
                raise SemanticStateError(
                    "Selected records contain insufficient local text for model training."
                )
            model = (
                fit(training, dimensions=dimensions)
                if fit is not None
                else fit_in_worker(training, dimensions=dimensions, cancelled=cancelled)
            )
        check_stop()
        updates = []
        untouched = 0
        for document, chunks in chosen_chunks:
            check_stop()
            digest = _digest(document, chunks)
            if not rebuild and metadata.get("model") and previous.get(document.reference) == digest:
                untouched += 1
                continue
            vectors = tuple(model.embed(chunk) for chunk in chunks)
            terms = Counter(word for chunk in chunks for word in tokens(chunk))
            updates.append(
                (
                    document.reference,
                    document.subject[:1024],
                    document.folder[:2048],
                    document.timestamp,
                    digest,
                    json.dumps(vectors, allow_nan=False, separators=(",", ":")),
                    json.dumps(
                        sorted(word for word, _ in terms.most_common(256)), separators=(",", ":")
                    ),
                )
            )
        serialized_model = model.to_bytes().decode()
        required_bytes = len(serialized_model.encode()) + sum(
            len(json.dumps(row).encode()) for row in updates
        )
        existing_bytes = self.path.stat().st_size if self.path.exists() else 0
        if required_bytes > MAX_DATABASE_BYTES // 2 or (
            existing_bytes + required_bytes * 2 > MAX_DATABASE_BYTES
        ):
            raise SemanticStateError("Semantic storage budget exceeded; existing data is retained.")
        check_stop()
        if current_source is not None and current_source() != source_digest:
            raise SemanticConflictError(
                "The selected local index changed during fitting; preceding model is retained."
            )
        new_revision = hashlib.sha256(
            json.dumps(
                [
                    revision,
                    model.identity,
                    [_digest(document, chunks) for document, chunks in chosen_chunks],
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._metadata(connection)
            if current.get("revision", "") != revision:
                raise SemanticConflictError(
                    "Another semantic build changed the store; review current status "
                    "before retrying."
                )
            if rebuild:
                connection.execute("DELETE FROM embeddings")
            connection.executemany(
                "INSERT INTO embeddings(reference,subject,folder,timestamp,digest,vectors,terms) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(reference) DO UPDATE SET "
                "subject=excluded.subject,folder=excluded.folder,timestamp=excluded.timestamp,"
                "digest=excluded.digest,vectors=excluded.vectors,terms=excluded.terms",
                updates,
            )
            # Input is an explicit complete bounded selection, not a failed remote scan.
            existing = [row[0] for row in connection.execute("SELECT reference FROM embeddings")]
            obsolete = set(existing) - set(refs)
            connection.executemany(
                "DELETE FROM embeddings WHERE reference=?", [(reference,) for reference in obsolete]
            )
            values = {
                "identity": self.identity,
                "model": serialized_model,
                "dimensions": str(model.dimensions),
                "requested_dimensions": str(dimensions),
                "include_body": str(include_body).lower(),
                "revision": new_revision,
                "updated_at": datetime.now(UTC).isoformat(),
                "source_digest": source_digest,
                "source_limit": str(source_limit),
            }
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values.items(),
            )
            check_stop()
        return {
            **self.status(),
            "updated_documents": len(updates),
            "unchanged_documents": untouched,
            "removed_documents": len(obsolete),
            "training_chunks": model.training_chunks,
            "training_digest": model.training_digest,
            "retrained": rebuild or not bool(metadata.get("model")),
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        semantic_weight: float = 0.8,
        expected_source_digest: str | None = None,
    ) -> tuple[SemanticHit, ...]:
        if not self.enabled:
            raise UnsupportedFeatureError(
                "Semantic search is disabled; lexical search remains usable."
            )
        if (
            not query.strip()
            or not 1 <= limit <= 500
            or not math.isfinite(semantic_weight)
            or not 0 <= semantic_weight <= 1
        ):
            raise SemanticStateError("Semantic query, limit or hybrid weight is invalid.")
        if not self.path.exists():
            raise SemanticStateError("No local semantic model is built; inspect semantic status.")
        query_terms = set(tokens(query))
        with self._connect(readonly=True) as connection:
            metadata = self._metadata(connection)
            if (
                expected_source_digest is not None
                and metadata.get("source_digest") != expected_source_digest
            ):
                raise SemanticConflictError(
                    "The selected local index changed; update the semantic index before searching."
                )
            if not metadata.get("model"):
                raise SemanticStateError(
                    "No usable local model is saved; lexical search remains usable."
                )
            model = LocalTopicModel.from_bytes(metadata["model"].encode())
            projected = model.embed(query)
            hits = []
            rows = connection.execute(
                "SELECT * FROM embeddings ORDER BY reference LIMIT ?", (MAX_DOCUMENTS + 1,)
            )
            for index, row in enumerate(rows):
                if index >= MAX_DOCUMENTS:
                    raise SemanticStateError("Semantic document count exceeds the supported bound.")
                try:
                    if len(row["vectors"]) > 64 * 1024 or len(row["terms"]) > 32 * 1024:
                        raise SemanticStateError("Corrupt oversized local semantic projection.")
                    vectors, terms = json.loads(row["vectors"]), json.loads(row["terms"])
                    if (
                        not isinstance(vectors, list)
                        or len(vectors) > 5
                        or any(
                            not isinstance(v, list)
                            or len(v) != model.dimensions
                            or not all(
                                type(x) in (int, float) and math.isfinite(x) and abs(x) <= 1.001
                                for x in v
                            )
                            or (
                                any(v)
                                and not math.isclose(sum(x * x for x in v), 1.0, abs_tol=1e-5)
                            )
                            for v in vectors
                        )
                    ):
                        raise SemanticStateError(
                            "Corrupt local embeddings; preserve this store and rebuild explicitly."
                        )
                    if (
                        not isinstance(terms, list)
                        or len(terms) > 256
                        or any(
                            not isinstance(term, str) or not 1 <= len(term) <= 64 for term in terms
                        )
                    ):
                        raise SemanticStateError("Corrupt local lexical projection.")
                    semantic = max(
                        (
                            max(
                                0.0,
                                min(
                                    1.0, sum(a * b for a, b in zip(projected, vector, strict=True))
                                ),
                            )
                            for vector in vectors
                        ),
                        default=0.0,
                    )
                    lexical = (
                        len(query_terms & set(terms)) / len(query_terms) if query_terms else 0.0
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise SemanticStateError(
                        "Corrupt semantic index data; existing mail and lexical data are unchanged."
                    ) from exc
                score = semantic_weight * semantic + (1 - semantic_weight) * lexical
                if score > 1e-8:
                    hits.append(
                        SemanticHit(
                            row["reference"],
                            row["subject"],
                            row["folder"],
                            row["timestamp"],
                            score,
                            semantic,
                            lexical,
                        )
                    )
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.reference))[:limit])

    def clear(self, *, confirmed: bool) -> dict[str, object]:
        if not confirmed:
            raise ConfirmationRequiredError(
                "Clearing the local semantic model and embeddings requires confirmation."
            )
        if not self.path.exists():
            return {"cleared": 0, "semantic_index_exists": False, "mailbox_mutations": 0}
        with self._connect() as connection:
            self._metadata(connection)
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
            connection.execute("DELETE FROM embeddings")
            connection.execute("DELETE FROM metadata")
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                [("identity", self.identity), ("revision", uuid4().hex)],
            )
            connection.commit()
            connection.execute("VACUUM")
        return {
            "cleared": count,
            "semantic_index_exists": False,
            "mailbox_mutations": 0,
            "note": (
                "SQLite-derived content removed; filesystem snapshots or forensic physical "
                "erasure are outside this guarantee."
            ),
        }
