"""Owned current-user desktop registration and read-only activation identity.

Inspection and preview never register a protocol or display a notification. Applying
a reviewed plan is explicit and rechecks every owned registry key and manifest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gucmail.errors import ConfigurationError, ErrorContext
from gucmail.security import redact_text, sanitize_terminal

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext

OWNER = "GUC Mail CLI desktop v1"
MANIFEST_NAME = "desktop-registration.json"
MAX_MANIFEST_BYTES = 16_384


def account_fingerprint(email: str, endpoint: str) -> str:
    if not email.strip():
        raise ConfigurationError("Configure the account email before desktop setup.")
    identity = email.strip().casefold() + "\n" + endpoint.strip().rstrip("/").casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DesktopIdentity:
    config: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    executable: Path
    account: str

    def __post_init__(self) -> None:
        from gucmail.notification_providers.windows import make_click_token

        make_click_token("m-validation", account_key=self.account)
        for name in ("config", "data_dir", "cache_dir", "log_dir", "executable"):
            path = getattr(self, name)
            if not path.is_absolute() or any(c in str(path) for c in ("\x00", "\n", "\r", '"')):
                raise ConfigurationError(
                    "Desktop setup requires absolute, valid application paths."
                )

    @classmethod
    def from_context(cls, context: AppContext) -> DesktopIdentity:
        executable = Path(sys.executable).resolve()
        if executable.name.casefold() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        return cls(
            context.paths.config_file.resolve(),
            context.paths.data_dir.resolve(),
            context.paths.cache_dir.resolve(),
            context.paths.log_dir.resolve(),
            executable,
            account_fingerprint(context.config.email or "", context.config.endpoint),
        )

    @property
    def manifest(self) -> Path:
        return self.data_dir / MANIFEST_NAME

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "config": str(self.config),
            "data_dir": str(self.data_dir),
            "cache_dir": str(self.cache_dir),
            "log_dir": str(self.log_dir),
            "executable": str(self.executable),
            "account": self.account,
        }

    @property
    def content(self) -> bytes:
        return (json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def app_id(self) -> str:
        return "GUC.MailCLI." + self.fingerprint[:24]

    @property
    def scheme(self) -> str:
        return "gucmail-" + self.fingerprint[:24]

    @property
    def registry_values(self) -> dict[str, dict[str, str]]:
        app = rf"Software\Classes\AppUserModelId\{self.app_id}"
        protocol = rf"Software\Classes\{self.scheme}"
        # ShellExecute launches Python directly. The URI is the last single
        # argument; toast_entry rejects extra argv rather than treating it as CLI.
        command = (
            subprocess.list2cmdline(
                [str(self.executable), "-I", "-m", "gucmail.toast_entry", str(self.manifest)]
            )
            + ' "%1"'
        )
        return {
            app: {
                "GUCmailOwner": OWNER,
                "GUCmailIdentity": self.fingerprint,
                "DisplayName": "GUC Mail",
            },
            protocol: {
                "GUCmailOwner": OWNER,
                "GUCmailIdentity": self.fingerprint,
                "": "URL:GUC Mail notification",
                "URL Protocol": "",
            },
            protocol + r"\shell": {},
            protocol + r"\shell\open": {},
            protocol + r"\shell\open\command": {"": command},
        }


class DesktopRegistry(Protocol):
    def read(self, paths: tuple[str, ...]) -> dict[str, dict[str, str] | None]: ...
    def write(self, values: Mapping[str, Mapping[str, str]]) -> None: ...
    def remove(self, paths: tuple[str, ...]) -> None: ...


class CurrentUserDesktopRegistry:
    """Only the exact application-owned HKCU keys are eligible for changes."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ConfigurationError("Native desktop setup is available on Windows only.")
        import winreg

        self.api = winreg

    def read(self, paths: tuple[str, ...]) -> dict[str, dict[str, str] | None]:
        api = self.api
        result: dict[str, dict[str, str] | None] = {}
        expected = set(paths)
        for path in paths:
            try:
                with api.OpenKey(api.HKEY_CURRENT_USER, path, 0, api.KEY_READ) as key:
                    subkeys, count, _ = api.QueryInfoKey(key)
                    if count > 20 or subkeys > 5:
                        raise ConfigurationError("Desktop registration contains unexpected data.")
                    for index in range(subkeys):
                        if path + "\\" + api.EnumKey(key, index) not in expected:
                            raise ConfigurationError(
                                "Desktop registration has an unowned child key."
                            )
                    values: dict[str, str] = {}
                    for index in range(count):
                        name, value, kind = api.EnumValue(key, index)
                        if kind != api.REG_SZ or not isinstance(value, str):
                            raise ConfigurationError(
                                "Desktop registration contains an unexpected value type."
                            )
                        values[name] = value
                    result[path] = values
            except FileNotFoundError:
                result[path] = None
        return result

    def write(self, values: Mapping[str, Mapping[str, str]]) -> None:
        api = self.api
        for path, entries in values.items():
            with api.CreateKeyEx(api.HKEY_CURRENT_USER, path, 0, api.KEY_WRITE) as key:
                for name, value in entries.items():
                    api.SetValueEx(key, name, 0, api.REG_SZ, value)

    def remove(self, paths: tuple[str, ...]) -> None:
        for path in sorted(paths, key=lambda p: p.count("\\"), reverse=True):
            self.api.DeleteKey(self.api.HKEY_CURRENT_USER, path)


@dataclass(frozen=True, slots=True)
class DesktopInspection:
    known: bool
    installed: bool
    owned: bool
    keys: dict[str, dict[str, str] | None]
    manifest_content: bytes | None
    detail: str


@dataclass(frozen=True, slots=True)
class DesktopSetupPreview:
    identity: DesktopIdentity
    inspection: DesktopInspection
    operation: str
    can_apply: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "can_apply": self.can_apply,
            "installed": self.inspection.installed if self.inspection.known else None,
            "state_known": self.inspection.known,
            "owned": self.inspection.owned,
            "reason": self.reason,
            "detail": self.inspection.detail,
            "app_id": self.identity.app_id,
            "scheme": self.identity.scheme,
            "manifest": str(self.identity.manifest),
            "scope": "Current user only; no administrator rights or mailbox operations.",
            "registry_values": self.identity.registry_values,
            "native_display_verified": False,
            "native_activation_verified": False,
        }


class WindowsDesktopService:
    def __init__(self, identity: DesktopIdentity, registry: DesktopRegistry | None = None) -> None:
        self.identity = identity
        self.registry = registry

    def _registry(self) -> DesktopRegistry:
        if self.registry is None:
            self.registry = CurrentUserDesktopRegistry()
        return self.registry

    def inspect(self) -> DesktopInspection:
        identity = self.identity
        try:
            keys = self._registry().read(tuple(identity.registry_values))
            path = identity.manifest
            if path.is_symlink():
                raise ConfigurationError("Desktop registration manifest may not be a link.")
            if path.exists():
                if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
                    raise ConfigurationError(
                        "Desktop registration manifest is invalid or too large."
                    )
                content = path.read_bytes()
            else:
                content = None
            present = {key: value for key, value in keys.items() if value is not None}
            expected = identity.registry_values
            # Only exact known values, or subsets with a matching owner marker,
            # may be repaired. Unknown extras are never overwritten or deleted.
            owned = all(
                key in expected and all(expected[key].get(k) == v for k, v in values.items())
                for key, values in present.items()
            ) and (
                not present
                or any(values.get("GUCmailOwner") == OWNER for values in present.values())
            )
            roots = tuple(expected)[:2]
            owned = owned and all(
                values is None
                or (
                    values.get("GUCmailOwner") == OWNER
                    and values.get("GUCmailIdentity") == identity.fingerprint
                )
                for values in (keys.get(root) for root in roots)
            )
            if any(keys.get(key) is not None for key in tuple(expected)[2:]):
                owned = owned and keys.get(roots[1]) is not None
            owned = owned and content in {None, identity.content}
            installed = keys == expected and content == identity.content
            detail = (
                "Ready"
                if installed
                else "Not configured"
                if not present and content is None
                else "Incomplete owned setup"
                if owned
                else "Conflicting registration; existing data preserved"
            )
            return DesktopInspection(True, installed, owned, keys, content, detail)
        except (OSError, ConfigurationError) as exc:
            return DesktopInspection(
                False,
                False,
                False,
                {},
                None,
                redact_text(sanitize_terminal(str(exc), max_length=600)),
            )

    def preview(self, operation: str = "setup") -> DesktopSetupPreview:
        if operation not in {"setup", "remove"}:
            raise ConfigurationError("Desktop operation must be setup or remove.")
        inspection = self.inspect()
        can_apply = inspection.known and inspection.owned
        reason = inspection.detail
        if not self.identity.executable.is_file() and operation == "setup":
            can_apply, reason = False, "The configured Python executable is unavailable."
        return DesktopSetupPreview(self.identity, inspection, operation, can_apply, reason)

    def apply(self, preview: DesktopSetupPreview, *, confirmed: bool) -> dict[str, object]:
        if not confirmed:
            raise ConfigurationError("Desktop setup requires explicit confirmation.")
        if preview.identity != self.identity or not preview.can_apply:
            raise ConfigurationError("The desktop preview is blocked or belongs to another setup.")
        current = self.preview(preview.operation)
        if not current.can_apply or current.inspection != preview.inspection:
            raise ConfigurationError(
                "Desktop registration changed after preview. Refresh and review again."
            )
        identity = self.identity
        try:
            if preview.operation == "setup":
                if current.inspection.installed:
                    return {"applied": False, "operation": "setup", "installed": True}
                # Exclusive first write prevents replacing a newly appeared file.
                if current.inspection.manifest_content is None:
                    identity.manifest.parent.mkdir(parents=True, exist_ok=True)
                    with identity.manifest.open("xb") as handle:
                        handle.write(identity.content)
                self._registry().write(identity.registry_values)
            else:
                present = tuple(
                    key for key, value in current.inspection.keys.items() if value is not None
                )
                if not present and current.inspection.manifest_content is None:
                    return {"applied": False, "operation": "remove", "installed": False}
                self._registry().remove(present)
                if current.inspection.manifest_content is not None:
                    if (
                        identity.manifest.is_symlink()
                        or identity.manifest.read_bytes() != identity.content
                    ):
                        raise ConfigurationError(
                            "The desktop manifest changed during removal; file preserved."
                        )
                    identity.manifest.unlink()
        except (OSError, ConfigurationError) as exc:
            raise ConfigurationError(
                "Desktop changes did not finish; some owned entries may have changed. "
                "Inspect the setup before retrying. "
                + redact_text(sanitize_terminal(str(exc), max_length=400)),
                context=ErrorContext(
                    retry_safe=False,
                    mutation_uncertain=True,
                    suggestion="Inspect desktop setup before retrying.",
                ),
            ) from exc
        after = self.inspect()
        complete = after.known and (
            after.installed
            if preview.operation == "setup"
            else not any(value is not None for value in after.keys.values())
            and after.manifest_content is None
        )
        if not complete:
            raise ConfigurationError(
                "Desktop changes could not be verified. Inspect before retrying; "
                "partial changes may remain.",
                context=ErrorContext(
                    retry_safe=False,
                    mutation_uncertain=True,
                    suggestion="Inspect desktop setup before retrying.",
                ),
            )
        return {"applied": True, "operation": preview.operation, "installed": after.installed}


def read_desktop_identity(manifest: Path) -> DesktopIdentity:
    path = manifest.resolve()
    if manifest.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ConfigurationError("Desktop activation manifest is invalid.")
    try:
        payload: Any = json.loads(path.read_bytes())
        fields = {"version", "config", "data_dir", "cache_dir", "log_dir", "executable", "account"}
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or not all(isinstance(payload[key], str) for key in fields - {"version"})
        ):
            raise ValueError("invalid manifest")
        identity = DesktopIdentity(
            Path(payload["config"]),
            Path(payload["data_dir"]),
            Path(payload["cache_dir"]),
            Path(payload["log_dir"]),
            Path(payload["executable"]),
            payload["account"],
        )
        if path != identity.manifest or path.read_bytes() != identity.content:
            raise ValueError("noncanonical manifest")
        return identity
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ConfigurationError("Desktop activation manifest is invalid.") from exc


def activation_reference(identity: DesktopIdentity, uri: str) -> str:
    from gucmail.notification_providers.windows import parse_click_token

    prefix = identity.scheme + ":"
    if not isinstance(uri, str) or not uri.startswith(prefix) or len(uri) > 1_100:
        raise ConfigurationError("Desktop activation URI does not match this setup.")
    try:
        activation = parse_click_token(uri[len(prefix) :])
    except ValueError as exc:
        raise ConfigurationError("Desktop activation token is invalid.") from exc
    if activation.account_key != identity.account:
        raise ConfigurationError("Desktop activation belongs to another account.")
    return activation.arguments[-1]


def desktop_notification_provider(context: AppContext, *, provider_id: str | None = None) -> Any:
    """Construct the same desktop adapter for CLI, watcher and Activity tests."""
    from gucmail.notification_providers.core import PrivacyMode
    from gucmail.notification_providers.windows import (
        ToastDuration,
        ToastSound,
        WindowsToastProvider,
        WindowsToastSettings,
        discover_windows_toast_backend,
    )
    from gucmail.services.notification_configuration import NotificationConfigurationService
    from gucmail.services.notifications import DesktopNotificationProvider

    options: dict[str, Any] = {}
    if provider_id is not None:
        metadata = NotificationConfigurationService(context.state).providers.get_provider(
            provider_id
        )
        if metadata is None or metadata.kind.casefold() != "desktop":
            raise ConfigurationError("The selected desktop provider does not exist.")
        options = dict(metadata.metadata)
    title = options.get("title", "GUC Mail")
    sound = options.get("sound", True)
    duration = options.get("duration", "short")
    grouping = options.get("group_by_folder", True)
    attachment = options.get("show_attachment_indicator", True)
    importance = options.get("show_importance_indicator", True)
    if not isinstance(title, str) or any(
        type(value) is not bool for value in (sound, grouping, attachment, importance)
    ):
        raise ConfigurationError(
            "Desktop title must be text; sound, grouping and indicators must be boolean."
        )
    try:
        settings = WindowsToastSettings(
            title=title,
            privacy=PrivacyMode.FULL,
            sound=ToastSound.DEFAULT if sound else ToastSound.SILENT,
            duration=ToastDuration(duration),
            group_by_folder=grouping,
            show_attachment_indicator=attachment,
            show_importance_indicator=importance,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Desktop title or duration is invalid.") from exc
    if sys.platform != "win32":
        return DesktopNotificationProvider(
            redact=context.config.redact_email_addresses, allow_sound=sound
        )
    identity = DesktopIdentity.from_context(context) if context.config.email else None
    backend, status = discover_windows_toast_backend(identity=identity)

    def known_local_message(reference: str) -> bool:
        if identity is None or context.state.account_key != identity.account:
            return False
        mapping = context.state.resolve_reference(reference, item_kind="message")
        return mapping is not None and mapping["item_kind"] == "message"

    # Explicit unavailable discovery must not trigger a second discovery in the
    # provider constructor; its result remains a truthful skipped delivery.
    provider = WindowsToastProvider(
        backend,
        settings,
        backend_status=status,
        activation_account=(context.config.email or "", identity.account)
        if identity is not None
        else None,
        activation_reference_validator=known_local_message,
    )
    return DesktopNotificationProvider(
        redact=context.config.redact_email_addresses,
        windows_provider=provider,
    )


def desktop_setup_service(context: AppContext, operation: str = "setup") -> WindowsDesktopService:
    identity = DesktopIdentity.from_context(context)
    if operation == "remove" and identity.manifest.exists():
        previous = read_desktop_identity(identity.manifest)
        if any(
            getattr(previous, field) != getattr(identity, field)
            for field in ("config", "data_dir", "cache_dir", "log_dir", "account")
        ):
            raise ConfigurationError("The desktop manifest belongs to another application context.")
        # An upgrade may replace Python. The exact previously owned registration
        # remains removable without requiring its old executable to exist.
        identity = previous
    return WindowsDesktopService(identity)
