"""Bounded metadata-only mailbox sampling for notification dry runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event

from gucmail.errors import ConfigurationError
from gucmail.notification_events import NewMailEvent
from gucmail.notification_policies import MonitoringProfile
from gucmail.services.messages import MessageService


@dataclass(frozen=True, slots=True)
class NotificationSample:
    events: tuple[NewMailEvent, ...]
    folders: tuple[str, ...]
    omitted_folder_count: int
    limit: int


class NotificationSimulationService:
    """Never read bodies, download attachments, deliver, or update watcher checkpoints."""

    def __init__(self, messages: MessageService, *, account_id: str) -> None:
        self.messages = messages
        self.account_id = account_id

    def recent(
        self,
        profile: MonitoringProfile,
        *,
        limit: int = 50,
        max_folders: int = 10,
        cancel: Event | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> NotificationSample:
        if not 1 <= limit <= 100 or not 1 <= max_folders <= 10:
            raise ConfigurationError("Simulation is limited to 100 messages across 10 folders.")
        folders = profile.folders[: min(max_folders, limit)]
        events: list[NewMailEvent] = []
        now = datetime.now(UTC)
        for index, folder in enumerate(folders):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Recent-mail simulation cancelled.")
            if progress:
                progress(index, len(folders))
            # Divide the fixed request budget fairly, rather than enumerating each folder fully.
            per_folder = limit // len(folders) + (index < limit % len(folders))
            page = self.messages.list_messages(folder, limit=per_folder)
            for message in page.items[:per_folder]:
                occurred = message.received_at or message.sent_at or now
                identity = (
                    f"{self.account_id}\n{folder}\n{message.reference}\n{occurred.isoformat()}"
                )
                events.append(
                    NewMailEvent(
                        event_id="simulation-" + sha256(identity.encode("utf-8")).hexdigest(),
                        account_id=self.account_id,
                        occurred_at=occurred,
                        detected_at=now,
                        folder=message.folder_path or folder,
                        message_reference=message.reference,
                        sender=message.sender,
                        subject=message.subject,
                        importance=message.importance,
                        is_read=message.is_read,
                        has_attachments=message.has_attachments,
                        size_bytes=message.size,
                        categories=message.categories,
                        conversation_id=message.conversation_id,
                    )
                )
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Recent-mail simulation cancelled.")
        if progress:
            progress(len(folders), len(folders))
        events.sort(key=lambda event: event.occurred_at, reverse=True)
        return NotificationSample(
            tuple(events), folders, len(profile.folders) - len(folders), limit
        )
