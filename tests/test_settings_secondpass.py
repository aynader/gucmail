from __future__ import annotations

from pathlib import Path

import pytest

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore, _reject_plaintext_secrets
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.errors import ConfigurationError
from gucmail.help import HelpRepository
from gucmail.settings import (
    DEFAULT_SETTINGS,
    InMemoryRevisionStore,
    SettingsContext,
    SettingScope,
    SettingsService,
    _validate_structured_value,
)
from gucmail.state import StateStore
from gucmail.tui.area_backends import SettingsAreaBackend
from gucmail.tui.branding import (
    BrandingCatalog,
    BrandingPreferences,
    BrandingSession,
)


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config" / "config.toml",
        state_database=root / "data" / "state.sqlite3",
        recovery_dir=root / "data" / "recovery",
        index_database=root / "data" / "index.sqlite3",
        backup_dir=root / "data" / "backups",
    )


def _store(root: Path) -> ConfigStore:
    store = ConfigStore(_paths(root))
    store.save(AppConfig(email="student@example.test"))
    return store


def _context(root: Path) -> AppContext:
    store = _store(root)
    paths = store.paths
    return AppContext(
        paths=paths,
        config_store=store,
        config=store.load(),
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def test_dotted_structured_provider_paths_cannot_hide_plaintext_secrets() -> None:
    payload = {
        "settings": {
            "integrations.providers": [
                [
                    {
                        "kind": "discord",
                        "url": "https://discord.example.test/api/webhooks/private-token",
                    }
                ]
            ]
        }
    }

    with pytest.raises(ConfigurationError, match="Secret field"):
        _reject_plaintext_secrets(payload)
    with pytest.raises(ValueError, match="secure secret reference"):
        _validate_structured_value(
            payload["settings"]["integrations.providers"],
            setting_id="integrations.providers",
        )


def test_structured_provider_accepts_verified_secret_reference() -> None:
    value = [{"kind": "discord", "url": "keyring://webhooks/provider-1"}]

    _validate_structured_value(value, setting_id="integrations.providers")


def test_every_setting_help_target_resolves_offline() -> None:
    repository = HelpRepository.load_default()

    unresolved = [
        spec.setting_id for spec in DEFAULT_SETTINGS if not _help_exists(repository, spec.help_id)
    ]

    assert unresolved == []


def _help_exists(repository: HelpRepository, page_id: str) -> bool:
    try:
        repository.get(page_id)
    except KeyError:
        return False
    return True


def test_branding_default_is_a_real_catalog_preset() -> None:
    spec = DEFAULT_SETTINGS.get("appearance.branding")

    assert BrandingCatalog().get(str(spec.default)).id == spec.default


def test_settings_reject_unknown_theme_and_branding_before_commit(tmp_path: Path) -> None:
    service = SettingsService(_store(tmp_path), revisions=InMemoryRevisionStore())

    with pytest.raises(ConfigurationError, match="Unknown theme"):
        service.set("appearance.theme", "not-a-real-theme")
    with pytest.raises(ConfigurationError, match=r"Invalid value for appearance\.branding"):
        service.set("appearance.branding", "builtin.not-real")


def test_account_scoped_settings_are_isolated_while_global_preferences_are_shared(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    revisions = InMemoryRevisionStore()
    first = SettingsService(
        store,
        context=SettingsContext(account_id="first@example.test"),
        revisions=revisions,
    )
    second = SettingsService(
        store,
        context=SettingsContext(account_id="second@example.test"),
        revisions=revisions,
    )

    first.set("mail.mark_read_on_open", True)
    first.set("appearance.density", "compact")

    assert first.value("mail.mark_read_on_open").configured_value is True
    assert second.value("mail.mark_read_on_open").configured_value is False
    assert second.value("appearance.density").configured_value == "compact"
    assert DEFAULT_SETTINGS.get("mail.mark_read_on_open").scope is SettingScope.ACCOUNT


def test_staged_settings_are_compare_and_swap_guarded(tmp_path: Path) -> None:
    service = SettingsService(_store(tmp_path), revisions=InMemoryRevisionStore())
    first = service.begin().set("appearance.density", "compact")
    stale = service.begin().set("appearance.border_style", "solid")

    first.apply()

    with pytest.raises(ConfigurationError, match="changed in another process"):
        stale.apply()


def test_revision_export_import_and_undo_exclude_private_and_secret_values(tmp_path: Path) -> None:
    revisions = InMemoryRevisionStore()
    service = SettingsService(_store(tmp_path), revisions=revisions)
    first_revision = service.set("appearance.density", "compact")
    assert first_revision is not None
    service.set("appearance.custom_branding", "private local art")

    exported = service.export()

    assert "appearance.custom_branding" not in exported["settings"]
    assert "integrations.webhook_secret" not in exported["settings"]
    assert "appearance.custom_branding" in exported["excluded_private"]
    assert "integrations.webhook_secret" in exported["excluded_secrets"]

    plan = service.plan_import(
        {
            "schema_version": 1,
            "application": "gucmail",
            "settings": {"appearance.density": "spacious"},
        }
    )
    assert [change.setting_id for change in plan.changes] == ["appearance.density"]
    service.apply_import(plan)
    assert service.value("appearance.density").configured_value == "spacious"

    service.undo()
    assert service.value("appearance.density").configured_value == "compact"


def test_category_reset_skips_environment_overrides_and_resets_other_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SettingsService(_store(tmp_path), revisions=InMemoryRevisionStore())
    service.set("appearance.density", "compact")
    service.set("appearance.border_style", "solid")
    theme_spec = DEFAULT_SETTINGS.get("appearance.theme")
    monkeypatch.setenv(theme_spec.environment_variable, "builtin.default-light")

    service.reset_category("Appearance")

    assert service.value("appearance.density").configured_value == "theme"
    assert service.value("appearance.border_style").configured_value == "theme"
    assert service.value("appearance.theme").effective_value == "builtin.default-light"


def test_tui_backend_uses_real_scope_keywords_and_numeric_bounds(tmp_path: Path) -> None:
    backend = SettingsAreaBackend.from_context(_context(tmp_path))
    effective = backend.service.value("layout.preview_ratio")

    view = backend._setting(effective)

    assert backend.service.context.account_id == "student@example.test"
    assert backend.service.context.machine_id
    assert "preview width" in view.keywords
    assert "make the preview wider" in view.keywords
    assert view.minimum == pytest.approx(0.20)
    assert view.maximum == pytest.approx(0.75)


def test_branding_apply_is_transactional_when_persistence_fails() -> None:
    original = BrandingPreferences()

    def fail(_preferences: BrandingPreferences) -> None:
        raise OSError("synthetic persistence failure")

    session = BrandingSession(original, persist=fail)
    session.preview(preset_id="builtin.guc-block")

    with pytest.raises(OSError, match="synthetic persistence failure"):
        session.apply()

    assert session.preferences == original
    assert session.is_previewing
    assert session.cancel() == original


def test_corrupt_primary_recovers_last_known_good_without_losing_preferences(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = SettingsService(store, revisions=InMemoryRevisionStore())
    service.set("appearance.density", "compact")
    store.paths.config_file.write_text("[broken\n", encoding="utf-8")

    recovered_store = ConfigStore(store.paths)
    recovered = SettingsService(recovered_store, revisions=InMemoryRevisionStore())

    assert recovered.value("appearance.density").configured_value == "compact"
    assert recovered_store.diagnostics.recovered
    assert recovered_store.diagnostics.quarantined_paths


def test_nullable_preference_is_explicit_and_survives_toml_reopen(tmp_path: Path) -> None:
    from gucmail.settings import ValueSource

    store = _store(tmp_path)
    service = SettingsService(store)
    service.set("appearance.branding_height", 5)
    service.set("appearance.branding_height", None)
    reopened = SettingsService(ConfigStore(store.paths))
    assert reopened.value("appearance.branding_height").effective_value is None
    assert reopened.value("appearance.branding_height").source is not ValueSource.DEFAULT
    reopened.reset("appearance.branding_height")
    assert reopened.value("appearance.branding_height").source is ValueSource.DEFAULT
