from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gucmail.config import AppConfig
from gucmail.credentials import CredentialResolver
from gucmail.errors import ConfigurationError, CredentialError
from gucmail.models import NotificationEvent, to_primitive
from gucmail.onboarding import migrate_legacy_to_keyring
from gucmail.secret_store import (
    InMemorySecretBackend,
    SecretStore,
    SecretStoreUnavailableError,
    SecretStoreVerificationError,
)
from gucmail.security import redact_text
from gucmail.services.notifications import DeliveryState
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookProvider,
    WebhookRegistry,
    WebhookSecretMigration,
)
from gucmail.state import StateStore
from gucmail.state_health import StateHealthChecker


def _legacy_file(path: Path, *, email: str = "student@example.test") -> Path:
    path.write_text(
        f'email = "{email}"\nusername = "student"\npassword = "synthetic-secret"\n',
        encoding="utf-8",
    )
    return path


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="evt-synthetic",
        event_type="new_message",
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
        account_id="synthetic-account",
        folder="Inbox",
        message_reference="msg-synthetic",
        subject="Synthetic subject",
    )


def test_namespaced_secret_store_round_trip_and_unavailable_behavior() -> None:
    backend = InMemorySecretBackend()
    store = SecretStore("webhooks", backend=backend)
    reference = store.put_and_verify("destination", "synthetic-secret")

    assert store.get(reference) == "synthetic-secret"
    assert "synthetic-secret" not in repr(reference)
    assert reference.redacted() == "keyring://<redacted>"
    assert backend.contains("gucmail/webhooks", "destination")

    other = SecretStore("credentials", backend=backend)
    with pytest.raises(CredentialError, match="different namespace"):
        other.get(reference)

    unavailable = SecretStore(
        "webhooks",
        backend=InMemorySecretBackend(available=False),
    )
    with pytest.raises(SecretStoreUnavailableError) as captured:
        unavailable.put("destination", "must-not-appear")
    assert "must-not-appear" not in str(captured.value)
    assert "synthetic backend" not in str(captured.value)


def test_secret_store_requires_verified_readback() -> None:
    class CorruptReadbackBackend(InMemorySecretBackend):
        def get_password(self, service_name: str, username: str) -> str | None:
            super().get_password(service_name, username)
            return "different-value"

    store = SecretStore("webhooks", backend=CorruptReadbackBackend())
    with pytest.raises(SecretStoreVerificationError) as captured:
        store.put_and_verify("destination", "original-value")
    assert "original-value" not in str(captured.value)
    assert "different-value" not in str(captured.value)


def test_webhook_config_repr_and_structured_output_never_expose_destination() -> None:
    config = WebhookConfig(
        name="legacy",
        url="https://hooks.example.test/private/token-value",
        headers={"Authorization": "Bearer header-token", "X-Public": "safe"},
        allow_legacy_plaintext_secrets=True,
    )

    rendered = repr(config)
    primitive = to_primitive(config)
    serialized = json.dumps(primitive, sort_keys=True)
    for private in (
        "https://hooks.example.test/private/token-value",
        "Bearer header-token",
        "token-value",
    ):
        assert private not in rendered
        assert private not in serialized
    assert primitive["destination"] == "legacy_https_configured"
    assert primitive["legacy_secret_migration_required"] is True

    with pytest.raises(ConfigurationError, match="keyring"):
        WebhookConfig(
            name="new-unsafe",
            url="https://hooks.example.test/path",
            headers={"Cookie": "session=plaintext"},
        )


def test_webhook_secret_migration_copies_and_verifies_before_scrub() -> None:
    backend = InMemorySecretBackend()
    store = SecretStore("webhooks", backend=backend)
    legacy = WebhookConfig(
        name="synthetic",
        url="https://hooks.example.test/private/token-value",
        headers={"Authorization": "Bearer header-token", "X-Public": "safe"},
        allow_legacy_plaintext_secrets=True,
    )

    result = WebhookSecretMigration(store).stage(legacy)

    assert result.safe_to_scrub_legacy_values is True
    assert result.destination_copied is True
    assert result.header_names_copied == ("Authorization", "X-Public")
    assert result.replacement.destination_is_reference
    assert result.replacement.headers["Authorization"].startswith("keyring://")
    assert result.replacement.headers["X-Public"].startswith("keyring://")
    assert store.get(result.replacement.headers["X-Public"]) == "safe"
    safe = json.dumps(to_primitive(result), sort_keys=True)
    assert "token-value" not in safe
    assert "header-token" not in safe

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    provider = WebhookProvider(
        result.replacement,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        secret_resolver=store.resolve,
    )
    outcome = provider.notify(_event())
    provider.close()

    assert outcome.state is DeliveryState.DELIVERED
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer header-token"


def test_registry_isolates_malformed_legacy_rows_without_exposing_values(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    registry = WebhookRegistry(
        state,
        secret_store=SecretStore("webhooks", backend=InMemorySecretBackend()),
    )
    registry.add(WebhookConfig(name="valid", url="https://hooks.example.test/private/token"))
    with state.transaction(immediate=True) as connection:
        cursor = connection.execute(
            """
            INSERT INTO webhooks(
                name, url, method, headers_json, field_allowlist_json, enabled,
                max_retries, created_at
            ) VALUES (?, ?, 'POST', '{}', NULL, 1, 3, ?)
            """,
            ("broken", "not-a-valid-destination-with-secret", "2026-08-09T00:00:00Z"),
        )
        malformed_id = int(cursor.lastrowid or 0)

    loaded = registry.load()

    assert [item.name for item in loaded.configurations] == ["valid"]
    assert len(loaded.issues) == 1
    assert loaded.issues[0].row_id == malformed_id
    rendered = json.dumps(to_primitive(loaded), sort_keys=True)
    assert "private/token" not in rendered
    assert "not-a-valid-destination-with-secret" not in rendered
    assert registry.load_issues == loaded.issues


def test_new_webhook_write_uses_only_verified_secret_references(tmp_path: Path) -> None:
    destination = "https://hooks.example.test/private/discord-like-token"
    authorization = "Bearer synthetic-header-secret"
    backend = InMemorySecretBackend()
    secrets = SecretStore("webhooks", backend=backend)
    state = StateStore(tmp_path / "state.sqlite3")
    registry = WebhookRegistry(state, secret_store=secrets)

    stored = registry.add(
        WebhookConfig(
            name="secure-new",
            url=destination,
            headers={"Authorization": authorization, "X-Public": "metadata"},
            allow_legacy_plaintext_secrets=True,
        )
    )

    assert stored.destination_is_reference
    assert stored.plaintext_headers == ()
    assert secrets.get(stored.url) == destination
    assert secrets.get(stored.headers["Authorization"]) == authorization
    with state.connection() as connection:
        row = connection.execute(
            "SELECT url, headers_json FROM webhooks WHERE name='secure-new'"
        ).fetchone()
    assert row is not None
    raw_persistence = f"{row['url']}\n{row['headers_json']}"
    assert destination not in raw_persistence
    assert authorization not in raw_persistence
    assert "discord-like-token" not in raw_persistence
    assert str(row["url"]).startswith("keyring://")
    assert json.loads(str(row["headers_json"]))["Authorization"].startswith("keyring://")


def test_new_webhook_write_has_no_plaintext_fallback_when_keyring_fails(
    tmp_path: Path,
) -> None:
    destination = "https://hooks.example.test/private/must-never-hit-sqlite"
    backend = InMemorySecretBackend(available=False)
    state = StateStore(tmp_path / "state.sqlite3")
    registry = WebhookRegistry(
        state,
        secret_store=SecretStore("webhooks", backend=backend),
    )

    with pytest.raises(SecretStoreUnavailableError) as captured:
        registry.add(WebhookConfig(name="unavailable", url=destination))

    assert destination not in str(captured.value)
    with state.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0]
    assert count == 0


def test_legacy_webhook_row_is_readable_only_by_verified_migration(tmp_path: Path) -> None:
    destination = "https://hooks.example.test/legacy/private-token"
    authorization = "Bearer legacy-header-secret"
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    with state.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO webhooks(
                name, url, method, headers_json, field_allowlist_json, enabled,
                max_retries, created_at
            ) VALUES (?, ?, 'POST', ?, NULL, 1, 3, ?)
            """,
            (
                "legacy-migrate",
                destination,
                json.dumps({"Authorization": authorization, "X-Public": "metadata"}),
                "2026-08-09T00:00:00Z",
            ),
        )
    secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(state, secret_store=secrets)

    loaded = registry.load()
    assert loaded.configurations == ()
    assert [issue.code for issue in loaded.issues] == ["legacy_webhook_secrets_require_migration"]
    with pytest.raises(ConfigurationError) as captured:
        registry.get("legacy-migrate")
    assert destination not in str(captured.value)
    assert authorization not in str(captured.value)

    migrated = registry.migrate_legacy("legacy-migrate")
    assert migrated.safe_to_scrub_legacy_values is True
    current = registry.get("legacy-migrate")
    assert current is not None
    assert current.destination_is_reference
    assert current.plaintext_headers == ()
    assert secrets.get(current.url) == destination
    assert secrets.get(current.headers["Authorization"]) == authorization
    with state.connection() as connection:
        row = connection.execute(
            "SELECT url, headers_json FROM webhooks WHERE name='legacy-migrate'"
        ).fetchone()
    assert row is not None
    persisted = f"{row['url']}\n{row['headers_json']}"
    assert destination not in persisted
    assert authorization not in persisted
    assert "private-token" not in persisted


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer highly-secret-token",
        "Proxy-Authorization=Basic dXNlcjpwYXNzd29yZA==",
        "Cookie: session=secret-cookie; other=value",
        'password = "a quoted multi word secret"',
        "token=first second third",
    ],
)
def test_redaction_removes_complete_multitoken_secrets(raw: str) -> None:
    rendered = redact_text(raw, redact_emails=False)
    assert "<redacted>" in rendered
    for private in (
        "highly-secret-token",
        "dXNlcjpwYXNzd29yZA==",
        "secret-cookie",
        "quoted multi word secret",
        "first second third",
    ):
        assert private not in rendered


def test_forced_legacy_selection_bypasses_keyring_and_surfaces_safe_metadata(
    tmp_path: Path,
) -> None:
    class MemoryCredentialStore:
        get_calls = 0

        def get(self, email: str) -> str | None:
            self.get_calls += 1
            return "wrong-keyring-value"

    store = MemoryCredentialStore()
    path = _legacy_file(tmp_path / "selected.env")
    resolver = CredentialResolver(store=store)  # type: ignore[arg-type]

    resolved = resolver.resolve(
        AppConfig(email="STUDENT@example.test"),
        preferred_legacy_path=path,
        force_legacy=True,
    )

    assert store.get_calls == 0
    assert resolved.source == "legacy_file_explicit"
    assert resolved.password == "synthetic-secret"
    assert resolved.warnings
    summary = json.dumps(resolved.safe_summary(), sort_keys=True)
    assert "synthetic-secret" not in summary
    assert "student@example.test" not in summary.casefold()
    assert "synthetic-secret" not in repr(resolved)


def test_preferred_legacy_path_preserves_default_keyring_precedence(tmp_path: Path) -> None:
    class MemoryCredentialStore:
        def get(self, email: str) -> str | None:
            return "keyring-value"

    path = _legacy_file(tmp_path / "preferred.env")
    resolved = CredentialResolver(store=MemoryCredentialStore()).resolve(  # type: ignore[arg-type]
        AppConfig(email="student@example.test"),
        preferred_legacy_path=path,
    )
    assert resolved.source == "keyring"
    assert resolved.password == "keyring-value"


def test_legacy_account_mismatch_never_copies_or_combines_password(tmp_path: Path) -> None:
    class MemoryCredentialStore:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def set(self, email: str, password: str) -> None:
            self.values[email] = password

    class MemoryConfigStore:
        saved = False

        def save(self, config: AppConfig) -> None:
            self.saved = True

    selected = _legacy_file(tmp_path / "selected.env", email="other@example.test")
    credential_store = MemoryCredentialStore()
    config_store = MemoryConfigStore()
    context = SimpleNamespace(
        config=AppConfig(email="configured@example.test"),
        config_store=config_store,
        credential_resolver=SimpleNamespace(store=credential_store),
    )

    with pytest.raises(CredentialError, match="does not match"):
        migrate_legacy_to_keyring(context, selected)  # type: ignore[arg-type]

    assert credential_store.values == {}
    assert config_store.saved is False

    resolver = CredentialResolver(store=SimpleNamespace(get=lambda _email: None))
    with pytest.raises(CredentialError, match="does not match"):
        resolver.resolve(  # type: ignore[arg-type]
            AppConfig(email="configured@example.test"),
            preferred_legacy_path=selected,
            force_legacy=True,
        )


def test_state_health_checker_detects_schema_gaps_and_missing_tables(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, 'now'), (3, 'now');
            """
        )

    report = StateHealthChecker(StateStore(path)).check()
    codes = {issue.code for issue in report.issues}

    assert report.healthy is False
    assert "migration_gap" in codes
    assert "missing_table" in codes
    assert "initialization_failed" in codes
    assert str(tmp_path) not in json.dumps(to_primitive(report))


def test_state_health_checker_detects_foreign_key_damage(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    healthy = StateHealthChecker(state).check()
    assert healthy.healthy is True

    with state.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO schedule_history(job_reference, event, occurred_at) VALUES (?, ?, ?)",
            ("missing-job", "synthetic", "2026-08-09T00:00:00Z"),
        )

    damaged = StateHealthChecker(state).check()
    assert damaged.healthy is False
    assert damaged.foreign_key_violation_count == 1
    assert any(issue.code == "foreign_key_violation" for issue in damaged.issues)
