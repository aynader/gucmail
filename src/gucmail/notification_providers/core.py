"""Pure provider metadata, privacy, and event-payload primitives.

This package deliberately stops at prepared payloads.  Network transports and secret
storage belong to the application integration layer, which keeps every adapter in this
module deterministic and safe to exercise in the normal offline test suite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TypeAlias, cast

from gucmail.models import NotificationEvent
from gucmail.notification_events import PrivacyMode
from gucmail.security import redact_text, sanitize_terminal

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ProviderKind(StrEnum):
    WINDOWS = "windows"
    DISCORD = "discord"
    SLACK = "slack"
    TEAMS = "teams"
    TELEGRAM = "telegram"
    GENERIC_WEBHOOK = "generic_webhook"
    NDJSON = "ndjson"


class ProviderCapability(StrEnum):
    DESKTOP = "desktop"
    CLICK_TO_OPEN = "click_to_open"
    GROUPING = "grouping"
    SOUND = "sound"
    EMBEDS = "embeds"
    PLAIN_TEXT = "plain_text"
    FIELD_ALLOWLIST = "field_allowlist"
    HMAC = "hmac"
    IDEMPOTENCY = "idempotency"
    RETRIES = "retries"
    QUIET_HOURS = "quiet_hours"
    DIGESTS = "digests"
    RULE_ROUTING = "rule_routing"


class SettingValueKind(StrEnum):
    BOOLEAN = "boolean"
    CHOICE = "choice"
    INTEGER = "integer"
    CREDENTIAL = "secret"
    TEXT = "text"
    URL = "url"


@dataclass(frozen=True, slots=True)
class ProviderSetting:
    setting_id: str
    label: str
    value_kind: SettingValueKind
    description: str
    default: JsonScalar = None
    choices: tuple[str, ...] = ()
    secret: bool = False

    def __post_init__(self) -> None:
        if not self.setting_id or not self.label or not self.description:
            raise ValueError("Provider setting metadata may not be blank.")
        if self.value_kind is SettingValueKind.CHOICE and not self.choices:
            raise ValueError(f"Choice setting {self.setting_id!r} requires choices.")
        if self.secret and self.value_kind not in {
            SettingValueKind.CREDENTIAL,
            SettingValueKind.URL,
        }:
            raise ValueError("Only secret or URL settings may be marked secret.")


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider_id: ProviderKind
    display_name: str
    description: str
    capabilities: frozenset[ProviderCapability]
    settings: tuple[ProviderSetting, ...]
    optional: bool = True

    def __post_init__(self) -> None:
        ids = [item.setting_id for item in self.settings]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate settings in provider {self.provider_id.value!r}.")


@dataclass(frozen=True, slots=True)
class ProviderTemplate:
    template_id: str
    provider_id: ProviderKind
    name: str
    description: str
    default_privacy: PrivacyMode
    secret_settings: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.template_id or not self.name or not self.description:
            raise ValueError("Provider template metadata may not be blank.")


_PRIVACY_FIELDS: Final[dict[PrivacyMode, frozenset[str]]] = {
    PrivacyMode.FULL: frozenset(
        {
            "event",
            "event_id",
            "received_at",
            "account_id",
            "folder",
            "message_reference",
            "sender",
            "subject",
            "importance",
            "has_attachments",
            "preview",
            "open_hint",
        }
    ),
    PrivacyMode.SENDER_AND_SUBJECT: frozenset(
        {
            "event",
            "event_id",
            "received_at",
            "folder",
            "sender",
            "subject",
            "importance",
            "has_attachments",
            "message_reference",
        }
    ),
    PrivacyMode.SENDER_ONLY: frozenset(
        {"event", "event_id", "received_at", "sender", "importance", "message_reference"}
    ),
    PrivacyMode.PRIVATE: frozenset({"event", "event_id", "received_at"}),
    PrivacyMode.METADATA_ONLY: frozenset(
        {
            "event",
            "event_id",
            "received_at",
            "folder",
            "importance",
            "has_attachments",
        }
    ),
}

EVENT_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset().union(*_PRIVACY_FIELDS.values())


def privacy_fields(mode: PrivacyMode) -> frozenset[str]:
    """The event fields a notification at this privacy level may carry."""
    return _PRIVACY_FIELDS[mode]


@dataclass(frozen=True, slots=True)
class PayloadPolicy:
    """One provider's local disclosure and size policy.

    ``field_allowlist`` can only reduce the selected privacy preset.  This fail-closed
    behavior prevents a future schema field from leaving the machine accidentally.
    """

    privacy: PrivacyMode = PrivacyMode.SENDER_AND_SUBJECT
    field_allowlist: frozenset[str] | None = None
    redact_email_addresses: bool = True
    expose_external_addresses: bool = False
    max_text_characters: int = 1_024
    max_payload_bytes: int = 16_384

    def __post_init__(self) -> None:
        if self.field_allowlist is not None:
            unknown = set(self.field_allowlist) - EVENT_PAYLOAD_FIELDS
            if unknown:
                raise ValueError(f"Unknown payload field(s): {', '.join(sorted(unknown))}")
            object.__setattr__(self, "field_allowlist", frozenset(self.field_allowlist))
        if not 32 <= self.max_text_characters <= 100_000:
            raise ValueError("max_text_characters must be between 32 and 100000.")
        if not 128 <= self.max_payload_bytes <= 1_000_000:
            raise ValueError("max_payload_bytes must be between 128 and 1000000.")

    @property
    def allowed_fields(self) -> frozenset[str]:
        preset = _PRIVACY_FIELDS[self.privacy]
        return preset if self.field_allowlist is None else preset & self.field_allowlist


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Transport-neutral HTTP request material; creating it performs no I/O."""

    provider: ProviderKind
    method: str
    url: str = field(repr=False)
    # Both collections may contain secrets or personal mailbox content.  They stay
    # available to an explicit transport, but must never spill through an innocent
    # dataclass ``repr`` in an exception, activity row, or debug log.
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    verify_tls: bool = True
    follow_redirects: bool = False
    timeout_seconds: float = 15.0
    secret_url: bool = True

    def json(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.body))

    def redacted_summary(self) -> dict[str, Any]:
        """Return request metadata safe for histories, diagnostics, and logs."""

        from urllib.parse import urlparse

        try:
            parsed = urlparse(self.url)
            host = parsed.hostname or "<invalid>"
            port = f":{parsed.port}" if parsed.port else ""
            destination = f"{parsed.scheme}://{host}{port}/<redacted>"
        except ValueError:
            destination = "<invalid URL>"
        return {
            "provider": self.provider.value,
            "method": self.method,
            "destination": destination,
            "header_names": tuple(sorted(self.headers)),
            "body_bytes": len(self.body),
            "verify_tls": self.verify_tls,
            "follow_redirects": self.follow_redirects,
            "timeout_seconds": self.timeout_seconds,
        }

    def to_safe_primitive(self) -> dict[str, Any]:
        """Prevent generic dataclass serializers from traversing request secrets."""

        return self.redacted_summary()


def _safe_text(value: object, policy: PayloadPolicy, *, limit: int | None = None) -> str:
    maximum = min(limit or policy.max_text_characters, policy.max_text_characters)
    safe = sanitize_terminal(value, max_length=maximum)
    # Secret-looking values are always removed.  Address removal follows provider policy.
    return redact_text(safe, redact_emails=policy.redact_email_addresses)


def _sender_payload(event: NotificationEvent, policy: PayloadPolicy) -> dict[str, JsonValue] | None:
    if event.sender is None:
        return None
    name = _safe_text(event.sender.name or "", policy, limit=300) or None
    address = event.sender.email
    account_domain = event.account_id.rsplit("@", 1)[-1].casefold()
    sender_domain = address.rsplit("@", 1)[-1].casefold()
    is_external = "@" in address and sender_domain != account_domain
    if is_external and not policy.expose_external_addresses:
        safe_address: str | None = None
    else:
        safe_address = _safe_text(address, policy, limit=320) or None
    result: dict[str, JsonValue] = {"name": name, "address": safe_address}
    return result


def stable_idempotency_key(event: NotificationEvent, provider: str | None = None) -> str:
    """Return a privacy-safe stable key without exposing an EWS or account identifier."""

    material = "\x00".join(
        (event.account_id.casefold(), event.event_id, provider.casefold() if provider else "")
    ).encode("utf-8", errors="replace")
    return f"gucmail-v1-{hashlib.sha256(material).hexdigest()}"


def event_payload(event: NotificationEvent, policy: PayloadPolicy) -> dict[str, JsonValue]:
    """Adapt the existing domain event to the stable outbound schema."""

    values: dict[str, JsonValue] = {
        "event": _safe_text(event.event_type, policy, limit=100),
        "event_id": _safe_text(event.event_id, policy, limit=512),
        "received_at": event.occurred_at.isoformat(),
        "account_id": _safe_text(event.account_id, policy, limit=320),
        "folder": _safe_text(event.folder, policy, limit=512) if event.folder else None,
        "message_reference": (
            _safe_text(event.message_reference, policy, limit=512)
            if event.message_reference
            else None
        ),
        "sender": _sender_payload(event, policy),
        "subject": _safe_text(event.subject, policy, limit=998) if event.subject else None,
        "importance": event.importance.value,
        "has_attachments": bool(event.has_attachments),
        "preview": _safe_text(event.preview, policy, limit=2_000) if event.preview else None,
        "open_hint": _safe_text(event.open_hint, policy, limit=512) if event.open_hint else None,
    }
    return {key: values[key] for key in values if key in policy.allowed_fields}


def canonical_json(payload: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def truncate_utf8(value: str, maximum_bytes: int, *, marker: str = "...") -> str:
    """Truncate on Unicode character boundaries to an exact UTF-8 byte budget."""

    if maximum_bytes < 0:
        raise ValueError("maximum_bytes may not be negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) > maximum_bytes:
        return marker_bytes[:maximum_bytes].decode("utf-8", errors="ignore")
    remaining = maximum_bytes - len(marker_bytes)
    prefix = encoded[:remaining].decode("utf-8", errors="ignore")
    return prefix + marker


def _string_paths(
    value: JsonValue, path: tuple[str | int, ...] = ()
) -> list[tuple[int, tuple[str | int, ...]]]:
    paths: list[tuple[int, tuple[str | int, ...]]] = []
    if isinstance(value, str):
        paths.append((len(value.encode("utf-8")), path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_string_paths(item, (*path, index)))
    elif isinstance(value, dict):
        for key in sorted(value):
            paths.extend(_string_paths(value[key], (*path, key)))
    return paths


def _replace_path(root: JsonValue, path: Sequence[str | int], value: JsonValue) -> None:
    current = root
    for component in path[:-1]:
        if isinstance(current, list):
            if not isinstance(component, int):
                raise TypeError("Invalid JSON path")
            current = current[component]
        elif isinstance(current, dict):
            if not isinstance(component, str):
                raise TypeError("Invalid JSON path")
            current = current[component]
        else:  # pragma: no cover - internal invariant
            raise TypeError("Invalid JSON path")
    final = path[-1]
    if isinstance(current, list):
        if not isinstance(final, int):
            raise TypeError("Invalid JSON path")
        current[final] = value
    elif isinstance(current, dict):
        if not isinstance(final, str):
            raise TypeError("Invalid JSON path")
        current[final] = value
    else:  # pragma: no cover - internal invariant
        raise TypeError("Invalid JSON path")


def bounded_json(
    payload: Mapping[str, JsonValue],
    maximum_bytes: int,
    *,
    protected_keys: frozenset[str] = frozenset({"event", "event_id", "received_at"}),
) -> bytes:
    """Serialize JSON under a byte ceiling by truncating the largest disclosure fields.

    Structural and idempotency fields remain intact.  If those fields alone cannot fit,
    callers receive a clear error instead of a silently malformed payload.
    """

    if maximum_bytes < 128:
        raise ValueError("maximum_bytes must be at least 128")
    mutable = cast(JsonValue, deepcopy(dict(payload)))
    while len(rendered := canonical_json(cast(dict[str, JsonValue], mutable))) > maximum_bytes:
        candidates = [
            (size, path)
            for size, path in _string_paths(mutable)
            if path and str(path[-1]) not in protected_keys and size > 3
        ]
        if not candidates:
            raise ValueError("Payload cannot fit without truncating protected fields.")
        size, path = max(candidates, key=lambda item: (item[0], tuple(map(str, item[1]))))
        reduction = max(len(rendered) - maximum_bytes, max(1, size // 3))
        current: JsonValue = mutable
        for component in path:
            if isinstance(current, list):
                if not isinstance(component, int):  # pragma: no cover
                    raise TypeError("Invalid JSON path")
                current = current[component]
            elif isinstance(current, dict):
                if not isinstance(component, str):  # pragma: no cover
                    raise TypeError("Invalid JSON path")
                current = current[component]
            else:  # pragma: no cover - internal invariant
                current = None
        if not isinstance(current, str):  # pragma: no cover - internal invariant
            raise TypeError("Expected string leaf")
        _replace_path(mutable, path, truncate_utf8(current, max(3, size - reduction)))
    return rendered


def _setting(
    setting_id: str,
    label: str,
    kind: SettingValueKind,
    description: str,
    *,
    default: JsonScalar = None,
    choices: tuple[str, ...] = (),
    secret: bool = False,
) -> ProviderSetting:
    return ProviderSetting(setting_id, label, kind, description, default, choices, secret)


_COMMON_PRIVACY = _setting(
    "privacy",
    "Privacy",
    SettingValueKind.CHOICE,
    "Controls which mailbox fields are disclosed to this provider.",
    default=PrivacyMode.SENDER_AND_SUBJECT.value,
    choices=tuple(item.value for item in PrivacyMode),
)
_COMMON_ENABLED = _setting(
    "enabled", "Enabled", SettingValueKind.BOOLEAN, "Enables this provider.", default=False
)


PROVIDER_CATALOG: Final[tuple[ProviderMetadata, ...]] = (
    ProviderMetadata(
        ProviderKind.WINDOWS,
        "Windows desktop",
        "Native or native-looking local toast notifications.",
        frozenset(
            {
                ProviderCapability.DESKTOP,
                ProviderCapability.GROUPING,
                ProviderCapability.QUIET_HOURS,
                ProviderCapability.DIGESTS,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting(
                "sound",
                "Sound",
                SettingValueKind.BOOLEAN,
                "Allow toast sound. The configured native Windows backend supports silence; "
                "Plyer skips silent delivery and keeps the event in Activity history.",
                default=True,
            ),
            _setting(
                "max_burst",
                "Maximum per burst",
                SettingValueKind.INTEGER,
                "Caps individual toasts before grouping the remainder.",
                default=5,
            ),
        ),
        optional=False,
    ),
    ProviderMetadata(
        ProviderKind.DISCORD,
        "Discord",
        "Discord incoming webhook with mention-safe embeds or plain text.",
        frozenset(
            {
                ProviderCapability.EMBEDS,
                ProviderCapability.PLAIN_TEXT,
                ProviderCapability.FIELD_ALLOWLIST,
                ProviderCapability.IDEMPOTENCY,
                ProviderCapability.RETRIES,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting(
                "webhook_url",
                "Webhook URL",
                SettingValueKind.URL,
                "Secret Discord webhook destination.",
                secret=True,
            ),
            _setting(
                "embeds",
                "Use embeds",
                SettingValueKind.BOOLEAN,
                "Use a compact embed.",
                default=True,
            ),
        ),
    ),
    ProviderMetadata(
        ProviderKind.SLACK,
        "Slack-compatible",
        "Incoming webhook using conservative Slack-compatible blocks.",
        frozenset(
            {
                ProviderCapability.PLAIN_TEXT,
                ProviderCapability.FIELD_ALLOWLIST,
                ProviderCapability.IDEMPOTENCY,
                ProviderCapability.RETRIES,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting("url", "URL", SettingValueKind.URL, "Incoming webhook URL.", secret=True),
        ),
    ),
    ProviderMetadata(
        ProviderKind.TEAMS,
        "Microsoft Teams-compatible",
        "Isolated Adaptive Card payload for Teams workflows and webhooks.",
        frozenset(
            {
                ProviderCapability.PLAIN_TEXT,
                ProviderCapability.FIELD_ALLOWLIST,
                ProviderCapability.IDEMPOTENCY,
                ProviderCapability.RETRIES,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting(
                "url", "URL", SettingValueKind.URL, "Teams workflow or webhook URL.", secret=True
            ),
        ),
    ),
    ProviderMetadata(
        ProviderKind.TELEGRAM,
        "Telegram",
        "Optional Telegram Bot API notification with plain-text formatting.",
        frozenset(
            {
                ProviderCapability.PLAIN_TEXT,
                ProviderCapability.FIELD_ALLOWLIST,
                ProviderCapability.IDEMPOTENCY,
                ProviderCapability.RETRIES,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting(
                "bot_token",
                "Bot token",
                SettingValueKind.CREDENTIAL,
                "Telegram bot credential.",
                secret=True,
            ),
            _setting("chat_id", "Chat ID", SettingValueKind.TEXT, "Destination chat identifier."),
        ),
    ),
    ProviderMetadata(
        ProviderKind.GENERIC_WEBHOOK,
        "Generic webhook",
        "Signed, idempotent, field-allowlisted JSON webhook.",
        frozenset(
            {
                ProviderCapability.FIELD_ALLOWLIST,
                ProviderCapability.HMAC,
                ProviderCapability.IDEMPOTENCY,
                ProviderCapability.RETRIES,
                ProviderCapability.RULE_ROUTING,
            }
        ),
        (
            _COMMON_ENABLED,
            _COMMON_PRIVACY,
            _setting("url", "URL", SettingValueKind.URL, "Outbound webhook URL.", secret=True),
            _setting(
                "hmac_secret",
                "HMAC secret",
                SettingValueKind.CREDENTIAL,
                "Optional signing secret.",
                secret=True,
            ),
        ),
        optional=False,
    ),
    ProviderMetadata(
        ProviderKind.NDJSON,
        "NDJSON event stream",
        "One bounded, sanitized JSON event per output line.",
        frozenset({ProviderCapability.FIELD_ALLOWLIST, ProviderCapability.IDEMPOTENCY}),
        (_COMMON_ENABLED, _COMMON_PRIVACY),
        optional=False,
    ),
)

PROVIDER_TEMPLATES: Final[tuple[ProviderTemplate, ...]] = (
    ProviderTemplate(
        "windows_toast",
        ProviderKind.WINDOWS,
        "Windows toast",
        "Local grouped toast with click-to-open activation.",
        PrivacyMode.SENDER_AND_SUBJECT,
    ),
    ProviderTemplate(
        "discord_embed",
        ProviderKind.DISCORD,
        "Discord embed",
        "Mention-safe Discord embed for an incoming webhook.",
        PrivacyMode.SENDER_AND_SUBJECT,
        frozenset({"webhook_url"}),
    ),
    ProviderTemplate(
        "discord_private",
        ProviderKind.DISCORD,
        "Private Discord alert",
        "Discord alert which discloses no sender or message content.",
        PrivacyMode.PRIVATE,
        frozenset({"webhook_url"}),
    ),
    ProviderTemplate(
        "slack_incoming",
        ProviderKind.SLACK,
        "Slack-compatible incoming webhook",
        "Conservative plain-text Slack-compatible blocks.",
        PrivacyMode.SENDER_AND_SUBJECT,
        frozenset({"url"}),
    ),
    ProviderTemplate(
        "teams_adaptive_card",
        ProviderKind.TEAMS,
        "Teams Adaptive Card",
        "Isolated Teams workflow/webhook Adaptive Card payload.",
        PrivacyMode.SENDER_AND_SUBJECT,
        frozenset({"url"}),
    ),
    ProviderTemplate(
        "telegram_bot",
        ProviderKind.TELEGRAM,
        "Telegram bot",
        "Plain-text Telegram Bot API notification.",
        PrivacyMode.PRIVATE,
        frozenset({"bot_token"}),
    ),
    ProviderTemplate(
        "generic_signed_json",
        ProviderKind.GENERIC_WEBHOOK,
        "Signed generic JSON",
        "Field-allowlisted JSON with idempotency and optional HMAC.",
        PrivacyMode.METADATA_ONLY,
        frozenset({"url", "hmac_secret"}),
    ),
    ProviderTemplate(
        "ndjson_stream",
        ProviderKind.NDJSON,
        "NDJSON event stream",
        "Bounded sanitized event line for pipes and local automation.",
        PrivacyMode.METADATA_ONLY,
    ),
)


def provider_metadata(provider: ProviderKind | str) -> ProviderMetadata:
    wanted = ProviderKind(provider)
    return next(item for item in PROVIDER_CATALOG if item.provider_id is wanted)


def provider_templates(provider: ProviderKind | str | None = None) -> tuple[ProviderTemplate, ...]:
    if provider is None:
        return PROVIDER_TEMPLATES
    wanted = ProviderKind(provider)
    return tuple(item for item in PROVIDER_TEMPLATES if item.provider_id is wanted)


__all__ = [
    "EVENT_PAYLOAD_FIELDS",
    "PROVIDER_CATALOG",
    "PROVIDER_TEMPLATES",
    "JsonValue",
    "PayloadPolicy",
    "PreparedRequest",
    "PrivacyMode",
    "ProviderCapability",
    "ProviderKind",
    "ProviderMetadata",
    "ProviderSetting",
    "ProviderTemplate",
    "SettingValueKind",
    "bounded_json",
    "canonical_json",
    "event_payload",
    "privacy_fields",
    "provider_metadata",
    "provider_templates",
    "stable_idempotency_key",
    "truncate_utf8",
]
