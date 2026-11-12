"""Compatibility and security bridges for pure notification request builders.

Request builders never perform I/O.  The safe transport wrapper below validates the
destination both before and after name resolution and owns redirect handling, while the
actual HTTP operation remains an injected dependency.  This keeps tests entirely offline
and prevents a permissive client from silently following a webhook into a private network.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import urlparse

from gucmail.models import NotificationEvent
from gucmail.notification_providers.core import PreparedRequest, ProviderKind
from gucmail.notification_providers.webhook_payloads import (
    DestinationError,
    RedirectPolicy,
    validate_destination,
    validate_redirect,
)
from gucmail.security import redact_text, sanitize_terminal
from gucmail.services.notifications import DeliveryResult, DeliveryState


class RequestBuilder(Protocol):
    provider: ProviderKind

    def prepare(self, event: NotificationEvent) -> PreparedRequest: ...


@dataclass(frozen=True, slots=True)
class TransportOutcome:
    delivered: bool
    status_code: int | None = None
    attempts: int = 1
    detail: str | None = field(default=None, repr=False)
    response_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def retry_after(self) -> str | None:
        for name, value in self.response_headers.items():
            if name.casefold() == "retry-after":
                return str(value)
        return None

    @property
    def redirect_location(self) -> str | None:
        for name, value in self.response_headers.items():
            if name.casefold() == "location":
                return str(value)
        return None

    def to_safe_primitive(self) -> dict[str, object]:
        return {
            "delivered": self.delivered,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "detail": (
                redact_text(self.detail, redact_emails=True, redact_urls=True)
                if self.detail
                else None
            ),
            "response_header_names": tuple(sorted(str(name) for name in self.response_headers)),
        }


class PreparedRequestTransport(Protocol):
    """Injected transport boundary; implementations own all network I/O."""

    def deliver(self, request: PreparedRequest) -> TransportOutcome: ...


class AddressResolver(Protocol):
    """Resolve a hostname immediately before delivery.

    Production adapters may use the system resolver.  Tests should inject a deterministic
    resolver and therefore never perform DNS or other network I/O.
    """

    def resolve(self, hostname: str) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class DestinationSecurityPolicy:
    allow_http: bool = False
    allow_private: bool = False
    redirects: RedirectPolicy = field(default_factory=RedirectPolicy)
    maximum_resolved_addresses: int = 16

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_resolved_addresses <= 64:
            raise ValueError("maximum_resolved_addresses must be between 1 and 64")


class SafePreparedRequestTransport:
    """Validate DNS answers and redirects around an injected one-request transport.

    Only same-origin 307/308 redirects are eligible.  This preserves the webhook method
    and body and avoids forwarding secret headers to another origin.  Redirect following
    remains off by default through :class:`RedirectPolicy`.
    """

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(
        self,
        transport: PreparedRequestTransport,
        resolver: AddressResolver,
        *,
        policy: DestinationSecurityPolicy | None = None,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self._policy = policy or DestinationSecurityPolicy()

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlparse(url)
        return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port

    def _validate_resolved_destination(self, url: str) -> None:
        validation = validate_destination(
            url,
            allow_http=self._policy.allow_http,
            allow_private=self._policy.allow_private,
        )
        parsed = urlparse(validation.url)
        hostname = parsed.hostname
        if hostname is None:  # pragma: no cover - guarded by validate_destination
            raise DestinationError("Webhook destination has no hostname.")
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            answers = tuple(
                dict.fromkeys(str(value).strip() for value in self._resolver.resolve(hostname))
            )
            if not answers:
                raise DestinationError(
                    "Webhook destination did not resolve to an address."
                ) from None
            if len(answers) > self._policy.maximum_resolved_addresses:
                raise DestinationError("Webhook destination returned too many addresses.") from None
            try:
                addresses = tuple(ipaddress.ip_address(value) for value in answers)
            except ValueError as exc:
                raise DestinationError("Webhook resolver returned an invalid address.") from exc
        else:
            addresses = (literal,)
        if not self._policy.allow_private and any(not address.is_global for address in addresses):
            raise DestinationError(
                "Webhook DNS resolved to a local, private, reserved, or otherwise "
                "non-public address."
            )

    def deliver(self, request: PreparedRequest) -> TransportOutcome:
        if request.follow_redirects:
            raise DestinationError("The injected HTTP transport must not follow redirects.")
        current = request
        count = 0
        while True:
            self._validate_resolved_destination(current.url)
            outcome = self._transport.deliver(current)
            status = outcome.status_code
            if status not in self._REDIRECT_STATUSES:
                return outcome
            location = outcome.redirect_location
            if not location:
                return replace(
                    outcome, delivered=False, detail="Webhook redirect omitted Location."
                )
            if status not in {307, 308}:
                return replace(
                    outcome,
                    delivered=False,
                    detail="Webhook redirect would change request semantics and was blocked.",
                )
            validation = validate_redirect(
                current.url,
                location,
                redirect_count=count,
                policy=self._policy.redirects,
                allow_private=self._policy.allow_private,
            )
            if self._origin(current.url) != self._origin(validation.url):
                raise DestinationError("Cross-origin webhook redirects are blocked.")
            current = replace(current, url=validation.url, follow_redirects=False)
            count += 1


class PreparedRequestNotificationProvider:
    """Make a pure builder usable by the established ``NotificationDispatcher``.

    Construction and request preparation remain offline.  A caller must deliberately
    supply the transport, which is straightforward to fake in unit tests.
    """

    def __init__(
        self,
        builder: RequestBuilder,
        transport: PreparedRequestTransport,
        *,
        name: str | None = None,
        enabled: bool = True,
        allow_integration_events: bool = False,
    ) -> None:
        self.builder = builder
        self.transport = transport
        self.name = sanitize_terminal(name or builder.provider.value, max_length=200) or "provider"
        self.enabled = enabled
        self.allow_integration_events = allow_integration_events

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.DISABLED,
                "Provider is disabled.",
                attempts=0,
            )
        if not self.allow_integration_events and event.event_type.casefold().startswith(
            ("webhook", "integration")
        ):
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                "Integration-generated events are blocked to prevent notification loops.",
                attempts=0,
            )
        try:
            request = self.builder.prepare(event)
        except Exception as exc:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                f"Provider request preparation failed ({type(exc).__name__}).",
                attempts=0,
            )
        try:
            outcome = self.transport.deliver(request)
        except Exception as exc:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                f"Provider transport failed ({type(exc).__name__}).",
                attempts=1,
            )
        detail = (
            redact_text(
                sanitize_terminal(outcome.detail, max_length=1_000),
                redact_emails=True,
                redact_urls=True,
            )
            if outcome.detail
            else None
        )
        return DeliveryResult(
            event.event_id,
            self.name,
            DeliveryState.DELIVERED if outcome.delivered else DeliveryState.FAILED,
            detail,
            attempts=max(0, min(outcome.attempts, 1_000)),
            status_code=outcome.status_code,
            retry_after=outcome.retry_after,
        )


__all__ = [
    "AddressResolver",
    "DestinationSecurityPolicy",
    "PreparedRequestNotificationProvider",
    "PreparedRequestTransport",
    "RequestBuilder",
    "SafePreparedRequestTransport",
    "TransportOutcome",
]
