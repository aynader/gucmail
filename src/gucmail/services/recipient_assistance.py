"""Bounded account-local recipient completion; never queries a mailbox or directory."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import getaddresses
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError
from gucmail.models import DraftData
from gucmail.security import sanitize_terminal
from gucmail.services.compose import normalize_recipient

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

RECIPIENT_SETTINGS = (
    "contacts.autocomplete",
    "contacts.recent_recipients",
    "contacts.remember_bcc",
)
RECIPIENT_HISTORY_SETTING = "palette.recent_people"
MAX_RECENT_RECIPIENTS = 20


def recent_addresses(values: Iterable[object]) -> tuple[str, ...]:
    """Validate legacy local history, deduplicate case-insensitively and bound work."""
    result: dict[str, str] = {}
    for index, value in enumerate(values):
        if index >= 100:
            break
        if not isinstance(value, str) or len(value) > 512:
            continue
        try:
            address = normalize_recipient(value).email
        except ConfigurationError:
            continue
        result.setdefault(address.casefold(), address)
        if len(result) >= MAX_RECENT_RECIPIENTS:
            break
    return tuple(result.values())


@dataclass(frozen=True, slots=True)
class RecipientPreferences:
    autocomplete: bool = True
    recent_recipients: bool = True
    remember_bcc: bool = False
    history: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, service: SettingsService) -> RecipientPreferences:
        values = {
            row.spec.setting_id: row.effective_value
            for row in service.snapshot((*RECIPIENT_SETTINGS, RECIPIENT_HISTORY_SETTING)).values
        }
        history = values[RECIPIENT_HISTORY_SETTING]
        return cls(
            values["contacts.autocomplete"],
            values["contacts.recent_recipients"],
            values["contacts.remember_bcc"],
            recent_addresses(history) if isinstance(history, (list, tuple)) else (),
        )

    @property
    def recent(self) -> tuple[str, ...]:
        return self.history if self.recent_recipients else ()

    def suggest(self, value: str) -> str | None:
        """Complete the final bare address, preserving the entire typed prefix."""
        if not self.autocomplete or not self.recent_recipients or len(value) > 8192:
            return None
        if sanitize_terminal(value) != value:
            return None
        quoted = escaped = False
        angle_depth = 0
        boundary = 0
        for index, character in enumerate(value):
            if escaped:
                escaped = False
            elif quoted and character == "\\":
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif not quoted:
                if character == "<":
                    angle_depth += 1
                elif character == ">":
                    angle_depth -= 1
                    if angle_depth < 0:
                        return None
                elif character == "," and angle_depth == 0:
                    boundary = index + 1
        if quoted or angle_depth or escaped:
            return None
        tail = value[boundary:]
        fragment = tail.lstrip()
        if not fragment or any(c.isspace() or c in '<>():,;\\"' for c in fragment):
            return None
        used: set[str] = set()
        if boundary:
            try:
                prior = getaddresses([value[: boundary - 1]])
                if not prior:
                    return None
                used = {normalize_recipient(email).email.casefold() for _name, email in prior}
            except (ConfigurationError, ValueError):
                return None
        for address in self.recent:
            key = address.casefold()
            if (
                key not in used
                and key.startswith(fragment.casefold())
                and len(address) > len(fragment)
            ):
                return value + address[len(fragment) :]
        return None


def remember_sent_recipients(
    service: SettingsService, draft: DraftData, *, own_address: str | None = None
) -> None:
    """Call only after an accepted immediate send, never after preview/queue/failure."""
    # Begin before reading: a concurrent history/config edit must reject this update
    # rather than let an older history snapshot overwrite the new document.
    session = service.begin()
    policy = RecipientPreferences.from_settings(service)
    if not policy.recent_recipients:
        return
    recipients = [*draft.to, *draft.cc]
    if policy.remember_bcc:
        recipients.extend(draft.bcc)
    own = (own_address or "").strip().casefold()
    sent = recent_addresses(address.email for address in recipients)
    sent = tuple(address for address in sent if address.casefold() != own)
    if not sent:
        return
    history = recent_addresses((*sent, *policy.history))
    session.set(RECIPIENT_HISTORY_SETTING, history)
    session.apply(reason="Successful-send recipient history updated")
