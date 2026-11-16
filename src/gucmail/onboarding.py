"""Moving a legacy creds.env password into the OS keyring.

First sign-in itself lives in gucmail.services.sign_in.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from gucmail.bootstrap import AppContext
from gucmail.credentials import parse_legacy_credentials
from gucmail.errors import CredentialError


def migrate_legacy_to_keyring(context: AppContext, path: Path) -> tuple[str, ...]:
    """Explicitly migrate, but never delete, the source file automatically."""

    parsed = parse_legacy_credentials(path)
    email = parsed.values["email"].strip()
    if not email:
        raise CredentialError("The legacy credential file contains an empty account email.")
    configured_email = (context.config.email or "").strip()
    if configured_email and configured_email.casefold() != email.casefold():
        raise CredentialError(
            "The configured account does not match the legacy credential file. "
            "No credential was copied."
        )
    context.credential_resolver.store.set(email, parsed.values["password"])
    updated = replace(
        context.config,
        email=email,
        username=parsed.values.get("username") or email,
    )
    context.config_store.save(updated)
    context.config = updated
    return (
        *parsed.warnings,
        "Credential source: legacy plaintext file (values not displayed).",
        "Credential copied to the OS keyring. Delete the plaintext file manually after "
        "verifying login.",
    )
