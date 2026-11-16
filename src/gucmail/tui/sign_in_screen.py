"""The first screen for someone without a saved account: email, password, Test & save."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, Static

from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    CredentialError,
    NetworkError,
)
from gucmail.security import sanitize_terminal
from gucmail.services.sign_in import SignInResult, check_address

SignIn = Callable[[str, str, str | None], SignInResult]


def sign_in_problem(exc: BaseException) -> str:
    """A plain sentence for a failed sign-in. Never echoes server or exception text."""

    if isinstance(exc, AuthenticationError):
        return "That email or password didn't work. Check both and try again."
    if isinstance(exc, NetworkError):
        return "Couldn't reach the GUC mail server. Check your internet connection and try again."
    if isinstance(exc, (CredentialError, ConfigurationError)):
        # Our own messages, written for people.
        return sanitize_terminal(str(exc), max_length=300)
    return "Sign-in didn't work. Run `gucmail doctor` in a terminal to see why."


class SignInScreen(ModalScreen[SignInResult | None]):
    """Collect an address and password, test them once, and save them only if they work."""

    DEFAULT_CSS = """
    SignInScreen { align: center middle; }
    #sign-in-box {
        width: 68; max-width: 96%; height: auto; max-height: 96%; padding: 1 3;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #sign-in-title { text-style: bold; margin-bottom: 1; }
    #sign-in-intro { color: $text-muted; height: auto; margin-bottom: 1; }
    .sign-in-label { margin-top: 1; }
    #sign-in-more { margin-top: 1; height: auto; }
    #sign-in-message { height: auto; min-height: 1; margin-top: 1; }
    #sign-in-message.-problem { color: $error; }
    #sign-in-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #sign-in-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "not_now", "Not now", show=False),
    ]

    def __init__(self, sign_in: SignIn, *, email: str = "", server: str = "") -> None:
        super().__init__()
        self._sign_in = sign_in
        self._email = email
        self._server = server
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sign-in-box"):
            yield Label("Sign in to your GUC mailbox", id="sign-in-title")
            yield Static(
                "gucmail is an independent app, not made by GUC or Microsoft. Your password "
                "is kept in your system's password store, never in a file.",
                id="sign-in-intro",
                markup=False,
            )
            yield Label("Email", classes="sign-in-label")
            yield Input(
                value=self._email,
                placeholder="firstname.lastname@student.guc.edu.eg",
                id="sign-in-email",
            )
            yield Label("Password", classes="sign-in-label")
            yield Input(password=True, placeholder="Your GUC password", id="sign-in-password")
            with Collapsible(title="More options", collapsed=True, id="sign-in-more"):
                yield Label("Sign-in name (only if your email doesn't work)")
                yield Input(placeholder="firstname.lastname", id="sign-in-username")
                if self._server:
                    yield Static(
                        f"Server: {sanitize_terminal(self._server, max_length=120)}",
                        markup=False,
                    )
            yield Static("", id="sign-in-message", markup=False)
            with Horizontal(id="sign-in-actions"):
                yield Button("Not now", id="sign-in-not-now")
                yield Button("Test & save", id="sign-in-submit", variant="primary")

    def on_mount(self) -> None:
        target = "#sign-in-password" if self._email else "#sign-in-email"
        self.query_one(target, Input).focus()

    def _say(self, text: str, *, problem: bool = False) -> None:
        message = self.query_one("#sign-in-message", Static)
        message.update(text)
        message.set_class(problem, "-problem")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for selector in ("#sign-in-email", "#sign-in-password", "#sign-in-username"):
            self.query_one(selector, Input).disabled = busy
        self.query_one("#sign-in-submit", Button).disabled = busy

    def submit(self) -> None:
        if self.busy:
            return
        email = self.query_one("#sign-in-email", Input).value
        password = self.query_one("#sign-in-password", Input).value
        username = self.query_one("#sign-in-username", Input).value.strip() or None
        try:
            check_address(email)
        except CredentialError as exc:
            self._say(sign_in_problem(exc), problem=True)
            self.query_one("#sign-in-email", Input).focus()
            return
        if not password:
            self._say("Enter your password.", problem=True)
            self.query_one("#sign-in-password", Input).focus()
            return
        self._set_busy(True)
        self._say("Checking with the GUC mail server…")
        self._run(email, password, username)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _run(self, email: str, password: str, username: str | None) -> None:
        try:
            result = self._sign_in(email, password, username)
        except Exception as exc:
            self.app.call_from_thread(self._failed, sign_in_problem(exc))
        else:
            self.app.call_from_thread(self.dismiss, result)

    def _failed(self, text: str) -> None:
        self._set_busy(False)
        self._say(text, problem=True)
        password = self.query_one("#sign-in-password", Input)
        password.value = ""
        password.focus()

    @on(Input.Submitted)
    def field_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "sign-in-email":
            self.query_one("#sign-in-password", Input).focus()
        else:
            self.submit()

    @on(Button.Pressed, "#sign-in-submit")
    def submit_pressed(self) -> None:
        self.submit()

    @on(Button.Pressed, "#sign-in-not-now")
    def not_now_pressed(self) -> None:
        self.action_not_now()

    def action_not_now(self) -> None:
        if not self.busy:
            self.dismiss(None)
