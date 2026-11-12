"""A small allowlist of harmless mail-shell state, separate from draft recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.state import StateStore

SESSION_KEY = "mail-shell.v1"
SESSION_SCOPE = "ui-session"
FOCUS_TARGETS = frozenset({"folder-tree", "message-table", "message-filter", "preview-scroll"})
SEARCH_SCOPES = frozenset({"all", "index", "attachments", "backups"})
MAIL_SEARCH_SCOPES = frozenset({"current_folder", "mailbox", "folder"})
DEFAULT_SEARCH_SCOPES = frozenset({"current_folder", "mailbox", "local_index", "backups", "all"})
MAIL_SCOPE_FIELDS = frozenset({"mail_search_scope", "mail_search_folder", "search_default_scope"})
MAX_SCROLL = 10_000


@dataclass(frozen=True, slots=True)
class MailSession:
    """No bodies, query text, item IDs, screen stack, or pending action can be represented."""

    version: int = 1
    folder: str = "inbox"
    focus: str = "message-table"
    message_row: int = 0
    message_scroll: int = 0
    folder_scroll: int = 0
    search_scope: str = "all"
    mail_search_scope: str | None = None
    mail_search_folder: str | None = None
    search_default_scope: str | None = None

    @staticmethod
    def _valid_folder(folder: Any) -> bool:
        return (
            isinstance(folder, str)
            and bool(folder.strip())
            and len(folder) <= 1_024
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in folder)
        )

    @classmethod
    def parse(cls, value: Any) -> MailSession | None:
        if not isinstance(value, dict):
            return None
        fields = set(cls.__dataclass_fields__)
        if set(value) == fields - MAIL_SCOPE_FIELDS:
            # Earlier v1 rows carry only the local workspace choice. Upgrade in memory.
            value = {**value, **dict.fromkeys(MAIL_SCOPE_FIELDS)}
        elif set(value) != fields:
            return None
        if type(value["version"]) is not int or value["version"] != 1:
            return None
        folder = value["folder"]
        if not cls._valid_folder(folder):
            return None
        if not isinstance(value["focus"], str) or value["focus"] not in FOCUS_TARGETS:
            return None
        if not isinstance(value["search_scope"], str) or value["search_scope"] not in SEARCH_SCOPES:
            return None
        mail_scope, mail_folder, default_scope = (
            value["mail_search_scope"],
            value["mail_search_folder"],
            value["search_default_scope"],
        )
        if mail_scope is None:
            if mail_folder is not None or default_scope is not None:
                return None
        else:
            if not isinstance(mail_scope, str) or mail_scope not in MAIL_SEARCH_SCOPES:
                return None
            if not isinstance(default_scope, str) or default_scope not in DEFAULT_SEARCH_SCOPES:
                return None
            if mail_scope == "folder":
                if not cls._valid_folder(mail_folder):
                    return None
            elif mail_folder is not None:
                return None
        for key, maximum in (
            ("message_row", MAX_PAGE_SIZE - 1),
            ("message_scroll", MAX_SCROLL),
            ("folder_scroll", MAX_SCROLL),
        ):
            if type(value[key]) is not int or not 0 <= value[key] <= maximum:
                return None
        return cls(**value)

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        if self.parse(value) is None:
            raise ValueError("Invalid mail-shell session state.")
        return value


class MailSessionStore:
    """Optional account-scoped state; a concurrent app must not overwrite a newer session."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._account_key = state.account_key
        self._revision = 0
        self._writable = False
        self._original: MailSession | None = None

    def load(self) -> MailSession | None:
        try:
            record = self.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
            self._revision = record.revision if record else 0
            self._original = MailSession.parse(record.value) if record else None
            self._writable = True
        except Exception:
            # Preserve the database and use the ordinary mail shell if reading fails.
            self._writable = False
            self._original = None
        return self._original

    def save(self, session: MailSession) -> bool:
        if not self._writable or self.state.account_key != self._account_key:
            return False
        try:
            payload = session.payload()
            if session == self._original:
                return True
            record = self.state.put_ui_preference(
                SESSION_KEY,
                payload,
                scope=SESSION_SCOPE,
                expected_revision=self._revision,
            )
            self._revision = record.revision
            self._original = session
            return True
        except Exception:
            # No retry, reset, or overwrite after a lock, corruption, or revision conflict.
            return False
