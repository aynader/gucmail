"""Textual adapter over the provider-independent local recipient policy."""

from textual.suggester import Suggester

from gucmail.services.recipient_assistance import RecipientPreferences


class RecipientSuggester(Suggester):
    def __init__(self, preferences: RecipientPreferences) -> None:
        # Preserve already typed names/addresses/case instead of casefolding the input.
        super().__init__(case_sensitive=True)
        self.preferences = preferences

    async def get_suggestion(self, value: str) -> str | None:
        return self.preferences.suggest(value)
