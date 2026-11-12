"""Transparent, optional, reversible metadata that never mutates Exchange by itself."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gucmail.errors import ConfigurationError, ItemNotFoundError
from gucmail.state import StateStore, iso


@dataclass(frozen=True, slots=True)
class LocalMessageMetadata:
    item_reference: str
    pinned: bool = False
    snoozed_until: datetime | None = None
    reminder_at: datetime | None = None
    tags: tuple[str, ...] = ()
    note: str | None = None
    muted: bool = False
    waiting_for_reply: bool = False


@dataclass(frozen=True, slots=True)
class NotificationRule:
    name: str
    query: str
    providers: tuple[str, ...]
    regex: str | None = None
    quiet_hours: tuple[str, str] | None = None
    digest: str | None = None
    enabled: bool = False


class LocalFeaturesService:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.state.initialize()

    def get(self, reference: str) -> LocalMessageMetadata:
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_item_metadata WHERE item_reference=?", (reference,)
            ).fetchone()
        if row is None:
            return LocalMessageMetadata(reference)
        return LocalMessageMetadata(
            item_reference=reference,
            pinned=bool(row["pinned"]),
            snoozed_until=_parse_datetime(row["snoozed_until"]),
            reminder_at=_parse_datetime(row["reminder_at"]),
            tags=tuple(json.loads(row["tags_json"])),
            note=row["note"],
            muted=bool(row["muted"]),
            waiting_for_reply=bool(row["waiting_for_reply"]),
        )

    def update(
        self,
        reference: str,
        *,
        pinned: bool | None = None,
        snoozed_until: datetime | object | None = ...,
        reminder_at: datetime | object | None = ...,
        tags: tuple[str, ...] | None = None,
        note: str | object | None = ...,
        muted: bool | None = None,
        waiting_for_reply: bool | None = None,
    ) -> LocalMessageMetadata:
        current = self.get(reference)
        normalized_tags = current.tags if tags is None else _normalize_tags(tags)
        values = {
            "pinned": current.pinned if pinned is None else pinned,
            "snoozed_until": current.snoozed_until if snoozed_until is ... else snoozed_until,
            "reminder_at": current.reminder_at if reminder_at is ... else reminder_at,
            "tags": normalized_tags,
            "note": current.note if note is ... else note,
            "muted": current.muted if muted is None else muted,
            "waiting_for_reply": (
                current.waiting_for_reply if waiting_for_reply is None else waiting_for_reply
            ),
        }
        if values["note"] is not None and len(str(values["note"])) > 20_000:
            raise ConfigurationError("Local notes are limited to 20,000 characters.")
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO local_item_metadata(
                    item_reference, pinned, snoozed_until, reminder_at, tags_json, note,
                    muted, waiting_for_reply, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_reference) DO UPDATE SET
                    pinned=excluded.pinned,
                    snoozed_until=excluded.snoozed_until,
                    reminder_at=excluded.reminder_at,
                    tags_json=excluded.tags_json,
                    note=excluded.note,
                    muted=excluded.muted,
                    waiting_for_reply=excluded.waiting_for_reply,
                    updated_at=excluded.updated_at
                """,
                (
                    reference,
                    int(bool(values["pinned"])),
                    _iso_optional(values["snoozed_until"]),
                    _iso_optional(values["reminder_at"]),
                    json.dumps(values["tags"], ensure_ascii=False),
                    values["note"],
                    int(bool(values["muted"])),
                    int(bool(values["waiting_for_reply"])),
                    iso(),
                ),
            )
            self._audit(connection, "local_metadata.update", reference, values)
        return self.get(reference)

    def clear(self, reference: str) -> bool:
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM local_item_metadata WHERE item_reference=?", (reference,)
            )
            if cursor.rowcount:
                self._audit(connection, "local_metadata.clear", reference, {})
        return cursor.rowcount > 0

    def save_template(self, name: str, template: dict[str, Any], *, enabled: bool = True) -> None:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 100:
            raise ConfigurationError("Template names must contain 1 to 100 characters.")
        payload = json.dumps(template, ensure_ascii=False, sort_keys=True)
        now = iso()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO message_templates(name, template_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET template_json=excluded.template_json,
                    enabled=excluded.enabled, updated_at=excluded.updated_at
                """,
                (clean_name, payload, int(enabled), now, now),
            )
            self._audit(connection, "template.save", clean_name, {"enabled": enabled})

    def list_templates(self) -> list[dict[str, Any]]:
        with self.state.connection() as connection:
            rows = connection.execute(
                "SELECT name, template_json, enabled, updated_at "
                "FROM message_templates ORDER BY name"
            ).fetchall()
        return [
            {
                "name": row["name"],
                "template": json.loads(row["template_json"]),
                "enabled": bool(row["enabled"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def save_rule(self, rule: NotificationRule) -> None:
        if rule.regex:
            try:
                re.compile(rule.regex)
            except re.error as exc:
                raise ConfigurationError(f"Invalid notification regular expression: {exc}") from exc
        payload = json.dumps(
            {
                "query": rule.query,
                "providers": list(rule.providers),
                "regex": rule.regex,
                "quiet_hours": list(rule.quiet_hours) if rule.quiet_hours else None,
                "digest": rule.digest,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        now = iso()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO notification_rules(name, rule_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET rule_json=excluded.rule_json,
                    enabled=excluded.enabled, updated_at=excluded.updated_at
                """,
                (rule.name, payload, int(rule.enabled), now, now),
            )
            self._audit(
                connection,
                "notification_rule.save",
                rule.name,
                {"enabled": rule.enabled},
            )

    def set_sender_note(self, address: str, *, alias: str | None, trust_note: str | None) -> None:
        normalized = address.strip().casefold()
        if "@" not in normalized:
            raise ConfigurationError("A sender email address is required.")
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sender_notes(normalized_address, alias, trust_note, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(normalized_address) DO UPDATE SET alias=excluded.alias,
                    trust_note=excluded.trust_note, updated_at=excluded.updated_at
                """,
                (normalized, alias, trust_note, iso()),
            )
            self._audit(connection, "sender_note.save", normalized, {})

    def save_workspace(self, name: str, workspace: dict[str, Any]) -> None:
        if not name.strip():
            raise ConfigurationError("Workspace name may not be empty.")
        payload = json.dumps(workspace, ensure_ascii=False, sort_keys=True)
        now = iso()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO saved_workspaces(name, workspace_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET workspace_json=excluded.workspace_json,
                    updated_at=excluded.updated_at
                """,
                (name.strip(), payload, now, now),
            )

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ConfigurationError("Audit limit must be between 1 and 1000.")
        with self.state.connection() as connection:
            rows = connection.execute(
                "SELECT action, target_reference, detail_json, occurred_at FROM local_audit_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "action": row["action"],
                "target_reference": row["target_reference"],
                "detail": json.loads(row["detail_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _audit(connection: Any, action: str, target: str, detail: Any) -> None:
        safe_detail = _json_safe(detail)
        connection.execute(
            "INSERT INTO local_audit_events(action, target_reference, detail_json, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            (action, target, json.dumps(safe_detail, sort_keys=True), iso()),
        )


def _normalize_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned or len(cleaned) > 100:
            raise ConfigurationError("Local tags must contain 1 to 100 characters.")
        if cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)
    return tuple(result)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso_optional(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ConfigurationError("Reminder and snooze values must be timezone-aware datetimes.")
    if value.tzinfo is None:
        raise ConfigurationError("Reminder and snooze times need an explicit time zone.")
    return value.astimezone(UTC).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is ...:
        return "unchanged"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ItemNotFoundError("Unsupported local metadata value.")
