"""First sign-in: the shared service, `gucmail login`, the sign-in screen and the tour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static
from typer.testing import CliRunner

import gucmail.cli as cli
import gucmail.cli.main  # noqa: F401 - cli.main is patched below
import gucmail.services.sign_in as sign_in_module
from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver
from gucmail.errors import AuthenticationError, ConfigurationError, CredentialError, NetworkError
from gucmail.services.sign_in import check_address, needs_sign_in, sign_in
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from gucmail.tui.sign_in_screen import SignInScreen, sign_in_problem
from gucmail.tui.welcome_tour import WelcomeTourScreen
from waiting import press_button, wait_until

EMAIL = "nour.ashraf@student.example.test"


class MemoryKeyring:
    """Stands in for the OS password store."""

    def __init__(self, *, broken: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.broken = broken

    def get(self, email: str) -> str | None:
        if self.broken:
            raise CredentialError("keyring unavailable")
        return self.values.get(email)

    def set(self, email: str, password: str) -> None:
        if self.broken:
            raise CredentialError("keyring unavailable")
        self.values[email] = password

    def delete(self, email: str) -> bool:
        return self.values.pop(email, None) is not None


class FakeAdapter:
    def __init__(self, password_ok: bool = True, error: Exception | None = None) -> None:
        self.password_ok = password_ok
        self.error = error
        self.status_calls = 0
        self.closed = False

    def status(self) -> dict[str, object]:
        self.status_calls += 1
        if self.error is not None:
            raise self.error
        if not self.password_ok:
            raise AuthenticationError("rejected")
        return {"connected": True, "inbox_count": 3}

    def close(self) -> None:
        self.closed = True


def make_context(tmp_path: Path, *, email: str | None = None, broken: bool = False) -> AppContext:
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
    config = AppConfig(email=email)
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(MemoryKeyring(broken=broken)),  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _no_ambient_account(monkeypatch, tmp_path):
    monkeypatch.delenv("GUCMAIL_EMAIL", raising=False)
    monkeypatch.delenv("GUCMAIL_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)  # no creds.env next to the tests


@pytest.mark.parametrize(
    "value", ["", "nour", "nour@", "@guc.edu.eg", "nour@guc", "a b@guc.edu.eg"]
)
def test_rejects_incomplete_addresses(value):
    with pytest.raises(CredentialError, match="full GUC email"):
        check_address(value)


def test_needs_sign_in_reads_only_local_sources(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    assert needs_sign_in(context)
    context.config = AppConfig(email=EMAIL)
    assert needs_sign_in(context)
    context.credential_resolver.store.set(EMAIL, "secret")
    assert not needs_sign_in(context)

    other = make_context(tmp_path / "other", email=EMAIL)
    monkeypatch.setenv("GUCMAIL_PASSWORD", "from-env")
    assert not needs_sign_in(other)
    monkeypatch.delenv("GUCMAIL_PASSWORD")
    (tmp_path / "creds.env").write_text('email = "x@example.test"\n', encoding="utf-8")
    assert not needs_sign_in(other)


def test_working_password_is_saved_and_the_tested_session_is_kept(tmp_path):
    context = make_context(tmp_path)
    adapter = FakeAdapter()
    result = sign_in(context, f"  {EMAIL} ", "secret", adapter_factory=lambda *_: adapter)
    assert result.checked and result.stored and result.warning is None
    assert result.status == {"connected": True, "inbox_count": 3}
    assert context.credential_resolver.store.get(EMAIL) == "secret"
    assert context.config.email == EMAIL and context.config.username == EMAIL
    assert context.config_store.load().email == EMAIL
    # The app reuses this connection instead of signing in again.
    assert context.gateway() is adapter and adapter.status_calls == 1
    assert not needs_sign_in(context)


def test_wrong_password_saves_nothing_and_claims_nothing(tmp_path):
    context = make_context(tmp_path)
    adapter = FakeAdapter(password_ok=False)
    with pytest.raises(AuthenticationError):
        sign_in(context, EMAIL, "wrong", adapter_factory=lambda *_: adapter)
    assert adapter.closed
    assert context.credential_resolver.store.get(EMAIL) is None
    assert context.config.email is None and context.config_store.load().email is None
    # A later, correct address can still claim the data folder.
    sign_in(context, "laila.samir@example.test", "ok", adapter_factory=lambda *_: FakeAdapter())
    assert context.config.email == "laila.samir@example.test"


def test_data_folder_of_another_account_is_refused_before_saving(tmp_path):
    context = make_context(tmp_path)
    sign_in(context, EMAIL, "one", adapter_factory=lambda *_: FakeAdapter())
    adapter = FakeAdapter()
    with pytest.raises(ConfigurationError, match="another mailbox"):
        sign_in(context, "omar.khaled@example.test", "two", adapter_factory=lambda *_: adapter)
    assert adapter.closed
    assert context.credential_resolver.store.get("omar.khaled@example.test") is None
    assert context.config.email == EMAIL


def test_unavailable_password_store_keeps_the_session_and_says_so(tmp_path):
    context = make_context(tmp_path, broken=True)
    adapter = FakeAdapter()
    result = sign_in(context, EMAIL, "secret", adapter_factory=lambda *_: adapter)
    assert not result.stored and result.warning and "asked again" in result.warning
    assert context.gateway() is adapter
    with pytest.raises(CredentialError):
        sign_in(make_context(tmp_path / "b", broken=True), EMAIL, "secret", check=False)


def test_separate_sign_in_name_is_used(tmp_path):
    context = make_context(tmp_path)
    seen = []
    sign_in(
        context,
        EMAIL,
        "secret",
        username="nour.ashraf",
        adapter_factory=lambda config, credentials: seen.append(credentials) or FakeAdapter(),
    )
    assert seen[0].username == "nour.ashraf" and context.config.username == "nour.ashraf"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("server text"), "email or password didn't work"),
        (NetworkError("server text"), "Couldn't reach the GUC mail server"),
        (CredentialError("Enter your full GUC email address."), "full GUC email"),
        (RuntimeError("secret detail"), "gucmail doctor"),
    ],
)
def test_problems_are_plain_and_never_echo_server_text(error, expected):
    text = sign_in_problem(error)
    assert expected in text
    assert "server text" not in text and "secret detail" not in text


def _login(tmp_path, monkeypatch, adapter, *args):
    context = make_context(tmp_path)
    monkeypatch.setattr(cli.core, "build_context", lambda **kwargs: context)
    monkeypatch.setattr(sign_in_module, "_default_adapter", lambda *_: adapter)
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "login", "--email", EMAIL, "--password-stdin", *args],
        input="secret\n",
    )
    return context, result


def test_cli_login_tests_before_saving(tmp_path, monkeypatch):
    context, result = _login(tmp_path, monkeypatch, FakeAdapter(password_ok=False))
    assert result.exit_code != 0
    assert context.credential_resolver.store.get(EMAIL) is None
    assert context.config_store.load().email is None


def test_cli_login_saves_a_working_password(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    context, result = _login(tmp_path, monkeypatch, adapter)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["checked"] is True and data["credential"] == "stored_in_os_keyring"
    assert data["status"]["inbox_count"] == 3
    assert context.credential_resolver.store.get(EMAIL) == "secret"
    assert adapter.status_calls == 1


def test_cli_login_without_check_does_not_connect(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    context, result = _login(tmp_path, monkeypatch, adapter, "--no-check")
    assert result.exit_code == 0, result.output
    assert adapter.status_calls == 0
    assert context.credential_resolver.store.get(EMAIL) == "secret"


# ---------------------------------------------------------------- the TUI


def _app(tmp_path, monkeypatch, adapters):
    context = make_context(tmp_path)
    app = GUCmailApp(context)
    started = []
    monkeypatch.setattr(app, "_start_online", lambda: started.append(True))
    monkeypatch.setattr(sign_in_module, "_default_adapter", lambda *_: adapters.pop(0))
    return context, app, started


def _sign_in_ready(app) -> bool:
    """The sign-in screen is up and its first field has focus (it mounts a moment later)."""
    screen = app.screen
    return isinstance(screen, SignInScreen) and any(
        field.has_focus for field in screen.query(Input)
    )


async def _type(pilot, screen, email, password):
    screen.query_one("#sign-in-email", Input).value = email
    screen.query_one("#sign-in-password", Input).value = password
    await press_button(pilot, screen.query_one("#sign-in-submit", Button))


@pytest.mark.asyncio
async def test_new_user_signs_in_then_takes_the_tour(tmp_path, monkeypatch):
    context, app, started = _app(
        tmp_path, monkeypatch, [FakeAdapter(password_ok=False), FakeAdapter()]
    )
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_until(pilot, lambda: _sign_in_ready(app))
        screen = app.screen
        assert screen.query_one("#sign-in-email", Input).has_focus
        assert not started  # nothing loads before sign-in

        await _type(pilot, screen, "nour", "secret")
        message = screen.query_one("#sign-in-message", Static)
        await wait_until(pilot, lambda: "full GUC email" in str(message.content))

        await _type(pilot, screen, EMAIL, "wrong")
        await wait_until(pilot, lambda: "didn't work" in str(message.content))
        assert screen.query_one("#sign-in-password", Input).value == ""
        assert context.credential_resolver.store.get(EMAIL) is None

        await _type(pilot, screen, EMAIL, "secret")
        # The tour becomes the screen a moment before its buttons exist.
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, WelcomeTourScreen) and app.screen.query("#tour-next"),
        )
        assert started and context.credential_resolver.store.get(EMAIL) == "secret"

        tour = app.screen
        for _ in range(3):
            await press_button(pilot, tour.query_one("#tour-next", Button))
        await wait_until(pilot, lambda: not isinstance(app.screen, WelcomeTourScreen))
        assert context.config.onboarding_complete
        assert context.config_store.load().onboarding_complete


@pytest.mark.asyncio
async def test_not_now_then_f5_opens_sign_in_again(tmp_path, monkeypatch):
    _context, app, started = _app(tmp_path, monkeypatch, [])
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_until(pilot, lambda: _sign_in_ready(app))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, SignInScreen))
        assert "Not signed in" in app._status_activity and not started
        await pilot.press("f5")
        await wait_until(pilot, lambda: _sign_in_ready(app))


@pytest.mark.asyncio
async def test_tour_is_skippable_and_shown_only_once(tmp_path, monkeypatch):
    context, app, _started = _app(tmp_path, monkeypatch, [FakeAdapter()])
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_until(pilot, lambda: _sign_in_ready(app))
        await _type(pilot, app.screen, EMAIL, "secret")
        await wait_until(pilot, lambda: isinstance(app.screen, WelcomeTourScreen))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, WelcomeTourScreen))
        assert context.config.onboarding_complete

    # Next start: signed in already, so neither screen appears.
    again = GUCmailApp(context)
    monkeypatch.setattr(again, "_start_online", lambda: None)
    async with again.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        assert not isinstance(again.screen, (SignInScreen, WelcomeTourScreen))


def test_bare_gucmail_prints_help_for_scripts(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("gucmail.tui.run_tui", lambda **kwargs: opened.append(kwargs))
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0 and "Usage" in result.output
    assert not opened


def test_bare_gucmail_opens_the_app_at_a_terminal(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    opened = []
    monkeypatch.setattr(cli.core, "build_context", lambda **kwargs: context)
    monkeypatch.setattr("gucmail.tui.run_tui", lambda **kwargs: opened.append(kwargs))
    monkeypatch.setattr(cli.main, "_at_terminal", lambda: True)
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert opened and opened[0]["offline"] is False
