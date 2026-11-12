"""Credential acquisition without storing passwords in application configuration."""

from __future__ import annotations

import ast
import getpass
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from gucmail.config import AppConfig
from gucmail.constants import APP_SLUG
from gucmail.errors import CredentialError

LEGACY_LINE = re.compile(r"^\s*(email|username|password)\s*=\s*(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class CredentialData:
    email: str
    username: str
    password: str
    source: str
    warnings: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "CredentialData(email='<redacted>', username='<redacted>', "
            "password='<redacted>', source="
            + repr(self.source)
            + f", warning_count={len(self.warnings)})"
        )

    def safe_summary(self) -> dict[str, object]:
        """Describe resolution without returning account identifiers or secret values."""

        return {
            "source": self.source,
            "warnings": self.warnings,
            "email_configured": bool(self.email),
            "username_configured": bool(self.username),
        }


@dataclass(frozen=True, slots=True)
class LegacyCredentialResult:
    values: dict[str, str]
    warnings: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Keep accidental diagnostics from exposing plaintext legacy credentials."""

        keys = tuple(sorted(self.values))
        return f"LegacyCredentialResult(keys={keys!r}, warnings={self.warnings!r})"


def parse_legacy_credentials(path: Path) -> LegacyCredentialResult:
    """Parse only three quoted scalar assignments; never execute the file."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise CredentialError(f"Could not read legacy credential file: {path}") from exc
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LEGACY_LINE.fullmatch(line)
        if not match:
            raise CredentialError(
                f"Unsupported content on line {number} of the legacy credential file. "
                "Only email, username, and password assignments are accepted."
            )
        key, literal = match.groups()
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError) as exc:
            raise CredentialError(
                f"The value for {key} on line {number} must be a quoted string."
            ) from exc
        if not isinstance(value, str):
            raise CredentialError(f"The value for {key} must be a string.")
        values[key] = value
    missing = {"email", "password"} - values.keys()
    if missing:
        raise CredentialError(f"Legacy credential file is missing: {', '.join(sorted(missing))}.")
    warnings = _permission_warnings(path)
    return LegacyCredentialResult(values=values, warnings=tuple(warnings))


def _permission_warnings(path: Path) -> list[str]:
    warnings: list[str] = []
    try:
        mode = path.stat().st_mode
        if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append("The legacy credential file is accessible to group or other users.")
        if os.name == "nt":
            # A complete Windows ACL assessment needs pywin32. The file itself is deliberately
            # never opened by this check, and users still receive the plaintext warning.
            warnings.append(
                "The legacy credential file stores a plaintext password; migrate it to Windows "
                "Credential Manager and remove the file."
            )
    except OSError:
        warnings.append("Credential file permissions could not be inspected.")
    return warnings


class KeyringBackend(Protocol):
    """Small typed surface used from the dynamically selected keyring backend."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialStore:
    """Thin, failure-tolerant wrapper around the operating-system keyring."""

    def _keyring(self) -> KeyringBackend:
        try:
            import keyring
        except ImportError as exc:
            raise CredentialError("OS keyring support is not installed.") from exc
        return cast(KeyringBackend, keyring)

    def get(self, email: str) -> str | None:
        try:
            return self._keyring().get_password(APP_SLUG, email)
        except Exception as exc:
            raise CredentialError(
                f"The operating-system keyring is unavailable ({type(exc).__name__}); "
                "no plaintext fallback was used."
            ) from exc

    def set(self, email: str, password: str) -> None:
        if not password:
            raise CredentialError("Refusing to store an empty password.")
        try:
            self._keyring().set_password(APP_SLUG, email, password)
        except Exception as exc:
            raise CredentialError(
                f"Could not store the credential in the OS keyring ({type(exc).__name__})."
            ) from exc

    def delete(self, email: str) -> bool:
        try:
            keyring = self._keyring()
            if keyring.get_password(APP_SLUG, email) is None:
                return False
            keyring.delete_password(APP_SLUG, email)
            return True
        except Exception as exc:
            name = type(exc).__name__
            if name in {"PasswordDeleteError", "KeyringError"} and "not found" in str(exc).lower():
                return False
            raise CredentialError(
                f"Could not remove the credential from the OS keyring ({name})."
            ) from exc


class CredentialResolver:
    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def resolve(
        self,
        config: AppConfig,
        *,
        explicit_email: str | None = None,
        explicit_username: str | None = None,
        explicit_password: str | None = None,
        interactive: bool = False,
        legacy_path: Path | None = None,
        preferred_legacy_path: Path | None = None,
        force_legacy: bool = False,
        allow_legacy: bool = True,
        prompt: Callable[[str], str] = input,
        password_prompt: Callable[[str], str] = getpass.getpass,
    ) -> CredentialData:
        email = explicit_email or os.environ.get("GUCMAIL_EMAIL") or config.email
        username = explicit_username or os.environ.get("GUCMAIL_USERNAME") or config.username
        warnings: list[str] = []
        if legacy_path is not None and preferred_legacy_path is not None:
            try:
                same_path = legacy_path.resolve() == preferred_legacy_path.resolve()
            except OSError:
                same_path = legacy_path == preferred_legacy_path
            if not same_path:
                raise CredentialError("Two different legacy credential files were selected.")
        selected_legacy_path = preferred_legacy_path or legacy_path

        if force_legacy:
            if not allow_legacy:
                raise CredentialError("Legacy credential use is disabled for this operation.")
            if selected_legacy_path is None:
                raise CredentialError(
                    "Forced legacy credential use requires an explicitly selected file."
                )
            if not selected_legacy_path.is_file():
                raise CredentialError(
                    "The explicitly selected legacy credential file was not found."
                )
            parsed_legacy = parse_legacy_credentials(selected_legacy_path)
            legacy_email = parsed_legacy.values["email"].strip()
            if not legacy_email:
                raise CredentialError(
                    "The selected legacy credential file contains an empty account email."
                )
            if email and email.strip().casefold() != legacy_email.casefold():
                raise CredentialError(
                    "The configured account does not match the selected legacy credential file. "
                    "No credential was resolved."
                )
            forced_warnings = (
                *parsed_legacy.warnings,
                "An explicitly selected plaintext legacy credential file was used; values were "
                "not displayed.",
            )
            return CredentialData(
                email=legacy_email,
                username=parsed_legacy.values.get("username") or legacy_email,
                password=parsed_legacy.values["password"],
                source="legacy_file_explicit",
                warnings=tuple(dict.fromkeys(forced_warnings)),
            )

        if explicit_password is not None:
            source = "explicit"
            password = explicit_password
        elif "GUCMAIL_PASSWORD" in os.environ:
            source = "environment"
            password = os.environ["GUCMAIL_PASSWORD"]
        else:
            password = None
            source = ""

        if email and password is None:
            try:
                password = self.store.get(email)
                if password is not None:
                    source = "keyring"
            except CredentialError:
                if not interactive:
                    raise
                warnings.append(
                    "The operating-system keyring was unavailable; the credential was requested "
                    "for this process only."
                )

        if interactive:
            email = email or prompt("GUC email address: ").strip()
            username = (
                username
                or prompt("Login username [press Enter to use the email address]: ").strip()
            )
            if password is None:
                password = password_prompt("Password: ")
                source = "interactive"

        if allow_legacy and (not email or password is None):
            candidate = selected_legacy_path or Path.cwd() / "creds.env"
            if candidate.is_file():
                parsed_legacy = parse_legacy_credentials(candidate)
                legacy = parsed_legacy.values
                legacy_email = legacy["email"].strip()
                if not legacy_email:
                    raise CredentialError(
                        "The legacy credential file contains an empty account email."
                    )
                if email and email.strip().casefold() != legacy_email.casefold():
                    raise CredentialError(
                        "The configured account does not match the legacy credential file. "
                        "Refusing to combine one account identity with another account password."
                    )
                warnings.extend(parsed_legacy.warnings)
                email = email or legacy_email
                username = username or legacy.get("username")
                if password is None:
                    password = legacy.get("password")
                    source = "legacy_file"
            elif selected_legacy_path is not None:
                raise CredentialError(
                    "The explicitly selected legacy credential file was not found."
                )

        if not email:
            raise CredentialError(
                "No account email is configured. Run 'gucmail login' or set GUCMAIL_EMAIL."
            )
        if password is None or not password:
            raise CredentialError(
                "No password is available. Run 'gucmail login', use the OS keyring, or set "
                "GUCMAIL_PASSWORD for this process."
            )
        return CredentialData(
            email=email,
            username=username or email,
            password=password,
            source=source,
            warnings=tuple(dict.fromkeys(warnings)),
        )
