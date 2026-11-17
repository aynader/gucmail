"""Events that screens send to the app.

They live here, apart from their screens, so the app can listen for them without loading
every screen at startup. Each screen module still re-exports its own events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from textual.message import Message

from gucmail.models import Address
from gucmail.security import sanitize_terminal
from gucmail.services.local_search import LocalSearchMode, LocalSearchResult
from gucmail.tui.area_common import clean_cell

if TYPE_CHECKING:
    from gucmail.tui.drafts_attachments_screen import DraftHandoffRequest
    from gucmail.tui.settings_screen import SettingsScreen


# from activity_screen
class MessageOperationRequested(Message):
    """Request an exact, read-only recovery record from the owning app."""

    def __init__(self, operation_id: str) -> None:
        super().__init__()
        self.operation_id = operation_id


# from contact_groups_screen
class ContactGroupsRequested(Message):
    """Open the local-first contact-group workspace."""


# from contact_groups_screen
class ContactGroupComposeRequested(Message):
    def __init__(self, recipients: tuple[Address, ...]) -> None:
        super().__init__()
        self.recipients = recipients


# from drafts_attachments_screen
class DraftHandoff(Message):
    """Typed boundary from this workspace to the app's composer/scheduler."""

    def __init__(self, request: DraftHandoffRequest) -> None:
        super().__init__()
        self.request = request


# from drafts_attachments_screen
class DraftRecoveryRequested(Message):
    """Open the latest composer recovery through the shared application service."""


# from help_screen
class HelpCommandRequested(Message):
    """Request a safe palette preview for a command link from Help."""

    def __init__(self, command_id: str) -> None:
        super().__init__()
        self.command_id = command_id


# from help_screen
class HelpSettingRequested(Message):
    """Request the exact Settings row linked from Help."""

    def __init__(self, setting_id: str) -> None:
        super().__init__()
        self.setting_id = setting_id


# from help_screen
class HelpTutorialsRequested(Message):
    def __init__(self, page_id: str) -> None:
        super().__init__()
        self.page_id = page_id


# from local_search_screen
class LocalSearchOpenRequested(Message):
    def __init__(self, result: LocalSearchResult) -> None:
        super().__init__()
        self.result = result


# from local_search_screen
class LocalSearchScopeChanged(Message):
    """Only an allowlisted source choice may be retained by the mail shell."""

    def __init__(self, mode: LocalSearchMode) -> None:
        super().__init__()
        self.mode = mode


# from local_search_screen
class LocalSearchHelpRequested(Message):
    pass


# from local_search_screen
class LocalSearchIndexRequested(Message):
    pass


# from local_search_screen
class LocalSearchSemanticSetupRequested(Message):
    pass


# from notification_settings_screen
class InboxRulesRequested(Message):
    """Navigate to native Exchange inbox rules without changing them."""


# from notification_settings_screen
class AutomaticRepliesRequested(Message):
    """Navigate to mailbox policy management without enabling it."""


# from pim_screens
class PimActionRequested(Message):
    """Request an application-level action such as compose/history navigation."""

    def __init__(
        self,
        area: str,
        action: str,
        references: Sequence[str],
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.area = clean_cell(area, max_length=30)
        self.action = clean_cell(action, max_length=60)
        self.references = tuple(clean_cell(item, max_length=500) for item in references)
        self.payload = dict(payload or {})


# from saved_search_screen
class SavedSearchRunRequested(Message):
    """Ask the app to run one exact advanced expression in its mail result view."""

    def __init__(
        self,
        *,
        name: str | None,
        query_text: str,
        payload: Mapping[str, Any],
        source: str,
    ) -> None:
        super().__init__()
        self.name = sanitize_terminal(name, max_length=120) if name else None
        self.query_text = query_text.strip()
        self.payload = dict(payload)
        self.source = source


# from saved_search_screen
class SavedSearchHelpRequested(Message):
    """Ask the app to open context Help for the saved-search product area."""


# from settings_screen
class ColumnPreviewRequested(Message):
    """Ask the mail shell to expose the actual rendered column preview."""

    def __init__(self, settings: SettingsScreen) -> None:
        super().__init__()
        self.settings = settings


# from settings_screen
class AppearanceBrowseRequested(Message):
    """Request the app-owned operational appearance browser from Settings."""

    def __init__(self, browser: str) -> None:
        if browser not in {"themes", "branding"}:
            raise ValueError("Unknown appearance browser")
        super().__init__()
        self.browser = browser


# from settings_screen
class SettingsHelpRequested(Message):
    """App-level route request for a setting's embedded Help topic."""

    def __init__(self, setting_id: str, help_topic: str) -> None:
        super().__init__()
        self.setting_id = clean_cell(setting_id, max_length=120)
        self.help_topic = clean_cell(help_topic, max_length=240)


# from settings_screen
class SettingsSetupRequested(Message):
    def __init__(self, area: str) -> None:
        super().__init__()
        if area not in {"startup", "notifications"}:
            raise ValueError("Unknown managed settings area")
        self.area = area


# from shortcuts_screen
class ShortcutsRequested(Message):
    """Open the local binding browser from Help."""


# from shortcuts_screen
class ShortcutSettingRequested(Message):
    def __init__(self, setting_id: str, binding_id: str | None = None) -> None:
        super().__init__()
        self.setting_id = setting_id
        self.binding_id = binding_id


# from tutorial_screen
class TutorialHelpRequested(Message):
    def __init__(self, page_id: str) -> None:
        super().__init__()
        self.page_id = page_id
