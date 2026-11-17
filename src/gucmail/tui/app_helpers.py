"""Small helpers shared by the main window and its screens."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _safe_failure(exc: BaseException, *, action: str = "") -> str:
    """A plain failure line that says what to do. Never echoes raw exception text."""

    from gucmail.problems import plain_problem

    problem = plain_problem(exc)
    return f"{action}. {problem}" if action else problem


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
