"""Turn any failure into one plain sentence that says what to do next.

Never echoes raw exception text. Our own errors carry messages written for people.
"""

from __future__ import annotations

from gucmail.errors import (
    AuthenticationError,
    ConflictError,
    CredentialError,
    GUCmailError,
    ItemNotFoundError,
    MutationExecutionError,
    NetworkError,
    SendOutcomeUncertainError,
    ThrottledError,
)
from gucmail.security import sanitize_terminal

SIGN_IN_AGAIN = "Your password didn't work. Press F5 to sign in again."
UNREACHABLE = "Can't reach the GUC mail server. Check your connection, then try again."
BUSY = "The mail server is busy. Wait a minute, then try again."
GONE = "That item is gone. It may have been moved or deleted. Refresh and try again."
CHANGED = "That item changed on the server. Refresh and try again."
SOMETHING_WRONG = (
    "Something went wrong. Try again. If it keeps failing, run gucmail doctor in a terminal."
)
NO_ACCESS = "A local file or folder can't be opened. Check that it exists and you can write to it."


def _own(exc: GUCmailError) -> str:
    text = exc.message.rstrip(".") + "."
    if exc.context.suggestion:
        text += " " + exc.context.suggestion
    return text


def _is_network(exc: BaseException) -> bool:
    if isinstance(exc, (NetworkError, ConnectionError, TimeoutError)):
        return True
    # The offline test guard stops network calls with this error.
    return type(exc).__name__ == "OfflineViolation"


def plain_problem(exc: BaseException) -> str:
    """One or two short sentences for people."""

    if isinstance(exc, (MutationExecutionError, SendOutcomeUncertainError)):
        text = _own(exc)
    elif isinstance(exc, AuthenticationError):
        text = SIGN_IN_AGAIN
    elif isinstance(exc, CredentialError):
        text = _own(exc)
    elif isinstance(exc, ThrottledError):
        text = BUSY
    elif _is_network(exc):
        text = UNREACHABLE
    elif isinstance(exc, ItemNotFoundError) and "Exchange object" in exc.message:
        # The adapter's generic wording. Other not-found messages name the item.
        text = GONE
    elif isinstance(exc, ConflictError) and "object changed or was ambiguous" in exc.message:
        text = CHANGED
    elif isinstance(exc, GUCmailError):
        # Our own messages are written for people and say what went wrong.
        text = _own(exc)
    elif isinstance(exc, OSError):
        text = NO_ACCESS
    else:
        text = SOMETHING_WRONG
    return sanitize_terminal(text, max_length=600)
