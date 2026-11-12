"""Bounded multi-page message-summary scans for local analysis commands.

The EWS gateway accepts at most ``MAX_PAGE_SIZE`` items per request. Insights,
clusters, cleanup and index builds advertise scans of up to 500 summaries, so they
must read consecutive bounded pages instead of passing the whole limit through.
"""

from __future__ import annotations

from typing import Any

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError


def scan_message_summaries(
    gateway: Any,
    *,
    folder: str,
    limit: int,
    page_size: int = MAX_PAGE_SIZE,
) -> tuple[Any, ...]:
    """Return at most ``limit`` summaries using pages no larger than ``page_size``.

    A single-page scan keeps the historic ``list_messages(folder=, limit=)`` call
    shape. Paging stops at the first short page, so no extra request is made once
    the folder is exhausted.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ConfigurationError("Message scan limit must be a positive integer.")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ConfigurationError(f"Message scan page size must be 1 to {MAX_PAGE_SIZE}.")
    first = min(limit, page_size)
    collected = list(gateway.list_messages(folder=folder, limit=first))[:first]
    exhausted = len(collected) < first
    while not exhausted and len(collected) < limit:
        request = min(page_size, limit - len(collected))
        page = list(gateway.list_messages(folder=folder, limit=request, offset=len(collected)))[
            :request
        ]
        collected.extend(page)
        exhausted = len(page) < request
    return tuple(collected)


__all__ = ["scan_message_summaries"]
