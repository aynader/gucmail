"""Opt-in bounded local index of explicitly observed correspondence metadata."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.models import Address
from gucmail.services.contacts import (
    CorrespondenceDirection,
    CorrespondenceEvent,
    normalize_email,
)

if TYPE_CHECKING:
    from gucmail.state import StateStore

MAX_HISTORY_ROWS = 10_000


class ContactHistoryIndex:
    """A disposable observation index, never a complete mailbox history."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _cutoff(retention_days: int, now: datetime | None) -> datetime:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= 365
        ):
            raise ConfigurationError("Contact history retention must be between 1 and 365 days.")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ConfigurationError("Contact history clock must include a time zone.")
        return current.astimezone(UTC) - timedelta(days=retention_days)

    def record(
        self,
        events: Iterable[CorrespondenceEvent],
        *,
        retention_days: int = 30,
        now: datetime | None = None,
    ) -> None:
        cutoff = self._cutoff(retention_days, now)
        rows = []
        for index, event in enumerate(events):
            if index >= MAX_HISTORY_ROWS:
                break
            address = normalize_email(event.address.email)
            if event.occurred_at.tzinfo is None:
                raise ConfigurationError("Correspondence timestamps must include a time zone.")
            occurred = event.occurred_at.astimezone(UTC)
            if occurred < cutoff:
                continue
            reference = event.message_reference or ""
            if len(reference) > 16_384 or any(ord(char) < 32 for char in reference):
                raise ConfigurationError("Invalid correspondence message reference.")
            direction = CorrespondenceDirection(event.direction).value
            identity = sha256(
                (
                    address + "\n" + reference + "\n" + occurred.isoformat() + "\n" + direction
                ).encode()
            ).hexdigest()
            rows.append(
                (
                    self.state.account_key,
                    identity,
                    address,
                    occurred.isoformat(),
                    direction,
                    reference,
                )
            )
        with self.state.transaction(immediate=True) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO contact_history "
                "(account_key, observation_id, address, occurred_at, direction, message_reference) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.execute(
                "DELETE FROM contact_history WHERE account_key=? AND occurred_at<?",
                (self.state.account_key, cutoff.isoformat()),
            )
            connection.execute(
                "DELETE FROM contact_history WHERE account_key=? AND observation_id NOT IN "
                "(SELECT observation_id FROM contact_history WHERE account_key=? "
                "ORDER BY occurred_at DESC, observation_id LIMIT ?)",
                (self.state.account_key, self.state.account_key, MAX_HISTORY_ROWS),
            )

    def status(self, *, retention_days: int = 30) -> dict[str, object]:
        self.prune(retention_days=retention_days)
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*), MAX(occurred_at) FROM contact_history WHERE account_key=?",
                (self.state.account_key,),
            ).fetchone()
        return {
            "observations": int(row[0]) if row else 0,
            "newest_observation": row[1] if row else None,
            "maximum_observations": MAX_HISTORY_ROWS,
            "retention_days": retention_days,
            "scope": "Explicitly observed metadata; incomplete mailbox history. "
            "No bodies or names.",
        }

    def prune(self, *, retention_days: int = 30, now: datetime | None = None) -> None:
        cutoff = self._cutoff(retention_days, now)
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM contact_history WHERE account_key=? AND occurred_at<?",
                (self.state.account_key, cutoff.isoformat()),
            )

    def query(
        self,
        address: str | None = None,
        *,
        limit: int = 100,
        retention_days: int = 30,
    ) -> tuple[CorrespondenceEvent, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ConfigurationError("Local history result limit must be between 1 and 200.")
        normalized = normalize_email(address) if address is not None else None
        self.prune(retention_days=retention_days)
        with self.state.connection() as connection:
            rows = connection.execute(
                "SELECT address, occurred_at, direction, message_reference FROM contact_history "
                "WHERE account_key=? AND (? IS NULL OR address=?) "
                "ORDER BY occurred_at DESC, observation_id LIMIT ?",
                (self.state.account_key, normalized, normalized, limit),
            ).fetchall()
        try:
            events = []
            for row in rows:
                occurred = datetime.fromisoformat(str(row["occurred_at"]))
                if occurred.tzinfo is None:
                    raise ValueError("Missing time zone")
                events.append(
                    CorrespondenceEvent(
                        Address(normalize_email(str(row["address"]))),
                        occurred.astimezone(UTC),
                        CorrespondenceDirection(row["direction"]),
                        row["message_reference"] or None,
                    )
                )
            return tuple(events)
        except (ValueError, ConfigurationError) as exc:
            raise LocalStateError(
                "The local contact history index is invalid; clear it to rebuild."
            ) from exc

    def clear(self) -> int:
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM contact_history WHERE account_key=?", (self.state.account_key,)
            )
            return cursor.rowcount
