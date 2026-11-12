"""Preview-first mailbox cleanup planning with callback-only execution.

This module intentionally contains no Exchange adapter and no delete operation.  A
caller must inspect a :class:`CleanupPlan`, obtain confirmation, and explicitly pass
an execution callback.  This keeps analysis safe in offline tools and in tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeAlias

from gucmail.errors import ConfirmationRequiredError
from gucmail.security import redact_text
from gucmail.services.insights import (
    DEFAULT_MAX_ITEMS,
    DuplicateReport,
    InsightMessage,
    duplicate_insights,
)

RECOVERABLE_ACTIONS = frozenset({"move_to_deleted", "archive", "move"})
PERMANENT_ACTIONS = frozenset({"hard_delete", "permanent_delete"})
ALLOWED_ACTIONS = RECOVERABLE_ACTIONS | PERMANENT_ACTIONS


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    reference: str
    reason: str
    size_bytes: int = 0
    folder_path: str = ""
    action: str = "move_to_deleted"
    recoverable: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    plan_id: str
    created_at: datetime
    candidates: tuple[CleanupCandidate, ...]
    excluded_references: tuple[str, ...] = ()
    action: str = "move_to_deleted"
    permanent: bool = False
    preview_only: bool = True
    recovery_guidance: str = (
        "Items are moved to Deleted Items by default. Restore them from that folder "
        "before the server retention period expires."
    )

    @property
    def item_count(self) -> int:
        return len(self.candidates)

    @property
    def estimated_reclaimable_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)

    @property
    def confirmation_token(self) -> str:
        """Short plan-specific token required for explicitly permanent execution."""

        return self.plan_id[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at.isoformat(),
            "action": self.action,
            "permanent": self.permanent,
            "preview_only": self.preview_only,
            "item_count": self.item_count,
            "estimated_reclaimable_bytes": self.estimated_reclaimable_bytes,
            "excluded_references": list(self.excluded_references),
            "recovery_guidance": self.recovery_guidance,
            "candidates": [asdict(candidate) for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class CleanupItemResult:
    reference: str
    succeeded: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupExecutionResult:
    plan_id: str
    attempted_count: int
    succeeded_count: int
    failed_count: int
    stopped_early: bool
    executed_at: datetime
    results: tuple[CleanupItemResult, ...]


CleanupCallback: TypeAlias = Callable[[CleanupCandidate], object]


def create_cleanup_plan(
    candidates: Iterable[CleanupCandidate | Mapping[str, Any]],
    *,
    exclusions: Iterable[str] = (),
    action: str = "move_to_deleted",
    permanent: bool | None = None,
    created_at: datetime | None = None,
) -> CleanupPlan:
    """Create an immutable preview; this function never executes an action."""

    action = action.strip().casefold()
    if action not in ALLOWED_ACTIONS:
        allowed = ", ".join(sorted(ALLOWED_ACTIONS))
        raise ValueError(f"Unsupported cleanup action {action!r}; expected one of: {allowed}.")
    action_is_permanent = action in PERMANENT_ACTIONS
    if permanent is not None and bool(permanent) is not action_is_permanent:
        raise ValueError(
            "The cleanup permanence flag must agree with the selected action; "
            "permanent actions cannot be downgraded and recoverable actions cannot be upgraded."
        )
    is_permanent = action_is_permanent
    excluded = tuple(sorted({str(reference) for reference in exclusions if str(reference)}))
    excluded_set = set(excluded)
    unique: dict[str, CleanupCandidate] = {}
    for value in candidates:
        candidate = _candidate(value, default_action=action, permanent=is_permanent)
        if candidate.reference in excluded_set:
            continue
        unique.setdefault(candidate.reference, candidate)

    ordered = tuple(sorted(unique.values(), key=lambda item: (item.reason, item.reference)))
    digest_input = json.dumps(
        {
            "action": action,
            "permanent": is_permanent,
            "excluded": excluded,
            "candidates": [
                (item.reference, item.reason, item.size_bytes, item.folder_path) for item in ordered
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    plan_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    guidance = (
        "Permanent deletion cannot normally be undone. Verify a current backup and the exact "
        "candidate list before executing."
        if is_permanent
        else (
            "Items are moved to Deleted Items by default. Restore them from that folder before "
            "the server retention period expires."
        )
    )
    return CleanupPlan(
        plan_id=plan_id,
        created_at=created_at or datetime.now(UTC),
        candidates=ordered,
        excluded_references=excluded,
        action=action,
        permanent=is_permanent,
        recovery_guidance=guidance,
    )


def analyze_cleanup(
    messages: Iterable[object],
    *,
    include_duplicates: bool = True,
    include_old_unread: bool = False,
    old_unread_days: int = 365,
    exclusions: Iterable[str] = (),
    now: datetime | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> CleanupPlan:
    """Analyze bounded local metadata and return a recoverable cleanup preview.

    Duplicate groups keep their first (oldest, then reference-sorted) item and propose
    the remaining copies.  Old unread candidates are opt-in because age alone does not
    establish that a message is unwanted.
    """

    if max_items < 1:
        raise ValueError("max_items must be at least 1.")
    if old_unread_days < 1:
        raise ValueError("old_unread_days must be at least 1.")
    records: list[InsightMessage] = []
    for position, value in enumerate(messages):
        if position >= max_items:
            break
        records.append(
            value if isinstance(value, InsightMessage) else InsightMessage.from_value(value)
        )
    by_reference = {item.reference: item for item in records}
    proposed: dict[str, CleanupCandidate] = {}

    if include_duplicates:
        report = duplicate_insights(records, max_items=max_items)
        for group in report.exact_groups:
            for reference in group.references[1:]:
                item = by_reference[reference]
                proposed[reference] = CleanupCandidate(
                    reference=reference,
                    reason="exact_duplicate",
                    size_bytes=item.size_bytes,
                    folder_path=item.folder_path,
                    details={
                        "duplicate_digest": group.digest,
                        "kept_reference": group.references[0],
                    },
                )

    if include_old_unread:
        current = now or datetime.now(UTC)
        threshold = current - timedelta(days=old_unread_days)
        for item in records:
            received = _aware(item.received_at)
            if not item.is_read and received is not None and received < threshold:
                proposed.setdefault(
                    item.reference,
                    CleanupCandidate(
                        reference=item.reference,
                        reason="old_unread",
                        size_bytes=item.size_bytes,
                        folder_path=item.folder_path,
                        details={
                            "received_at": received.isoformat(),
                            "older_than_days": old_unread_days,
                        },
                    ),
                )

    return create_cleanup_plan(proposed.values(), exclusions=exclusions)


def plan_duplicate_cleanup(
    messages: Iterable[object] | None = None,
    *,
    report: DuplicateReport | None = None,
    message_sizes: Mapping[str, int] | None = None,
    exclusions: Iterable[str] = (),
) -> CleanupPlan:
    """Build a duplicate-only preview from messages or an existing duplicate report."""

    records: list[InsightMessage] = []
    if messages is not None:
        records = [
            value if isinstance(value, InsightMessage) else InsightMessage.from_value(value)
            for value in messages
        ]
    if report is None:
        if messages is None:
            raise ValueError("messages or report is required.")
        report = duplicate_insights(records)
    sizes = dict(message_sizes or {})
    sizes.update({item.reference: item.size_bytes for item in records})
    paths = {item.reference: item.folder_path for item in records}
    candidates = [
        CleanupCandidate(
            reference=reference,
            reason="exact_duplicate",
            size_bytes=max(0, int(sizes.get(reference, 0))),
            folder_path=paths.get(reference, ""),
            details={"duplicate_digest": group.digest, "kept_reference": group.references[0]},
        )
        for group in report.exact_groups
        for reference in group.references[1:]
    ]
    return create_cleanup_plan(candidates, exclusions=exclusions)


def exclude_from_plan(plan: CleanupPlan, references: Iterable[str]) -> CleanupPlan:
    """Return a new preview with additional candidates excluded."""

    exclusions = set(plan.excluded_references)
    exclusions.update(str(reference) for reference in references)
    return create_cleanup_plan(
        plan.candidates,
        exclusions=exclusions,
        action=plan.action,
        permanent=plan.permanent,
    )


def execute_cleanup_plan(
    plan: CleanupPlan,
    callback: CleanupCallback | None = None,
    *,
    executor: CleanupCallback | None = None,
    confirmed: bool = False,
    confirmation_token: str | None = None,
    allow_permanent: bool = False,
    continue_on_error: bool = False,
) -> CleanupExecutionResult:
    """Execute through an explicitly supplied callback after confirmation.

    There is intentionally no default executor.  The callback receives one fully
    described candidate at a time and is responsible for calling the mailbox adapter.
    No retries occur here; retrying a destructive request automatically can be unsafe.
    """

    selected = callback or executor
    if callback is not None and executor is not None:
        raise ValueError("Pass callback or executor, not both.")
    if selected is None or not callable(selected):
        raise ValueError("An explicit cleanup execution callback is required.")
    _validate_plan_safety(plan)
    if not confirmed:
        raise ConfirmationRequiredError(
            "Cleanup execution requires explicit confirmation after previewing the plan."
        )
    if plan.permanent:
        if not allow_permanent:
            raise ConfirmationRequiredError(
                "Permanent cleanup requires an explicit allow_permanent override."
            )
        if confirmation_token != plan.confirmation_token:
            raise ConfirmationRequiredError(
                "Permanent cleanup requires the plan-specific confirmation token."
            )

    results: list[CleanupItemResult] = []
    stopped_early = False
    for candidate in plan.candidates:
        try:
            outcome = selected(candidate)
            detail = None if outcome is None else str(outcome)
            results.append(CleanupItemResult(candidate.reference, True, detail))
        except Exception as exc:  # Callback boundary: report safely, never retry implicitly.
            results.append(CleanupItemResult(candidate.reference, False, _safe_error(exc)))
            if not continue_on_error:
                stopped_early = True
                break
    succeeded = sum(item.succeeded for item in results)
    return CleanupExecutionResult(
        plan_id=plan.plan_id,
        attempted_count=len(results),
        succeeded_count=succeeded,
        failed_count=len(results) - succeeded,
        stopped_early=stopped_early,
        executed_at=datetime.now(UTC),
        results=tuple(results),
    )


def export_plan_json(plan: CleanupPlan, *, indent: int = 2) -> str:
    """Serialize a preview for audit/export without changing local or remote state."""

    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


class CleanupPlanner:
    """Small facade used by CLI/TUI layers."""

    def __init__(self, *, max_items: int = DEFAULT_MAX_ITEMS) -> None:
        self.max_items = max_items

    def analyze(self, messages: Iterable[object], **options: Any) -> CleanupPlan:
        options.setdefault("max_items", self.max_items)
        return analyze_cleanup(messages, **options)

    def execute(
        self,
        plan: CleanupPlan,
        callback: CleanupCallback,
        *,
        confirmed: bool = False,
        **options: Any,
    ) -> CleanupExecutionResult:
        return execute_cleanup_plan(plan, callback, confirmed=confirmed, **options)


preview_cleanup = analyze_cleanup
execute_plan = execute_cleanup_plan


def _candidate(
    value: CleanupCandidate | Mapping[str, Any], *, default_action: str, permanent: bool
) -> CleanupCandidate:
    if isinstance(value, CleanupCandidate):
        if value.action == default_action and value.recoverable == (not permanent):
            return value
        return CleanupCandidate(
            reference=value.reference,
            reason=value.reason,
            size_bytes=value.size_bytes,
            folder_path=value.folder_path,
            action=default_action,
            recoverable=not permanent,
            details=value.details,
        )
    reference = str(value.get("reference") or value.get("id") or "").strip()
    if not reference:
        raise ValueError("Every cleanup candidate must have a non-empty reference.")
    raw_details = value.get("details")
    details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
    return CleanupCandidate(
        reference=reference,
        reason=str(value.get("reason") or "user_selected"),
        size_bytes=max(0, int(value.get("size_bytes") or value.get("size") or 0)),
        folder_path=str(value.get("folder_path") or value.get("folder") or ""),
        action=default_action,
        recoverable=not permanent,
        details=details,
    )


def _validate_plan_safety(plan: CleanupPlan) -> None:
    """Re-check invariants at the execution boundary, including hand-built plans."""

    action = plan.action.strip().casefold()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Cleanup plan contains unsupported action {plan.action!r}.")
    expected_permanent = action in PERMANENT_ACTIONS
    if plan.permanent is not expected_permanent:
        raise ValueError("Cleanup plan action and permanence are inconsistent.")
    for candidate in plan.candidates:
        if candidate.action.strip().casefold() != action:
            raise ValueError(
                f"Cleanup candidate {candidate.reference!r} has an action "
                "inconsistent with its plan."
            )
        if candidate.recoverable is expected_permanent:
            raise ValueError(
                f"Cleanup candidate {candidate.reference!r} has inconsistent recoverability."
            )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_error(exc: Exception) -> str:
    # Callback exceptions might contain server internals.  Expose only a small, local
    # description; higher layers can log a separately redacted diagnostic if desired.
    text = redact_text(str(exc).replace("\r", " ").replace("\n", " ").strip())
    return f"{type(exc).__name__}: {text[:300]}"


__all__ = [
    "ALLOWED_ACTIONS",
    "PERMANENT_ACTIONS",
    "RECOVERABLE_ACTIONS",
    "CleanupCallback",
    "CleanupCandidate",
    "CleanupExecutionResult",
    "CleanupItemResult",
    "CleanupPlan",
    "CleanupPlanner",
    "analyze_cleanup",
    "create_cleanup_plan",
    "exclude_from_plan",
    "execute_cleanup_plan",
    "execute_plan",
    "export_plan_json",
    "plan_duplicate_cleanup",
    "preview_cleanup",
]
