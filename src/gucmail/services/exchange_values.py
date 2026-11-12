"""Pure mappings between stable PIM values and Exchange indexed choices.

These helpers deliberately do not import exchangelib.  They can therefore be used by
services, import previews, adapter mapping code, and unit tests without constructing an
account or touching the network.
"""

from __future__ import annotations

import re

from gucmail.errors import ConfigurationError


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


_LOCAL_AVAILABILITY = {
    "free": "free",
    "workingelsewhere": "working_elsewhere",
    "tentative": "tentative",
    "busy": "busy",
    "outofoffice": "out_of_office",
    "oof": "out_of_office",
}
_EWS_AVAILABILITY = {
    "free": "Free",
    "working_elsewhere": "WorkingElsewhere",
    "tentative": "Tentative",
    "busy": "Busy",
    "out_of_office": "OOF",
}


def normalize_availability(value: str) -> str:
    """Normalize local or EWS availability spelling to the stable domain value."""

    try:
        return _LOCAL_AVAILABILITY[_key(value)]
    except (AttributeError, KeyError) as exc:
        raise ConfigurationError(
            "Availability must be free, working_elsewhere, tentative, busy, or out_of_office."
        ) from exc


def to_exchange_availability(value: str) -> str:
    """Return the exact ``LegacyFreeBusyStatus`` spelling expected by EWS."""

    return _EWS_AVAILABILITY[normalize_availability(value)]


_PHONE_LABELS = {
    "assistant": "AssistantPhone",
    "assistantphone": "AssistantPhone",
    "businessfax": "BusinessFax",
    "workfax": "BusinessFax",
    "business": "BusinessPhone",
    "businessphone": "BusinessPhone",
    "work": "BusinessPhone",
    "workphone": "BusinessPhone",
    "businessphone2": "BusinessPhone2",
    "workphone2": "BusinessPhone2",
    "callback": "Callback",
    "car": "CarPhone",
    "carphone": "CarPhone",
    "company": "CompanyMainPhone",
    "companymain": "CompanyMainPhone",
    "companymainphone": "CompanyMainPhone",
    "homefax": "HomeFax",
    "home": "HomePhone",
    "homephone": "HomePhone",
    "homephone2": "HomePhone2",
    "isdn": "Isdn",
    "cell": "MobilePhone",
    "cellphone": "MobilePhone",
    "mobile": "MobilePhone",
    "mobilephone": "MobilePhone",
    "otherfax": "OtherFax",
    "fax": "OtherFax",
    "other": "OtherTelephone",
    "otherphone": "OtherTelephone",
    "othertelephone": "OtherTelephone",
    "pager": "Pager",
    "primary": "PrimaryPhone",
    "primaryphone": "PrimaryPhone",
    "radio": "RadioPhone",
    "radiophone": "RadioPhone",
    "telex": "Telex",
    "ttytdd": "TtyTddPhone",
    "ttytddphone": "TtyTddPhone",
}

_POSTAL_LABELS = {
    "business": "Business",
    "work": "Business",
    "office": "Business",
    "home": "Home",
    "other": "Other",
}


def exchange_phone_label(value: str) -> str:
    """Map human/vCard phone labels to an exact EWS indexed-property label."""

    try:
        return _PHONE_LABELS[_key(value)]
    except (AttributeError, KeyError) as exc:
        raise ConfigurationError(
            f"Unsupported contact phone label {value!r}; use home, work, mobile, fax, or other."
        ) from exc


def exchange_postal_label(value: str) -> str:
    """Map human/vCard postal labels to Business, Home, or Other."""

    try:
        return _POSTAL_LABELS[_key(value)]
    except (AttributeError, KeyError) as exc:
        raise ConfigurationError(
            f"Unsupported contact postal label {value!r}; use home, work, or other."
        ) from exc


def exchange_email_label(value: str | int) -> str:
    """Normalize an email slot to Exchange's ``EmailAddress1`` .. ``3`` labels."""

    if isinstance(value, bool):
        raise ConfigurationError("Contact email slot must be 1, 2, or 3.")
    if isinstance(value, int):
        slot = value
    else:
        key = _key(value)
        aliases = {
            "1": 1,
            "email": 1,
            "email1": 1,
            "emailaddress1": 1,
            "primary": 1,
            "2": 2,
            "email2": 2,
            "emailaddress2": 2,
            "secondary": 2,
            "3": 3,
            "email3": 3,
            "emailaddress3": 3,
            "tertiary": 3,
        }
        try:
            slot = aliases[key]
        except KeyError as exc:
            raise ConfigurationError("Contact email slot must be 1, 2, or 3.") from exc
    if slot not in (1, 2, 3):
        raise ConfigurationError("Contact email slot must be 1, 2, or 3.")
    return f"EmailAddress{slot}"


__all__ = [
    "exchange_email_label",
    "exchange_phone_label",
    "exchange_postal_label",
    "normalize_availability",
    "to_exchange_availability",
]
