"""Durable, account-scoped claims for explicitly requested immediate sends.

An interrupted claim never expires into an automatic retry: Exchange may have
accepted the message. Records contain local references and outcomes only.
"""

from __future__ import annotations

from dataclasses import dataclass

from gucmail.errors import ConflictError, ErrorContext
from gucmail.state import StateStore, iso


@dataclass(frozen=True, slots=True)
class SendAttempt:
    draft_reference: str
    attempt_id: str
    state: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompositionSendStatus:
    """Local send/queue projection; an accepted request is not recipient delivery."""

    state: str
    attempt_id: str
    updated_at: str
    source: str = "immediate"
    queue_state: str | None = None


class SendAttemptStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get(self, reference: str) -> SendAttempt | None:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM send_attempts WHERE account_key=? AND draft_reference=?",
                (self.state.account_key, reference),
            ).fetchone()
        if row is None:
            return None
        return SendAttempt(reference, row["attempt_id"], row["state"], row["updated_at"])

    def composition_status(self, reference: str) -> CompositionSendStatus | None:
        immediate = self.get(reference)
        if immediate and immediate.state != "failed":
            return CompositionSendStatus(
                immediate.state, immediate.attempt_id, immediate.updated_at
            )
        job = self.state.blocking_job_for_draft(reference)
        if job:
            queued_state = str(job["state"])
            state = {"sent": "sent", "sending": "sending", "send_state_uncertain": "uncertain"}.get(
                queued_state, "queued"
            )
            return CompositionSendStatus(
                state, str(job["reference"]), str(job["updated_at"]), "scheduled", queued_state
            )
        return (
            CompositionSendStatus(immediate.state, immediate.attempt_id, immediate.updated_at)
            if immediate
            else None
        )

    def claim(self, reference: str, attempt_id: str) -> None:
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM send_attempts WHERE account_key=? AND draft_reference=?",
                (self.state.account_key, reference),
            ).fetchone()
            if row is not None and row["state"] != "failed":
                outcome = str(row["state"])
                raise ConflictError(
                    f"This draft already has a send attempt ({outcome}); it was not sent again.",
                    context=ErrorContext(
                        retry_safe=False,
                        mutation_uncertain=outcome in {"sending", "uncertain"},
                        suggestion=(
                            "Check Sent Items and the recipient's mailbox before sending again. "
                            "Use 'drafts send-status' to inspect the local attempt. If another "
                            "send is intentional, duplicate the draft and review the new copy."
                        ),
                    ),
                )
            now = iso()
            connection.execute(
                """
                INSERT INTO send_attempts VALUES (?, ?, ?, 'sending', ?, ?)
                ON CONFLICT(account_key, draft_reference) DO UPDATE SET
                    attempt_id=excluded.attempt_id, state='sending', updated_at=excluded.updated_at
                """,
                (self.state.account_key, reference, attempt_id, now, now),
            )

    def finish(self, reference: str, attempt_id: str, outcome: str) -> None:
        if outcome not in {"sent", "uncertain", "failed"}:
            raise ValueError("Invalid send attempt outcome")
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE send_attempts SET state=?, updated_at=?
                WHERE account_key=? AND draft_reference=? AND attempt_id=? AND state='sending'
                """,
                (outcome, iso(), self.state.account_key, reference, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("The send attempt record changed concurrently.")

    def reconcile(self, reference: str, attempt_id: str, outcome: str) -> None:
        """Record a proven outcome for this exact attempt; never clear another send."""
        if outcome not in {"sent", "failed"}:
            raise ValueError("Reconciliation requires a definite outcome")
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT attempt_id, state FROM send_attempts "
                "WHERE account_key=? AND draft_reference=?",
                (self.state.account_key, reference),
            ).fetchone()
            if row is not None and (
                row["attempt_id"] != attempt_id or (row["state"] == "sent" and outcome != "sent")
            ):
                raise ConflictError(
                    "Reconciliation cannot replace a different or accepted send attempt."
                )
            now = iso()
            connection.execute(
                """INSERT INTO send_attempts VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key, draft_reference) DO UPDATE SET
                    state=excluded.state, updated_at=excluded.updated_at""",
                (self.state.account_key, reference, attempt_id, outcome, now, now),
            )
