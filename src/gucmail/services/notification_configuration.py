"""Durable, account-scoped notification configuration repositories.

The notification rule and policy modules intentionally contain no persistence code.  This
module bridges their public repository protocols to the versioned :class:`StateStore`
schema.  All multi-row changes use ``BEGIN IMMEDIATE`` transactions, malformed rows are
isolated behind explicit diagnostics, and ordinary SQLite JSON is rejected if it contains
URLs or secret-bearing fields.  Provider credentials remain keyring references only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Any, Generic, TypeVar, cast

from gucmail.errors import LocalStateError
from gucmail.notification_events import EventDisclosurePolicy, NewMailEvent, PrivacyMode
from gucmail.notification_policies import (
    DigestSchedule,
    MonitoringProfile,
    NotificationRouter,
    ProviderPolicy,
    QuietHoursBehavior,
    QuietHoursSchedule,
    QuietHoursWindow,
    RoutingMatrix,
    RoutingPlan,
    export_profiles,
    import_profiles,
    profile_from_data,
    profile_to_data,
)
from gucmail.notification_rules import (
    NotificationRule,
    NotificationRuleEngine,
    NotificationRuleSimulator,
    RuleAction,
    RuleActionKind,
    RuleDecision,
    RuleEvaluationContext,
    RuleSimulationResult,
    WeekdayCondition,
    export_rules,
    import_rules,
    rule_from_data,
    rule_to_data,
)
from gucmail.state import StateStore, iso

_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_PROVIDER_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_PROVIDER_CONFIGS = 1_000
_MAX_NESTING = 32
_URL_PATTERN = re.compile(r"(?:https?|wss?|ftp)://|\bwww\.", re.IGNORECASE)
_TOKEN_VALUE_PATTERN = re.compile(
    r"(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+|\b(?:gh[pousr]_|xox[baprs]-|sk-)[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_SECRET_KEY_PARTS = frozenset(
    {
        "access_key",
        "api_key",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "dsn",
        "encryption_key",
        "endpoint",
        "header",
        "headers",
        "hmac_key",
        "hmac_secret",
        "password",
        "passphrase",
        "private_key",
        "refresh_token",
        "secret",
        "session_token",
        "token",
        "url",
        "uri",
        "webhook_url",
    }
)
_SECRET_COMPONENTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)
_PROFILE_METADATA_SCHEMA = "gucmail.notification-profile-state"
_PROFILE_METADATA_VERSION = 1
_QUIET_METADATA_SCHEMA = "gucmail.notification-quiet-window"
_QUIET_METADATA_VERSION = 1
_ACTIVE_SCOPE = "notifications"
_ACTIVE_KEY = "active_profile_id"
_MONITOR_STRATEGIES = frozenset(
    {"ews_streaming", "incremental_poll", "incremental_sync", "bounded_poll"}
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PersistenceDiagnostic:
    """Safe information about one configuration row that could not be loaded."""

    component: str
    identifier: str
    message: str


@dataclass(frozen=True, slots=True)
class PersistenceSnapshot(Generic[T]):
    """Valid values plus isolated-row diagnostics from one repository read."""

    items: tuple[T, ...]
    diagnostics: tuple[PersistenceDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderConfigurationMetadata:
    """Non-secret provider metadata; delivery endpoints live in secure storage."""

    provider_id: str
    kind: str
    name: str
    enabled: bool = False
    secret_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _clean_text(self.provider_id, label="provider_id", maximum=200)
        _clean_text(self.kind, label="provider kind", maximum=100)
        _clean_text(self.name, label="provider name", maximum=300)
        if _contains_url(self.provider_id) or _contains_url(self.kind) or _contains_url(self.name):
            raise ValueError("provider identifiers and names cannot contain URLs")
        if self.secret_ref is not None:
            _validate_secret_reference(self.secret_ref)
        metadata = {} if self.metadata is None else dict(self.metadata)
        _validate_ordinary_payload(metadata, path=("provider_metadata",))
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True, slots=True)
class WatcherProfileConfiguration:
    """Small adapter value consumed when constructing a durable watcher."""

    profile_id: str
    folders: tuple[str, ...]
    poll_interval_seconds: float
    strategy_preference: str | None
    enabled: bool
    notify_existing: bool


@dataclass(frozen=True, slots=True)
class NotificationConfigurationTestResult:
    """Pure dry-run result; no provider or network operation is performed."""

    decision: RuleDecision
    routing: RoutingPlan


def _clean_text(value: str, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum} characters")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError(f"{label} cannot contain control characters")
    return cleaned


def _canonical_id(value: str, *, label: str, maximum: int = 200) -> str:
    return _clean_text(value, label=label, maximum=maximum).casefold()


def _storage_id(prefix: str, profile_id: str, child_id: str) -> str:
    material = f"{profile_id.casefold()}\x00{child_id.casefold()}".encode()
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:40]}"


def _route_storage_id(profile_id: str, rule_id: str, provider_id: str) -> str:
    material = (
        f"{profile_id.casefold()}\x00{rule_id.casefold()}\x00{provider_id.casefold()}"
    ).encode()
    return f"route-{hashlib.sha256(material).hexdigest()[:40]}"


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _contains_url(value: str) -> bool:
    return _URL_PATTERN.search(value) is not None


def _validate_ordinary_payload(
    value: object,
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> None:
    """Reject secrets and URLs before they can enter ordinary SQLite JSON."""

    if depth > _MAX_NESTING:
        raise ValueError("notification configuration is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("notification configuration numbers must be finite")
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("notification configuration cannot contain NUL characters")
        if _contains_url(value):
            raise ValueError("URLs must not be stored in notification configuration rows")
        if _TOKEN_VALUE_PATTERN.search(value) is not None:
            raise ValueError("plaintext authorization tokens must not be stored in ordinary state")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("notification configuration keys must be non-empty text")
            normalized = _normalized_key(key)
            components = set(re.findall(r"[a-z0-9]+", normalized))
            if (
                normalized in _SECRET_KEY_PARTS
                or any(normalized.endswith(f"_{part}") for part in _SECRET_KEY_PARTS)
                or bool(components & _SECRET_COMPONENTS)
            ):
                location = ".".join((*path, key))
                raise ValueError(
                    f"secret-bearing field {location!r} is not allowed in ordinary state"
                )
            _validate_ordinary_payload(item, path=(*path, key), depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_ordinary_payload(
                item,
                path=(*path, str(index)),
                depth=depth + 1,
            )
        return
    raise ValueError("notification configuration contains a non-JSON value")


def _encode_json(value: object) -> str:
    _validate_ordinary_payload(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("notification configuration is too large")
    return encoded


def _decode_json(value: object, *, label: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be JSON text")
    if len(value.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} is too large")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    _validate_ordinary_payload(decoded, path=(label,))
    return decoded


def _validate_secret_reference(value: str) -> str:
    cleaned = _clean_text(value, label="provider secret reference", maximum=1_024)
    if not cleaned.startswith("keyring://") or cleaned == "keyring://":
        raise ValueError("provider secrets must be represented by a keyring:// reference")
    return cleaned


def _bool_column(value: object, *, label: str) -> bool:
    if not isinstance(value, int) or value not in {0, 1}:
        raise ValueError(f"{label} must be 0 or 1")
    return bool(value)


def _safe_diagnostic(
    component: str,
    identifier: object,
    error: BaseException,
) -> PersistenceDiagnostic:
    safe_identifier = str(identifier)[:200].replace("\x00", "?")
    return PersistenceDiagnostic(
        component,
        safe_identifier,
        f"Stored {component} failed validation ({type(error).__name__}).",
    )


def _state_failure(action: str, error: sqlite3.Error) -> LocalStateError:
    return LocalStateError(
        f"Could not {action} notification configuration ({type(error).__name__})."
    )


def _read_rule_row(
    row: sqlite3.Row,
    *,
    profile_id: str,
) -> tuple[NotificationRule | None, PersistenceDiagnostic | None]:
    identifier = row["rule_id"]
    try:
        data = _decode_json(row["definition_json"], label="notification rule definition")
        rule = rule_from_data(data)
        expected_storage = _storage_id("rule", profile_id, rule.rule_id)
        if identifier != expected_storage:
            raise ValueError("rule storage identity does not match its definition")
        enabled = _bool_column(row["enabled"], label="rule enabled")
        priority = int(row["priority"])
        position = int(row["position"])
        if not -1_000 <= priority <= 1_000 or position < 0:
            raise ValueError("rule ordering projection is outside its valid range")
        name = _clean_text(row["name"], label="rule name", maximum=300)
        projected = replace(
            rule,
            name=name,
            enabled=enabled,
            priority=priority,
            order=position,
        )
        _validate_ordinary_payload(rule_to_data(projected))
        return projected, None
    except (TypeError, ValueError) as exc:
        return None, _safe_diagnostic("notification rule", identifier, exc)


def _rule_rows(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
) -> PersistenceSnapshot[NotificationRule]:
    rows = connection.execute(
        """
        SELECT rule_id, name, enabled, priority, position, definition_json
        FROM notification_rule_metadata
        WHERE account_key=? AND profile_id=?
        ORDER BY priority DESC, position, rule_id
        """,
        (account_key, profile_id),
    ).fetchall()
    rules: list[NotificationRule] = []
    diagnostics: list[PersistenceDiagnostic] = []
    for row in rows:
        rule, diagnostic = _read_rule_row(row, profile_id=profile_id)
        if rule is not None:
            rules.append(rule)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return PersistenceSnapshot(tuple(rules), tuple(diagnostics))


def _upsert_rule(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
    rule: NotificationRule,
    timestamp: str,
) -> None:
    definition = _encode_json(rule_to_data(rule))
    storage_id = _storage_id("rule", profile_id, rule.rule_id)
    connection.execute(
        """
        INSERT INTO notification_rule_metadata(
            account_key, rule_id, profile_id, name, enabled, priority, position,
            definition_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, rule_id) DO UPDATE SET
            profile_id=excluded.profile_id,
            name=excluded.name,
            enabled=excluded.enabled,
            priority=excluded.priority,
            position=excluded.position,
            definition_json=excluded.definition_json,
            updated_at=excluded.updated_at
        """,
        (
            account_key,
            storage_id,
            profile_id,
            rule.name,
            int(rule.enabled),
            rule.priority,
            rule.order,
            definition,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        UPDATE notification_routes SET priority=?, updated_at=?
        WHERE account_key=? AND profile_id=? AND rule_id=? COLLATE NOCASE
        """,
        (rule.priority, timestamp, account_key, profile_id, rule.rule_id),
    )


def _prune_profile_rule_routes(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
    allowed_rule_ids: set[str],
    timestamp: str,
) -> None:
    """Remove routing cells whose rule no longer exists, in the caller's transaction."""

    row = connection.execute(
        """
        SELECT metadata_json FROM notification_profiles
        WHERE account_key=? AND profile_id=?
        """,
        (account_key, profile_id),
    ).fetchone()
    if row is None:
        return
    try:
        metadata = _decode_json(row["metadata_json"], label="notification profile metadata")
        if not isinstance(metadata, dict):
            raise ValueError("profile metadata must be an object")
        routing = metadata.get("routing_matrix")
        if not isinstance(routing, dict):
            raise ValueError("profile routing matrix must be an object")
        cells = routing.get("cells")
        if not isinstance(cells, list):
            raise ValueError("profile routing cells must be a list")
        retained: list[object] = []
        for cell in cells:
            if not isinstance(cell, Mapping) or not isinstance(cell.get("rule_id"), str):
                raise ValueError("profile routing cell is malformed")
            if cast(str, cell["rule_id"]).casefold() in allowed_rule_ids:
                retained.append(cell)
        routing["cells"] = retained
        metadata["routing_matrix"] = routing
        connection.execute(
            """
            UPDATE notification_profiles SET metadata_json=?, updated_at=?
            WHERE account_key=? AND profile_id=?
            """,
            (_encode_json(metadata), timestamp, account_key, profile_id),
        )
    except (TypeError, ValueError) as exc:
        raise LocalStateError(
            _safe_diagnostic("notification profile", profile_id, exc).message
        ) from exc
    route_rows = connection.execute(
        """
        SELECT route_id, rule_id FROM notification_routes
        WHERE account_key=? AND profile_id=?
        """,
        (account_key, profile_id),
    ).fetchall()
    for route in route_rows:
        rule_id = route["rule_id"]
        if not isinstance(rule_id, str) or rule_id.casefold() not in allowed_rule_ids:
            connection.execute(
                """
                DELETE FROM notification_routes
                WHERE account_key=? AND profile_id=? AND route_id=?
                """,
                (account_key, profile_id, route["route_id"]),
            )


class PersistentNotificationRuleRepository:
    """SQLite implementation of ``NotificationRuleRepository`` for one profile."""

    def __init__(self, store: StateStore, profile_id: str = "default") -> None:
        self._store = store
        self.profile_id = _canonical_id(profile_id, label="notification profile ID")

    def inspect(self) -> PersistenceSnapshot[NotificationRule]:
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                return _rule_rows(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                )
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc

    def list_rules(self) -> tuple[NotificationRule, ...]:
        snapshot = self.inspect()
        if snapshot.diagnostics:
            raise LocalStateError(
                "Stored notification rules contain invalid rows; inspect diagnostics first."
            )
        return snapshot.items

    def get_rule(self, rule_id: str) -> NotificationRule | None:
        storage_id = _storage_id(
            "rule",
            self.profile_id,
            _canonical_id(rule_id, label="notification rule ID"),
        )
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                row = connection.execute(
                    """
                    SELECT rule_id, name, enabled, priority, position, definition_json
                    FROM notification_rule_metadata
                    WHERE account_key=? AND profile_id=? AND rule_id=?
                    """,
                    (self._store.account_key, self.profile_id, storage_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc
        if row is None:
            return None
        rule, diagnostic = _read_rule_row(row, profile_id=self.profile_id)
        if diagnostic is not None:
            raise LocalStateError(diagnostic.message)
        return rule

    def save_rule(self, rule: NotificationRule) -> None:
        timestamp = iso()
        try:
            with self._store.transaction(immediate=True) as connection:
                _upsert_rule(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                    rule=rule,
                    timestamp=timestamp,
                )
        except sqlite3.Error as exc:
            raise _state_failure("save", exc) from exc

    def delete_rule(self, rule_id: str) -> bool:
        storage_id = _storage_id(
            "rule",
            self.profile_id,
            _canonical_id(rule_id, label="notification rule ID"),
        )
        try:
            with self._store.transaction(immediate=True) as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM notification_rule_metadata
                    WHERE account_key=? AND profile_id=? AND rule_id=?
                    """,
                    (self._store.account_key, self.profile_id, storage_id),
                )
                if cursor.rowcount == 1:
                    remaining = _rule_rows(
                        connection,
                        account_key=self._store.account_key,
                        profile_id=self.profile_id,
                    )
                    if remaining.diagnostics:
                        raise LocalStateError(
                            "Cannot update routes while stored notification rules are invalid."
                        )
                    _prune_profile_rule_routes(
                        connection,
                        account_key=self._store.account_key,
                        profile_id=self.profile_id,
                        allowed_rule_ids={item.rule_id.casefold() for item in remaining.items},
                        timestamp=iso(),
                    )
        except sqlite3.Error as exc:
            raise _state_failure("delete", exc) from exc
        return cursor.rowcount == 1

    def _required_in_transaction(
        self,
        connection: sqlite3.Connection,
        rule_id: str,
    ) -> NotificationRule:
        storage_id = _storage_id(
            "rule",
            self.profile_id,
            _canonical_id(rule_id, label="notification rule ID"),
        )
        row = connection.execute(
            """
            SELECT rule_id, name, enabled, priority, position, definition_json
            FROM notification_rule_metadata
            WHERE account_key=? AND profile_id=? AND rule_id=?
            """,
            (self._store.account_key, self.profile_id, storage_id),
        ).fetchone()
        if row is None:
            raise KeyError(rule_id)
        rule, diagnostic = _read_rule_row(row, profile_id=self.profile_id)
        if rule is None:
            raise LocalStateError(cast(PersistenceDiagnostic, diagnostic).message)
        return rule

    def set_enabled(self, rule_id: str, *, enabled: bool) -> NotificationRule:
        try:
            with self._store.transaction(immediate=True) as connection:
                updated = replace(
                    self._required_in_transaction(connection, rule_id),
                    enabled=enabled,
                )
                _upsert_rule(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                    rule=updated,
                    timestamp=iso(),
                )
        except sqlite3.Error as exc:
            raise _state_failure("update", exc) from exc
        return updated

    def duplicate_rule(self, rule_id: str, *, new_id: str, new_name: str) -> NotificationRule:
        new_key = _canonical_id(new_id, label="new notification rule ID")
        _clean_text(new_name, label="new notification rule name", maximum=300)
        try:
            with self._store.transaction(immediate=True) as connection:
                existing = connection.execute(
                    """
                    SELECT 1 FROM notification_rule_metadata
                    WHERE account_key=? AND profile_id=? AND rule_id=?
                    """,
                    (
                        self._store.account_key,
                        self.profile_id,
                        _storage_id("rule", self.profile_id, new_key),
                    ),
                ).fetchone()
                if existing is not None:
                    raise ValueError(f"notification rule already exists: {new_id}")
                source = self._required_in_transaction(connection, rule_id)
                count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM notification_rule_metadata
                        WHERE account_key=? AND profile_id=?
                        """,
                        (self._store.account_key, self.profile_id),
                    ).fetchone()[0]
                )
                duplicate = replace(
                    source,
                    rule_id=new_id,
                    name=new_name,
                    enabled=False,
                    order=count,
                )
                _upsert_rule(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                    rule=duplicate,
                    timestamp=iso(),
                )
        except sqlite3.Error as exc:
            raise _state_failure("duplicate", exc) from exc
        return duplicate

    def reorder(self, rule_ids: Sequence[str]) -> tuple[NotificationRule, ...]:
        normalized = [_canonical_id(value, label="notification rule ID") for value in rule_ids]
        if len(normalized) != len(set(normalized)):
            raise ValueError("reorder list contains duplicate rule identifiers")
        try:
            with self._store.transaction(immediate=True) as connection:
                snapshot = _rule_rows(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                )
                if snapshot.diagnostics:
                    raise LocalStateError(
                        "Cannot reorder while stored notification rules are invalid."
                    )
                by_id = {rule.rule_id.casefold(): rule for rule in snapshot.items}
                if set(normalized) != set(by_id):
                    raise ValueError("reorder list must contain every rule exactly once")
                timestamp = iso()
                for position, rule_id in enumerate(normalized):
                    _upsert_rule(
                        connection,
                        account_key=self._store.account_key,
                        profile_id=self.profile_id,
                        rule=replace(by_id[rule_id], order=position),
                        timestamp=timestamp,
                    )
        except sqlite3.Error as exc:
            raise _state_failure("reorder", exc) from exc
        return self.list_rules()

    def export_document(self) -> str:
        return export_rules(self.list_rules())

    def import_document(
        self,
        document: str | bytes,
        *,
        replace_existing: bool = False,
    ) -> tuple[NotificationRule, ...]:
        rules = import_rules(document)
        for rule in rules:
            _validate_ordinary_payload(rule_to_data(rule))
        try:
            with self._store.transaction(immediate=True) as connection:
                existing = _rule_rows(
                    connection,
                    account_key=self._store.account_key,
                    profile_id=self.profile_id,
                )
                if existing.diagnostics:
                    raise LocalStateError("Cannot import over invalid stored notification rules.")
                existing_ids = {rule.rule_id.casefold() for rule in existing.items}
                incoming_ids = {rule.rule_id.casefold() for rule in rules}
                collisions = existing_ids & incoming_ids
                if collisions and not replace_existing:
                    raise ValueError(
                        "notification-rule import would overwrite existing identifiers"
                    )
                if replace_existing:
                    connection.execute(
                        """
                        DELETE FROM notification_rule_metadata
                        WHERE account_key=? AND profile_id=?
                        """,
                        (self._store.account_key, self.profile_id),
                    )
                timestamp = iso()
                for rule in rules:
                    _upsert_rule(
                        connection,
                        account_key=self._store.account_key,
                        profile_id=self.profile_id,
                        rule=rule,
                        timestamp=timestamp,
                    )
                if replace_existing:
                    _prune_profile_rule_routes(
                        connection,
                        account_key=self._store.account_key,
                        profile_id=self.profile_id,
                        allowed_rule_ids={rule.rule_id.casefold() for rule in rules},
                        timestamp=timestamp,
                    )
        except sqlite3.Error as exc:
            raise _state_failure("import", exc) from exc
        return rules

    def simulate(
        self,
        events: Sequence[NewMailEvent],
        *,
        context: RuleEvaluationContext | None = None,
        provider_policies: Mapping[str, EventDisclosurePolicy] | None = None,
        max_examples: int = 10,
    ) -> RuleSimulationResult:
        engine = NotificationRuleEngine(self.list_rules(), context=context)
        return NotificationRuleSimulator(
            engine,
            provider_policies=provider_policies,
            max_examples=max_examples,
        ).simulate(events)


def _profile_envelope(profile: MonitoringProfile, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": _PROFILE_METADATA_SCHEMA,
        "version": _PROFILE_METADATA_VERSION,
        "profile_id": profile.profile_id,
        "notify_existing": profile.notify_existing,
        "providers": data["providers"],
        "routing_matrix": data["routing_matrix"],
    }


def _quiet_exceptions(window: QuietHoursWindow) -> dict[str, Any]:
    return {
        "schema": _QUIET_METADATA_SCHEMA,
        "version": _QUIET_METADATA_VERSION,
        "window_id": window.window_id,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "timezone": window.timezone,
        "bypass_importance": sorted(value.value for value in window.bypass_importance),
        "bypass_senders": sorted(window.bypass_senders),
        "bypass_sender_domains": sorted(window.bypass_sender_domains),
        "bypass_rule_ids": sorted(window.bypass_rule_ids),
    }


def _write_profile(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile: MonitoringProfile,
    timestamp: str,
) -> None:
    profile_key = _canonical_id(profile.profile_id, label="notification profile ID")
    data = profile_to_data(profile)
    _validate_ordinary_payload(data)
    rule_ids = {rule.rule_id.casefold() for rule in profile.rules}
    unknown_route_rules = sorted(
        {
            cell.rule_id
            for cell in profile.routing_matrix.cells
            if cell.rule_id.casefold() not in rule_ids
        }
    )
    if unknown_route_rules:
        raise ValueError(
            "routing matrix references unknown rules: " + ", ".join(unknown_route_rules)
        )
    folders_json = _encode_json(list(profile.folders))
    metadata_json = _encode_json(_profile_envelope(profile, data))
    digest_runtime = {
        str(row["digest_id"]): (row["last_emitted_at"], row["next_due_at"])
        for row in connection.execute(
            """
            SELECT digest_id, last_emitted_at, next_due_at
            FROM notification_digest_schedules
            WHERE account_key=? AND profile_id=?
            """,
            (account_key, profile_key),
        ).fetchall()
    }
    connection.execute(
        """
        INSERT INTO notification_profiles(
            account_key, profile_id, name, enabled, folders_json, strategy_preference,
            poll_interval_seconds, privacy_mode, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, profile_id) DO UPDATE SET
            name=excluded.name,
            enabled=excluded.enabled,
            folders_json=excluded.folders_json,
            poll_interval_seconds=excluded.poll_interval_seconds,
            privacy_mode=excluded.privacy_mode,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            account_key,
            profile_key,
            profile.name,
            int(profile.enabled),
            folders_json,
            profile.poll_interval_seconds,
            PrivacyMode.PRIVATE.value,
            metadata_json,
            timestamp,
            timestamp,
        ),
    )
    for table in (
        "notification_rule_metadata",
        "notification_quiet_hours",
        "notification_digest_schedules",
        "notification_routes",
    ):
        connection.execute(
            f"DELETE FROM {table} WHERE account_key=? AND profile_id=?",  # noqa: S608
            (account_key, profile_key),
        )
    for rule in profile.rules:
        _upsert_rule(
            connection,
            account_key=account_key,
            profile_id=profile_key,
            rule=rule,
            timestamp=timestamp,
        )
    for window in profile.quiet_hours.windows:
        exceptions_json = _encode_json(_quiet_exceptions(window))
        start_minute = window.start.hour * 60 + window.start.minute
        end_minute = window.end.hour * 60 + window.end.minute
        storage_id = _storage_id("quiet", profile_key, window.window_id)
        for weekday in sorted(window.weekdays):
            connection.execute(
                """
                INSERT INTO notification_quiet_hours(
                    account_key, schedule_id, profile_id, weekday, start_minute,
                    end_minute, behavior, enabled, exceptions_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_key,
                    storage_id,
                    profile_key,
                    weekday,
                    start_minute,
                    end_minute,
                    window.behavior.value,
                    int(window.enabled),
                    exceptions_json,
                    timestamp,
                    timestamp,
                ),
            )
    digest_values = cast(list[Mapping[str, Any]], data["digest_schedules"])
    for schedule, schedule_data in zip(profile.digest_schedules, digest_values, strict=True):
        digest_storage_id = _storage_id("digest", profile_key, schedule.schedule_id)
        last_emitted_at, next_due_at = digest_runtime.get(digest_storage_id, (None, None))
        connection.execute(
            """
            INSERT INTO notification_digest_schedules(
                account_key, digest_id, profile_id, name, enabled, schedule_json,
                privacy_mode, last_emitted_at, next_due_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_key,
                digest_storage_id,
                profile_key,
                schedule.name,
                int(schedule.enabled),
                _encode_json(schedule_data),
                PrivacyMode.PRIVATE.value,
                last_emitted_at,
                next_due_at,
                timestamp,
                timestamp,
            ),
        )
    providers = {provider.provider_id.casefold(): provider for provider in profile.providers}
    priorities = {rule.rule_id.casefold(): rule.priority for rule in profile.rules}
    for cell in profile.routing_matrix.cells:
        provider = providers.get(cell.provider_id.casefold())
        privacy = provider.privacy if provider is not None else EventDisclosurePolicy()
        allowlist = (
            _encode_json(sorted(privacy.field_allowlist))
            if privacy.field_allowlist is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO notification_routes(
                account_key, route_id, profile_id, rule_id, provider_id, enabled,
                priority, privacy_mode, field_allowlist_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_key,
                _route_storage_id(profile_key, cell.rule_id, cell.provider_id),
                profile_key,
                cell.rule_id,
                cell.provider_id,
                int(cell.enabled and cell.mode.value != "off"),
                priorities.get(cell.rule_id.casefold(), 0),
                privacy.mode.value,
                allowlist,
                timestamp,
                timestamp,
            ),
        )


def _quiet_values(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
) -> tuple[list[dict[str, Any]], list[PersistenceDiagnostic]]:
    rows = connection.execute(
        """
        SELECT schedule_id, weekday, start_minute, end_minute, behavior,
               enabled, exceptions_json
        FROM notification_quiet_hours
        WHERE account_key=? AND profile_id=?
        ORDER BY schedule_id, weekday
        """,
        (account_key, profile_id),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["schedule_id"]), []).append(row)
    values: list[dict[str, Any]] = []
    diagnostics: list[PersistenceDiagnostic] = []
    required = {
        "schema",
        "version",
        "window_id",
        "start",
        "end",
        "timezone",
        "bypass_importance",
        "bypass_senders",
        "bypass_sender_domains",
        "bypass_rule_ids",
    }
    for storage_id, window_rows in grouped.items():
        try:
            first = window_rows[0]
            metadata = _decode_json(first["exceptions_json"], label="quiet-hours exceptions")
            if not isinstance(metadata, dict) or set(metadata) != required:
                raise ValueError("quiet-hours metadata has an invalid shape")
            if (
                metadata["schema"] != _QUIET_METADATA_SCHEMA
                or metadata["version"] != _QUIET_METADATA_VERSION
            ):
                raise ValueError("quiet-hours metadata version is unsupported")
            window_id = _clean_text(metadata["window_id"], label="quiet window ID", maximum=200)
            if storage_id != _storage_id("quiet", profile_id, window_id):
                raise ValueError("quiet-hours storage identity does not match its definition")
            comparison = (
                first["start_minute"],
                first["end_minute"],
                first["behavior"],
                first["enabled"],
                first["exceptions_json"],
            )
            if any(
                (
                    row["start_minute"],
                    row["end_minute"],
                    row["behavior"],
                    row["enabled"],
                    row["exceptions_json"],
                )
                != comparison
                for row in window_rows[1:]
            ):
                raise ValueError("quiet-hours weekday projections disagree")
            start_text = _clean_text(metadata["start"], label="quiet start", maximum=32)
            end_text = _clean_text(metadata["end"], label="quiet end", maximum=32)
            start_parts = time.fromisoformat(start_text)
            end_parts = time.fromisoformat(end_text)
            if start_parts.hour * 60 + start_parts.minute != int(first["start_minute"]):
                raise ValueError("quiet-hours start projection disagrees")
            if end_parts.hour * 60 + end_parts.minute != int(first["end_minute"]):
                raise ValueError("quiet-hours end projection disagrees")
            enabled = _bool_column(first["enabled"], label="quiet-hours enabled")
            weekdays = [int(row["weekday"]) for row in window_rows]
            if len(weekdays) != len(set(weekdays)) or any(day < 0 or day > 6 for day in weekdays):
                raise ValueError("quiet-hours weekdays are invalid")
            values.append(
                {
                    "window_id": window_id,
                    "start": start_text,
                    "end": end_text,
                    "timezone": metadata["timezone"],
                    "weekdays": weekdays,
                    "behavior": first["behavior"],
                    "bypass_importance": metadata["bypass_importance"],
                    "bypass_senders": metadata["bypass_senders"],
                    "bypass_sender_domains": metadata["bypass_sender_domains"],
                    "bypass_rule_ids": metadata["bypass_rule_ids"],
                    "enabled": enabled,
                }
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(_safe_diagnostic("quiet-hours window", storage_id, exc))
    return values, diagnostics


def _digest_values(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
) -> tuple[list[dict[str, Any]], list[PersistenceDiagnostic]]:
    rows = connection.execute(
        """
        SELECT digest_id, name, enabled, schedule_json
        FROM notification_digest_schedules
        WHERE account_key=? AND profile_id=?
        ORDER BY digest_id
        """,
        (account_key, profile_id),
    ).fetchall()
    values: list[dict[str, Any]] = []
    diagnostics: list[PersistenceDiagnostic] = []
    for row in rows:
        try:
            data = _decode_json(row["schedule_json"], label="digest schedule")
            if not isinstance(data, dict):
                raise ValueError("digest schedule must be an object")
            raw_schedule_id = data.get("schedule_id")
            if not isinstance(raw_schedule_id, str):
                raise ValueError("digest ID must be text")
            schedule_id = _clean_text(raw_schedule_id, label="digest ID", maximum=200)
            if row["digest_id"] != _storage_id("digest", profile_id, schedule_id):
                raise ValueError("digest storage identity does not match its definition")
            data["name"] = _clean_text(row["name"], label="digest name", maximum=300)
            data["enabled"] = _bool_column(row["enabled"], label="digest enabled")
            _validate_ordinary_payload(data)
            values.append(data)
        except (TypeError, ValueError) as exc:
            diagnostics.append(_safe_diagnostic("digest schedule", row["digest_id"], exc))
    return values, diagnostics


def _route_projection_diagnostics(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    profile_id: str,
    routing_matrix: Mapping[str, Any],
) -> list[PersistenceDiagnostic]:
    raw_cells = routing_matrix.get("cells")
    if not isinstance(raw_cells, list):
        return []
    expected: set[str] = set()
    for value in raw_cells:
        if isinstance(value, Mapping):
            rule_id = value.get("rule_id")
            provider_id = value.get("provider_id")
            if isinstance(rule_id, str) and isinstance(provider_id, str):
                expected.add(_route_storage_id(profile_id, rule_id, provider_id))
    rows = connection.execute(
        """
        SELECT route_id FROM notification_routes
        WHERE account_key=? AND profile_id=?
        """,
        (account_key, profile_id),
    ).fetchall()
    actual = {str(row["route_id"]) for row in rows}
    if actual == expected:
        return []
    return [
        PersistenceDiagnostic(
            "notification route projection",
            profile_id,
            "Stored route projection does not match the canonical profile metadata.",
        )
    ]


def _read_profile_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    account_key: str,
) -> tuple[MonitoringProfile | None, tuple[PersistenceDiagnostic, ...]]:
    profile_key = str(row["profile_id"])
    diagnostics: list[PersistenceDiagnostic] = []
    try:
        metadata = _decode_json(row["metadata_json"], label="notification profile metadata")
        required = {
            "schema",
            "version",
            "profile_id",
            "notify_existing",
            "providers",
            "routing_matrix",
        }
        if not isinstance(metadata, dict) or set(metadata) != required:
            raise ValueError("profile metadata has an invalid shape")
        if (
            metadata["schema"] != _PROFILE_METADATA_SCHEMA
            or metadata["version"] != _PROFILE_METADATA_VERSION
        ):
            raise ValueError("profile metadata version is unsupported")
        original_id = _clean_text(
            metadata["profile_id"], label="notification profile ID", maximum=200
        )
        if profile_key != original_id.casefold():
            raise ValueError("profile storage identity does not match its definition")
        folders = _decode_json(row["folders_json"], label="profile folders")
        if not isinstance(folders, list) or any(not isinstance(item, str) for item in folders):
            raise ValueError("profile folders must be a list of text")
        enabled = _bool_column(row["enabled"], label="profile enabled")
        strategy = row["strategy_preference"]
        if strategy is not None:
            _validate_ordinary_payload(strategy, path=("strategy_preference",))
            if not isinstance(strategy, str) or strategy not in _MONITOR_STRATEGIES:
                raise ValueError("profile strategy preference is unsupported")
        PrivacyMode(str(row["privacy_mode"]))
        notify_existing = metadata["notify_existing"]
        if not isinstance(notify_existing, bool):
            raise ValueError("profile notify_existing must be boolean")
        rules = _rule_rows(
            connection,
            account_key=account_key,
            profile_id=profile_key,
        )
        diagnostics.extend(rules.diagnostics)
        quiet, quiet_diagnostics = _quiet_values(
            connection,
            account_key=account_key,
            profile_id=profile_key,
        )
        diagnostics.extend(quiet_diagnostics)
        digests, digest_diagnostics = _digest_values(
            connection,
            account_key=account_key,
            profile_id=profile_key,
        )
        diagnostics.extend(digest_diagnostics)
        routing = metadata["routing_matrix"]
        if not isinstance(routing, Mapping):
            raise ValueError("profile routing_matrix must be an object")
        raw_cells = routing.get("cells")
        if not isinstance(raw_cells, list):
            raise ValueError("profile routing cells must be a list")
        persisted_rule_ids = {rule.rule_id.casefold() for rule in rules.items}
        stale_cells = [
            cell
            for cell in raw_cells
            if isinstance(cell, Mapping)
            and isinstance(cell.get("rule_id"), str)
            and cast(str, cell["rule_id"]).casefold() not in persisted_rule_ids
        ]
        if stale_cells:
            diagnostics.append(
                PersistenceDiagnostic(
                    "notification route projection",
                    profile_key,
                    "Stored routing matrix references a rule that is not configured.",
                )
            )
        diagnostics.extend(
            _route_projection_diagnostics(
                connection,
                account_key=account_key,
                profile_id=profile_key,
                routing_matrix=routing,
            )
        )
        data = {
            "profile_id": original_id,
            "name": _clean_text(row["name"], label="profile name", maximum=300),
            "folders": folders,
            "poll_interval_seconds": float(row["poll_interval_seconds"]),
            "enabled": enabled,
            "notify_existing": notify_existing,
            "rules": [rule_to_data(rule) for rule in rules.items],
            "providers": metadata["providers"],
            "quiet_hours": quiet,
            "digest_schedules": digests,
            "routing_matrix": routing,
        }
        _validate_ordinary_payload(data)
        return profile_from_data(data), tuple(diagnostics)
    except (TypeError, ValueError) as exc:
        diagnostics.append(_safe_diagnostic("notification profile", profile_key, exc))
        return None, tuple(diagnostics)


class PersistentMonitoringProfileRepository:
    """SQLite implementation of ``MonitoringProfileRepository``."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def inspect(self) -> PersistenceSnapshot[MonitoringProfile]:
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT profile_id, name, enabled, folders_json, strategy_preference,
                           poll_interval_seconds, privacy_mode, metadata_json
                    FROM notification_profiles
                    WHERE account_key=?
                    ORDER BY name COLLATE NOCASE, profile_id
                    """,
                    (self._store.account_key,),
                ).fetchall()
                profiles: list[MonitoringProfile] = []
                diagnostics: list[PersistenceDiagnostic] = []
                for row in rows:
                    profile, row_diagnostics = _read_profile_row(
                        connection,
                        row,
                        account_key=self._store.account_key,
                    )
                    if profile is not None and not row_diagnostics:
                        profiles.append(profile)
                    diagnostics.extend(row_diagnostics)
                return PersistenceSnapshot(tuple(profiles), tuple(diagnostics))
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc

    def list_profiles(self) -> tuple[MonitoringProfile, ...]:
        return self.inspect().items

    def get_profile(self, profile_id: str) -> MonitoringProfile | None:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                row = connection.execute(
                    """
                    SELECT profile_id, name, enabled, folders_json, strategy_preference,
                           poll_interval_seconds, privacy_mode, metadata_json
                    FROM notification_profiles
                    WHERE account_key=? AND profile_id=?
                    """,
                    (self._store.account_key, profile_key),
                ).fetchone()
                if row is None:
                    return None
                profile, diagnostics = _read_profile_row(
                    connection,
                    row,
                    account_key=self._store.account_key,
                )
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc
        if profile is None or diagnostics:
            message = diagnostics[-1].message if diagnostics else "Stored profile is invalid."
            raise LocalStateError(message)
        return profile

    def save_profile(self, profile: MonitoringProfile) -> None:
        try:
            with self._store.transaction(immediate=True) as connection:
                _write_profile(
                    connection,
                    account_key=self._store.account_key,
                    profile=profile,
                    timestamp=iso(),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("a monitoring profile with that name already exists") from exc
        except sqlite3.Error as exc:
            raise _state_failure("save", exc) from exc

    def update_profile(
        self,
        profile_id: str,
        transform: Callable[[MonitoringProfile], MonitoringProfile],
    ) -> MonitoringProfile:
        """Read, transform, validate, and replace one profile in one writer transaction."""

        profile_key = _canonical_id(profile_id, label="notification profile ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                row = connection.execute(
                    """
                    SELECT profile_id, name, enabled, folders_json, strategy_preference,
                           poll_interval_seconds, privacy_mode, metadata_json
                    FROM notification_profiles
                    WHERE account_key=? AND profile_id=?
                    """,
                    (self._store.account_key, profile_key),
                ).fetchone()
                if row is None:
                    raise KeyError(profile_id)
                current, diagnostics = _read_profile_row(
                    connection,
                    row,
                    account_key=self._store.account_key,
                )
                if current is None or diagnostics:
                    raise LocalStateError(diagnostics[-1].message)
                updated = transform(current)
                if not isinstance(updated, MonitoringProfile):
                    raise TypeError("profile transform must return MonitoringProfile")
                if updated.profile_id.casefold() != current.profile_id.casefold():
                    raise ValueError("profile transform cannot change profile_id")
                _write_profile(
                    connection,
                    account_key=self._store.account_key,
                    profile=updated,
                    timestamp=iso(),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("a monitoring profile with that name already exists") from exc
        except sqlite3.Error as exc:
            raise _state_failure("update", exc) from exc
        return updated

    def delete_profile(self, profile_id: str) -> bool:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                for table in (
                    "notification_rule_metadata",
                    "notification_quiet_hours",
                    "notification_digest_schedules",
                    "notification_routes",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE account_key=? AND profile_id=?",  # noqa: S608
                        (self._store.account_key, profile_key),
                    )
                cursor = connection.execute(
                    "DELETE FROM notification_profiles WHERE account_key=? AND profile_id=?",
                    (self._store.account_key, profile_key),
                )
                active = connection.execute(
                    """
                    SELECT value_json FROM ui_preferences
                    WHERE account_key=? AND scope=? AND preference_key=?
                    """,
                    (self._store.account_key, _ACTIVE_SCOPE, _ACTIVE_KEY),
                ).fetchone()
                if active is not None:
                    try:
                        active_id = json.loads(active["value_json"])
                    except json.JSONDecodeError:
                        active_id = None
                    if isinstance(active_id, str) and active_id.casefold() == profile_key:
                        connection.execute(
                            """
                            DELETE FROM ui_preferences
                            WHERE account_key=? AND scope=? AND preference_key=?
                            """,
                            (self._store.account_key, _ACTIVE_SCOPE, _ACTIVE_KEY),
                        )
        except sqlite3.Error as exc:
            raise _state_failure("delete", exc) from exc
        return cursor.rowcount == 1

    def set_enabled(self, profile_id: str, *, enabled: bool) -> MonitoringProfile:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                row = connection.execute(
                    """
                    SELECT profile_id, name, enabled, folders_json, strategy_preference,
                           poll_interval_seconds, privacy_mode, metadata_json
                    FROM notification_profiles
                    WHERE account_key=? AND profile_id=?
                    """,
                    (self._store.account_key, profile_key),
                ).fetchone()
                if row is None:
                    raise KeyError(profile_id)
                current, diagnostics = _read_profile_row(
                    connection,
                    row,
                    account_key=self._store.account_key,
                )
                if current is None or diagnostics:
                    raise LocalStateError(diagnostics[-1].message)
                connection.execute(
                    """
                    UPDATE notification_profiles SET enabled=?, updated_at=?
                    WHERE account_key=? AND profile_id=?
                    """,
                    (int(enabled), iso(), self._store.account_key, profile_key),
                )
                updated = replace(current, enabled=enabled)
        except sqlite3.Error as exc:
            raise _state_failure("update", exc) from exc
        return updated

    def strategy_preference(self, profile_id: str) -> str | None:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                row = connection.execute(
                    """
                    SELECT strategy_preference FROM notification_profiles
                    WHERE account_key=? AND profile_id=?
                    """,
                    (self._store.account_key, profile_key),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _state_failure("read strategy", exc) from exc
        if row is None:
            raise KeyError(profile_id)
        value = row["strategy_preference"]
        if value is None:
            return None
        if not isinstance(value, str) or value not in _MONITOR_STRATEGIES:
            raise LocalStateError("Stored notification strategy preference is invalid.")
        return value

    def set_strategy_preference(
        self,
        profile_id: str,
        strategy: str | None,
    ) -> str | None:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        if strategy is not None and strategy not in _MONITOR_STRATEGIES:
            raise ValueError("unsupported notification monitoring strategy")
        try:
            with self._store.transaction(immediate=True) as connection:
                cursor = connection.execute(
                    """
                    UPDATE notification_profiles
                    SET strategy_preference=?, updated_at=?
                    WHERE account_key=? AND profile_id=?
                    """,
                    (strategy, iso(), self._store.account_key, profile_key),
                )
                if cursor.rowcount != 1:
                    raise KeyError(profile_id)
        except sqlite3.Error as exc:
            raise _state_failure("update strategy", exc) from exc
        return strategy

    def activate(self, profile_id: str) -> MonitoringProfile:
        profile_key = _canonical_id(profile_id, label="notification profile ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                row = connection.execute(
                    """
                    SELECT profile_id, name, enabled, folders_json, strategy_preference,
                           poll_interval_seconds, privacy_mode, metadata_json
                    FROM notification_profiles
                    WHERE account_key=? AND profile_id=?
                    """,
                    (self._store.account_key, profile_key),
                ).fetchone()
                if row is None:
                    raise KeyError(profile_id)
                profile, diagnostics = _read_profile_row(
                    connection,
                    row,
                    account_key=self._store.account_key,
                )
                if profile is None or diagnostics:
                    raise LocalStateError(diagnostics[-1].message)
                value_json = _encode_json(profile.profile_id)
                connection.execute(
                    """
                    INSERT INTO ui_preferences(
                        account_key, scope, preference_key, value_json, revision, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(account_key, scope, preference_key) DO UPDATE SET
                        value_json=excluded.value_json,
                        revision=ui_preferences.revision + 1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        self._store.account_key,
                        _ACTIVE_SCOPE,
                        _ACTIVE_KEY,
                        value_json,
                        iso(),
                    ),
                )
        except sqlite3.Error as exc:
            raise _state_failure("activate", exc) from exc
        return profile

    def active_profile(self) -> MonitoringProfile | None:
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                row = connection.execute(
                    """
                    SELECT value_json FROM ui_preferences
                    WHERE account_key=? AND scope=? AND preference_key=?
                    """,
                    (self._store.account_key, _ACTIVE_SCOPE, _ACTIVE_KEY),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _state_failure("read active", exc) from exc
        if row is None:
            return None
        try:
            profile_id = _decode_json(row["value_json"], label="active profile pointer")
            if not isinstance(profile_id, str):
                raise ValueError("active profile pointer must be text")
        except ValueError as exc:
            raise LocalStateError(
                _safe_diagnostic("active profile pointer", _ACTIVE_KEY, exc).message
            ) from exc
        return self.get_profile(profile_id)

    def export_document(self) -> str:
        snapshot = self.inspect()
        if snapshot.diagnostics:
            raise LocalStateError(
                "Cannot export while stored monitoring profiles contain invalid rows."
            )
        return export_profiles(snapshot.items)

    def import_document(
        self,
        document: str | bytes,
        *,
        replace_existing: bool = False,
    ) -> tuple[MonitoringProfile, ...]:
        profiles = import_profiles(document)
        names = [profile.name.casefold() for profile in profiles]
        if len(names) != len(set(names)):
            raise ValueError("monitoring-profile document contains duplicate names")
        for profile in profiles:
            _validate_ordinary_payload(profile_to_data(profile))
        try:
            with self._store.transaction(immediate=True) as connection:
                rows = connection.execute(
                    "SELECT profile_id, name FROM notification_profiles WHERE account_key=?",
                    (self._store.account_key,),
                ).fetchall()
                existing_ids = {str(row["profile_id"]).casefold() for row in rows}
                existing_names = {str(row["name"]).casefold() for row in rows}
                incoming_ids = {profile.profile_id.casefold() for profile in profiles}
                incoming_names = set(names)
                if not replace_existing and (
                    existing_ids & incoming_ids or existing_names & incoming_names
                ):
                    raise ValueError("monitoring-profile import would overwrite existing data")
                if replace_existing:
                    for table in (
                        "notification_rule_metadata",
                        "notification_quiet_hours",
                        "notification_digest_schedules",
                        "notification_routes",
                        "notification_profiles",
                    ):
                        connection.execute(
                            f"DELETE FROM {table} WHERE account_key=?",  # noqa: S608
                            (self._store.account_key,),
                        )
                    connection.execute(
                        """
                        DELETE FROM ui_preferences
                        WHERE account_key=? AND scope=? AND preference_key=?
                        """,
                        (self._store.account_key, _ACTIVE_SCOPE, _ACTIVE_KEY),
                    )
                timestamp = iso()
                for profile in profiles:
                    _write_profile(
                        connection,
                        account_key=self._store.account_key,
                        profile=profile,
                        timestamp=timestamp,
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("monitoring-profile import contains a conflicting name") from exc
        except sqlite3.Error as exc:
            raise _state_failure("import", exc) from exc
        return profiles


def _provider_to_data(
    provider: ProviderConfigurationMetadata,
    *,
    include_secret_reference: bool,
) -> dict[str, Any]:
    return {
        "provider_id": provider.provider_id,
        "kind": provider.kind,
        "name": provider.name,
        "enabled": provider.enabled,
        "secret_ref": provider.secret_ref if include_secret_reference else None,
        "metadata": dict(provider.metadata),
    }


class PersistentProviderMetadataRepository:
    """Account-scoped metadata for concrete notification provider instances."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    @staticmethod
    def _from_row(
        row: sqlite3.Row,
    ) -> tuple[ProviderConfigurationMetadata | None, PersistenceDiagnostic | None]:
        identifier = row["provider_id"]
        try:
            metadata = _decode_json(row["metadata_json"], label="provider metadata")
            if not isinstance(metadata, dict):
                raise ValueError("provider metadata must be an object")
            secret_ref = row["secret_ref"]
            if secret_ref is not None and not isinstance(secret_ref, str):
                raise ValueError("provider secret reference must be text")
            provider = ProviderConfigurationMetadata(
                _clean_text(identifier, label="provider ID", maximum=200),
                _clean_text(row["kind"], label="provider kind", maximum=100),
                _clean_text(row["name"], label="provider name", maximum=300),
                _bool_column(row["enabled"], label="provider enabled"),
                secret_ref,
                metadata,
            )
            return provider, None
        except (TypeError, ValueError) as exc:
            return None, _safe_diagnostic("provider metadata", identifier, exc)

    def inspect(self) -> PersistenceSnapshot[ProviderConfigurationMetadata]:
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT provider_id, kind, name, enabled, secret_ref, metadata_json
                    FROM notification_provider_configs
                    WHERE account_key=?
                    ORDER BY name COLLATE NOCASE, provider_id
                    """,
                    (self._store.account_key,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc
        values: list[ProviderConfigurationMetadata] = []
        diagnostics: list[PersistenceDiagnostic] = []
        for row in rows:
            provider, diagnostic = self._from_row(row)
            if provider is not None:
                values.append(provider)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return PersistenceSnapshot(tuple(values), tuple(diagnostics))

    def list_providers(self) -> tuple[ProviderConfigurationMetadata, ...]:
        return self.inspect().items

    def get_provider(self, provider_id: str) -> ProviderConfigurationMetadata | None:
        key = _canonical_id(provider_id, label="provider ID")
        self._store.initialize()
        try:
            with self._store.connection() as connection:
                row = connection.execute(
                    """
                    SELECT provider_id, kind, name, enabled, secret_ref, metadata_json
                    FROM notification_provider_configs
                    WHERE account_key=? AND provider_id=?
                    """,
                    (self._store.account_key, key),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _state_failure("read", exc) from exc
        if row is None:
            return None
        provider, diagnostic = self._from_row(row)
        if provider is None:
            raise LocalStateError(cast(PersistenceDiagnostic, diagnostic).message)
        return provider

    def _upsert(
        self,
        connection: sqlite3.Connection,
        provider: ProviderConfigurationMetadata,
        *,
        timestamp: str,
    ) -> None:
        key = _canonical_id(provider.provider_id, label="provider ID")
        connection.execute(
            """
            INSERT INTO notification_provider_configs(
                account_key, provider_id, kind, name, enabled, secret_ref,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key, provider_id) DO UPDATE SET
                kind=excluded.kind,
                name=excluded.name,
                enabled=excluded.enabled,
                secret_ref=excluded.secret_ref,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                self._store.account_key,
                key,
                provider.kind,
                provider.name,
                int(provider.enabled),
                provider.secret_ref,
                _encode_json(dict(provider.metadata)),
                timestamp,
                timestamp,
            ),
        )

    def save_provider(self, provider: ProviderConfigurationMetadata) -> None:
        try:
            with self._store.transaction(immediate=True) as connection:
                self._upsert(connection, provider, timestamp=iso())
        except sqlite3.Error as exc:
            raise _state_failure("save", exc) from exc

    def delete_provider(self, provider_id: str) -> bool:
        key = _canonical_id(provider_id, label="provider ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                used = connection.execute(
                    """
                    SELECT 1 FROM notification_routes
                    WHERE account_key=? AND provider_id=? COLLATE NOCASE AND enabled=1 LIMIT 1
                    """,
                    (self._store.account_key, key),
                ).fetchone()
                if used is not None:
                    raise ValueError("provider is referenced by an enabled notification route")
                cursor = connection.execute(
                    """
                    DELETE FROM notification_provider_configs
                    WHERE account_key=? AND provider_id=?
                    """,
                    (self._store.account_key, key),
                )
        except sqlite3.Error as exc:
            raise _state_failure("delete", exc) from exc
        return cursor.rowcount == 1

    def set_enabled(
        self,
        provider_id: str,
        *,
        enabled: bool,
    ) -> ProviderConfigurationMetadata:
        key = _canonical_id(provider_id, label="provider ID")
        try:
            with self._store.transaction(immediate=True) as connection:
                row = connection.execute(
                    """
                    SELECT provider_id, kind, name, enabled, secret_ref, metadata_json
                    FROM notification_provider_configs
                    WHERE account_key=? AND provider_id=?
                    """,
                    (self._store.account_key, key),
                ).fetchone()
                if row is None:
                    raise KeyError(provider_id)
                current, diagnostic = self._from_row(row)
                if current is None:
                    raise LocalStateError(cast(PersistenceDiagnostic, diagnostic).message)
                updated = replace(current, enabled=enabled)
                self._upsert(connection, updated, timestamp=iso())
        except sqlite3.Error as exc:
            raise _state_failure("update", exc) from exc
        return updated

    def export_document(self, *, include_secret_references: bool = False) -> str:
        snapshot = self.inspect()
        if snapshot.diagnostics:
            raise LocalStateError(
                "Cannot export while stored provider metadata contains invalid rows."
            )
        document = {
            "schema": "gucmail.notification-provider-metadata",
            "version": 1,
            "providers": [
                _provider_to_data(
                    provider,
                    include_secret_reference=include_secret_references,
                )
                for provider in snapshot.items
            ],
        }
        return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def import_document(
        self,
        document: str | bytes,
        *,
        replace_existing: bool = False,
    ) -> tuple[ProviderConfigurationMetadata, ...]:
        raw = document.encode("utf-8") if isinstance(document, str) else document
        if len(raw) > _MAX_PROVIDER_DOCUMENT_BYTES:
            raise ValueError("provider metadata document is too large")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider metadata document is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict) or set(decoded) != {"schema", "version", "providers"}:
            raise ValueError("provider metadata document has an invalid shape")
        if decoded["schema"] != "gucmail.notification-provider-metadata" or decoded["version"] != 1:
            raise ValueError("unsupported provider metadata document schema or version")
        raw_providers = decoded["providers"]
        if not isinstance(raw_providers, list) or len(raw_providers) > _MAX_PROVIDER_CONFIGS:
            raise ValueError("provider metadata document has an invalid provider list")
        providers: list[ProviderConfigurationMetadata] = []
        for value in raw_providers:
            if not isinstance(value, dict) or set(value) != {
                "provider_id",
                "kind",
                "name",
                "enabled",
                "secret_ref",
                "metadata",
            }:
                raise ValueError("provider metadata entry has an invalid shape")
            enabled = value["enabled"]
            metadata = value["metadata"]
            if not isinstance(enabled, bool) or not isinstance(metadata, dict):
                raise ValueError("provider enabled and metadata values have invalid types")
            secret_ref = value["secret_ref"]
            if secret_ref is not None and not isinstance(secret_ref, str):
                raise ValueError("provider secret_ref must be text or null")
            providers.append(
                ProviderConfigurationMetadata(
                    value["provider_id"],
                    value["kind"],
                    value["name"],
                    enabled,
                    secret_ref,
                    metadata,
                )
            )
        identifiers = [provider.provider_id.casefold() for provider in providers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("provider metadata document contains duplicate identifiers")
        try:
            with self._store.transaction(immediate=True) as connection:
                rows = connection.execute(
                    """
                    SELECT provider_id FROM notification_provider_configs
                    WHERE account_key=?
                    """,
                    (self._store.account_key,),
                ).fetchall()
                existing = {str(row["provider_id"]).casefold() for row in rows}
                if existing & set(identifiers) and not replace_existing:
                    raise ValueError("provider metadata import would overwrite existing data")
                if replace_existing:
                    connection.execute(
                        "DELETE FROM notification_provider_configs WHERE account_key=?",
                        (self._store.account_key,),
                    )
                timestamp = iso()
                for provider in providers:
                    self._upsert(connection, provider, timestamp=timestamp)
        except sqlite3.Error as exc:
            raise _state_failure("import", exc) from exc
        return tuple(providers)


class NotificationConfigurationService:
    """Application adapter shared by watcher, Settings, CLI, and TUI surfaces."""

    def __init__(self, store: StateStore) -> None:
        self.profiles = PersistentMonitoringProfileRepository(store)
        self.providers = PersistentProviderMetadataRepository(store)
        self._store = store

    def rules(self, profile_id: str = "default") -> PersistentNotificationRuleRepository:
        return PersistentNotificationRuleRepository(self._store, profile_id)

    def active_or_default_profile(self) -> MonitoringProfile:
        """Return a durable active profile, creating the private default when needed."""

        profile = self.profiles.active_profile()
        if profile is not None:
            return profile
        existing = self.profiles.list_profiles()
        if existing:
            return self.profiles.activate(existing[0].profile_id)
        profile = MonitoringProfile.safe_default()
        self.profiles.save_profile(profile)
        return self.profiles.activate(profile.profile_id)

    def set_desktop_notifications(self, *, enabled: bool) -> MonitoringProfile:
        """Persist an executable desktop provider policy for the active profile."""

        profile = self.active_or_default_profile()
        metadata = tuple(
            item for item in self.providers.list_providers() if item.kind.casefold() == "desktop"
        )
        if metadata:
            provider_id = metadata[0].provider_id
        else:
            provider_id = "desktop"
            created = ProviderConfigurationMetadata(
                provider_id,
                "desktop",
                "Windows desktop",
                enabled,
            )
            self.providers.save_provider(created)
            metadata = (created,)
        for item in metadata:
            self.providers.set_enabled(item.provider_id, enabled=enabled)

        providers = list(profile.providers)
        target_ids = {item.provider_id.casefold() for item in metadata}
        found = False
        for index, policy in enumerate(providers):
            if policy.provider_id.casefold() in target_ids:
                providers[index] = replace(policy, enabled=enabled)
                found = True
        if not found:
            providers.append(
                ProviderPolicy(
                    provider_id,
                    enabled=enabled,
                    privacy=EventDisclosurePolicy(mode=PrivacyMode.SENDER_AND_SUBJECT),
                )
            )

        managed_rule_id = "desktop-notifications-all-mail"
        rules = list(profile.rules)
        managed = next(
            (index for index, rule in enumerate(rules) if rule.rule_id == managed_rule_id),
            None,
        )
        rule = NotificationRule(
            managed_rule_id,
            "Desktop notifications for new mail",
            WeekdayCondition(frozenset(range(7))),
            (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, provider_id),),
            enabled=enabled,
            priority=-1_000,
            order=max((item.order for item in rules), default=-1) + 1,
            description="Managed by the desktop-notification palette setting.",
        )
        if managed is None:
            rules.append(rule)
        else:
            rules[managed] = replace(rule, order=rules[managed].order)
        return self.profiles.update_profile(
            profile.profile_id,
            lambda current: replace(
                current,
                providers=tuple(providers),
                rules=tuple(rules),
            ),
        )

    def mute_notifications(self) -> MonitoringProfile:
        """Disable delivery policies without stopping mailbox synchronization."""

        profile = self.active_or_default_profile()
        return self.replace_provider_policies(
            profile.profile_id,
            tuple(replace(policy, enabled=False) for policy in profile.providers),
        )

    def enable_quiet_hours(self) -> MonitoringProfile:
        """Enable configured quiet windows or create a conservative nightly default."""

        profile = self.active_or_default_profile()
        windows = profile.quiet_hours.windows
        if windows:
            enabled = tuple(replace(window, enabled=True) for window in windows)
        else:
            enabled = (
                QuietHoursWindow(
                    "default-night",
                    time(22, 0),
                    time(7, 0),
                    "Africa/Cairo",
                    behavior=QuietHoursBehavior.QUEUE,
                ),
            )
        return self.replace_quiet_hours(profile.profile_id, QuietHoursSchedule(enabled))

    def set_notification_privacy(self, mode: PrivacyMode) -> MonitoringProfile:
        """Apply one disclosure mode to every active-profile destination."""

        profile = self.active_or_default_profile()
        providers = profile.providers
        if not providers:
            metadata = ProviderConfigurationMetadata(
                "desktop",
                "desktop",
                "Windows desktop",
                False,
            )
            self.providers.save_provider(metadata)
            providers = (ProviderPolicy("desktop"),)
        updated = tuple(
            replace(policy, privacy=replace(policy.privacy, mode=mode)) for policy in providers
        )
        return self.replace_provider_policies(profile.profile_id, updated)

    def watcher_configuration(
        self,
        profile_id: str | None = None,
    ) -> WatcherProfileConfiguration | None:
        profile = (
            self.profiles.get_profile(profile_id)
            if profile_id is not None
            else self.profiles.active_profile()
        )
        if profile is None:
            return None
        return WatcherProfileConfiguration(
            profile.profile_id,
            profile.folders,
            profile.poll_interval_seconds,
            self.profiles.strategy_preference(profile.profile_id),
            profile.enabled,
            profile.notify_existing,
        )

    def replace_quiet_hours(
        self,
        profile_id: str,
        quiet_hours: QuietHoursSchedule,
    ) -> MonitoringProfile:
        return self.profiles.update_profile(
            profile_id,
            lambda profile: replace(profile, quiet_hours=quiet_hours),
        )

    def replace_rules(
        self,
        profile_id: str,
        rules: Sequence[NotificationRule],
    ) -> MonitoringProfile:
        values = tuple(rules)
        return self.profiles.update_profile(
            profile_id,
            lambda profile: replace(profile, rules=values),
        )

    def replace_digest_schedules(
        self,
        profile_id: str,
        schedules: Sequence[DigestSchedule],
    ) -> MonitoringProfile:
        values = tuple(schedules)
        return self.profiles.update_profile(
            profile_id,
            lambda profile: replace(profile, digest_schedules=values),
        )

    def replace_provider_policies(
        self,
        profile_id: str,
        providers: Sequence[ProviderPolicy],
    ) -> MonitoringProfile:
        values = tuple(providers)
        return self.profiles.update_profile(
            profile_id,
            lambda profile: replace(profile, providers=values),
        )

    def replace_routing_matrix(
        self,
        profile_id: str,
        routing_matrix: RoutingMatrix,
    ) -> MonitoringProfile:
        return self.profiles.update_profile(
            profile_id,
            lambda profile: replace(profile, routing_matrix=routing_matrix),
        )

    def simulate(
        self,
        profile_id: str,
        events: Sequence[NewMailEvent],
        *,
        context: RuleEvaluationContext | None = None,
        max_examples: int = 10,
        rule_id: str | None = None,
        at: datetime | None = None,
    ) -> RuleSimulationResult:
        profile = self.profiles.get_profile(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        engine = NotificationRuleEngine(self._preview_rules(profile, rule_id), context=context)
        router = NotificationRouter(context=context)
        return NotificationRuleSimulator(
            engine,
            max_examples=max_examples,
            route=lambda event, decision: router.plan(event, decision, profile, at=at),
        ).simulate(events)

    @staticmethod
    def _preview_rules(
        profile: MonitoringProfile, rule_id: str | None
    ) -> tuple[NotificationRule, ...]:
        if rule_id is None:
            return profile.rules
        selected = tuple(
            rule for rule in profile.rules if rule.rule_id.casefold() == rule_id.casefold()
        )
        if not selected:
            raise KeyError(rule_id)
        # Explicit single-rule tests work before enabling the saved rule. The copy
        # exists only in this preview; all-profile previews retain enabled state.
        return tuple(replace(rule, enabled=True) for rule in selected)

    def test_event(
        self,
        profile_id: str,
        event: NewMailEvent,
        *,
        context: RuleEvaluationContext | None = None,
        at: datetime | None = None,
        rule_id: str | None = None,
    ) -> NotificationConfigurationTestResult:
        profile = self.profiles.get_profile(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        engine = NotificationRuleEngine(self._preview_rules(profile, rule_id), context=context)
        decision = engine.evaluate(event)
        routing = NotificationRouter(context=context).plan(event, decision, profile, at=at)
        return NotificationConfigurationTestResult(decision, routing)


__all__ = [
    "NotificationConfigurationService",
    "NotificationConfigurationTestResult",
    "PersistenceDiagnostic",
    "PersistenceSnapshot",
    "PersistentMonitoringProfileRepository",
    "PersistentNotificationRuleRepository",
    "PersistentProviderMetadataRepository",
    "ProviderConfigurationMetadata",
    "WatcherProfileConfiguration",
]
