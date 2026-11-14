"""Provider-specific webhook and event-stream payload adapters.

Every builder in this module is pure: it returns request material but never opens a
socket, resolves DNS, sleeps, or reads a secret store.  Secret values are accepted only
as runtime inputs and are hidden from dataclass representations.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from gucmail.models import NotificationEvent
from gucmail.notification_providers.core import (
    JsonValue,
    PayloadPolicy,
    PreparedRequest,
    PrivacyMode,
    ProviderKind,
    bounded_json,
    event_payload,
    stable_idempotency_key,
    truncate_utf8,
)
from gucmail.security import hmac_signature, sanitize_terminal

_DISCORD_HOSTS: Final[frozenset[str]] = frozenset(
    {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
)
_DISCORD_PATH = re.compile(
    r"^/api(?:/v\d+)?/webhooks/(?P<id>\d{5,30})/(?P<token>[A-Za-z0-9._-]{20,200})/?$"
)
_TELEGRAM_TOKEN = re.compile(r"^[1-9]\d{4,15}:[A-Za-z0-9_-]{20,100}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BLOCKED_HEADERS = frozenset({"connection", "content-length", "host", "transfer-encoding"})
_REDACTED_BULLETS = "\u2022" * 12


class DestinationError(ValueError):
    """A webhook destination failed local validation."""


@dataclass(frozen=True, slots=True)
class DestinationValidation:
    url: str = field(repr=False)
    secure_transport: bool
    local_network: bool
    warnings: tuple[str, ...] = ()

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "destination_configured": bool(self.url),
            "secure_transport": self.secure_transport,
            "local_network": self.local_network,
            "warnings": self.warnings,
        }


@dataclass(frozen=True, slots=True)
class RedirectPolicy:
    maximum_redirects: int = 0
    allow_https_downgrade: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.maximum_redirects <= 5:
            raise ValueError("maximum_redirects must be between 0 and 5.")


def _literal_is_local(host: str) -> bool:
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return host.casefold().endswith((".local", ".internal", ".localhost"))


def validate_destination(
    url: str,
    *,
    allow_http: bool = False,
    allow_private: bool = False,
) -> DestinationValidation:
    """Validate a destination without DNS or network access.

    Private/self-hosted endpoints remain possible, but only behind an explicit trust
    decision.  DNS rebinding checks still belong immediately before live delivery.
    """

    if len(url) > 4_096 or any(character in url for character in "\r\n\x00"):
        raise DestinationError("Webhook URL is empty, too long, or contains control data.")
    parsed = urlparse(url)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise DestinationError("Webhook URLs must use HTTP or HTTPS.")
    if scheme == "http" and not allow_http:
        raise DestinationError("Plain HTTP requires an explicit insecure-transport opt-in.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise DestinationError("Webhook URL needs a host and may not contain credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DestinationError("Webhook URL contains an invalid port.") from exc
    if port is not None and not 1 <= port <= 65_535:  # pragma: no cover - urlparse guards
        raise DestinationError("Webhook URL contains an invalid port.")
    local = _literal_is_local(parsed.hostname)
    if local and not allow_private:
        raise DestinationError("Local and private webhook targets require explicit trust.")
    warnings: list[str] = []
    if scheme == "http":
        warnings.append("Traffic to this endpoint is not encrypted.")
    if local:
        warnings.append("This endpoint is on a trusted local/private network.")
    return DestinationValidation(url, scheme == "https", local, tuple(warnings))


def validate_redirect(
    origin_url: str,
    location: str,
    *,
    redirect_count: int,
    policy: RedirectPolicy | None = None,
    allow_private: bool = False,
) -> DestinationValidation:
    """Resolve and validate one redirect, including HTTPS-to-HTTP downgrade defense."""

    selected_policy = policy or RedirectPolicy()
    if redirect_count >= selected_policy.maximum_redirects:
        raise DestinationError("Webhook redirect limit reached.")
    target = urljoin(origin_url, location)
    origin = urlparse(origin_url)
    parsed = urlparse(target)
    downgraded = origin.scheme.casefold() == "https" and parsed.scheme.casefold() == "http"
    if downgraded and not selected_policy.allow_https_downgrade:
        raise DestinationError("HTTPS-to-HTTP webhook redirects are blocked.")
    return validate_destination(
        target,
        allow_http=selected_policy.allow_https_downgrade,
        allow_private=allow_private,
    )


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name, value = str(raw_name), str(raw_value)
        if not _HEADER_NAME.fullmatch(name) or name.casefold() in _BLOCKED_HEADERS:
            raise ValueError(f"Invalid or transport-controlled header: {name!r}")
        if any(character in value for character in "\r\n\x00"):
            raise ValueError(f"Header {name!r} contains control data.")
        validated[name] = value
    return validated


def sign_payload(secret: bytes, payload: bytes, timestamp: str) -> str:
    if len(secret) < 16:
        raise ValueError("HMAC secrets must contain at least 16 bytes.")
    if not timestamp or any(character in timestamp for character in "\r\n\x00"):
        raise ValueError("HMAC timestamp is invalid.")
    return hmac_signature(secret, payload, timestamp=timestamp)


def verify_payload_signature(
    secret: bytes,
    payload: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    try:
        expected = sign_payload(secret, payload, timestamp)
    except ValueError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", errors="ignore"))


def _request_headers(
    event: NotificationEvent,
    provider: ProviderKind,
    *,
    body: bytes,
    timestamp: str,
    secret: bytes | None = None,
    custom_headers: Mapping[str, str] = {},
) -> dict[str, str]:
    result = _headers(custom_headers)
    result.update(
        {
            "Content-Type": "application/json",
            "Idempotency-Key": stable_idempotency_key(event, provider.value),
            "X-GUCMail-Event": sanitize_terminal(event.event_type, max_length=100),
            "X-GUCMail-Event-ID": stable_idempotency_key(event),
            "X-GUCMail-Timestamp": timestamp,
        }
    )
    if secret is not None:
        result["X-GUCMail-Signature"] = sign_payload(secret, body, timestamp)
    return result


def _timestamp(event: NotificationEvent, supplied: datetime | None) -> str:
    moment = supplied or event.occurred_at
    if moment.tzinfo is None:
        raise ValueError("Webhook timestamps must be timezone-aware.")
    return str(int(moment.timestamp()))


def _visible_sender(payload: Mapping[str, JsonValue]) -> str | None:
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    name, address = sender.get("name"), sender.get("address")
    return str(name or address) if name or address else None


def _plain_lines(payload: Mapping[str, JsonValue], *, private_label: str) -> list[str]:
    lines = [private_label]
    sender = _visible_sender(payload)
    if sender:
        lines.append(f"From: {sender}")
    if subject := payload.get("subject"):
        lines.append(f"Subject: {subject}")
    if folder := payload.get("folder"):
        lines.append(f"Folder: {folder}")
    if payload.get("importance") == "high":
        lines.append("Importance: High")
    if payload.get("has_attachments"):
        lines.append("Attachments: Yes")
    if preview := payload.get("preview"):
        lines.extend(("", str(preview)))
    return lines


def neutralize_mentions(value: object) -> str:
    """Prevent email-controlled text from producing chat-platform mentions."""

    return sanitize_terminal(value).replace("@", "@\u200b")


def validate_discord_webhook_url(url: str) -> str:
    validation = validate_destination(url)
    parsed = urlparse(validation.url)
    if parsed.hostname is None or parsed.hostname.casefold() not in _DISCORD_HOSTS:
        raise DestinationError("Discord webhook must use an official Discord hostname.")
    if parsed.query or parsed.fragment or not _DISCORD_PATH.fullmatch(parsed.path):
        raise DestinationError("Discord webhook URL has an invalid webhook path.")
    return url


def mask_discord_webhook_url(url: str) -> str:
    """Return a display-safe identifier that never contains the webhook token."""

    try:
        validate_discord_webhook_url(url)
        parsed = urlparse(url)
        match = _DISCORD_PATH.fullmatch(parsed.path)
        assert match is not None
        return f"https://{parsed.hostname}/api/webhooks/{_REDACTED_BULLETS}/{_REDACTED_BULLETS}"
    except (DestinationError, AssertionError):
        return "<invalid Discord webhook>"


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    url: str = field(repr=False)
    policy: PayloadPolicy = field(
        default_factory=lambda: PayloadPolicy(privacy=PrivacyMode.SENDER_AND_SUBJECT)
    )
    embeds: bool = True
    username: str = "GUC Mail"
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        validate_discord_webhook_url(self.url)
        if not 1 <= len(self.username) <= 80:
            raise ValueError("Discord username must contain between 1 and 80 characters.")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Discord timeout must be between 0 and 120 seconds.")

    @property
    def masked_url(self) -> str:
        return mask_discord_webhook_url(self.url)

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "provider": ProviderKind.DISCORD.value,
            "destination_configured": bool(self.url),
            "policy": self.policy,
            "embeds": self.embeds,
            "username": self.username,
            "timeout_seconds": self.timeout_seconds,
        }


class DiscordAdapter:
    provider = ProviderKind.DISCORD

    def __init__(self, config: DiscordConfig) -> None:
        self.config = config

    def prepare(
        self,
        event: NotificationEvent,
        *,
        delivery_time: datetime | None = None,
    ) -> PreparedRequest:
        disclosed = event_payload(event, self.config.policy)
        lines = [
            neutralize_mentions(item)
            for item in _plain_lines(disclosed, private_label="New GUC email")
        ]
        if self.config.embeds:
            fields: list[JsonValue] = []
            for label, key in (
                ("Sender", "sender"),
                ("Subject", "subject"),
                ("Folder", "folder"),
                ("Importance", "importance"),
            ):
                value: object = (
                    _visible_sender(disclosed) if key == "sender" else disclosed.get(key)
                )
                if value:
                    fields.append(
                        {
                            "name": label,
                            "value": truncate_utf8(neutralize_mentions(value), 1_024),
                            "inline": label != "Subject",
                        }
                    )
            if disclosed.get("has_attachments"):
                fields.append({"name": "Attachments", "value": "Present", "inline": True})
            embed: dict[str, JsonValue] = {
                "title": "New GUC email",
                "description": (
                    truncate_utf8(neutralize_mentions(disclosed["preview"]), 2_000)
                    if disclosed.get("preview")
                    else None
                ),
                "color": 0xDD0000,
                "timestamp": event.occurred_at.isoformat(),
                "fields": fields,
            }
            payload: dict[str, JsonValue] = {
                "username": neutralize_mentions(self.config.username),
                "embeds": [embed],
                "allowed_mentions": {
                    "parse": [],
                    "users": [],
                    "roles": [],
                    "replied_user": False,
                },
            }
        else:
            payload = {
                "username": neutralize_mentions(self.config.username),
                "content": truncate_utf8("\n".join(lines), 2_000),
                "allowed_mentions": {
                    "parse": [],
                    "users": [],
                    "roles": [],
                    "replied_user": False,
                },
            }
        body = bounded_json(
            payload, min(self.config.policy.max_payload_bytes, 32_000), protected_keys=frozenset()
        )
        timestamp = _timestamp(event, delivery_time)
        return PreparedRequest(
            self.provider,
            "POST",
            self.config.url,
            _request_headers(event, self.provider, body=body, timestamp=timestamp),
            body,
            timeout_seconds=self.config.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class CompatibleWebhookConfig:
    url: str = field(repr=False)
    policy: PayloadPolicy = field(default_factory=PayloadPolicy)
    timeout_seconds: float = 15.0
    allow_private: bool = False

    def __post_init__(self) -> None:
        validate_destination(self.url, allow_private=self.allow_private)
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Webhook timeout must be between 0 and 120 seconds.")

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "destination_configured": bool(self.url),
            "policy": self.policy,
            "timeout_seconds": self.timeout_seconds,
            "allow_private": self.allow_private,
        }


class SlackAdapter:
    provider = ProviderKind.SLACK

    def __init__(self, config: CompatibleWebhookConfig) -> None:
        self.config = config

    def prepare(
        self, event: NotificationEvent, *, delivery_time: datetime | None = None
    ) -> PreparedRequest:
        disclosed = event_payload(event, self.config.policy)
        text = truncate_utf8(
            "\n".join(
                neutralize_mentions(item)
                for item in _plain_lines(disclosed, private_label="New GUC email")
            ),
            3_000,
        )
        payload: dict[str, JsonValue] = {
            "text": text,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": text, "emoji": True},
                }
            ],
        }
        body = bounded_json(
            payload, min(self.config.policy.max_payload_bytes, 32_000), protected_keys=frozenset()
        )
        timestamp = _timestamp(event, delivery_time)
        return PreparedRequest(
            self.provider,
            "POST",
            self.config.url,
            _request_headers(event, self.provider, body=body, timestamp=timestamp),
            body,
            timeout_seconds=self.config.timeout_seconds,
        )


class TeamsAdapter:
    provider = ProviderKind.TEAMS

    def __init__(self, config: CompatibleWebhookConfig) -> None:
        self.config = config

    def prepare(
        self, event: NotificationEvent, *, delivery_time: datetime | None = None
    ) -> PreparedRequest:
        disclosed = event_payload(event, self.config.policy)
        facts: list[JsonValue] = []
        for label, value in (
            ("Sender", _visible_sender(disclosed)),
            ("Subject", disclosed.get("subject")),
            ("Folder", disclosed.get("folder")),
            ("Importance", disclosed.get("importance")),
        ):
            if value:
                facts.append({"title": label, "value": neutralize_mentions(value)})
        content: dict[str, JsonValue] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": "New GUC email", "weight": "Bolder"},
                {"type": "FactSet", "facts": facts},
            ],
        }
        if disclosed.get("preview"):
            cast_body = content["body"]
            assert isinstance(cast_body, list)
            cast_body.append(
                {
                    "type": "TextBlock",
                    "text": truncate_utf8(neutralize_mentions(disclosed["preview"]), 2_000),
                    "wrap": True,
                }
            )
        payload: dict[str, JsonValue] = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": content,
                }
            ],
        }
        body = bounded_json(
            payload, min(self.config.policy.max_payload_bytes, 28_000), protected_keys=frozenset()
        )
        timestamp = _timestamp(event, delivery_time)
        return PreparedRequest(
            self.provider,
            "POST",
            self.config.url,
            _request_headers(event, self.provider, body=body, timestamp=timestamp),
            body,
            timeout_seconds=self.config.timeout_seconds,
        )


def validate_telegram_token(token: str) -> str:
    if not _TELEGRAM_TOKEN.fullmatch(token):
        raise ValueError("Telegram bot token has an invalid format.")
    return token


def mask_telegram_token(token: str) -> str:
    try:
        validate_telegram_token(token)
    except ValueError:
        return "<invalid Telegram token>"
    return f"<configured Telegram token:{_REDACTED_BULLETS}>"


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)
    policy: PayloadPolicy = field(default_factory=PayloadPolicy)
    silent: bool = False
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        validate_telegram_token(self.bot_token)
        if not re.fullmatch(r"-?\d{1,24}|@[A-Za-z][A-Za-z0-9_]{4,31}", self.chat_id):
            raise ValueError("Telegram chat ID has an invalid format.")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Telegram timeout must be between 0 and 120 seconds.")

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "provider": ProviderKind.TELEGRAM.value,
            "bot_token_configured": bool(self.bot_token),
            "chat_configured": bool(self.chat_id),
            "policy": self.policy,
            "silent": self.silent,
            "timeout_seconds": self.timeout_seconds,
        }


class TelegramAdapter:
    provider = ProviderKind.TELEGRAM

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    def prepare(
        self, event: NotificationEvent, *, delivery_time: datetime | None = None
    ) -> PreparedRequest:
        disclosed = event_payload(event, self.config.policy)
        text = truncate_utf8(
            "\n".join(_plain_lines(disclosed, private_label="New GUC email")),
            4_096,
        )
        payload: dict[str, JsonValue] = {
            "chat_id": self.config.chat_id,
            "text": text,
            "disable_notification": self.config.silent,
            "link_preview_options": {"is_disabled": True},
        }
        body = bounded_json(
            payload,
            min(self.config.policy.max_payload_bytes, 16_000),
            protected_keys=frozenset({"chat_id"}),
        )
        timestamp = _timestamp(event, delivery_time)
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        return PreparedRequest(
            self.provider,
            "POST",
            url,
            _request_headers(event, self.provider, body=body, timestamp=timestamp),
            body,
            timeout_seconds=self.config.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class GenericWebhookConfig:
    name: str
    url: str = field(repr=False)
    method: str = "POST"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    query: Mapping[str, str] = field(default_factory=dict, repr=False)
    policy: PayloadPolicy = field(default_factory=PayloadPolicy)
    hmac_secret: bytes | None = field(default=None, repr=False, compare=False)
    timeout_seconds: float = 15.0
    verify_tls: bool = True
    allow_http: bool = False
    allow_private: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 100:
            raise ValueError("Generic webhook name must contain between 1 and 100 characters.")
        method = self.method.upper()
        if method not in {"POST", "PUT", "PATCH"}:
            raise ValueError("Generic webhook method must be POST, PUT, or PATCH.")
        validate_destination(
            self.url,
            allow_http=self.allow_http,
            allow_private=self.allow_private,
        )
        _headers(self.headers)
        for key, value in self.query.items():
            if not key or any(character in str(key) + str(value) for character in "\r\n\x00"):
                raise ValueError("Webhook query parameters contain control data.")
        if self.hmac_secret is not None and len(self.hmac_secret) < 16:
            raise ValueError("HMAC secrets must contain at least 16 bytes.")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Webhook timeout must be between 0 and 120 seconds.")
        if not self.verify_tls and urlparse(self.url).scheme.casefold() == "https":
            raise ValueError("TLS certificate verification may not be disabled.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "method", method)

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "provider": ProviderKind.GENERIC_WEBHOOK.value,
            "name": self.name,
            "destination_configured": bool(self.url),
            "method": self.method,
            "header_names": tuple(sorted(str(name) for name in self.headers)),
            "query_parameter_count": len(self.query),
            "policy": self.policy,
            "hmac_secret_configured": self.hmac_secret is not None,
            "timeout_seconds": self.timeout_seconds,
            "verify_tls": self.verify_tls,
            "allow_http": self.allow_http,
            "allow_private": self.allow_private,
        }


class GenericWebhookAdapter:
    provider = ProviderKind.GENERIC_WEBHOOK

    def __init__(self, config: GenericWebhookConfig) -> None:
        self.config = config

    def prepare(
        self, event: NotificationEvent, *, delivery_time: datetime | None = None
    ) -> PreparedRequest:
        payload = event_payload(event, self.config.policy)
        body = bounded_json(payload, self.config.policy.max_payload_bytes)
        timestamp = _timestamp(event, delivery_time)
        parsed = urlparse(self.config.url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((str(key), str(value)) for key, value in self.config.query.items())
        url = urlunparse(parsed._replace(query=urlencode(query)))
        headers = _request_headers(
            event,
            self.provider,
            body=body,
            timestamp=timestamp,
            secret=self.config.hmac_secret,
            custom_headers=self.config.headers,
        )
        return PreparedRequest(
            self.provider,
            self.config.method,
            url,
            headers,
            body,
            verify_tls=self.config.verify_tls,
            follow_redirects=False,
            timeout_seconds=self.config.timeout_seconds,
        )


class NDJSONAdapter:
    provider = ProviderKind.NDJSON

    def __init__(self, policy: PayloadPolicy | None = None) -> None:
        self.policy = policy or PayloadPolicy()

    def prepare(self, event: NotificationEvent) -> bytes:
        return (
            bounded_json(event_payload(event, self.policy), self.policy.max_payload_bytes) + b"\n"
        )


def mask_secret_url(url: str) -> str:
    """Generic secret-safe URL display form for logs and setting summaries."""

    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return "<invalid URL>"
        port = f":{parsed.port}" if parsed.port else ""
        path_hash = hashlib.sha256(parsed.path.encode("utf-8")).hexdigest()[:10]
        return f"{parsed.scheme}://{parsed.hostname}{port}/<secret:{path_hash}>"
    except ValueError:
        return "<invalid URL>"


def escape_chat_markup(value: object) -> str:
    """Escape HTML-like chat markup for adapters which ever opt into rich text."""

    return escape(neutralize_mentions(value), quote=False)


__all__ = [
    "CompatibleWebhookConfig",
    "DestinationError",
    "DestinationValidation",
    "DiscordAdapter",
    "DiscordConfig",
    "GenericWebhookAdapter",
    "GenericWebhookConfig",
    "NDJSONAdapter",
    "RedirectPolicy",
    "SlackAdapter",
    "TeamsAdapter",
    "TelegramAdapter",
    "TelegramConfig",
    "escape_chat_markup",
    "mask_discord_webhook_url",
    "mask_secret_url",
    "mask_telegram_token",
    "neutralize_mentions",
    "sign_payload",
    "validate_destination",
    "validate_discord_webhook_url",
    "validate_redirect",
    "validate_telegram_token",
    "verify_payload_signature",
]
