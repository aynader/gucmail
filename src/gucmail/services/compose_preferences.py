"""Shared persisted composer defaults and warning policy, with no mailbox I/O."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from gucmail.models import DraftData, Importance

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

QUOTED_SECTION_RE = re.compile(
    r"\n\n(?:On .+? wrote:|--- (?:Original|Forwarded) message ---)", re.IGNORECASE
)


class BodyFormat(StrEnum):
    PLAIN = "plain"
    HTML = "html"


@dataclass(frozen=True, slots=True)
class ComposerPreferences:
    body_format: BodyFormat = BodyFormat.PLAIN
    importance: Importance = Importance.NORMAL
    read_receipt: bool = False
    delivery_receipt: bool = False
    signature_enabled: bool = False
    signature: str = ""
    warn_missing_attachment: bool = True
    recipient_warning_threshold: int = 20
    empty_subject_policy: str = "require"
    save_sent_copy: bool = True

    @property
    def allow_empty_subject(self) -> bool:
        return self.empty_subject_policy == "warn"

    @classmethod
    def from_settings(cls, service: SettingsService) -> ComposerPreferences:
        identifiers = (
            "default_body_format",
            "default_importance",
            "request_read_receipt",
            "request_delivery_receipt",
            "signature_enabled",
            "signature",
            "warn_missing_attachment",
            "recipient_warning_threshold",
            "empty_subject_policy",
        )
        snapshot = service.snapshot(
            (*(f"composer.{identifier}" for identifier in identifiers), "mail.save_sent_copy")
        )
        values = {row.spec.setting_id: row.effective_value for row in snapshot.values}

        def value(identifier: str) -> object:
            return values["composer." + identifier]

        return cls(
            body_format=BodyFormat(str(value("default_body_format"))),
            importance=Importance(str(value("default_importance"))),
            read_receipt=bool(value("request_read_receipt")),
            delivery_receipt=bool(value("request_delivery_receipt")),
            signature_enabled=bool(value("signature_enabled")),
            signature=str(value("signature")),
            warn_missing_attachment=bool(value("warn_missing_attachment")),
            recipient_warning_threshold=int(str(value("recipient_warning_threshold"))),
            empty_subject_policy=str(value("empty_subject_policy")),
            save_sent_copy=bool(values["mail.save_sent_copy"]),
        )

    def seed_new(
        self,
        draft: DraftData,
        *,
        body_format: BodyFormat | None = None,
        importance: Importance | None = None,
        read_receipt: bool | None = None,
        delivery_receipt: bool | None = None,
        signature_enabled: bool | None = None,
        save_sent_copy: bool | None = None,
    ) -> DraftData:
        """Seed a new composition only; callers must never use this to resume a draft."""
        body = draft.body_text
        body_html = draft.body_html
        selected_format = body_format or self.body_format
        if selected_format is BodyFormat.PLAIN:
            body_html = None
        include_signature = (
            self.signature_enabled if signature_enabled is None else signature_enabled
        )
        if include_signature and self.signature.strip():
            quote = QUOTED_SECTION_RE.search(body)
            split = quote.start() if quote else len(body)
            body = (body[:split].rstrip() + "\n\n" + self.signature).strip() + body[split:]
            if body_html is not None:
                body_html += "<p>" + html.escape(self.signature).replace("\n", "<br>") + "</p>"
        if selected_format is BodyFormat.HTML and body_html is None:
            body_html = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>" if body else ""
        return replace(
            draft,
            body_text=body,
            body_html=body_html,
            importance=importance if importance is not None else self.importance,
            request_read_receipt=read_receipt if read_receipt is not None else self.read_receipt,
            request_delivery_receipt=(
                delivery_receipt if delivery_receipt is not None else self.delivery_receipt
            ),
            save_sent_copy=self.save_sent_copy if save_sent_copy is None else save_sent_copy,
        )
