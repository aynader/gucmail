"""Secure outbound webhook notifications and their local registry/history."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from gucmail.constants import USER_AGENT
from gucmail.errors import ConfigurationError, ItemNotFoundError
from gucmail.models import NotificationEvent
from gucmail.secret_store import (
    SECRET_REFERENCE_PREFIX,
    SecretReference,
    SecretStore,
)
from gucmail.security import hmac_signature, redact_text, validate_webhook_url
from gucmail.services.notifications import (
    EVENT_FIELDS,
    DeliveryResult,
    DeliveryState,
    event_payload,
)
from gucmail.state import StateStore, iso

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BLOCKED_HEADERS = frozenset(
    {"connection", "content-length", "host", "transfer-encoding", "upgrade"}
)
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_ALLOWED_METHODS = frozenset({"POST", "PUT", "PATCH"})
_KEYRING_PREFIX = SECRET_REFERENCE_PREFIX
_DELIVERY_OPTIONS = {
    "connect_timeout_seconds": float,
    "request_timeout_seconds": float,
    "retry_base_seconds": float,
    "retry_max_seconds": float,
    "redact": bool,
    "allow_private_target": bool,
    "allow_integration_events": bool,
    "history_limit": int,
}


SecretResolver = Callable[[str], str | bytes | None]
Sleeper = Callable[[float], None]
Clock = Callable[[], datetime]
RandomSource = Callable[[], float]


def _header_is_sensitive(name: str) -> bool:
    """Recognize conventional and vendor-specific credential header names."""

    normalized = name.casefold().replace("_", "-")
    if normalized in _SENSITIVE_HEADERS:
        return True
    components = frozenset(part for part in re.split(r"[^a-z0-9]+", normalized) if part)
    if {"token", "secret", "signature", "authorization", "auth"} & components:
        return True
    return "api-key" in normalized or "apikey" in normalized


def _validate_headers(
    headers: Mapping[str, str], *, allow_legacy_plaintext_secrets: bool = False
) -> dict[str, str]:
    validated: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name)
        value = str(raw_value)
        if not _HEADER_NAME.fullmatch(name):
            raise ConfigurationError(f"Invalid webhook header name: {name!r}")
        if name.casefold() in _BLOCKED_HEADERS:
            raise ConfigurationError(f"Webhook header {name!r} is controlled by the HTTP client.")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ConfigurationError(f"Webhook header {name!r} contains an invalid character.")
        if value.startswith(_KEYRING_PREFIX):
            SecretReference.parse(value)
        if (
            _header_is_sensitive(name)
            and not value.startswith(_KEYRING_PREFIX)
            and not allow_legacy_plaintext_secrets
        ):
            raise ConfigurationError(
                f"Secret webhook header {name!r} must use a keyring:// reference."
            )
        validated[name] = value
    return validated


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Configuration for one outbound destination.

    ``secret`` is runtime-only and is never persisted by :class:`WebhookRegistry`.
    Custom header values may use ``keyring://entry-name`` and are resolved immediately
    before a request.
    """

    name: str
    url: str = field(repr=False)
    method: str = "POST"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    secret_keyring_name: str | None = field(default=None, repr=False)
    secret: bytes | None = field(default=None, repr=False, compare=False)
    field_allowlist: frozenset[str] | None = None
    enabled: bool = True
    max_retries: int = 3
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 15.0
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    redact: bool = True
    allow_private_target: bool = False
    allow_integration_events: bool = False
    webhook_id: int | None = None
    history_limit: int = 500
    allow_legacy_plaintext_secrets: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 100:
            raise ConfigurationError("Webhook name must contain between 1 and 100 characters.")
        method = self.method.upper()
        if method not in _ALLOWED_METHODS:
            raise ConfigurationError("Webhook method must be POST, PUT, or PATCH.")
        if self.url.startswith(_KEYRING_PREFIX):
            SecretReference.parse(self.url)
        else:
            validate_webhook_url(self.url, allow_private=self.allow_private_target)
        headers = _validate_headers(
            self.headers,
            allow_legacy_plaintext_secrets=self.allow_legacy_plaintext_secrets,
        )
        if self.field_allowlist is not None:
            unknown = set(self.field_allowlist) - EVENT_FIELDS
            if unknown:
                raise ConfigurationError(
                    f"Unknown webhook payload field(s): {', '.join(sorted(unknown))}"
                )
        if not 0 <= self.max_retries <= 10:
            raise ConfigurationError("Webhook max_retries must be between 0 and 10.")
        if self.secret is not None and len(self.secret) < 16:
            raise ConfigurationError("Webhook HMAC secrets must contain at least 16 bytes.")
        if not 0 < self.connect_timeout_seconds <= self.request_timeout_seconds <= 120:
            raise ConfigurationError(
                "Webhook timeouts must be positive, no more than 120 seconds, and the "
                "connection timeout may not exceed the request timeout."
            )
        if not 0 < self.retry_base_seconds <= self.retry_max_seconds <= 600:
            raise ConfigurationError("Webhook retry delays must be between 0 and 600 seconds.")
        if not 10 <= self.history_limit <= 10_000:
            raise ConfigurationError("Webhook history_limit must be between 10 and 10000.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "headers", headers)
        if self.field_allowlist is not None:
            object.__setattr__(self, "field_allowlist", frozenset(self.field_allowlist))

    @property
    def destination_is_reference(self) -> bool:
        return self.url.startswith(_KEYRING_PREFIX)

    @property
    def legacy_secret_headers(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name, value in self.headers.items()
                if _header_is_sensitive(name) and not value.startswith(_KEYRING_PREFIX)
            )
        )

    @property
    def plaintext_headers(self) -> tuple[str, ...]:
        """All header values which have not yet crossed the secure-store boundary.

        Header names are not a reliable sensitivity oracle: providers commonly use names
        such as ``X-Client-Key`` or entirely custom identifiers.  Persistence therefore
        treats every custom header value as secret unless it is already an opaque reference.
        """

        return tuple(
            sorted(
                name
                for name, value in self.headers.items()
                if not value.startswith(_KEYRING_PREFIX)
            )
        )

    @property
    def requires_secret_migration(self) -> bool:
        """Whether ordinary persistence still contains secret-bearing values."""

        return not self.destination_is_reference or bool(self.plaintext_headers)

    def redacted_dict(self) -> dict[str, Any]:
        """Serialize registry metadata without destination or header secret values."""

        return {
            "name": self.name,
            "destination": (
                "secure_reference" if self.destination_is_reference else "legacy_https_configured"
            ),
            "method": self.method,
            "header_names": tuple(sorted(self.headers)),
            "secret_header_names": tuple(
                sorted(name for name in self.headers if _header_is_sensitive(name))
            ),
            "signing_secret_configured": bool(self.secret_keyring_name or self.secret),
            "field_allowlist": (
                tuple(sorted(self.field_allowlist)) if self.field_allowlist is not None else None
            ),
            "enabled": self.enabled,
            "max_retries": self.max_retries,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "redact": self.redact,
            "allow_private_target": self.allow_private_target,
            "allow_integration_events": self.allow_integration_events,
            "webhook_id": self.webhook_id,
            "history_limit": self.history_limit,
            "legacy_secret_migration_required": self.requires_secret_migration,
        }

    def to_safe_primitive(self) -> dict[str, Any]:
        """Protocol used by structured CLI output to stay fail-closed."""

        return self.redacted_dict()


@dataclass(frozen=True, slots=True)
class WebhookSecretMigrationResult:
    """A verified staging result; it never retains the legacy plaintext separately."""

    replacement: WebhookConfig = field(repr=False)
    destination_copied: bool
    header_names_copied: tuple[str, ...]
    safe_to_scrub_legacy_values: bool

    def to_safe_primitive(self) -> dict[str, Any]:
        return {
            "webhook": self.replacement.name,
            "destination_copied": self.destination_copied,
            "header_names_copied": self.header_names_copied,
            "safe_to_scrub_legacy_values": self.safe_to_scrub_legacy_values,
            "replacement": self.replacement.redacted_dict(),
        }


class WebhookSecretMigration:
    """Copy legacy destinations/header values and verify before permitting a DB scrub.

    This service deliberately does not update SQLite.  A schema/data migration can persist the
    returned replacement only after this method reports ``safe_to_scrub_legacy_values``.
    """

    def __init__(self, secret_store: SecretStore) -> None:
        self._secret_store = secret_store

    @staticmethod
    def _entry_name(webhook_name: str, purpose: str) -> str:
        digest = hashlib.sha256(f"{webhook_name}\0{purpose}".encode()).hexdigest()[:24]
        return f"webhook-{digest}-{purpose.split(':', 1)[0]}"

    def stage(self, config: WebhookConfig) -> WebhookSecretMigrationResult:
        destination_copied = False
        destination = config.url
        if not config.destination_is_reference:
            reference = self._secret_store.put_and_verify(
                self._entry_name(config.name, "destination"), config.url
            )
            destination = str(reference)
            destination_copied = True

        headers = dict(config.headers)
        copied_headers: list[str] = []
        for name in config.plaintext_headers:
            reference = self._secret_store.put_and_verify(
                self._entry_name(config.name, f"header:{name.casefold()}"),
                headers[name],
            )
            headers[name] = str(reference)
            copied_headers.append(name)

        replacement = replace(
            config,
            url=destination,
            headers=headers,
            allow_legacy_plaintext_secrets=False,
        )
        safe = replacement.destination_is_reference and not replacement.legacy_secret_headers
        return WebhookSecretMigrationResult(
            replacement=replacement,
            destination_copied=destination_copied,
            header_names_copied=tuple(sorted(copied_headers)),
            safe_to_scrub_legacy_values=safe,
        )


@dataclass(frozen=True, slots=True)
class WebhookRegistryIssue:
    """Secret-safe metadata for one malformed persisted row."""

    row_id: int | None
    code: str = "malformed_webhook_row"
    detail: str = "A stored webhook definition is malformed and was not loaded."


@dataclass(frozen=True, slots=True)
class WebhookRegistryLoadResult:
    configurations: tuple[WebhookConfig, ...]
    issues: tuple[WebhookRegistryIssue, ...]

    def to_safe_primitive(self) -> dict[str, Any]:
        return {
            "configurations": tuple(item.redacted_dict() for item in self.configurations),
            "issues": tuple(
                {"row_id": issue.row_id, "code": issue.code, "detail": issue.detail}
                for issue in self.issues
            ),
        }


class WebhookRegistry:
    """Persist webhook metadata and secure references in the shared state database.

    Rows created by older builds may still contain a plaintext destination or sensitive header.
    Such rows remain readable solely so :class:`WebhookSecretMigration` can stage them safely.
    Normal representations and structured serialization are always redacted.
    """

    def __init__(
        self,
        state: StateStore,
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._state = state
        # Construction is side-effect free.  The operating-system keyring is touched only
        # when a caller adds or migrates secret-bearing material.
        self._secret_store = secret_store or SecretStore("webhooks")
        self._load_issues: tuple[WebhookRegistryIssue, ...] = ()

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> WebhookConfig:
        allowlist_raw = row["field_allowlist_json"]
        allowlist = frozenset(json.loads(allowlist_raw)) if allowlist_raw else None
        options = json.loads(str(row["delivery_options_json"]))
        if not isinstance(options, dict) or set(options) - set(_DELIVERY_OPTIONS):
            raise ConfigurationError("Stored webhook delivery options are malformed.")
        for name, value in options.items():
            expected = _DELIVERY_OPTIONS[name]
            # bool is a subclass of int; accept only literal JSON booleans for
            # safety decisions, and only JSON numbers for timeouts and limits.
            if expected is bool and type(value) is not bool:
                raise ConfigurationError("Stored webhook delivery options are malformed.")
            if expected is int and type(value) is not int:
                raise ConfigurationError("Stored webhook delivery options are malformed.")
            if expected is float and type(value) not in (int, float):
                raise ConfigurationError("Stored webhook delivery options are malformed.")
        candidate = WebhookConfig(
            webhook_id=int(row["id"]),
            name=str(row["name"]),
            url=str(row["url"]),
            method=str(row["method"]),
            headers=json.loads(str(row["headers_json"])),
            secret_keyring_name=(
                str(row["secret_keyring_name"]) if row["secret_keyring_name"] is not None else None
            ),
            field_allowlist=allowlist,
            enabled=bool(row["enabled"]),
            max_retries=int(row["max_retries"]),
            allow_legacy_plaintext_secrets=True,
            **options,
        )
        if not candidate.requires_secret_migration:
            return replace(candidate, allow_legacy_plaintext_secrets=False)
        return candidate

    @staticmethod
    def _migration_issue(row_id: int | None) -> WebhookRegistryIssue:
        return WebhookRegistryIssue(
            row_id=row_id,
            code="legacy_webhook_secrets_require_migration",
            detail=(
                "A legacy webhook contains plaintext secret material and is disabled until "
                "it is copied to secure storage."
            ),
        )

    @staticmethod
    def _row_id(row: Mapping[str, Any]) -> int | None:
        raw_id = row.get("id")
        return int(raw_id) if isinstance(raw_id, int) else None

    def _prepare_for_persistence(self, config: WebhookConfig) -> WebhookConfig:
        staged = WebhookSecretMigration(self._secret_store).stage(config)
        if not staged.safe_to_scrub_legacy_values:
            raise ConfigurationError(
                "Webhook secret material could not be moved to secure storage."
            )
        return staged.replacement

    def add(
        self,
        config: WebhookConfig,
        *,
        signing_secret: str | None = None,
    ) -> WebhookConfig:
        """Persist one definition and optionally its newly entered HMAC secret.

        Raw destinations, custom header values, and the optional signing secret cross
        the secure-store boundary while the case-insensitive name lock is held.  This
        prevents concurrent setup attempts from overwriting another definition's
        deterministic secret entries.
        """

        if config.webhook_id is not None:
            raise ConfigurationError("A new webhook may not already have a database ID.")
        if signing_secret is not None and len(signing_secret.encode("utf-8")) < 16:
            raise ConfigurationError("Webhook HMAC secrets must contain at least 16 bytes.")
        with self._state.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT 1 FROM webhooks WHERE name=? COLLATE NOCASE",
                (config.name,),
            ).fetchone()
            if existing is not None:
                raise ConfigurationError("A webhook with this name already exists.")
            if signing_secret is not None:
                signing_reference = self._secret_store.put_and_verify(
                    WebhookSecretMigration._entry_name(config.name, "hmac"),
                    signing_secret,
                )
                config = replace(
                    config,
                    secret=None,
                    secret_keyring_name=str(signing_reference),
                )
            # Keep the cross-process writer lock while copying deterministic keyring entries.
            # This prevents two concurrent adds with the same case-insensitive name from
            # overwriting one another's destination before the UNIQUE constraint is checked.
            persisted = self._prepare_for_persistence(config)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO webhooks(
                        name, url, method, headers_json, secret_keyring_name,
                        field_allowlist_json, enabled, max_retries, created_at,
                        delivery_options_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        persisted.name,
                        persisted.url,
                        persisted.method,
                        json.dumps(dict(persisted.headers), sort_keys=True),
                        persisted.secret_keyring_name,
                        (
                            json.dumps(sorted(persisted.field_allowlist))
                            if persisted.field_allowlist is not None
                            else None
                        ),
                        int(persisted.enabled),
                        persisted.max_retries,
                        iso(),
                        json.dumps(
                            {name: getattr(persisted, name) for name in _DELIVERY_OPTIONS},
                            sort_keys=True,
                        ),
                    ),
                )
            except Exception as exc:
                # sqlite error messages may include user-controlled values; keep the public
                # diagnostic stable and secret-free.
                raise ConfigurationError(
                    f"Could not add webhook ({type(exc).__name__}); its name may already exist."
                ) from exc
        if cursor.lastrowid is None:
            raise ConfigurationError("The webhook was stored without an identifier.")
        return replace(persisted, webhook_id=int(cursor.lastrowid))

    def list(self) -> tuple[WebhookConfig, ...]:
        return self.load().configurations

    @property
    def load_issues(self) -> tuple[WebhookRegistryIssue, ...]:
        return self._load_issues

    def load(self) -> WebhookRegistryLoadResult:
        """Load valid rows independently and report malformed rows without their values."""

        self._state.initialize()
        with self._state.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM webhooks ORDER BY name COLLATE NOCASE"
            ).fetchall()
        configurations: list[WebhookConfig] = []
        issues: list[WebhookRegistryIssue] = []
        for row in rows:
            values = dict(row)
            try:
                config = self._from_row(values)
                if config.requires_secret_migration:
                    issues.append(self._migration_issue(self._row_id(values)))
                    continue
                configurations.append(config)
            except Exception:
                issues.append(WebhookRegistryIssue(row_id=self._row_id(values)))
        self._load_issues = tuple(issues)
        return WebhookRegistryLoadResult(tuple(configurations), self._load_issues)

    def get(self, name: str) -> WebhookConfig | None:
        self._state.initialize()
        with self._state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM webhooks WHERE name=? COLLATE NOCASE",
                (name,),
            ).fetchone()
        if row is None:
            return None
        try:
            config = self._from_row(dict(row))
        except Exception as exc:
            raise ConfigurationError(
                f"The stored webhook definition is malformed ({type(exc).__name__}); "
                "no destination value was displayed."
            ) from exc
        if config.requires_secret_migration:
            raise ConfigurationError(
                "This legacy webhook is disabled until its secret material is migrated to "
                "secure storage; no destination value was displayed."
            )
        return config

    def migrate_legacy(self, name: str) -> WebhookSecretMigrationResult:
        """Copy one legacy row to the keyring, verify it, then scrub SQLite.

        The source row is updated only after every copied value has passed an exact keyring
        read-back.  Normal ``get`` and ``list`` calls never return a legacy plaintext row.
        """

        with self._state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM webhooks WHERE name=? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if row is None:
                raise ItemNotFoundError(f"Webhook not found: {name}")
            values = dict(row)
            try:
                legacy = self._from_row(values)
            except Exception as exc:
                raise ConfigurationError(
                    f"The stored webhook definition is malformed ({type(exc).__name__}); "
                    "no destination value was displayed."
                ) from exc

            # Keep the database writer lock through copy, verified read-back, and scrub so
            # another process cannot replace this row while deterministic secret entries are
            # being written.
            staged = WebhookSecretMigration(self._secret_store).stage(legacy)
            if not staged.safe_to_scrub_legacy_values:
                raise ConfigurationError(
                    "Webhook secret migration was not verified; the legacy row was left unchanged."
                )
            replacement = staged.replacement
            cursor = connection.execute(
                "UPDATE webhooks SET url=?, headers_json=? WHERE id=?",
                (
                    replacement.url,
                    json.dumps(dict(replacement.headers), sort_keys=True),
                    legacy.webhook_id,
                ),
            )
            if int(cursor.rowcount) != 1:  # pragma: no cover - selected under the same lock
                raise ConfigurationError(
                    "The webhook changed during secret migration; the legacy row was left "
                    "unchanged."
                )
        return staged

    def remove(self, name: str) -> bool:
        with self._state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM webhooks WHERE name=? COLLATE NOCASE",
                (name,),
            )
        return int(cursor.rowcount) > 0

    def set_enabled(self, name: str, *, enabled: bool) -> WebhookConfig:
        """Enable or disable a persisted definition without changing its destination."""

        with self._state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM webhooks WHERE name=? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if row is None:
                raise ItemNotFoundError(f"Webhook not found: {name}")
            try:
                current = self._from_row(dict(row))
            except Exception as exc:
                raise ConfigurationError(
                    f"The stored webhook definition is malformed ({type(exc).__name__}); "
                    "no destination value was displayed."
                ) from exc
            if current.requires_secret_migration:
                raise ConfigurationError(
                    "This legacy webhook cannot be changed until its secret material is "
                    "migrated to secure storage."
                )
            cursor = connection.execute(
                "UPDATE webhooks SET enabled=? WHERE name=? COLLATE NOCASE",
                (int(enabled), name),
            )
        if int(cursor.rowcount) < 1:
            raise ItemNotFoundError(f"Webhook not found: {name}")
        return replace(current, enabled=enabled)

    def ensure(self, config: WebhookConfig) -> WebhookConfig:
        if config.webhook_id is not None:
            return config
        current = self.get(config.name)
        if current is None:
            return self.add(config)
        if current.url != config.url or current.method != config.method:
            raise ConfigurationError(
                "A webhook with this name already exists with a different destination."
            )
        return replace(config, webhook_id=current.webhook_id)


class WebhookNotificationProvider:
    """Deliver signed, idempotent JSON over HTTPS with bounded retries."""

    def __init__(
        self,
        config: WebhookConfig,
        *,
        state: StateStore | None = None,
        client: httpx.Client | None = None,
        secret_resolver: SecretResolver | None = None,
        secret_store: SecretStore | None = None,
        sleep: Sleeper = time.sleep,
        clock: Clock | None = None,
        random_source: RandomSource = random.random,
    ) -> None:
        self._state = state
        resolved_secret_store = secret_store
        if state is not None and resolved_secret_store is None:
            resolved_secret_store = SecretStore("webhooks")
        self._config = (
            WebhookRegistry(state, secret_store=resolved_secret_store).ensure(config)
            if state is not None
            else config
        )
        self._client = client or httpx.Client(verify=True, follow_redirects=False)
        self._owns_client = client is None
        self._secret_resolver = secret_resolver or (
            resolved_secret_store.resolve if resolved_secret_store is not None else None
        )
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random = random_source

    @property
    def name(self) -> str:
        return f"webhook:{self._config.name}"

    @property
    def config(self) -> WebhookConfig:
        return self._config

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> WebhookNotificationProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _secret(self) -> bytes | None:
        if self._config.secret is not None:
            return self._config.secret
        name = self._config.secret_keyring_name
        if not name:
            return None
        if self._secret_resolver is None:
            raise ConfigurationError("Webhook signing requires a secure secret resolver.")
        resolver_name = (
            str(SecretReference.parse(name)) if name.startswith(_KEYRING_PREFIX) else name
        )
        resolved = self._secret_resolver(resolver_name)
        if resolved is None:
            raise ConfigurationError("The webhook signing secret is unavailable.")
        secret = resolved if isinstance(resolved, bytes) else resolved.encode("utf-8")
        if len(secret) < 16:
            raise ConfigurationError("The webhook signing secret is too short.")
        return secret

    def _headers(self, event: NotificationEvent, payload: bytes, timestamp: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in self._config.headers.items():
            if value.startswith(_KEYRING_PREFIX):
                if self._secret_resolver is None:
                    raise ConfigurationError(
                        f"Webhook header {name!r} requires a secure secret resolver."
                    )
                reference = SecretReference.parse(value)
                resolved = self._secret_resolver(str(reference))
                if resolved is None:
                    raise ConfigurationError(
                        f"Secure value for webhook header {name!r} is missing."
                    )
                headers[name] = (
                    resolved.decode("utf-8") if isinstance(resolved, bytes) else resolved
                )
            else:
                if name.casefold() in _SENSITIVE_HEADERS:
                    raise ConfigurationError(
                        f"Webhook header {name!r} requires migration to secure storage."
                    )
                headers[name] = value
        headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Idempotency-Key": self._safe_event_header(event.event_id),
                "X-GUCMail-Event-Id": self._safe_event_header(event.event_id),
                "X-GUCMail-Timestamp": timestamp,
                "X-GUCMail-Source": "local-watcher",
            }
        )
        secret = self._secret()
        if secret is not None:
            headers["X-GUCMail-Signature"] = hmac_signature(
                secret,
                payload,
                timestamp=timestamp,
            )
        return headers

    @staticmethod
    def _safe_event_header(event_id: str) -> str:
        if not event_id or any(character in event_id for character in "\r\n\x00"):
            raise ConfigurationError("Webhook event ID is not safe for an HTTP header.")
        return event_id[:512]

    def _destination(self) -> str:
        if not self._config.destination_is_reference:
            return self._config.url
        if self._secret_resolver is None:
            raise ConfigurationError("Webhook destination requires a secure secret resolver.")
        reference = SecretReference.parse(self._config.url)
        resolved = self._secret_resolver(str(reference))
        if resolved is None:
            raise ConfigurationError("The secure webhook destination is unavailable.")
        try:
            return resolved.decode("utf-8") if isinstance(resolved, bytes) else resolved
        except UnicodeDecodeError as exc:
            raise ConfigurationError("The secure webhook destination is invalid.") from exc

    def _next_attempt(self, event_id: str) -> int:
        if self._state is None or self._config.webhook_id is None:
            return 1
        self._state.initialize()
        with self._state.connection() as connection:
            row = connection.execute(
                "SELECT MAX(attempt) FROM webhook_deliveries WHERE webhook_id=? AND event_id=?",
                (self._config.webhook_id, event_id),
            ).fetchone()
        return int(row[0] or 0) + 1

    def _record(
        self,
        *,
        event_id: str,
        attempt: int,
        state: DeliveryState,
        status_code: int | None,
        error: str | None,
    ) -> None:
        if self._state is None or self._config.webhook_id is None:
            return
        safe_error = (
            redact_text(error, redact_emails=True, redact_urls=True)[:500] if error else None
        )
        with self._state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                    webhook_id, event_id, attempt, status_code, state, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(webhook_id, event_id, attempt) DO UPDATE SET
                    status_code=excluded.status_code,
                    state=excluded.state,
                    error=excluded.error,
                    created_at=excluded.created_at
                """,
                (
                    self._config.webhook_id,
                    event_id,
                    attempt,
                    status_code,
                    state.value,
                    safe_error,
                    iso(),
                ),
            )
            connection.execute(
                """
                DELETE FROM webhook_deliveries
                WHERE webhook_id=? AND id NOT IN (
                    SELECT id FROM webhook_deliveries WHERE webhook_id=?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (
                    self._config.webhook_id,
                    self._config.webhook_id,
                    self._config.history_limit,
                ),
            )

    def delivery_history(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        if self._state is None or self._config.webhook_id is None:
            return ()
        if not 1 <= limit <= self._config.history_limit:
            raise ValueError("history limit is outside the configured bound")
        self._state.initialize()
        with self._state.connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, attempt, status_code, state, error, created_at
                FROM webhook_deliveries WHERE webhook_id=? ORDER BY id DESC LIMIT ?
                """,
                (self._config.webhook_id, limit),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("error") is not None:
                item["error"] = redact_text(
                    item["error"],
                    redact_emails=True,
                    redact_urls=True,
                )[:500]
            history.append(item)
        return tuple(history)

    @staticmethod
    def _retry_after(response: httpx.Response, now: datetime) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw)
            # NaN and infinity are not valid Retry-After values.  Passing NaN
            # through to sleep raises an exception and can terminate a watcher.
            return max(seconds, 0.0) if math.isfinite(seconds) else None
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max((parsed - now).total_seconds(), 0.0)
            except (TypeError, ValueError, OverflowError):
                return None

    def _delay(self, retry_index: int, response: httpx.Response | None = None) -> float:
        server_delay = self._retry_after(response, self._clock()) if response is not None else None
        if server_delay is not None:
            return min(server_delay, self._config.retry_max_seconds)
        ceiling = min(
            self._config.retry_base_seconds * (2**retry_index),
            self._config.retry_max_seconds,
        )
        # Equal jitter prevents a synchronized retry wave while retaining a useful floor.
        jitter = max(0.0, min(float(self._random()), 1.0))
        return float(ceiling * (0.5 + (jitter * 0.5)))

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        if not self._config.enabled:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.DISABLED,
                "Webhook is disabled.",
            )
        if not self._config.allow_integration_events and event.event_type.casefold().startswith(
            ("webhook", "integration")
        ):
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                "Integration-generated events are blocked to prevent webhook loops.",
            )

        try:
            destination = self._destination()
            validate_webhook_url(
                destination,
                allow_private=self._config.allow_private_target,
                resolve_dns=self._owns_client,
            )
        except ConfigurationError:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                "Webhook destination is unavailable or failed network safety validation.",
                attempts=0,
            )

        # A real network transport resolves the host immediately before delivery to reject
        # private/loopback DNS answers.  An injected client owns its transport policy, which
        # also keeps MockTransport tests entirely offline.
        body = json.dumps(
            event_payload(
                event,
                field_allowlist=self._config.field_allowlist,
                redact=self._config.redact,
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(self._clock().timestamp()))
        try:
            headers = self._headers(event, body, timestamp)
        except (ConfigurationError, UnicodeDecodeError):
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                "Webhook signing or secret-header configuration is unavailable.",
                attempts=0,
            )
        timeout = httpx.Timeout(
            self._config.request_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        first_history_attempt = self._next_attempt(event.event_id)
        total_attempts = self._config.max_retries + 1
        last_status: int | None = None

        for index in range(total_attempts):
            history_attempt = first_history_attempt + index
            try:
                # Only the status and retry headers are needed.  A regular request
                # eagerly buffers the entire response body, allowing a faulty or
                # hostile endpoint to exhaust the watcher process's memory.
                with self._client.stream(
                    self._config.method,
                    destination,
                    content=body,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                ) as response:
                    pass
            except httpx.TransportError as exc:
                detail = f"Webhook transport failed ({type(exc).__name__})."
                self._record(
                    event_id=event.event_id,
                    attempt=history_attempt,
                    state=DeliveryState.FAILED,
                    status_code=None,
                    error=detail,
                )
                if index < self._config.max_retries:
                    self._sleep(self._delay(index))
                    continue
                return DeliveryResult(
                    event.event_id,
                    self.name,
                    DeliveryState.FAILED,
                    detail,
                    attempts=index + 1,
                )

            last_status = response.status_code
            if 200 <= response.status_code < 300:
                self._record(
                    event_id=event.event_id,
                    attempt=history_attempt,
                    state=DeliveryState.DELIVERED,
                    status_code=response.status_code,
                    error=None,
                )
                return DeliveryResult(
                    event.event_id,
                    self.name,
                    DeliveryState.DELIVERED,
                    attempts=index + 1,
                    status_code=response.status_code,
                )

            is_redirect = 300 <= response.status_code < 400
            detail = (
                "Webhook redirects are not followed."
                if is_redirect
                else f"Webhook returned HTTP {response.status_code}."
            )
            self._record(
                event_id=event.event_id,
                attempt=history_attempt,
                state=DeliveryState.FAILED,
                status_code=response.status_code,
                error=detail,
            )
            if response.status_code in _RETRYABLE_STATUS and index < self._config.max_retries:
                self._sleep(self._delay(index, response))
                continue
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                detail,
                attempts=index + 1,
                status_code=response.status_code,
            )

        # The loop always returns.  This guard keeps type-checkers and future edits honest.
        return DeliveryResult(
            event.event_id,
            self.name,
            DeliveryState.FAILED,
            "Webhook delivery ended without a response.",
            attempts=total_attempts,
            status_code=last_status,
        )


# A concise alias for callers that prefer the provider noun.
WebhookProvider = WebhookNotificationProvider


__all__ = [
    "WebhookConfig",
    "WebhookNotificationProvider",
    "WebhookProvider",
    "WebhookRegistry",
    "WebhookRegistryIssue",
    "WebhookRegistryLoadResult",
    "WebhookSecretMigration",
    "WebhookSecretMigrationResult",
]
