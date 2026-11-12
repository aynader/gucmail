from __future__ import annotations

from pathlib import Path

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.state import StateStore


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.sqlite3",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.sqlite3",
        backup_dir=tmp_path / "data" / "backups",
    )


class _ConfigStore:
    def __init__(self, value: AppConfig) -> None:
        self.value = value

    def load(self) -> AppConfig:
        return self.value


class _Gateway:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _context(tmp_path: Path, store: _ConfigStore) -> AppContext:
    paths = _paths(tmp_path)
    return AppContext(
        paths=paths,
        config_store=store,  # type: ignore[arg-type]
        config=store.value,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def test_reload_config_invalidates_adapter_built_from_old_settings(tmp_path: Path) -> None:
    store = _ConfigStore(AppConfig(email="old@example.test", request_timeout_seconds=30))
    context = _context(tmp_path, store)
    gateway = _Gateway()
    context._gateway = gateway  # type: ignore[assignment]

    store.value = AppConfig(email="old@example.test", request_timeout_seconds=45)
    reloaded = context.reload_config()

    assert reloaded.request_timeout_seconds == 45
    assert context._gateway is None
    assert gateway.close_count == 1


def test_reload_config_keeps_session_when_effective_configuration_is_unchanged(
    tmp_path: Path,
) -> None:
    store = _ConfigStore(AppConfig(email="same@example.test"))
    context = _context(tmp_path, store)
    gateway = _Gateway()
    context._gateway = gateway  # type: ignore[assignment]

    context.reload_config()

    assert id(context._gateway) == id(gateway)
    assert gateway.close_count == 0


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = _ConfigStore(AppConfig())
    context = _context(tmp_path, store)
    gateway = _Gateway()
    context._gateway = gateway  # type: ignore[assignment]

    context.close()
    context.close()

    assert gateway.close_count == 1


def test_mailbox_state_owner_survives_restart_and_preserves_legacy_drafts(tmp_path: Path) -> None:
    import pytest

    from gucmail.errors import ConfigurationError
    from gucmail.models import DraftData
    from gucmail.services.drafts import DraftService

    state = StateStore(tmp_path / "state.db")
    drafts = DraftService(state, tmp_path / "recovery")
    draft = drafts.create(DraftData(reference="legacy-draft", subject="Synthetic retained draft"))
    state.bind_mailbox("first@example.test", "https://exchange.example.test/EWS/Exchange.asmx")
    reopened = StateStore(state.path)
    reopened.bind_mailbox("FIRST@example.test", "https://exchange.example.test/EWS/Exchange.asmx")
    with pytest.raises(ConfigurationError, match="separate GUCMAIL_DATA_DIR"):
        reopened.bind_mailbox(
            "second@example.test", "https://exchange.example.test/EWS/Exchange.asmx"
        )
    assert drafts.get(draft.reference).subject == draft.subject
    with reopened.connection() as connection:
        value = connection.execute(
            "SELECT value FROM metadata WHERE key='mailbox_owner_v1'"
        ).fetchone()[0]
    assert "@" not in value
