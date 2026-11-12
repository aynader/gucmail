"""Bounded conversation response evidence from message metadata, shared by CLI/TUI.

Absence is reported only within the selected folders and their covered time windows.
No subject matching, message body inspection, or guess about whether a reply is owed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Protocol

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError
from gucmail.models import MessageSummary
from gucmail.security import validate_email_address


class ConversationGateway(Protocol):
    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 50,
        offset: int = 0,
        order_by: str = "-datetime_received",
    ) -> Sequence[MessageSummary]: ...


@dataclass(frozen=True, slots=True)
class FolderCoverage:
    folder: str
    scanned: int
    fetched: int
    complete: bool
    oldest_sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResponseFinding:
    message: MessageSummary
    status: str
    explanation: str


@dataclass(frozen=True, slots=True)
class UnansweredReport:
    findings: tuple[ResponseFinding, ...]
    coverage: tuple[FolderCoverage, ...]
    self_addresses: tuple[str, ...]
    scanned: int
    no_reply_found: int
    uncertain: int
    later_self_message: int
    missing_conversation_id: int
    notes: tuple[str, ...] = (
        "Results cover only the selected folders and supplied self addresses/aliases.",
        "No reply found means no later self-authored message was observed in that scope; "
        "it does not establish that a reply is expected. Other folders/accounts are not checked.",
        "Conversation IDs are required. Missing metadata, tied timestamps and incomplete "
        "time coverage are reported as uncertain. Exchange reads are not an atomic snapshot.",
        "At most one extra metadata record per folder is fetched to detect truncation. "
        "Increase the per-folder limit to expand uncertain time coverage.",
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _when(item: MessageSummary) -> datetime | None:
    return _utc(item.sent_at or item.received_at)


def _sender(item: MessageSummary) -> str | None:
    value = item.sender.email.strip().casefold() if item.sender else ""
    return (
        value
        if validate_email_address(value) and not any(char.isspace() for char in value)
        else None
    )


def normalize_self_addresses(values: Sequence[str]) -> tuple[str, ...]:
    addresses = tuple(dict.fromkeys(value.strip().casefold() for value in values))
    if not addresses or any(
        not validate_email_address(value) or any(char.isspace() for char in value)
        for value in addresses
    ):
        raise ConfigurationError("Supply your mailbox address and all sending aliases.")
    return addresses


class UnansweredService:
    def __init__(self, gateway: ConversationGateway) -> None:
        self.gateway = gateway

    def analyze(
        self,
        *,
        self_addresses: Sequence[str],
        folders: Sequence[str] = ("inbox",),
        sent_folders: Sequence[str] = ("sent",),
        limit: int = 100,
        cancelled: Callable[[], bool] | None = None,
    ) -> UnansweredReport:
        addresses = normalize_self_addresses(self_addresses)
        incoming = tuple(dict.fromkeys(value.strip() for value in folders if value.strip()))
        outgoing = tuple(dict.fromkeys(value.strip() for value in sent_folders if value.strip()))
        selected = tuple(dict.fromkeys((*incoming, *outgoing)))
        if not incoming or not outgoing or len(selected) > 10 or not 1 <= limit <= 1000:
            raise ConfigurationError(
                "Choose incoming and sent folders (at most 10 total), "
                "and a 1-1000 per-folder limit."
            )

        def check_cancel() -> None:
            if cancelled and cancelled():
                raise InterruptedError("Conversation analysis cancelled.")

        coverage: list[FolderCoverage] = []
        by_conversation: dict[str, dict[str, MessageSummary]] = defaultdict(dict)
        incoming_refs: set[str] = set()
        missing_ids = 0
        unlinked_outgoing = False
        scanned = 0
        for folder in selected:
            window: list[MessageSummary] = []
            exhausted = False
            # One lookahead item distinguishes an exact full page from a truncated scan.
            while len(window) <= limit:
                check_cancel()
                size = min(MAX_PAGE_SIZE, limit + 1 - len(window))
                page = tuple(
                    self.gateway.list_messages(
                        folder=folder,
                        limit=size,
                        offset=len(window),
                        order_by="-datetime_sent",
                    )
                )
                if len(page) > size:
                    raise ConfigurationError(
                        "The message provider exceeded the requested page size."
                    )
                window.extend(page)
                if len(page) < size:
                    exhausted = True
                    break
            complete = exhausted and len(window) <= limit
            values = window[:limit]
            times = [_utc(value.sent_at) for value in values]
            ordered = all(time is not None for time in times) and all(
                first >= second
                for first, second in pairwise(times)
                if first is not None and second is not None
            )
            floor = times[-1] if times and ordered else None
            coverage.append(FolderCoverage(folder, len(values), len(window), complete, floor))
            scanned += len(values)
            for value in values:
                check_cancel()
                sender = _sender(value)
                if not value.conversation_id:
                    missing_ids += 1
                    if sender in addresses or sender is None:
                        unlinked_outgoing = True
                    continue
                by_conversation[value.conversation_id][value.reference] = value
                if folder in incoming and sender not in addresses:
                    incoming_refs.add(value.reference)

        findings: list[ResponseFinding] = []
        replied = 0
        for messages in by_conversation.values():
            check_cancel()
            candidates = [value for value in messages.values() if value.reference in incoming_refs]
            if not candidates:
                continue
            latest = max(
                candidates,
                key=lambda value: (
                    _when(value) or datetime.min.replace(tzinfo=UTC),
                    value.reference,
                ),
            )
            when = _when(latest)
            own = [value for value in messages.values() if _sender(value) in addresses]
            unknown = any(
                _sender(value) is None or _when(value) is None for value in messages.values()
            )
            if not unknown and when and any((_when(value) or when) > when for value in own):
                replied += 1
                continue
            reason = None
            if unknown or when is None:
                reason = "A conversation message lacks sender or timestamp metadata."
            elif any(_when(value) == when for value in own):
                reason = "Incoming and self-authored messages have the same timestamp."
            elif unlinked_outgoing:
                reason = "A possible outgoing message lacks a conversation ID."
            elif any(
                not part.complete and (part.oldest_sent_at is None or when <= part.oldest_sent_at)
                for part in coverage
            ):
                reason = "A folder's bounded scan does not cover this message's later time window."
            findings.append(
                ResponseFinding(
                    latest,
                    "uncertain" if reason else "no_reply_found",
                    reason or "No later self-authored message found in the covered folders.",
                )
            )
        findings.sort(
            key=lambda finding: (
                _when(finding.message) or datetime.min.replace(tzinfo=UTC),
                finding.message.reference,
            ),
            reverse=True,
        )
        uncertain = sum(finding.status == "uncertain" for finding in findings)
        return UnansweredReport(
            tuple(findings),
            tuple(coverage),
            addresses,
            scanned,
            len(findings) - uncertain,
            uncertain,
            replied,
            missing_ids,
        )
