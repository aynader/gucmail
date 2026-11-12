"""Account-scoped restore intent, dispatch, outcome and conservative replay fences."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from gucmail.errors import ConflictError, LocalStateError
from gucmail.state import StateStore, iso


@dataclass(frozen=True, slots=True)
class RestoreTargetIntent:
    item_key: str
    source_reference: str
    destination: str
    previous_token: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreRecovery:
    operation_id: str
    ordinal: int
    status: str
    result_reference: str | None

    @property
    def reference(self) -> str:
        return f"{self.operation_id}:{self.ordinal}"

    @property
    def token(self) -> str:
        return f"{self.reference}:{self.status}"


def replay_key(chain_digest: str, item_key: str, destination: str) -> str:
    payload = json.dumps([chain_digest, item_key, destination], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class RestoreJournal:
    def __init__(self, state: StateStore):
        self.state = state

    def _latest(self, connection: sqlite3.Connection, key: str) -> RestoreRecovery | None:
        row = connection.execute(
            "SELECT operation_id, ordinal, status, result_reference FROM restore_targets "
            "WHERE account_key=? AND replay_key=? "
            "AND status IN ('dispatching','completed','uncertain') "
            "ORDER BY sequence DESC LIMIT 1",
            (self.state.account_key, key),
        ).fetchone()
        return RestoreRecovery(*tuple(row)) if row is not None else None

    def latest(self, chain_digest: str, item_key: str, destination: str) -> RestoreRecovery | None:
        with self.snapshot() as lookup:
            return lookup(chain_digest, item_key, destination)

    @contextmanager
    def snapshot(self) -> Iterator[Callable[[str, str, str], RestoreRecovery | None]]:
        """One bounded read connection for an entire streaming restore preview."""
        self.state.initialize()
        with self.state.connection() as connection:
            connection.execute("BEGIN")
            try:
                yield lambda chain, item, destination: self._latest(
                    connection, replay_key(chain, item, destination)
                )
            finally:
                connection.rollback()

    def begin(
        self, operation_id: str, backup_id: str, chain_digest: str,
        targets: Iterable[RestoreTargetIntent], *, allow_completed_duplicates: bool = False,
    ) -> None:
        now = iso()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO restore_operations "
                "(account_key,operation_id,backup_id,chain_digest,allow_completed_duplicates,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (self.state.account_key, operation_id, backup_id, chain_digest,
                 int(allow_completed_duplicates), now, now),
            )
            connection.executemany(
                "INSERT INTO restore_targets "
                "(account_key,operation_id,ordinal,item_key,source_reference,destination,replay_key,"
                "previous_token,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'not_started',?,?)",
                (
                    (self.state.account_key, operation_id, ordinal, target.item_key,
                     target.source_reference, target.destination,
                     replay_key(chain_digest, target.item_key, target.destination),
                     target.previous_token, now, now)
                    for ordinal, target in enumerate(targets)
                ),
            )

    def dispatch(self, operation_id: str, ordinal: int) -> None:
        """Compare the reviewed recovery state and claim this exact create atomically."""
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT t.*, o.allow_completed_duplicates FROM restore_targets AS t "
                "JOIN restore_operations AS o USING(account_key,operation_id) "
                "WHERE t.account_key=? AND t.operation_id=? AND t.ordinal=?",
                (self.state.account_key, operation_id, ordinal),
            ).fetchone()
            if row is None or row["status"] != "not_started":
                raise ConflictError("This restore target is missing or was already started.")
            previous = self._latest(connection, row["replay_key"])
            if (previous.token if previous else None) != row["previous_token"]:
                raise ConflictError(
                    "Restore recovery changed after review. "
                    "Generate a new plan; nothing was started."
                )
            if previous and (
                previous.status != "completed" or not row["allow_completed_duplicates"]
            ):
                raise ConflictError(
                    "A previous restore protects this item from replay. Review its recovery record."
                )
            connection.execute(
                "UPDATE restore_targets SET status='dispatching',updated_at=? "
                "WHERE account_key=? AND operation_id=? AND ordinal=?",
                (iso(), self.state.account_key, operation_id, ordinal),
            )

    def finish(
        self, operation_id: str, ordinal: int, status: str, *,
        result_reference: str | None = None, error_type: str | None = None,
    ) -> None:
        if status not in {"completed", "rejected", "uncertain"}:
            raise LocalStateError("Invalid restore outcome")
        if status == "completed" and (
            not isinstance(result_reference, str) or not result_reference.strip()
            or result_reference != result_reference.strip() or len(result_reference) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in result_reference)
        ):
            raise LocalStateError("A completed restore requires its usable result reference.")
        with self.state.transaction(immediate=True) as connection:
            now = iso()
            cursor = connection.execute(
                "UPDATE restore_targets SET status=?,result_reference=?,error_type=?,updated_at=? "
                "WHERE account_key=? AND operation_id=? AND ordinal=? AND status='dispatching'",
                (status, result_reference, error_type, now,
                 self.state.account_key, operation_id, ordinal),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    "The restore dispatch record is missing or already acknowledged."
                )
            connection.execute(
                "UPDATE restore_operations SET updated_at=? WHERE account_key=? AND operation_id=?",
                (now, self.state.account_key, operation_id),
            )

    def cancel_unstarted(self, operation_id: str) -> None:
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE restore_targets SET status='cancelled',updated_at=? "
                "WHERE account_key=? AND operation_id=? AND status='not_started'",
                (iso(), self.state.account_key, operation_id),
            )
            connection.execute(
                "UPDATE restore_operations SET updated_at=? WHERE account_key=? AND operation_id=?",
                (iso(), self.state.account_key, operation_id),
            )

    def cancel_before_provider(self, operation_id: str, ordinal: int) -> None:
        """Only for the owning service before entering its provider call."""
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE restore_targets SET status='cancelled',updated_at=? "
                "WHERE account_key=? AND operation_id=? AND ordinal=? AND status='dispatching'",
                (iso(), self.state.account_key, operation_id, ordinal),
            )
            if cursor.rowcount != 1:
                raise ConflictError("The undispatched restore claim changed.")
            connection.execute(
                "UPDATE restore_operations SET updated_at=? WHERE account_key=? AND operation_id=?",
                (iso(), self.state.account_key, operation_id),
            )

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 200 or offset < 0:
            raise LocalStateError("Restore history paging is outside safe bounds.")

    def operations(self, *, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
        self._page(limit, offset)
        self.state.initialize()
        with self.state.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM restore_operations WHERE account_key=? "
                "ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (self.state.account_key, limit, offset),
            )]

    def targets(
        self, operation_id: str, *, limit: int = 200, offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._page(limit, offset)
        self.state.initialize()
        with self.state.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT ordinal,source_reference,destination,status,result_reference,error_type "
                "FROM restore_targets WHERE account_key=? AND operation_id=? "
                "ORDER BY ordinal LIMIT ? OFFSET ?",
                (self.state.account_key, operation_id, limit, offset),
            )]

    def operation(
        self, operation_id: str, *, limit: int = 100, offset: int = 0,
    ) -> dict[str, Any] | None:
        self._page(limit, offset)
        self.state.initialize()
        with self.state.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM restore_operations WHERE account_key=? AND operation_id=?",
                (self.state.account_key, operation_id),
            ).fetchone()
            if row is None:
                return None
            counts = dict(connection.execute(
                "SELECT status,count(*) FROM restore_targets "
                "WHERE account_key=? AND operation_id=? GROUP BY status",
                (self.state.account_key, operation_id),
            ))
            targets = [dict(target) for target in connection.execute(
                "SELECT ordinal,source_reference,destination,status,result_reference,error_type "
                "FROM restore_targets WHERE account_key=? AND operation_id=? "
                "ORDER BY ordinal LIMIT ? OFFSET ?",
                (self.state.account_key, operation_id, limit, offset),
            )]
            total = sum(counts.values())
            return {
                "operation": dict(row), "counts": counts, "targets": targets,
                "total_targets": total, "offset": offset, "limit": limit,
                "next_offset": offset + len(targets) if offset + len(targets) < total else None,
            }
