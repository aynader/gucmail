"""Account-local contact presentation without changing contact identity or fields."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from gucmail.errors import ConfigurationError
from gucmail.models import ContactInfo, to_primitive
from gucmail.security import sanitize_terminal
from gucmail.services.pim import PimPage

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

CONTACT_DISPLAY_SETTINGS = (
    "contacts.sort_order",
    "contacts.display_name_format",
    "contacts.duplicate_threshold",
)


def validate_duplicate_threshold(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ConfigurationError("Duplicate threshold must be an integer between 1 and 100.")
    return value


@dataclass(frozen=True, slots=True)
class ContactsDisplayPreferences:
    sort_order: str = "display_name"
    display_name_format: str = "stored"
    duplicate_threshold: int = 70

    def __post_init__(self) -> None:
        if self.sort_order not in {"display_name", "family_name", "company"}:
            raise ConfigurationError("Choose display name, family name or company sorting.")
        if self.display_name_format not in {"stored", "given_family", "family_given"}:
            raise ConfigurationError("Choose stored, given-family or family-given display names.")
        validate_duplicate_threshold(self.duplicate_threshold)

    @classmethod
    def from_settings(
        cls, service: SettingsService, *, threshold_override: int | None = None
    ) -> ContactsDisplayPreferences:
        keys: tuple[str, ...] = CONTACT_DISPLAY_SETTINGS
        if threshold_override is not None:
            validate_duplicate_threshold(threshold_override)
            keys = keys[:2]
        values = {row.spec.setting_id: row.effective_value for row in service.snapshot(keys).values}
        return cls(
            str(values["contacts.sort_order"]),
            str(values["contacts.display_name_format"]),
            threshold_override
            if threshold_override is not None
            else values["contacts.duplicate_threshold"],
        )

    def label(self, contact: ContactInfo) -> str:
        given = " ".join(
            part.strip()
            for part in (contact.given_name, contact.middle_name)
            if part and part.strip()
        )
        family = (contact.family_name or "").strip()
        full_name = " ".join(part for part in (given, family) if part)
        if self.display_name_format == "given_family":
            preferred = full_name
        elif self.display_name_format == "family_given":
            preferred = ", ".join(part for part in (family, given) if part)
        else:
            preferred = contact.display_name.strip()
        fallback = contact.display_name.strip() or full_name
        return sanitize_terminal(preferred or fallback or "(unnamed contact)", max_length=180)

    def sort(self, contacts: Iterable[ContactInfo]) -> tuple[ContactInfo, ...]:
        def key(contact: ContactInfo) -> tuple[str, str, str]:
            label = self.label(contact)
            primary = (
                label
                if self.sort_order == "display_name"
                else str(getattr(contact, self.sort_order) or "")
            )
            return sanitize_terminal(primary).casefold(), label.casefold(), contact.reference

        return tuple(sorted(contacts, key=key))

    def page(self, page: PimPage[ContactInfo]) -> PimPage[ContactInfo]:
        """Sort only this candidate page, retaining every continuation field."""
        return replace(page, items=self.sort(page.items))

    def human_contact(self, contact: ContactInfo) -> dict[str, Any]:
        """Project a readable name while preserving original metadata separately."""
        result = dict(to_primitive(contact))
        result["stored_display_name"] = result["display_name"]
        result["display_name"] = self.label(contact)
        return result

    def human_data(self, value: Any) -> Any:
        if isinstance(value, ContactInfo):
            return self.human_contact(value)
        if isinstance(value, PimPage):
            result = dict(to_primitive(value))
            result["items"] = self.human_data(value.items)
            return result
        if isinstance(value, (tuple, list)):
            if all(isinstance(item, ContactInfo) for item in value):
                return [
                    {
                        "reference": contact.reference,
                        "name": self.label(contact),
                        "email": ", ".join(address.email for address in contact.emails),
                        "company": contact.company or "",
                    }
                    for contact in value
                ]
            return [self.human_data(item) for item in value]
        # Ranked search results keep explicit fuzzy ranking and scores.
        if isinstance(getattr(value, "item", None), ContactInfo):
            result = dict(to_primitive(value))
            result["item"] = self.human_contact(value.item)
            return result
        return value
