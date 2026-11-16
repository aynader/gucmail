from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gucmail.actions import DEFAULT_ACTIONS, UserMode
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialData, parse_legacy_credentials
from gucmail.dates import backoff_seconds, parse_datetime_expression
from gucmail.errors import ConfigurationError, CredentialError
from gucmail.formatting import quota_text
from gucmail.models import QuotaInfo
from gucmail.onboarding import migrate_legacy_to_keyring
from gucmail.security import (
    hmac_signature,
    html_to_safe_text,
    is_dangerous_attachment,
    redact_text,
    safe_filename,
    sanitize_terminal,
    unique_download_path,
)


def test_legacy_credentials_are_parsed_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    path = tmp_path / "creds.env"
    path.write_text(
        'email = "user@example.test"\nusername = "user"\npassword = "secret"\n',
        encoding="utf-8",
    )
    result = parse_legacy_credentials(path)
    assert result.values == {
        "email": "user@example.test",
        "username": "user",
        "password": "secret",
    }
    rendered = repr(result)
    assert "user@example.test" not in rendered
    assert "secret" not in rendered
    assert "password" in rendered
    assert not marker.exists()


def test_legacy_credentials_reject_executable_content(tmp_path: Path) -> None:
    path = tmp_path / "creds.env"
    path.write_text(
        'email = "user@example.test"\npassword = __import__("os").getcwd()\n',
        encoding="utf-8",
    )
    with pytest.raises(CredentialError):
        parse_legacy_credentials(path)


def test_credential_repr_never_contains_secret() -> None:
    credential = CredentialData("person@example.test", "person", "top-secret", "test")
    assert "top-secret" not in repr(credential)
    assert "person@example.test" not in repr(credential)


def test_config_round_trip_and_no_password(tmp_path: Path) -> None:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.db",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.db",
        backup_dir=tmp_path / "data" / "backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="user@example.test", username="user", page_size=17)
    store.save(config)
    loaded = store.load()
    assert loaded.email == "user@example.test"
    assert loaded.page_size == 17
    assert "password =" not in paths.config_file.read_text(encoding="utf-8").lower()


def test_config_rejects_unsafe_endpoint_and_polling() -> None:
    with pytest.raises(ConfigurationError):
        AppConfig(endpoint="http://mail.example.test/EWS").validate()
    for endpoint in (
        "https://user:password@mail.example.test/EWS/Exchange.asmx",
        "https://mail.example.test/EWS/Exchange.asmx?token=secret",
        "https://mail.example.test/EWS/Exchange.asmx#secret",
    ):
        with pytest.raises(ConfigurationError):
            AppConfig(endpoint=endpoint).validate()
    with pytest.raises(ConfigurationError):
        AppConfig(poll_interval_seconds=1).validate()


def test_redacted_config_is_fail_closed_for_private_free_text() -> None:
    config = AppConfig(
        email="private.person@example.test",
        username="private-user",
        endpoint="https://user:password@mail.example.test/private?token=secret",
        default_folder="Private folder name",
        external_editor="editor --token secret",
        download_directory=r"C:\Users\PrivatePerson\Downloads",
        signature="Private Person private.person@example.test +20 123",
    )

    rendered = str(config.redacted_dict())

    for private_value in (
        "private.person@example.test",
        "private-user",
        "password",
        "token=secret",
        "Private folder name",
        "editor --token secret",
        "PrivatePerson",
        "+20 123",
    ):
        assert private_value not in rendered
    assert "https://mail.example.test/<custom-path>" in rendered


def test_terminal_and_html_sanitization() -> None:
    assert sanitize_terminal("hello\x1b[31mred\x1b[0m\x00") == "hellored"
    html = '<script>alert(1)</script><p>Hello</p><img src="https://tracker.example/pixel">'
    text = html_to_safe_text(html)
    assert "Hello" in text
    assert "alert" not in text
    assert "tracker" not in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../secret.txt", "secret.txt"),
        (r"..\..\payload.exe", "payload.exe"),
        ("CON.txt", "_CON.txt"),
        ("nul.tar.gz", "_nul.tar.gz"),
        ("COM¹.backup.txt", "_COM¹.backup.txt"),
        ("lpt³.txt", "_lpt³.txt"),
        ("PRN .txt", "_PRN .txt"),
        ("COM10.txt", "COM10.txt"),
        ("report?.pdf", "report_.pdf"),
    ],
)
def test_safe_attachment_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_download_path_stays_contained_and_avoids_collision(tmp_path: Path) -> None:
    first = unique_download_path(tmp_path, "../../report.txt")
    assert first == tmp_path.resolve() / "report.txt"
    first.write_text("x", encoding="utf-8")
    second = unique_download_path(tmp_path, "report.txt")
    assert second.name == "report (1).txt"


def test_dangerous_attachment_and_redaction() -> None:
    assert is_dangerous_attachment("invoice.pdf.exe")
    redacted = redact_text("password=hunter2 user@example.test")
    assert "hunter2" not in redacted
    assert "user@" not in redacted


def test_webhook_hmac_is_stable() -> None:
    assert hmac_signature(b"key", b"payload", timestamp="123") == hmac_signature(
        b"key", b"payload", timestamp="123"
    )


def test_schedule_parser_relative_and_explicit_timezone() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    relative = parse_datetime_expression(
        "in 45 minutes", timezone="Africa/Cairo", now=now, future_required=True
    )
    assert relative.local.hour == 12
    assert relative.local.minute == 45
    explicit = parse_datetime_expression(
        "2026-08-06T09:00:00+03:00",
        timezone="Africa/Cairo",
        now=now,
        future_required=True,
    )
    assert explicit.local.isoformat() == "2026-08-06T09:00:00+03:00"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("45 minutes", datetime(2026, 8, 5, 12, 45)),
        ("20 days", datetime(2026, 8, 25, 12, 0)),
        ("3 hrs", datetime(2026, 8, 5, 15, 0)),
        ("after 10 mins", datetime(2026, 8, 5, 12, 10)),
    ],
)
def test_schedule_parser_bare_relative_delay_is_not_a_wall_clock(expression, expected) -> None:
    # ``send --in "45 minutes"`` passes the delay without the "in" prefix. dateutil
    # used to read it as 00:45 today (or "20 days" as the 20th of this month).
    now = datetime(2026, 8, 5, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    resolved = parse_datetime_expression(
        expression, timezone="Africa/Cairo", now=now, future_required=True
    )
    assert resolved.local.replace(tzinfo=None) == expected


@pytest.mark.parametrize(
    "expression", ["30", "12", "1h", "1.5 hours", "2 hours 30 minutes", "-5 minutes", "2026"]
)
def test_schedule_parser_rejects_delay_like_text_instead_of_guessing(expression) -> None:
    # dateutil read ``send --in 30`` as the 30th of this month at midnight and
    # ``2 hours 30 minutes`` as 02:30 today; neither may become a schedule.
    now = datetime(2026, 11, 11, 15, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    with pytest.raises(ConfigurationError, match="not a supported date or delay"):
        parse_datetime_expression(
            expression, timezone="Africa/Cairo", now=now, future_required=True
        )
    assert parse_datetime_expression(
        "20 Nov 2026 9:00", timezone="Africa/Cairo", now=now, future_required=True
    ).local.replace(tzinfo=None) == datetime(2026, 11, 20, 9, 0)


def test_schedule_parser_rejects_ambiguous_slash_date() -> None:
    with pytest.raises(ConfigurationError, match="ambiguous"):
        parse_datetime_expression("03/04/2027", timezone="Africa/Cairo")


def test_backoff_can_be_deterministic() -> None:
    class MaximumRandom:
        def uniform(self, low: float, high: float) -> float:
            assert low == 0
            return high

    assert backoff_seconds(2, base=2, maximum=100, random_source=MaximumRandom()) == 8  # type: ignore[arg-type]


def test_quota_unavailable_and_exact_rendering() -> None:
    assert quota_text(QuotaInfo()) == "Mailbox: Quota unavailable"
    rendered = quota_text(QuotaInfo(used_bytes=7, quota_bytes=10), monochrome=True)
    assert "70.0%" in rendered
    assert "[#" in rendered


def test_action_registry_progressively_discloses_without_raw_soap() -> None:
    basic = DEFAULT_ACTIONS.visible(UserMode.BASIC)
    expert = DEFAULT_ACTIONS.visible(UserMode.EXPERT)
    assert len(basic) < len(expert)
    assert DEFAULT_ACTIONS.get("messages.delete").confirmation is True
    assert DEFAULT_ACTIONS.get("ews.request_preview").offline is True
    assert all(action.name != "ews.raw" for action in DEFAULT_ACTIONS.all())
    with pytest.raises(KeyError, match="Unknown action"):
        DEFAULT_ACTIONS.get("missing")


def test_explicit_legacy_migration_copies_to_keyring_and_keeps_file(tmp_path: Path) -> None:
    class MemoryConfigStore:
        def __init__(self) -> None:
            self.saved: AppConfig | None = None

        def save(self, config: AppConfig) -> None:
            self.saved = config

    class MemoryCredentialStore:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def set(self, email: str, password: str) -> None:
            self.values[email] = password

    config_store = MemoryConfigStore()
    credential_store = MemoryCredentialStore()
    context = SimpleNamespace(
        config=AppConfig(),
        config_store=config_store,
        credential_resolver=SimpleNamespace(store=credential_store),
    )
    legacy = tmp_path / "legacy.env"
    legacy.write_text(
        'email = "student@example.test"\npassword = "synthetic-secret"\n',
        encoding="utf-8",
    )
    warnings = migrate_legacy_to_keyring(context, legacy)  # type: ignore[arg-type]
    assert credential_store.values == {"student@example.test": "synthetic-secret"}
    assert legacy.exists()
    assert warnings
