"""Remember verified attachment uploads without fetching remote attachment bodies."""

from __future__ import annotations

from gucmail.state import StateStore


class AttachmentUploadStore:
    def __init__(self, state: StateStore | None) -> None:
        self.state = state
        self._memory: dict[tuple[str, str], tuple[str, str]] = {}

    def record(self, item_id: str, name: str, attachment_id: str, digest: str) -> None:
        if self.state is None:
            self._memory[(item_id, name)] = (attachment_id, digest)
            return
        self.state.initialize()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO draft_attachment_uploads VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_key, item_id, attachment_name) DO UPDATE SET
                    attachment_id=excluded.attachment_id, content_sha256=excluded.content_sha256
                """,
                (self.state.account_key, item_id, name, attachment_id, digest),
            )

    def matches(self, item_id: str, name: str, attachment_id: str, digest: str) -> bool:
        if self.state is None:
            return self._memory.get((item_id, name)) == (attachment_id, digest)
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                """
                SELECT attachment_id, content_sha256 FROM draft_attachment_uploads
                WHERE account_key=? AND item_id=? AND attachment_name=?
                """,
                (self.state.account_key, item_id, name),
            ).fetchone()
        return row is not None and tuple(row) == (attachment_id, digest)
