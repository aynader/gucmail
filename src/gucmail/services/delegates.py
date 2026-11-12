"""Read-only information about delegates of the configured Exchange mailbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gucmail.errors import ConfigurationError

MAX_DELEGATES = 1_000


@dataclass(frozen=True, slots=True)
class DelegatePermission:
    folder: str
    level: str | None


@dataclass(frozen=True, slots=True)
class DelegateInfo:
    display_name: str
    email_address: str | None
    security_identifier: str | None
    distinguished_user: str | None
    external_user_identity: str | None
    permissions: tuple[DelegatePermission, ...]
    receive_meeting_copies: bool | None
    view_private_items: bool | None


@dataclass(frozen=True, slots=True)
class DelegatePage:
    entries: tuple[DelegateInfo, ...]
    total: int
    offset: int
    limit: int
    next_offset: int | None
    pagination: str = "local_slice_of_single_ews_collection"


class DelegateGateway(Protocol):
    def list_delegates(self) -> tuple[DelegateInfo, ...]: ...


def delegate_details_text(delegate: DelegateInfo) -> str:
    def flag(value: bool | None) -> str:
        return "Not reported" if value is None else "Yes" if value else "No"

    return "\n".join(
        [
            f"Name: {delegate.display_name or '—'}",
            f"Address: {delegate.email_address or 'Not reported'}",
            f"Security identifier: {delegate.security_identifier or 'Not reported'}",
            f"Special principal: {delegate.distinguished_user or 'Not reported'}",
            f"External identity: {delegate.external_user_identity or 'Not reported'}",
            f"Receives meeting copies: {flag(delegate.receive_meeting_copies)}",
            f"May view private items: {flag(delegate.view_private_items)}",
            "",
            "Reported folder permissions:",
            *(
                f"{value.folder.title()}: "
                f"{value.level if value.level is not None else 'Not reported'}"
                for value in delegate.permissions
            ),
        ]
    )


def delegate_page(
    entries: tuple[DelegateInfo, ...], *, limit: int = 100, offset: int = 0
) -> DelegatePage:
    validate_delegate_window(limit, offset)
    end = offset + limit
    return DelegatePage(
        entries[offset:end], len(entries), offset, limit, end if end < len(entries) else None
    )


def validate_delegate_window(limit: int, offset: int) -> None:
    if (
        type(limit) is not int
        or type(offset) is not int
        or not 1 <= limit <= MAX_DELEGATES
        or not 0 <= offset <= MAX_DELEGATES
    ):
        raise ConfigurationError(
            "Use a delegate limit from 1 to 1000 and an offset from 0 to 1000."
        )


class DelegateService:
    def __init__(self, gateway: DelegateGateway) -> None:
        self.gateway = gateway

    def list(self, *, limit: int = 100, offset: int = 0) -> DelegatePage:
        # Validate before resolving credentials or making the single collection request.
        validate_delegate_window(limit, offset)
        return delegate_page(self.gateway.list_delegates(), limit=limit, offset=offset)
