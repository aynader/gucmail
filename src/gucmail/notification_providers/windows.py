"""Windows toast request construction behind a small injectable backend protocol."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from gucmail.constants import APP_NAME
from gucmail.models import NotificationEvent
from gucmail.notification_providers.core import PayloadPolicy, PrivacyMode
from gucmail.security import sanitize_terminal
from gucmail.services.notifications import DeliveryResult, DeliveryState

if TYPE_CHECKING:
    from gucmail.services.windows_desktop import DesktopIdentity, WindowsDesktopService


class ToastDuration(StrEnum):
    SHORT = "short"
    LONG = "long"


class ToastSound(StrEnum):
    DEFAULT = "default"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class ToastActivation:
    """Opaque click token plus a shell-free argument vector for the app host."""

    token: str
    arguments: tuple[str, ...]
    account_key: str | None = None


@dataclass(frozen=True, slots=True)
class WindowsToastRequest:
    title: str
    body: str
    tag: str
    group: str
    activation: ToastActivation | None
    duration: ToastDuration
    sound: ToastSound
    importance: str
    constituent_event_ids: tuple[str, ...]


class WindowsToastBackend(Protocol):
    """Platform boundary implemented by a WinRT/desktop-extra integration."""

    def show(self, request: WindowsToastRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class WindowsBackendStatus:
    available: bool
    backend_name: str | None
    native_actions: bool
    click_to_open: bool
    limitation: str | None = None
    native_grouping: bool = False
    sound_control: bool = False
    native_display_verified: bool = False
    native_activation_verified: bool = False


class NativeWindowsToastBackend:
    """WinRT requests using only a verified identity owned by this application.

    Protocol activation targets the dedicated read-only entry point. Opening
    after the watcher exits still requires native Windows verification. API
    acceptance alone does not prove visible display or activation.
    """

    def __init__(self, api: Any, identity: DesktopIdentity, service: WindowsDesktopService) -> None:
        self.api = api
        self.identity = identity
        self.service = service
        self._toaster: Any | None = None

    def show(self, request: WindowsToastRequest) -> None:
        inspection = self.service.inspect()
        if not inspection.known or not inspection.installed:
            raise RuntimeError("Native desktop registration is unavailable. Review desktop setup.")
        launch: str | None = None
        if request.activation is not None:
            activation = parse_click_token(request.activation.token)
            if activation.account_key == self.identity.account:
                launch = self.identity.scheme + ":" + activation.token
        api = self.api
        toast = api.Toast(
            [request.title, request.body],
            audio=api.ToastAudio(silent=request.sound is ToastSound.SILENT),
            duration=api.ToastDuration.Long
            if request.duration is ToastDuration.LONG
            else api.ToastDuration.Short,
            group=request.group,
            launch_action=launch,
        )
        toast.tag = request.tag
        if self._toaster is None:
            self._toaster = api.InteractableWindowsToaster(
                APP_NAME, notifierAUMID=self.identity.app_id
            )
        self._toaster.show_toast(toast)


class PlyerWindowsToastBackend:
    """Small optional Windows backend using plyer's native notification facade.

    Plyer does not expose dependable activation callbacks or action buttons.  The request
    still carries a verified click token for richer future backends, but this backend does
    not claim click-to-open support.
    """

    def __init__(self, notification_api: Any) -> None:
        self._notification = notification_api

    def show(self, request: WindowsToastRequest) -> None:
        self._notification.notify(
            title=request.title,
            message=request.body,
            app_name=APP_NAME,
            timeout=12 if request.duration is ToastDuration.LONG else 8,
        )


def discover_windows_toast_backend(
    *,
    platform_name: str | None = None,
    notification_api: Any | None = None,
    identity: DesktopIdentity | None = None,
    desktop_service: WindowsDesktopService | None = None,
    native_api: Any | None = None,
) -> tuple[WindowsToastBackend | None, WindowsBackendStatus]:
    """Discover the optional Windows backend without overstating its capabilities."""

    selected_platform = platform_name or sys.platform
    if selected_platform != "win32":
        return None, WindowsBackendStatus(
            False,
            None,
            False,
            False,
            "Windows desktop notifications are only available on Windows.",
        )
    if identity is not None:
        from gucmail.services.windows_desktop import WindowsDesktopService

        service = desktop_service or WindowsDesktopService(identity)
        inspection = service.inspect()
        if inspection.known and inspection.installed:
            try:
                api = (
                    native_api
                    if native_api is not None
                    else importlib.import_module("windows_toasts")
                )
                if native_api is None and sys.getwindowsversion().build < 15063:
                    raise RuntimeError(
                        "The native backend requires Windows 10 build 15063 or newer."
                    )
                for name in ("Toast", "ToastAudio", "ToastDuration", "InteractableWindowsToaster"):
                    if not hasattr(api, name):
                        raise ImportError("Incomplete Windows-Toasts installation")
                return NativeWindowsToastBackend(api, identity, service), WindowsBackendStatus(
                    True,
                    "windows-toasts",
                    False,
                    True,
                    "Native API capabilities configured; visible display, sound and protocol "
                    "activation require local Windows validation. Open-message only; "
                    "read/reply action buttons are unavailable.",
                    native_grouping=True,
                    sound_control=True,
                )
            except (ImportError, RuntimeError, OSError, AttributeError):
                pass
    api = notification_api
    if api is None:
        try:
            api = importlib.import_module("plyer").notification
        except (ImportError, RuntimeError):
            return None, WindowsBackendStatus(
                False,
                None,
                False,
                False,
                "Install the optional desktop extra to enable Windows notifications.",
            )
    return PlyerWindowsToastBackend(api), WindowsBackendStatus(
        True,
        "plyer",
        False,
        False,
        "This backend displays Windows notifications but does not provide action buttons "
        "or click-to-open callbacks, native grouping, or sound control. Silent notifications "
        "are skipped; use notification history in Activity.",
    )


@dataclass(frozen=True, slots=True)
class WindowsToastSettings:
    enabled: bool = True
    title: str = "GUC Mail"
    privacy: PrivacyMode = PrivacyMode.SENDER_SUBJECT
    show_attachment_indicator: bool = True
    show_importance_indicator: bool = True
    sound: ToastSound = ToastSound.DEFAULT
    duration: ToastDuration = ToastDuration.SHORT
    group_by_folder: bool = True
    maximum_notifications_per_burst: int = 5

    def __post_init__(self) -> None:
        if not self.title.strip() or len(self.title) > 120:
            raise ValueError("Toast title must contain between 1 and 120 characters.")
        if not 1 <= self.maximum_notifications_per_burst <= 20:
            raise ValueError("Toast burst maximum must be between 1 and 20.")


def make_click_token(message_reference: str, *, account_key: str | None = None) -> ToastActivation:
    """Encode only a stable local reference, never a raw EWS identifier."""

    # An identity is validated, never sanitized, truncated or stripped. Rewriting
    # it could select a different message. The grammar also excludes CLI options.
    reference = message_reference
    if not isinstance(reference, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", reference
    ):
        raise ValueError("Message reference is invalid for toast activation.")
    if account_key is not None and (
        not isinstance(account_key, str) or not re.fullmatch(r"[0-9a-f]{64}", account_key)
    ):
        raise ValueError("Account identity is invalid for toast activation.")
    version = 2 if account_key is not None else 1
    payload: dict[str, object] = {"action": "open-message", "ref": reference, "version": version}
    if account_key is not None:
        payload["account"] = account_key
    envelope = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    token = f"gucmail.v{version}." + base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")
    return ToastActivation(token, ("messages", "show", reference), account_key)


def parse_click_token(token: str) -> ToastActivation:
    prefix = token[:11]
    if prefix not in {"gucmail.v1.", "gucmail.v2."} or len(token) > 1_024:
        raise ValueError("Toast activation token is invalid.")
    encoded = token[len(prefix) :]
    try:
        raw = base64.urlsafe_b64decode(encoded + ("=" * (-len(encoded) % 4)))
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Toast activation token is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != (2 if prefix == "gucmail.v2." else 1)
        or payload.get("action") != "open-message"
        or not isinstance(payload.get("ref"), str)
    ):
        raise ValueError("Toast activation token is invalid.")
    account = payload.get("account")
    if prefix == "gucmail.v2." and not isinstance(account, str):
        raise ValueError("Toast activation token is invalid.")
    activation = make_click_token(payload["ref"], account_key=account)
    if activation.token != token:
        raise ValueError("Toast activation token is not canonical.")
    return activation


def _sender(event: NotificationEvent) -> str | None:
    if event.sender is None:
        return None
    return sanitize_terminal(event.sender.name or event.sender.email, max_length=200)


def _toast_text(event: NotificationEvent, settings: WindowsToastSettings) -> tuple[str, str]:
    sender = _sender(event)
    subject = sanitize_terminal(event.subject or "", max_length=300)
    preview = sanitize_terminal(event.preview or "", max_length=500)
    if sender is None and event.subject is None and not preview:
        # The profile's privacy policy withheld sender and subject; never claim the
        # message has no subject when it was only hidden.
        title, lines = settings.title, ["A new message arrived."]
    elif settings.privacy is PrivacyMode.PRIVATE:
        title, lines = settings.title, ["A new message arrived."]
    elif settings.privacy is PrivacyMode.SENDER_ONLY:
        title, lines = settings.title, [f"New email from {sender}" if sender else "New email"]
    elif settings.privacy is PrivacyMode.METADATA_ONLY:
        title, lines = settings.title, ["A new mailbox event arrived."]
        if event.folder:
            lines.append(f"Folder: {sanitize_terminal(event.folder, max_length=120)}")
    elif settings.privacy is PrivacyMode.SENDER_SUBJECT:
        title = sender or settings.title
        lines = [subject or "(No subject)"]
    else:
        title = sender or settings.title
        lines = [subject or "(No subject)"]
        if preview:
            lines.append(preview)
    if settings.show_importance_indicator and event.importance.value == "high":
        lines.append("High importance")
    if settings.show_attachment_indicator and event.has_attachments:
        lines.append("Includes attachment(s)")
    return sanitize_terminal(title, max_length=120), "\n".join(lines)


def build_windows_toast(
    event: NotificationEvent,
    settings: WindowsToastSettings | None = None,
) -> WindowsToastRequest:
    settings = settings or WindowsToastSettings()
    title, body = _toast_text(event, settings)
    try:
        activation = make_click_token(event.message_reference) if event.message_reference else None
    except ValueError:
        # A bad opening target must not suppress an otherwise safe notification.
        activation = None
    group_material = (
        f"{event.account_id.casefold()}\x00{event.folder or ''}"
        if settings.group_by_folder
        else event.account_id.casefold()
    )
    group = hashlib.sha256(group_material.encode()).hexdigest()[:24]
    tag = hashlib.sha256(event.event_id.encode("utf-8", errors="replace")).hexdigest()[:32]
    return WindowsToastRequest(
        title,
        body,
        tag,
        group,
        activation,
        settings.duration,
        settings.sound,
        event.importance.value,
        (event.event_id,),
    )


def build_toast_burst(
    events: Sequence[NotificationEvent],
    settings: WindowsToastSettings | None = None,
) -> tuple[WindowsToastRequest, ...]:
    """Cap a burst and group the remainder into one privacy-safe summary toast."""

    settings = settings or WindowsToastSettings()
    if not events:
        return ()
    maximum = settings.maximum_notifications_per_burst
    if len(events) <= maximum:
        return tuple(build_windows_toast(event, settings) for event in events)
    individual_count = max(0, maximum - 1)
    requests = [build_windows_toast(event, settings) for event in events[:individual_count]]
    grouped = events[individual_count:]
    group_material = "\x00".join(event.event_id for event in grouped)
    summary = WindowsToastRequest(
        settings.title,
        f"{len(grouped)} additional new messages",
        hashlib.sha256(group_material.encode("utf-8", errors="replace")).hexdigest()[:32],
        "gucmail-burst",
        None,
        settings.duration,
        settings.sound,
        "normal",
        tuple(event.event_id for event in grouped),
    )
    requests.append(summary)
    return tuple(requests)


class WindowsToastProvider:
    """Existing ``NotificationProvider``-compatible Windows adapter."""

    name = "windows"

    def __init__(
        self,
        backend: WindowsToastBackend | None = None,
        settings: WindowsToastSettings | None = None,
        *,
        backend_status: WindowsBackendStatus | None = None,
        activation_account: tuple[str, str] | None = None,
        activation_reference_validator: Callable[[str], bool] | None = None,
    ) -> None:
        if backend is None and backend_status is None:
            backend, discovered = discover_windows_toast_backend()
        else:
            discovered = WindowsBackendStatus(
                backend is not None,
                type(backend).__name__ if backend is not None else None,
                False,
                False,
            )
        self.backend = backend
        self.status = backend_status or discovered
        self.settings = settings or WindowsToastSettings()
        self.activation_account = activation_account
        self.activation_reference_validator = activation_reference_validator

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        if not self.settings.enabled:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.DISABLED,
                "Windows notifications are disabled.",
            )
        if self.backend is None:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                self.status.limitation or "Windows notification backend is unavailable.",
                attempts=0,
            )
        if (
            isinstance(self.backend, PlyerWindowsToastBackend)
            and self.settings.sound is ToastSound.SILENT
        ):
            # Plyer's Windows facade silently ignores sound parameters. Do not
            # emit a potentially audible notification against the user's choice.
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.SKIPPED,
                "The installed desktop backend cannot guarantee silent delivery. "
                "The event remains available in notification history.",
                attempts=0,
            )
        request = build_windows_toast(event, self.settings)
        if self.activation_account is not None and request.activation is not None:
            email, fingerprint = self.activation_account
            allowed = event.account_id.casefold().strip() == email.casefold().strip()
            if event.account_id == "configured-mailbox" and self.activation_reference_validator:
                # Profile privacy may withhold account_id while permitting a
                # local reference. Only an account-bound local mapping can then
                # authorize opening; text delivery survives unavailable state.
                try:
                    allowed = (
                        self.activation_reference_validator(request.activation.arguments[-1])
                        is True
                    )
                except Exception:
                    allowed = False
            activation = (
                make_click_token(request.activation.arguments[-1], account_key=fingerprint)
                if allowed
                else None
            )
            request = replace(request, activation=activation)
        self.backend.show(request)
        detail = (
            "Accepted by the native Windows API; visibility depends on Windows "
            "notification settings."
            if isinstance(self.backend, NativeWindowsToastBackend)
            else None
        )
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED, detail)


def windows_payload_policy(settings: WindowsToastSettings) -> PayloadPolicy:
    """Expose the toast privacy selection through the common policy model."""

    return PayloadPolicy(privacy=settings.privacy, max_payload_bytes=8_192)


__all__ = [
    "NativeWindowsToastBackend",
    "PlyerWindowsToastBackend",
    "ToastActivation",
    "ToastDuration",
    "ToastSound",
    "WindowsBackendStatus",
    "WindowsToastBackend",
    "WindowsToastProvider",
    "WindowsToastRequest",
    "WindowsToastSettings",
    "build_toast_burst",
    "build_windows_toast",
    "discover_windows_toast_backend",
    "make_click_token",
    "parse_click_token",
    "windows_payload_policy",
]
