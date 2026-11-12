"""Lazy composition root. Offline commands never initialize Exchange."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.logging_config import configure_logging
from gucmail.state import StateStore

if TYPE_CHECKING:
    from gucmail.ews.adapter import EWSAdapter


@dataclass(slots=True)
class AppContext:
    paths: AppPaths
    config_store: ConfigStore
    config: AppConfig
    state: StateStore
    credential_resolver: CredentialResolver
    credentials_file: Path | None = None
    credential_warnings: tuple[str, ...] = ()
    credential_source: str | None = None
    _gateway: EWSAdapter | None = None
    _gateway_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(
        cls,
        *,
        config_path: Path | None = None,
        credentials_file: Path | None = None,
    ) -> AppContext:
        paths = AppPaths.discover()
        if config_path is not None:
            paths = AppPaths(
                config_dir=config_path.parent,
                data_dir=paths.data_dir,
                cache_dir=paths.cache_dir,
                log_dir=paths.log_dir,
                config_file=config_path,
                state_database=paths.state_database,
                recovery_dir=paths.recovery_dir,
                index_database=paths.index_database,
                backup_dir=paths.backup_dir,
            )
        store = ConfigStore(paths)
        config = store.load()
        paths.ensure()
        configure_logging(
            paths.log_dir,
            level=config.logging_level,
            redact_emails=config.redact_email_addresses,
        )
        state = StateStore(paths.state_database)
        if config.email:
            state.bind_mailbox(config.email, config.endpoint)
        return cls(
            paths=paths,
            config_store=store,
            config=config,
            state=state,
            credential_resolver=CredentialResolver(CredentialStore()),
            credentials_file=(
                credentials_file.expanduser().resolve(strict=False)
                if credentials_file is not None
                else None
            ),
        )

    def gateway(
        self,
        *,
        interactive: bool = False,
        allow_legacy: bool = True,
    ) -> EWSAdapter:
        """Resolve credentials and build exactly one adapter on first intentional online use."""

        if self._gateway is not None:
            return self._gateway
        with self._gateway_lock:
            if self._gateway is not None:
                return self._gateway
            credentials = self.credential_resolver.resolve(
                self.config,
                interactive=interactive,
                allow_legacy=allow_legacy,
                preferred_legacy_path=self.credentials_file,
                force_legacy=self.credentials_file is not None,
            )
            self.state.bind_mailbox(credentials.email, self.config.endpoint)
            self.credential_warnings = credentials.warnings
            self.credential_source = credentials.source
            from gucmail.ews.adapter import EWSAdapter

            self._gateway = EWSAdapter(
                config=self.config,
                credentials=credentials,
                state=self.state,
            )
            return self._gateway

    def close(self) -> None:
        """Close the current online session, if any, exactly once.

        Gateway creation and teardown share the same lock.  This matters for the TUI and
        watcher, where a configuration reload can otherwise race a first connection and
        leave an adapter alive with stale endpoint, timeout, or credential settings.
        """

        with self._gateway_lock:
            gateway = self._gateway
            self._gateway = None
            if gateway is not None:
                gateway.close()

    def reload_config(self) -> AppConfig:
        """Reload configuration and invalidate any adapter built from old values.

        ``EWSAdapter`` captures connection and retry settings at construction time.  A
        context therefore must not keep returning that adapter after configuration is
        reloaded.  Local-only callers still pay no online cost: a replacement remains
        lazy until :meth:`gateway` is intentionally called again.
        """

        updated = self.config_store.load()
        if updated != self.config:
            self.close()
        if updated.email:
            self.state.bind_mailbox(updated.email, updated.endpoint)
        self.config = updated
        return updated

    def local_status(self) -> dict[str, Any]:
        state_health = self.state.health()
        state_health["path"] = "<configured>"
        return {
            "configured": bool(self.config.email),
            "endpoint": self.config.redacted_dict()["endpoint"],
            "mode": self.config.mode,
            "config_path": "<configured>",
            "state": state_health,
            "online_session_initialized": self._gateway is not None,
        }


def build_context(
    *,
    config_path: Path | None = None,
    credentials_file: Path | None = None,
) -> AppContext:
    return AppContext.create(config_path=config_path, credentials_file=credentials_file)
