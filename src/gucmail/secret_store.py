"""Namespaced secret references with keyring and deterministic test backends.

The application stores only :class:`SecretReference` values in ordinary configuration or
SQLite rows.  Secret bytes remain behind a deliberately small backend surface.  Backend
exceptions are translated to stable messages because keyring implementations are external
code and their exception text is not safe application output.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import quote, unquote, urlsplit

from gucmail.constants import APP_SLUG
from gucmail.errors import CredentialError

SECRET_REFERENCE_PREFIX = "keyring://"  # noqa: S105 - URI scheme, not a credential
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SecretStoreUnavailableError(CredentialError):
    """The configured secure storage backend cannot currently be used."""


class SecretStoreVerificationError(CredentialError):
    """A secret was written but could not be read back exactly."""


@dataclass(frozen=True, slots=True)
class SecretReference:
    """An opaque keyring locator that is safe to persist but not reveal verbatim."""

    uri: str = field(repr=False)

    @classmethod
    def parse(cls, value: str) -> SecretReference:
        raw = str(value).strip()
        if len(raw) > 1_024 or _CONTROL_RE.search(raw):
            raise CredentialError("The secure secret reference is invalid.")
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() != "keyring" or not parsed.netloc:
            raise CredentialError("Secure values must use a keyring:// reference.")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise CredentialError("The secure secret reference is invalid.")
        if not cls._segments(raw):
            raise CredentialError("The secure secret reference is incomplete.")
        return cls(raw)

    @staticmethod
    def _segments(value: str) -> tuple[str, ...]:
        parsed = urlsplit(value)
        values = (parsed.netloc, *(part for part in parsed.path.split("/") if part))
        return tuple(unquote(part) for part in values if part)

    @property
    def segments(self) -> tuple[str, ...]:
        return self._segments(self.uri)

    @property
    def is_namespaced(self) -> bool:
        return len(self.segments) == 3

    @property
    def resolver_key(self) -> str:
        """Return the legacy callback key without ever including it in ``repr``."""

        return self.uri[len(SECRET_REFERENCE_PREFIX) :]

    def redacted(self) -> str:
        return "keyring://<redacted>"

    def to_safe_primitive(self) -> str:
        return self.redacted()

    def __str__(self) -> str:
        return self.uri

    def __repr__(self) -> str:
        return "SecretReference('keyring://<redacted>')"


class SecretBackend(Protocol):
    """Minimal backend contract shared by system keyrings and in-memory tests."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringSecretBackend:
    """Lazy adapter over the installed ``keyring`` package."""

    @staticmethod
    def _backend() -> SecretBackend:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency is present in normal installs
            raise SecretStoreUnavailableError(
                "Secure secret storage is unavailable because keyring support is not installed."
            ) from exc
        return cast(SecretBackend, keyring)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._backend().get_password(service_name, username)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._backend().set_password(service_name, username, password)

    def delete_password(self, service_name: str, username: str) -> None:
        self._backend().delete_password(service_name, username)


@dataclass(slots=True)
class InMemorySecretBackend:
    """Deterministic backend for tests; it never calls an operating-system keyring."""

    available: bool = True
    _values: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def _ensure_available(self) -> None:
        if not self.available:
            raise RuntimeError("synthetic backend unavailable")

    def get_password(self, service_name: str, username: str) -> str | None:
        self._ensure_available()
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._ensure_available()
        self._values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self._ensure_available()
        try:
            del self._values[(service_name, username)]
        except KeyError as exc:
            raise KeyError("synthetic secret not found") from exc

    def contains(self, service_name: str, username: str) -> bool:
        """Inspect test state without returning a secret value."""

        return (service_name, username) in self._values


class SecretStore:
    """A namespace-scoped secret store that emits stable opaque references."""

    def __init__(
        self,
        namespace: str,
        *,
        backend: SecretBackend | None = None,
        application: str = APP_SLUG,
    ) -> None:
        if not _COMPONENT_RE.fullmatch(application) or not _COMPONENT_RE.fullmatch(namespace):
            raise CredentialError("Secret-store application and namespace names are invalid.")
        self.application = application
        self.namespace = namespace
        self.backend = backend or KeyringSecretBackend()
        self.service_name = f"{application}/{namespace}"

    @staticmethod
    def _validate_name(name: str) -> str:
        clean = str(name).strip()
        if not clean or len(clean) > 256 or _CONTROL_RE.search(clean):
            raise CredentialError("The secure secret name is invalid.")
        return clean

    def reference(self, name: str) -> SecretReference:
        clean = self._validate_name(name)
        return SecretReference.parse(
            f"{SECRET_REFERENCE_PREFIX}{quote(self.application, safe='')}/"
            f"{quote(self.namespace, safe='')}/{quote(clean, safe='')}"
        )

    def _name_from_reference(self, reference: SecretReference) -> str:
        segments = reference.segments
        if len(segments) != 3:
            raise CredentialError("A legacy secret reference cannot be used by this namespace.")
        application, namespace, name = segments
        if application != self.application or namespace != self.namespace:
            raise CredentialError("The secret reference belongs to a different namespace.")
        return self._validate_name(name)

    @staticmethod
    def _unavailable(exc: BaseException) -> SecretStoreUnavailableError:
        return SecretStoreUnavailableError(
            "Secure secret storage is unavailable; no plaintext fallback was used."
        )

    def put(self, name: str, value: str) -> SecretReference:
        clean = self._validate_name(name)
        if not value:
            raise CredentialError("Refusing to store an empty secret.")
        try:
            self.backend.set_password(self.service_name, clean, value)
        except Exception as exc:
            raise self._unavailable(exc) from exc
        return self.reference(clean)

    def get(self, reference_or_name: SecretReference | str) -> str | None:
        if isinstance(reference_or_name, SecretReference):
            name = self._name_from_reference(reference_or_name)
        elif str(reference_or_name).startswith(SECRET_REFERENCE_PREFIX):
            name = self._name_from_reference(SecretReference.parse(str(reference_or_name)))
        else:
            name = self._validate_name(str(reference_or_name))
        try:
            return self.backend.get_password(self.service_name, name)
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def delete(self, reference_or_name: SecretReference | str) -> bool:
        if isinstance(reference_or_name, SecretReference):
            name = self._name_from_reference(reference_or_name)
        elif str(reference_or_name).startswith(SECRET_REFERENCE_PREFIX):
            name = self._name_from_reference(SecretReference.parse(str(reference_or_name)))
        else:
            name = self._validate_name(str(reference_or_name))
        if self.get(name) is None:
            return False
        try:
            self.backend.delete_password(self.service_name, name)
        except Exception as exc:
            raise self._unavailable(exc) from exc
        return True

    def put_and_verify(self, name: str, value: str) -> SecretReference:
        """Copy a secret and verify exact read-back before callers discard the source."""

        reference = self.put(name, value)
        recovered = self.get(reference)
        if recovered is None or not hmac.compare_digest(recovered, value):
            raise SecretStoreVerificationError(
                "Secure secret storage could not verify the copied value; the source is unchanged."
            )
        return reference

    def resolve(self, reference: str) -> str | None:
        """Callback-compatible resolver for webhook providers."""

        value = str(reference)
        if value.startswith(SECRET_REFERENCE_PREFIX):
            return self.get(SecretReference.parse(value))
        return self.get(value)


__all__ = [
    "SECRET_REFERENCE_PREFIX",
    "InMemorySecretBackend",
    "KeyringSecretBackend",
    "SecretBackend",
    "SecretReference",
    "SecretStore",
    "SecretStoreUnavailableError",
    "SecretStoreVerificationError",
]
