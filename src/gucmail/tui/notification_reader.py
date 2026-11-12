"""A bounded read-only notification reader with no automatic read-state writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from gucmail.errors import ConfigurationError
from gucmail.security import html_to_safe_text, redact_text, sanitize_terminal
from gucmail.services.windows_desktop import account_fingerprint

if TYPE_CHECKING:
    from gucmail.bootstrap import AppContext


class NotificationReaderApp(App[None]):
    TITLE = "GUC Mail · notification"
    CSS = """
    #toast-reader-status { height: auto; padding: 1; }
    #toast-reader-content { width: 100%; height: auto; padding: 1 2; }
    #toast-reader-scroll { height: 1fr; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q,escape", "quit", "Close"),
        Binding("f5", "reload", "Refresh"),
    ]

    def __init__(self, context: AppContext, account: str, reference: str) -> None:
        super().__init__()
        self.context = context
        self.account = account
        self.reference = reference
        self._generation = 0

    def compose(self) -> ComposeResult:
        yield Static("Opening notification… · Esc closes", id="toast-reader-status", markup=False)
        with VerticalScroll(id="toast-reader-scroll"):
            yield Static("", id="toast-reader-content", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#toast-reader-scroll").focus()
        self.action_reload()

    def action_reload(self) -> None:
        if any(worker.is_running for worker in self.workers):
            return
        self._generation += 1
        self.query_one("#toast-reader-status", Static).update(
            "Loading selected message… · Esc closes"
        )
        self._read(self._generation)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _read(self, generation: int) -> None:
        try:
            if (
                account_fingerprint(self.context.config.email or "", self.context.config.endpoint)
                != self.account
            ):
                raise ConfigurationError(
                    "The configured account changed; this notification cannot be opened."
                )
            gateway = self.context.gateway(interactive=False, allow_legacy=False)
            # AppContext.gateway checks resolved credentials against the persistent
            # mailbox owner before constructing the adapter or authenticating.
            detail = gateway.show_message(self.reference)
            summary = detail.summary
            body = detail.body_text or html_to_safe_text(detail.body_html or "")
            content = "\n".join(
                (
                    f"Subject: {summary.subject}",
                    f"From: {summary.sender.name or summary.sender.email}"
                    if summary.sender
                    else "From: (unknown)",
                    f"Local reference: {self.reference}",
                    "",
                    body,
                )
            )
            self.call_from_thread(
                self._complete, generation, sanitize_terminal(content, max_length=1_000_000), False
            )
        except Exception as exc:
            self.call_from_thread(
                self._complete,
                generation,
                redact_text(sanitize_terminal(str(exc), max_length=700)),
                True,
            )

    def _complete(self, generation: int, content: str, failed: bool) -> None:
        if generation != self._generation:
            return
        self.query_one("#toast-reader-content", Static).update(content)
        self.query_one("#toast-reader-status", Static).update(
            "Message unavailable · F5 retries · Esc closes"
            if failed
            else "Read-only message · F5 refreshes · Esc closes"
        )
