"""Local observations of accepted Task updates, never reconstructed server history."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.security import sanitize_terminal

if TYPE_CHECKING:
    from gucmail.state import StateStore

MAX_OBSERVATIONS = 10_000
ACTIONS = frozenset({"edit", "complete", "reopen"})
OUTCOMES = frozenset(
    {"accepted", "readback_verified", "readback_unavailable", "readback_not_requested"}
)


def history_reference(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ConfigurationError("Task history requires a bounded local reference.")
    result = value.strip()
    if (
        result.startswith("ews:")
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
        or sanitize_terminal(result) != result
    ):
        raise ConfigurationError("Task history requires a safe local reference, not a raw EWS ID.")
    return result


@dataclass(frozen=True, slots=True)
class TaskObservation:
    sequence: int
    operation_id: str
    observed_at: datetime
    family_reference: str
    original_reference: str
    result_reference: str
    action: str
    distinct_instance: bool
    outcome: str


@dataclass(frozen=True, slots=True)
class TaskHistoryPage:
    requested_reference: str
    family_reference: str
    observations: tuple[TaskObservation, ...]
    next_before: int | None
    persistence: str
    retained_limit: int
    scope: str = "Local accepted updates only; unseen server history is unavailable."


def observation_from_row(row: dict[str, Any]) -> TaskObservation:
    try:
        observed_at = datetime.fromisoformat(row["observed_at"])
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Missing observation time zone")
        if row["action"] not in ACTIONS or row["outcome"] not in OUTCOMES:
            raise ValueError("Unknown observation vocabulary")
        if type(row["sequence"]) is not int or row["sequence"] <= 0:
            raise ValueError("Invalid observation sequence")
        identifier = row["operation_id"]
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(char not in "0123456789abcdef" for char in identifier)
        ):
            raise ValueError("Invalid observation identity")
        for name in ("family_reference", "original_reference", "result_reference"):
            if history_reference(row[name]) != row[name]:
                raise ValueError("Noncanonical observation reference")
        distinct = row["distinct_instance"]
        if type(distinct) not in {bool, int} or distinct not in (0, 1):
            raise ValueError("Invalid observation relation")
        if bool(distinct) != (row["original_reference"] != row["result_reference"]):
            raise ValueError("Inconsistent observed identities")
    except (KeyError, TypeError, ValueError, ConfigurationError) as exc:
        raise LocalStateError(
            "A local Task observation is invalid; existing state was preserved."
        ) from exc
    return TaskObservation(
        sequence=int(row["sequence"]),
        operation_id=str(row["operation_id"]),
        observed_at=observed_at,
        family_reference=str(row["family_reference"]),
        original_reference=str(row["original_reference"]),
        result_reference=str(row["result_reference"]),
        action=str(row["action"]),
        distinct_instance=bool(row["distinct_instance"]),
        outcome=str(row["outcome"]),
    )


class TaskHistory:
    """Same bounded metadata contract for durable state and cacheless sessions."""

    def __init__(
        self, state: StateStore | None = None, *, retained_limit: int = MAX_OBSERVATIONS
    ) -> None:
        if type(retained_limit) is not int or not 1 <= retained_limit <= MAX_OBSERVATIONS:
            raise ConfigurationError("Task history retention must be between 1 and 10000 updates.")
        self.state = state
        self.retained_limit = retained_limit
        self._lock = threading.RLock()
        self._sequence = 0
        self._observations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._members: dict[str, str] = {}

    def accepted(
        self,
        original_reference: str,
        result_reference: str,
        *,
        action: str,
        distinct_instance: bool,
        operation_id: str | None = None,
    ) -> TaskObservation:
        original = history_reference(original_reference)
        result = history_reference(result_reference)
        if self.state is not None:
            canonical_original = self.state.resolve_reference(original, "task")
            canonical_result = self.state.resolve_reference(result, "task")
            if canonical_original:
                original = history_reference(str(canonical_original["canonical_ref"]))
            if canonical_result:
                result = history_reference(str(canonical_result["canonical_ref"]))
        if action not in ACTIONS or type(distinct_instance) is not bool:
            raise ConfigurationError("Invalid Task history action or identity observation.")
        if distinct_instance != (original != result):
            raise ConfigurationError("The observed Task identities disagree with their references.")
        identifier = operation_id or uuid4().hex
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(c not in "0123456789abcdef" for c in identifier)
        ):
            raise ConfigurationError("Task history operation IDs must be opaque UUID hex values.")
        now = datetime.now(UTC).isoformat()
        fields: dict[str, Any] = dict(
            operation_id=identifier,
            observed_at=now,
            original_reference=original,
            result_reference=result,
            action=action,
            distinct_instance=distinct_instance,
            retained_limit=self.retained_limit,
        )
        if self.state is not None:
            return observation_from_row(self.state.record_task_observation(**fields))
        with self._lock:
            previous = self._observations.get(identifier)
            if previous is not None:
                if any(
                    previous[key] != fields[key]
                    for key in (
                        "original_reference",
                        "result_reference",
                        "action",
                        "distinct_instance",
                    )
                ):
                    raise ConfigurationError(
                        "Task history operation identity was reused with different data."
                    )
                return observation_from_row(previous)
            family = self._members.get(original, original)
            if result in self._members and self._members[result] != family:
                raise ConfigurationError(
                    "The returned Task already belongs to another observed family."
                )
            self._sequence += 1
            row = {
                **fields,
                "sequence": self._sequence,
                "family_reference": family,
                "outcome": "accepted",
            }
            row.pop("retained_limit")
            self._observations[identifier] = row
            while len(self._observations) > self.retained_limit:
                self._observations.popitem(last=False)
            self._members = {
                reference: r["family_reference"]
                for r in self._observations.values()
                for reference in (
                    r["original_reference"],
                    r["result_reference"],
                    r["family_reference"],
                )
            }
            return observation_from_row(row)

    def finish(self, operation_id: str, *, verified: bool | None) -> None:
        if verified is not None and type(verified) is not bool:
            raise ConfigurationError("Task history readback state must be explicit.")
        outcome = (
            "readback_not_requested"
            if verified is None
            else ("readback_verified" if verified else "readback_unavailable")
        )
        if self.state is not None:
            self.state.finish_task_observation(operation_id, outcome=outcome)
            return
        with self._lock:
            row = self._observations.get(operation_id)
            if row is None:
                raise ConfigurationError("The Task observation is no longer retained.")
            if row["outcome"] not in {"accepted", outcome}:
                raise ConfigurationError(
                    "The Task observation already has a different readback outcome."
                )
            row["outcome"] = outcome

    def page(
        self, reference: str, *, limit: int = 20, before: int | None = None
    ) -> TaskHistoryPage:
        requested = history_reference(reference)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ConfigurationError("Task history pages must contain 1 to 200 updates.")
        if before is not None and (type(before) is not int or before <= 0):
            raise ConfigurationError(
                "Task history continuation must be a positive sequence number."
            )
        if self.state is not None:
            family, rows = self.state.task_observations(requested, limit=limit + 1, before=before)
            persistence = "persistent local state"
        else:
            with self._lock:
                family = self._members.get(requested, requested)
                rows = [
                    dict(row)
                    for row in reversed(self._observations.values())
                    if row["family_reference"] == family
                    and (before is None or row["sequence"] < before)
                ][: limit + 1]
            persistence = "session only; unavailable after exit"
        observations = tuple(observation_from_row(row) for row in rows[:limit])
        next_before = observations[-1].sequence if len(rows) > limit else None
        return TaskHistoryPage(
            requested, family, observations, next_before, persistence, self.retained_limit
        )


def history_text(page: TaskHistoryPage) -> str:
    lines = [
        "Local Task history",
        page.scope,
        f"Storage: {page.persistence}",
        f"Retention: at most {page.retained_limit} accepted updates per account.",
        f"Observed family: {page.family_reference}",
        "",
    ]
    if not page.observations:
        lines.append("No retained updates observed by this application for this Task.")
    for row in page.observations:
        relation = "separate returned instance" if row.distinct_instance else "same Task identity"
        lines.extend(
            (
                f"{row.observed_at.isoformat()} · {row.action} · {row.outcome}",
                f"Original: {row.original_reference}",
                f"Result: {row.result_reference} ({relation})",
                "",
            )
        )
    if page.next_before is not None:
        lines.append("Older retained updates are available.")
    return sanitize_terminal("\n".join(lines))
