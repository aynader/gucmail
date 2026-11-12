"""Back/forward navigation and a persistence boundary for local Help preferences."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from gucmail.help.models import HelpPage, HelpRuntimeContext, HelpSearchResult, HelpUserState
from gucmail.help.repository import HelpRepository


class HelpStateStore(Protocol):
    """Minimal adapter implemented later by SQLite-backed application state."""

    def load_help_state(self) -> HelpUserState: ...

    def save_help_state(self, state: HelpUserState) -> None: ...


class MemoryHelpStateStore:
    """Deterministic store used by tests and callers that do not need persistence."""

    def __init__(self, state: HelpUserState | None = None) -> None:
        self._state = state or HelpUserState()

    def load_help_state(self) -> HelpUserState:
        return self._state

    def save_help_state(self, state: HelpUserState) -> None:
        self._state = state


class HelpNavigator:
    """Session navigation independent from any Textual widgets."""

    def __init__(
        self,
        repository: HelpRepository,
        *,
        store: HelpStateStore | None = None,
        recent_limit: int = 30,
        query_limit: int = 20,
    ) -> None:
        if not 1 <= recent_limit <= 200 or not 1 <= query_limit <= 200:
            raise ValueError("Help state limits must be between 1 and 200.")
        self.repository = repository
        self.store = store or MemoryHelpStateStore()
        self.recent_limit = recent_limit
        self.query_limit = query_limit
        self._state = self.store.load_help_state()
        self._history: list[str] = []
        self._cursor = -1

    @property
    def state(self) -> HelpUserState:
        return self._state

    @property
    def current(self) -> HelpPage | None:
        if self._cursor < 0:
            return None
        return self.repository.get(self._history[self._cursor])

    @property
    def can_go_back(self) -> bool:
        return self._cursor > 0

    @property
    def can_go_forward(self) -> bool:
        return 0 <= self._cursor < len(self._history) - 1

    def open(self, page_id: str) -> HelpPage:
        page = self.repository.get(page_id)
        if self._cursor < len(self._history) - 1:
            del self._history[self._cursor + 1 :]
        if not self._history or self._history[-1] != page.page_id:
            self._history.append(page.page_id)
        self._cursor = len(self._history) - 1
        self._state = HelpUserState(
            recent_page_ids=_prepend_unique(
                page.page_id, self._state.recent_page_ids, self.recent_limit
            ),
            favorite_page_ids=self._state.favorite_page_ids,
            recent_queries=self._state.recent_queries,
        )
        self._persist()
        return page

    def home(self) -> HelpPage:
        return self.open(self.repository.home_page_id)

    def back(self) -> HelpPage | None:
        if not self.can_go_back:
            return self.current
        self._cursor -= 1
        return self.current

    def forward(self) -> HelpPage | None:
        if not self.can_go_forward:
            return self.current
        self._cursor += 1
        return self.current

    def breadcrumbs(self) -> tuple[str, ...]:
        page = self.current
        if page is None:
            return ("Help",)
        return ("Help", page.category, page.title)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        context: HelpRuntimeContext | None = None,
    ) -> tuple[HelpSearchResult, ...]:
        cleaned = " ".join(query.split())
        results = self.repository.search(cleaned, limit=limit, context=context)
        if cleaned:
            self._state = HelpUserState(
                recent_page_ids=self._state.recent_page_ids,
                favorite_page_ids=self._state.favorite_page_ids,
                recent_queries=_prepend_unique(
                    cleaned, self._state.recent_queries, self.query_limit
                ),
            )
            self._persist()
        return results

    def toggle_favorite(self, page_id: str) -> bool:
        page = self.repository.get(page_id)
        favorites = set(self._state.favorite_page_ids)
        if page.page_id in favorites:
            favorites.remove(page.page_id)
            enabled = False
        else:
            favorites.add(page.page_id)
            enabled = True
        self._state = HelpUserState(
            recent_page_ids=self._state.recent_page_ids,
            favorite_page_ids=frozenset(favorites),
            recent_queries=self._state.recent_queries,
        )
        self._persist()
        return enabled

    def favorite_pages(self) -> tuple[HelpPage, ...]:
        return tuple(
            self.repository.get(page_id)
            for page_id in sorted(self._state.favorite_page_ids)
            if page_id in {page.page_id for page in self.repository.pages}
        )

    def _persist(self) -> None:
        self.store.save_help_state(self._state)


def _prepend_unique(value: str, current: Sequence[str], limit: int) -> tuple[str, ...]:
    return (value, *(item for item in current if item != value))[:limit]
