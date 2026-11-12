"""Saved download defaults shared by the CLI and attachment workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gucmail.services.attachments import CollisionPolicy

if TYPE_CHECKING:
    from gucmail.settings import SettingsService


@dataclass(frozen=True, slots=True)
class AttachmentPreferences:
    collision: CollisionPolicy = CollisionPolicy.RENAME

    @classmethod
    def from_settings(cls, service: SettingsService) -> AttachmentPreferences:
        return cls(CollisionPolicy(service.value("mail.attachment_collision").effective_value))
