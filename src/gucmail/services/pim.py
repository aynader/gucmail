"""Shared safety primitives for calendar, contact, and task workflows.

The EWS adapter intentionally accepts at most one bounded page at a time.  PIM
services use :func:`bounded_pim_limit` so presentation defaults cannot accidentally
request an unsupported or privacy-invasive page.  Batch operations return explicit
outcomes instead of hiding partial success when a later item fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from gucmail.errors import ConfigurationError
from gucmail.security import redact_text, sanitize_terminal

MAX_PIM_PAGE_SIZE = 200

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PimPage(Generic[T]):
    """One bounded candidate window; offsets count candidates, not matches.

    A full window offers a continuation even if the next page may be empty. This
    avoids an extra request just to discover whether another candidate exists.
    """

    items: tuple[T, ...]
    offset: int
    scanned: int
    next_offset: int | None = None
    residual: str | None = None

    @property
    def notice(self) -> str:
        text = (
            f"{len(self.items)} result(s) · {self.scanned} candidates examined "
            f"at offset {self.offset}."
        )
        if self.residual:
            text += f" {self.residual}"
        if self.next_offset is not None:
            text += " Continue to check later candidates, including after an empty page."
        return text


def bounded_pim_limit(
    value: int | None,
    *,
    default: int,
    subject: str,
    maximum: int = MAX_PIM_PAGE_SIZE,
) -> int:
    """Validate and clamp a one-page PIM request to a transport-safe maximum.

    Callers that need more than one page must implement explicit pagination.  Silent
    truncation here is intentional and safe: these service methods have historically
    represented a single bounded view, while passing a larger value made the adapter
    fail before issuing any request.
    """

    requested = default if value is None else value
    if requested <= 0:
        raise ConfigurationError(f"{subject} result limit must be positive.")
    if maximum <= 0:
        raise ConfigurationError("PIM maximum result limit must be positive.")
    return min(requested, maximum)


@dataclass(frozen=True, slots=True)
class BatchFailure:
    """One failed step in an otherwise bounded batch operation."""

    index: int
    operation: str
    reference: str | None
    error_type: str
    detail: str
    retry_safe: bool = False

    @classmethod
    def from_exception(
        cls,
        *,
        index: int,
        operation: str,
        reference: str | None,
        error: BaseException,
        retry_safe: bool = False,
    ) -> BatchFailure:
        """Build a terminal-safe failure record without tracebacks or secret material."""

        error_type = sanitize_terminal(type(error).__name__, max_length=100) or "Error"
        detail = sanitize_terminal(redact_text(error), max_length=500) or "Operation failed."
        return cls(
            index=index,
            operation=sanitize_terminal(operation, max_length=100),
            reference=sanitize_terminal(reference, max_length=500) if reference else None,
            error_type=error_type,
            detail=detail,
            retry_safe=retry_safe,
        )


@dataclass(frozen=True, slots=True)
class BatchSuccess(Generic[T]):
    """One completed step and its optional provider result."""

    index: int
    operation: str
    reference: str | None
    value: T | None = None


@dataclass(frozen=True, slots=True)
class BatchOperationResult(Generic[T]):
    """Structured, reconciliation-friendly result for a bounded mutation batch."""

    attempted: int
    completed: tuple[BatchSuccess[T], ...] = ()
    failures: tuple[BatchFailure, ...] = ()
    skipped_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.attempted < 0:
            raise ValueError("Batch attempted count may not be negative.")
        accounted = len(self.completed) + len(self.failures) + len(self.skipped_references)
        if accounted > self.attempted:
            raise ValueError("Batch result accounts for more steps than were attempted.")
        indexes = [item.index for item in self.completed]
        indexes.extend(item.index for item in self.failures)
        if len(indexes) != len(set(indexes)):
            raise ValueError("Batch result step indexes must be unique.")
        if any(index < 0 or index >= self.attempted for index in indexes):
            raise ValueError("Batch result step index is outside the requested batch.")

    @property
    def succeeded(self) -> int:
        return len(self.completed)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def skipped(self) -> int:
        return len(self.skipped_references)

    @property
    def is_complete(self) -> bool:
        return self.failed == 0 and self.succeeded + self.skipped == self.attempted

    @property
    def is_partial(self) -> bool:
        return self.succeeded > 0 and (self.failed > 0 or self.skipped > 0)


__all__ = [
    "MAX_PIM_PAGE_SIZE",
    "BatchFailure",
    "BatchOperationResult",
    "BatchSuccess",
    "bounded_pim_limit",
]
