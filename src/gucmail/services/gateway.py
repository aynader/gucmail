"""Defer provider access until a shared service actually performs online work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class DeferredGateway:
    """Planning/confirmation stays local; the owning context reuses its online adapter."""

    def __init__(self, resolver: Callable[[], Any]) -> None:
        self._resolver = resolver

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolver(), name)


def resolve(gateway: Any) -> Any:
    """Return the real provider behind a DeferredGateway.

    Use this before ``isinstance(gateway, SomeProtocol)``. From Python 3.12 those checks
    ignore ``__getattr__``, so a deferred wrapper never matches a protocol on its own.
    """

    if isinstance(gateway, DeferredGateway):
        return gateway._resolver()
    return gateway
