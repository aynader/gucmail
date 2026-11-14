"""Small helpers shared by the main window and its screens."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

from gucmail.errors import (
    MutationExecutionError,
)
from gucmail.security import sanitize_terminal


def _safe_failure(exc: BaseException, *, action: str = "Operation unavailable") -> str:
    """Return a useful failure label without echoing arbitrary exception text."""

    if isinstance(exc, MutationExecutionError):
        return exc.message + " " + (exc.context.suggestion or "")
    error_type = sanitize_terminal(type(exc).__name__, max_length=100) or "Error"
    return f"{action} ({error_type}). Details omitted; use gucmail doctor or redacted logs."


def _open_file_with_registered_handler(path: Path) -> None:
    """Open one validated file through the OS registry without a command shell."""

    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise OSError("The exported message is not a regular file.")
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if not callable(startfile):
            raise OSError("The Windows registered-file launcher is unavailable.")
        startfile(str(resolved))
        return
    command = (
        ["open", "--", str(resolved)] if sys.platform == "darwin" else ["xdg-open", str(resolved)]
    )
    subprocess.Popen(  # noqa: S603 - fixed executable plus one validated absolute path
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _compose_attachment_paths(value: str) -> list[str]:
    """Read legacy delimited input or an unambiguous encoded list from the file picker."""
    if value.startswith("["):
        with contextlib.suppress(json.JSONDecodeError):
            decoded = json.loads(value)
            if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
                return decoded
    return [part.strip() for part in value.replace("\n", ";").split(";") if part.strip()]


def _is_mail_folder_class(folder_class: str | None) -> bool:
    """True unless Exchange explicitly marks the folder as a non-mail item store."""

    if not folder_class:
        return True
    return folder_class.casefold().startswith("ipf.note") or folder_class.casefold() == "ipf"
