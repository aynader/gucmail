"""Complete owned temporary files without replacing an unapproved destination."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

from gucmail.errors import ConflictError


def _same_file(path: Path, identity: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


def complete_temporary_file(temporary: Path, target: Path, *, overwrite: bool) -> None:
    if overwrite:
        os.replace(temporary, target)
        return
    identity = temporary.stat(follow_symlinks=False)
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ConflictError(f"Destination file already exists: {target.name}") from exc
    except OSError:
        # Keep the exclusive destination handle open while copying. Closing a reservation
        # and then replacing its pathname creates a window that can overwrite a new file.
        created: os.stat_result | None = None
        try:
            with target.open("xb") as destination:
                created = os.fstat(destination.fileno())
                with temporary.open("rb") as source:
                    shutil.copyfileobj(source, destination, length=256 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if not _same_file(target, created):
                raise ConflictError(
                    "Destination changed during completion; the new file was preserved."
                )
        except FileExistsError as exc:
            raise ConflictError(f"Destination file already exists: {target.name}") from exc
        except BaseException:
            if created is not None and _same_file(target, created):
                with contextlib.suppress(OSError):
                    target.unlink()
            raise
        temporary.unlink()
    else:
        if not _same_file(target, identity):
            raise ConflictError(
                "Destination changed during completion; the new file was preserved."
            )
        temporary.unlink()
