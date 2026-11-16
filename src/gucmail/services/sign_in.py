"""First sign-in, shared by ``gucmail login`` and the TUI's sign-in screen.

The password is tested with one small mailbox request before anything is saved. Only a
password that works is stored, and only then is the local data folder claimed for the
account (that claim is permanent, so a mistyped address must never make it).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig
from gucmail.credentials import CredentialData
from gucmail.errors import CredentialError

AdapterFactory = Callable[[AppConfig, CredentialData], Any]


@dataclass(frozen=True, slots=True)
class SignInResult:
    email: str
    checked: bool
    stored: bool
    status: dict[str, Any] | None = None
    warning: str | None = None


def needs_sign_in(context: AppContext) -> bool:
    """True when starting online would fail for lack of an account or password.

    Looks only at local sources (configuration, environment, keyring, a credential file);
    it never contacts the server.
    """

    if context._gateway is not None or context.credentials_file is not None:
        return False
    email = os.environ.get("GUCMAIL_EMAIL") or context.config.email
    if not email:
        return not (Path.cwd() / "creds.env").is_file()
    if os.environ.get("GUCMAIL_PASSWORD"):
        return False
    try:
        if context.credential_resolver.store.get(email):
            return False
    except CredentialError:
        pass
    return not (Path.cwd() / "creds.env").is_file()


def check_address(email: str) -> str:
    """Return the trimmed address, or raise with a plain explanation."""

    value = email.strip()
    local, at, domain = value.partition("@")
    if not at or not local or "." not in domain or " " in value:
        raise CredentialError("Enter your full GUC email address, like name@student.guc.edu.eg.")
    return value


def _default_adapter(config: AppConfig, credentials: CredentialData, context: AppContext) -> Any:
    from gucmail.ews.adapter import EWSAdapter

    return EWSAdapter(config=config, credentials=credentials, state=context.state)


def sign_in(
    context: AppContext,
    email: str,
    password: str,
    *,
    username: str | None = None,
    check: bool = True,
    adapter_factory: AdapterFactory | None = None,
) -> SignInResult:
    """Test (unless ``check`` is False), then store the credential and save the account.

    On success with ``check``, the tested connection becomes the context's session, so
    the app does not sign in a second time.
    """

    address = check_address(email)
    if not password:
        raise CredentialError("Enter your password.")
    sign_in_name = (username or "").strip() or context.config.username or address
    if context.config.email and context.config.email.casefold() != address.casefold():
        # A different account: its own sign-in name, not the previous one.
        sign_in_name = (username or "").strip() or address
    config = replace(context.config, email=address, username=sign_in_name).validate()

    adapter = None
    status = None
    if check:
        credentials = CredentialData(
            email=address, username=sign_in_name, password=password, source="interactive"
        )
        if adapter_factory is not None:
            adapter = adapter_factory(config, credentials)
        else:
            adapter = _default_adapter(config, credentials, context)
        try:
            status = adapter.status()
        except BaseException:
            _close(adapter)
            raise

    store = context.credential_resolver.store
    warning = None
    stored = True
    previous: str | None = None
    try:
        previous = store.get(address)
        store.set(address, password)
    except CredentialError:
        stored = False
        warning = (
            "Your system's password store is unavailable, so the password was not saved. "
            "You'll be asked again next time."
        )
        if adapter is None:
            raise

    # Claim the local data folder last: it is permanent.
    try:
        context.state.bind_mailbox(address, config.endpoint)
    except BaseException:
        _close(adapter)
        if stored:
            with contextlib.suppress(CredentialError):
                if previous is None:
                    store.delete(address)
                else:
                    store.set(address, previous)
        raise

    context.config_store.save(config)
    context.close()
    context.config = config
    if adapter is not None:
        with context._gateway_lock:
            context._gateway = adapter
        context.credential_source = "interactive" if not stored else "keyring"
    return SignInResult(email=address, checked=check, stored=stored, status=status, warning=warning)


def _close(adapter: Any) -> None:
    if adapter is None:
        return
    # A failed sign-in must report the sign-in error, not a close error.
    with contextlib.suppress(Exception):
        adapter.close()
