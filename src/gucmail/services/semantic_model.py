"""Bounded local TF-IDF/SVD topic model with validated data-only persistence.

Fitting requires optional NumPy; query projection uses standard-library arithmetic.
There are no remote embedding services, pretrained model downloads or pickled models.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import multiprocessing
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from gucmail.errors import ConfigurationError, ExitCode, UnsupportedFeatureError
from gucmail.services.extractors import ensure_resource_tracker

MODEL_VERSION = "local-lsa-v1"
MAX_TRAINING_CHUNKS = 2_048
MAX_VOCABULARY = 2_048
MAX_COMPONENTS = 96
MAX_CHUNK_CHARACTERS = 8_192
MAX_MODEL_BYTES = 8 * 1024 * 1024
_WORDS = re.compile(r"[^\W\d_][\w\u2019-]{1,63}", re.UNICODE)
# Common function words are deliberately small; all languages still tokenize locally.
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
    ]
)


class LocalModelError(ConfigurationError):
    pass


class ModelBuildCancelled(LocalModelError):
    exit_code = ExitCode.INTERRUPTED


def tokens(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or len(text) > MAX_CHUNK_CHARACTERS:
        raise LocalModelError("Model text exceeds the bounded chunk size.")
    return tuple(word for word in _WORDS.findall(text.casefold()) if word not in _STOP)


def unit(values: Sequence[float]) -> tuple[float, ...]:
    converted = tuple(float(value) for value in values)
    if (
        not converted
        or len(converted) > MAX_COMPONENTS
        or not all(math.isfinite(x) for x in converted)
    ):
        raise LocalModelError("Embedding dimensions or finite values are invalid.")
    norm = math.sqrt(sum(x * x for x in converted))
    return tuple(x / norm for x in converted) if norm else tuple(0.0 for _ in converted)


@dataclass(frozen=True, slots=True)
class LocalTopicModel:
    vocabulary: tuple[str, ...]
    inverse_document_frequency: tuple[float, ...]
    components: tuple[tuple[float, ...], ...]
    training_chunks: int
    training_digest: str

    @property
    def dimensions(self) -> int:
        return len(self.components)

    @property
    def identity(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def to_bytes(self) -> bytes:
        value = {
            "version": MODEL_VERSION,
            "vocabulary": self.vocabulary,
            "idf": self.inverse_document_frequency,
            "components": self.components,
            "training_chunks": self.training_chunks,
            "training_digest": self.training_digest,
        }
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        if len(data) > MAX_MODEL_BYTES:
            raise LocalModelError("Local model exceeds the storage limit.")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> LocalTopicModel:
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_MODEL_BYTES:
            raise LocalModelError("Local model size is invalid.")
        try:
            raw = json.loads(data)
            if (
                not isinstance(raw, dict)
                or set(raw)
                != {
                    "version",
                    "vocabulary",
                    "idf",
                    "components",
                    "training_chunks",
                    "training_digest",
                }
                or raw["version"] != MODEL_VERSION
            ):
                raise LocalModelError("Unsupported local model version or structure.")
            vocabulary, idf, components = raw["vocabulary"], raw["idf"], raw["components"]
            if (
                not isinstance(vocabulary, list)
                or not 1 <= len(vocabulary) <= MAX_VOCABULARY
                or any(not isinstance(word, str) or not 1 <= len(word) <= 64 for word in vocabulary)
                or len(set(vocabulary)) != len(vocabulary)
            ):
                raise LocalModelError("Invalid local model vocabulary.")
            if (
                not isinstance(idf, list)
                or len(idf) != len(vocabulary)
                or not all(
                    type(v) in (int, float) and math.isfinite(v) and 0 < v <= 20 for v in idf
                )
            ):
                raise LocalModelError("Invalid local model term weights.")
            if not isinstance(components, list) or not 1 <= len(components) <= min(
                MAX_COMPONENTS, len(vocabulary)
            ):
                raise LocalModelError("Invalid local model dimensions.")
            if any(
                not isinstance(row, list)
                or len(row) != len(vocabulary)
                or not all(
                    type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1.001 for v in row
                )
                for row in components
            ):
                raise LocalModelError("Invalid local model components.")
            chunks = raw["training_chunks"]
            digest = raw["training_digest"]
            if (
                type(chunks) is not int
                or not 1 <= chunks <= MAX_TRAINING_CHUNKS
                or not isinstance(digest, str)
                or not re.fullmatch("[0-9a-f]{64}", digest)
            ):
                raise LocalModelError("Invalid local model provenance.")
            # Reject a corrupted basis instead of interpreting arbitrary weights as a model.
            for i, row in enumerate(components):
                if abs(sum(value * value for value in row) - 1.0) > 1e-4:
                    raise LocalModelError("Local model basis is not normalized.")
                for other in components[:i]:
                    if abs(sum(a * b for a, b in zip(row, other, strict=True))) > 1e-4:
                        raise LocalModelError("Local model basis is not orthogonal.")
            return cls(
                tuple(vocabulary),
                tuple(float(x) for x in idf),
                tuple(tuple(float(x) for x in row) for row in components),
                chunks,
                digest,
            )
        except (
            KeyError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise LocalModelError("Local model data is corrupt.") from exc

    @classmethod
    def fit(cls, chunks: Sequence[str], *, dimensions: int = 32) -> LocalTopicModel:
        if (
            type(dimensions) is not int
            or not 1 <= dimensions <= MAX_COMPONENTS
            or not 2 <= len(chunks) <= MAX_TRAINING_CHUNKS
        ):
            raise LocalModelError("Choose 2-2048 text chunks and 1-96 dimensions.")
        counts = [Counter(tokens(chunk)) for chunk in chunks]
        frequency = Counter(word for chunk in counts for word in chunk)
        vocabulary = tuple(
            sorted(frequency, key=lambda word: (-frequency[word], word))[:MAX_VOCABULARY]
        )
        if len(vocabulary) < 2:
            raise LocalModelError("The selected text needs more distinct terms to train a model.")
        idf = tuple(math.log((1 + len(chunks)) / (1 + frequency[word])) + 1 for word in vocabulary)
        try:
            numpy: Any = importlib.import_module("numpy")
        except ImportError as exc:
            raise LocalModelError(
                "Optional NumPy is unavailable; lexical search remains usable."
            ) from exc
        matrix = numpy.zeros((len(chunks), len(vocabulary)), dtype=numpy.float64)
        columns = {word: index for index, word in enumerate(vocabulary)}
        for index, count in enumerate(counts):
            for word, number in count.items():
                if word in columns:
                    column = columns[word]
                    matrix[index, column] = (1 + math.log(number)) * idf[column]
            length = numpy.linalg.norm(matrix[index])
            if length:
                matrix[index] /= length
        try:
            _, singular, basis = numpy.linalg.svd(matrix, full_matrices=False)
        except numpy.linalg.LinAlgError as exc:
            raise LocalModelError(
                "Local model decomposition failed; no new model was saved."
            ) from exc
        rank = int(
            numpy.count_nonzero(
                singular > max(matrix.shape) * numpy.finfo(matrix.dtype).eps * singular[0]
            )
        )
        if not rank:
            raise LocalModelError("The selected text has no usable semantic basis.")
        # Retain a genuinely reduced space whenever rank permits it.
        size = min(dimensions, max(1, rank - 1))
        weights = []
        for row in basis[:size]:
            # Fix the arbitrary SVD sign to preserve byte identity across equivalent fits.
            sign = -1 if row[int(numpy.argmax(numpy.abs(row)))] < 0 else 1
            weights.append(tuple(float(value) * sign for value in row))
        digest = hashlib.sha256(
            json.dumps(list(chunks), ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        model = cls(vocabulary, idf, tuple(weights), len(chunks), digest)
        return cls.from_bytes(model.to_bytes())

    def embed(self, text: str) -> tuple[float, ...]:
        count = Counter(tokens(text))
        sparse = [
            (column, (1 + math.log(count[word])) * self.inverse_document_frequency[column])
            for column, word in enumerate(self.vocabulary)
            if count[word]
        ]
        return unit(
            tuple(sum(row[column] * weight for column, weight in sparse) for row in self.components)
        )

    def similarity(self, left: str, right: str) -> float:
        a, b = self.embed(left), self.embed(right)
        return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))


def _model_worker(connection: Connection, chunks: tuple[str, ...], dimensions: int) -> None:
    """Fit only validated local strings; never serialize an exception or a traceback."""

    def no_remote(event: str, _args: tuple[object, ...]) -> None:
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise LocalModelError("The local model worker cannot access the network.")

    sys.addaudithook(no_remote)
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"
    try:
        try:
            model = LocalTopicModel.fit(chunks, dimensions=dimensions)
            connection.send_bytes(b"M" + model.to_bytes())
        except LocalModelError:
            # Raw model/parser exception text can contain indexed words. Keep the
            # cross-process boundary fixed, bounded and independent of user text.
            available = importlib.util.find_spec("numpy") is not None
            connection.send_bytes(b"E" if available else b"D")
        except Exception:
            connection.send_bytes(b"E")
    finally:
        connection.close()


def fit_in_worker(
    chunks: Sequence[str],
    *,
    dimensions: int = 32,
    cancelled: Callable[[], bool] | None = None,
    timeout_seconds: float = 60,
) -> LocalTopicModel:
    """Bound fitting in one owned process; existing persisted data is never modified."""
    if (
        not 2 <= len(chunks) <= MAX_TRAINING_CHUNKS
        or type(dimensions) is not int
        or not 1 <= dimensions <= MAX_COMPONENTS
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 120
    ):
        raise LocalModelError("Model request exceeds the supported fitting bounds.")
    for chunk in chunks:
        tokens(chunk)
    if cancelled and cancelled():
        raise ModelBuildCancelled("Local model build cancelled; existing embeddings are retained.")
    ensure_resource_tracker()
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_model_worker, args=(send, tuple(chunks), dimensions))
    process.daemon = True
    deadline = time.monotonic() + timeout_seconds
    try:
        process.start()
        send.close()
        while not receive.poll(0.05):
            if cancelled and cancelled():
                raise ModelBuildCancelled(
                    "Local model build cancelled; existing embeddings are retained."
                )
            if time.monotonic() >= deadline:
                raise LocalModelError("Local model fitting timed out; existing model is retained.")
            if not process.is_alive():
                raise LocalModelError("Local model worker stopped without a usable result.")
        response = receive.recv_bytes(MAX_MODEL_BYTES + 1)
        if time.monotonic() >= deadline:
            raise LocalModelError("Local model fitting timed out; existing model is retained.")
        if cancelled and cancelled():
            raise ModelBuildCancelled(
                "Local model build cancelled; existing embeddings are retained."
            )
        if response == b"D":
            raise UnsupportedFeatureError(
                "Model fitting requires the optional semantic dependency. "
                "Lexical search and existing model queries remain usable."
            )
        if not response.startswith(b"M"):
            raise LocalModelError("Local model fitting failed; existing model is retained.")
        return LocalTopicModel.from_bytes(response[1:])
    except (EOFError, OSError) as exc:
        raise LocalModelError("Local model worker failed; existing model is retained.") from exc
    finally:
        receive.close()
        send.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()
