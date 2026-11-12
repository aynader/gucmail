"""Bounded native EWS change pages and versioned, folder-bound checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from gucmail.errors import ConfigurationError

PREFIX = "gucmail-ews-sync-v1:"


@dataclass(frozen=True, slots=True)
class SyncCursor:
    scope: str
    token: str | None = None
    baseline: bool = True

    @classmethod
    def read(cls, value: str | None, *, scope: str) -> SyncCursor:
        if value is None:
            return cls(scope)
        try:
            if not value.startswith(PREFIX) or len(value) > 1_000_000:
                raise ValueError
            data = json.loads(value[len(PREFIX) :])
            if (
                not isinstance(data, dict)
                or data.get("scope") != scope
                or not isinstance(data.get("token"), str)
                or not data["token"]
                or type(data.get("baseline")) is not bool
            ):
                raise ValueError
            return cls(scope, data["token"], data["baseline"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ConfigurationError(
                "The incremental checkpoint does not match this folder. "
                "Use watch --reset-checkpoint to establish a new baseline."
            ) from exc

    def encode(self) -> str:
        return PREFIX + json.dumps(
            {"scope": self.scope, "token": self.token, "baseline": self.baseline},
            sort_keys=True,
            separators=(",", ":"),
        )


def sync_scope(account: str, folder_id: str, item_kind: str) -> str:
    return hashlib.sha256(
        "\0".join((account.casefold().strip(), folder_id, item_kind)).encode()
    ).hexdigest()


def read_change_page(
    account: Any,
    folder: Any,
    *,
    fields: tuple[str, ...],
    cursor: SyncCursor,
    limit: int,
) -> tuple[tuple[tuple[str, Any], ...], SyncCursor, bool]:
    """Issue exactly one SyncFolderItems page; do not use the auto-draining iterator."""
    from exchangelib.errors import ErrorInvalidSyncStateData
    from exchangelib.services import SyncFolderItems

    service = SyncFolderItems(account=account)
    additional_fields = {
        field for field in folder.normalize_fields(fields=fields) if not field.field.is_attribute
    }
    try:
        changes = tuple(
            service.call(
                folder=folder,
                shape="IdOnly",
                additional_fields=additional_fields,
                sync_state=cursor.token,
                ignore=None,
                max_changes_returned=limit,
                sync_scope="NormalItems",
            )
        )
    except ErrorInvalidSyncStateData as exc:
        raise ConfigurationError(
            "Exchange rejected the incremental checkpoint. No progress was committed. "
            "Use watch --reset-checkpoint to establish a new quiet baseline."
        ) from exc
    for change in changes:
        if isinstance(change, BaseException):
            raise change
    token = service.sync_state
    last = service.includes_last_item_in_range
    if not isinstance(token, str) or not token or type(last) is not bool:
        raise ConfigurationError("Exchange returned an incomplete synchronization response.")
    if len(changes) > limit or (not last and token == cursor.token):
        raise ConfigurationError("Exchange synchronization exceeded its page bound or stalled.")
    next_cursor = SyncCursor(cursor.scope, token, cursor.baseline and not last)
    return changes, next_cursor, not last
