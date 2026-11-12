"""Guided, conservative first-run choices shared by CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from gucmail.bootstrap import AppContext
from gucmail.credentials import parse_legacy_credentials
from gucmail.errors import CredentialError


@dataclass(frozen=True, slots=True)
class OnboardingStatus:
    unofficial_notice: str
    legacy_credentials_detected: bool
    legacy_warning: str | None
    monitoring_enabled: bool
    attachment_indexing_enabled: bool
    semantic_search_enabled: bool
    complete: bool


def onboarding_status(context: AppContext, *, legacy_path: Path | None = None) -> OnboardingStatus:
    candidate = legacy_path or Path.cwd() / "creds.env"
    return OnboardingStatus(
        unofficial_notice=(
            "GUC Mail CLI is an independent client. It is not an official GUC or Microsoft product."
        ),
        legacy_credentials_detected=candidate.is_file(),
        legacy_warning=(
            "A plaintext legacy credential file was detected. Its contents have not been displayed."
            if candidate.is_file()
            else None
        ),
        monitoring_enabled=context.config.monitoring_enabled,
        attachment_indexing_enabled=context.config.index_attachments,
        semantic_search_enabled=context.config.semantic_search_enabled,
        complete=context.config.onboarding_complete,
    )


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


def complete_onboarding(context: AppContext, **choices: object) -> None:
    allowed = {
        "theme",
        "download_directory",
        "timezone",
        "mode",
        "cache_enabled",
        "monitoring_enabled",
        "index_attachments",
        "semantic_search_enabled",
    }
    invalid = set(choices) - allowed
    if invalid:
        raise ValueError(f"Unsupported onboarding choice(s): {', '.join(sorted(invalid))}")

    def text_choice(name: str, current: str) -> str:
        value = choices.get(name, current)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string.")
        return value

    def optional_text_choice(name: str, current: str | None) -> str | None:
        value = choices.get(name, current)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string or null.")
        return value

    def boolean_choice(name: str, current: bool) -> bool:
        value = choices.get(name, current)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false.")
        return value

    updated = replace(
        context.config,
        theme=text_choice("theme", context.config.theme),
        download_directory=optional_text_choice(
            "download_directory", context.config.download_directory
        ),
        timezone=text_choice("timezone", context.config.timezone),
        mode=text_choice("mode", context.config.mode),
        cache_enabled=boolean_choice("cache_enabled", context.config.cache_enabled),
        monitoring_enabled=boolean_choice("monitoring_enabled", context.config.monitoring_enabled),
        index_attachments=boolean_choice("index_attachments", context.config.index_attachments),
        semantic_search_enabled=boolean_choice(
            "semantic_search_enabled", context.config.semantic_search_enabled
        ),
        onboarding_complete=True,
    ).validate()
    context.config_store.save(updated)
    context.config = updated
