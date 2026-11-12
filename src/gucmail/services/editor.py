"""Safe external-editor integration for local draft bodies.

The editor receives one private temporary UTF-8 file as an argument vector.
No shell parses the command, and no mailbox-derived text is interpolated into
arguments.  A recovery copy is written before launch and again when a failing
editor left useful changes behind.
"""

from __future__ import annotations

import codecs
import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.models import DraftData
from gucmail.security import safe_filename
from gucmail.services.drafts import DraftService, draft_from_dict, draft_to_dict

EDITOR_FILE_PREAMBLE = "# GUC Mail CLI draft body - this line is removed on save\n"
MAX_EDITOR_BODY_BYTES = 20_000_000


class ExternalEditorError(LocalStateError):
    """The editor did not return a safely readable draft."""


class EditorRunner(Protocol):
    def __call__(self, arguments: Sequence[str], **kwargs: Any) -> object: ...


def _default_editor_runner(arguments: Sequence[str], **kwargs: Any) -> object:
    """Typed adapter around ``subprocess.run``; callers still pass ``shell=False``."""

    return subprocess.run(list(arguments), **kwargs)  # noqa: S603 - argv only; shell is disabled


@dataclass(frozen=True, slots=True)
class EditorResult:
    draft: DraftData
    recovery_path: Path
    editor_arguments: tuple[str, ...]
    return_code: int


def parse_editor_command(command: str | Sequence[str]) -> tuple[str, ...]:
    """Parse user configuration into argv without invoking a command shell."""

    values: tuple[str, ...]
    if isinstance(command, str):
        raw = command.strip()
        if not raw:
            raise ConfigurationError("No external editor is configured.")
        # An existing path is unambiguously a single executable, including a
        # Windows path containing spaces. Otherwise conventional quoting is
        # accepted, but the resulting argv is still executed directly.
        if Path(raw).expanduser().exists():
            values = (str(Path(raw).expanduser()),)
        else:
            try:
                parsed = shlex.split(raw, posix=os.name != "nt")
            except ValueError as exc:
                raise ConfigurationError(f"External editor command is invalid: {exc}") from exc
            values = tuple(_strip_matching_quotes(item) for item in parsed)
    else:
        values = tuple(str(item) for item in command)
    if not values or not values[0].strip():
        raise ConfigurationError("No external editor executable is configured.")
    if any(not item or any(character in item for character in "\x00\r\n") for item in values):
        raise ConfigurationError(
            "External editor arguments may not be empty or contain control lines."
        )
    for item in values:
        if "{file}" in item and item != "{file}":
            raise ConfigurationError("Use {file} as a separate editor argument only.")
    if sum(item == "{file}" for item in values) > 1:
        raise ConfigurationError("The editor command may contain {file} only once.")
    return values


def build_editor_arguments(command: str | Sequence[str], path: Path) -> tuple[str, ...]:
    values = parse_editor_command(command)
    if "{file}" in values:
        return tuple(str(path) if item == "{file}" else item for item in values)
    return (*values, str(path))


class ExternalDraftEditor:
    """Edit a draft body while keeping SQLite and recovery state independent."""

    def __init__(
        self,
        drafts: DraftService,
        command: str | Sequence[str],
        *,
        runner: EditorRunner | None = None,
        temporary_directory: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("External editor timeout must be positive.")
        self.drafts = drafts
        self.command = parse_editor_command(command)
        self.runner = runner or _default_editor_runner
        self.temporary_directory = temporary_directory
        self.timeout_seconds = timeout_seconds

    def edit(self, reference: str) -> EditorResult:
        draft = self.drafts.get(reference)
        recovery_path = self.drafts.write_recovery(draft)
        path = self._create_editor_file(draft)
        arguments = build_editor_arguments(self.command, path)
        return_code = -1
        editor_error: BaseException | None = None
        try:
            try:
                completed = self.runner(
                    arguments,
                    check=False,
                    shell=False,
                    timeout=self.timeout_seconds,
                )
                return_code = int(getattr(completed, "returncode", 0))
            except (OSError, subprocess.SubprocessError) as exc:
                editor_error = exc

            edited: DraftData | None = None
            if path.exists():
                try:
                    body = _read_editor_body(path)
                    edited = draft_from_dict(draft_to_dict(draft))
                    edited.body_text = body
                    # Preserve any changes a nonzero editor left behind. They
                    # remain a recovery conflict rather than silently replacing
                    # the durable draft record.
                    if body != draft.body_text:
                        self.drafts.write_recovery(edited)
                except (OSError, UnicodeError, LocalStateError) as exc:
                    if editor_error is None:
                        editor_error = exc
            elif editor_error is None:
                editor_error = ExternalEditorError(
                    "The external editor removed its temporary draft file. "
                    "The original recovery copy is still available."
                )

            if editor_error is not None:
                raise ExternalEditorError(
                    "The external editor failed; the recoverable draft was preserved "
                    f"({type(editor_error).__name__})."
                ) from editor_error
            if return_code != 0:
                raise ExternalEditorError(
                    f"The external editor exited with status {return_code}; "
                    "its recoverable draft was preserved."
                )
            if edited is None:
                raise ExternalEditorError(
                    "The external editor produced no readable draft; recovery was preserved."
                )
            saved = self.drafts.save(edited, expected_revision=draft.revision)
            return EditorResult(
                draft=saved,
                recovery_path=recovery_path,
                editor_arguments=arguments,
                return_code=return_code,
            )
        finally:
            with suppress(OSError):
                path.unlink(missing_ok=True)

    def _create_editor_file(self, draft: DraftData) -> Path:
        directory = self.temporary_directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f"gucmail-{safe_filename(draft.reference, fallback='draft')[:40]}-",
            suffix=".txt",
            dir=directory,
        )
        path = Path(name)
        try:
            with suppress(OSError):
                os.chmod(path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(EDITOR_FILE_PREAMBLE)
                handle.write(draft.body_text)
                handle.flush()
                os.fsync(handle.fileno())
            return path
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _decode_editor_bytes(raw: bytes) -> str:
    if len(raw) > MAX_EDITOR_BODY_BYTES:
        raise ExternalEditorError("The external editor draft exceeds the safe size limit.")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("utf-8")


def _read_editor_body(path: Path) -> str:
    text = _decode_editor_bytes(path.read_bytes())
    if text.startswith(EDITOR_FILE_PREAMBLE):
        return text[len(EDITOR_FILE_PREAMBLE) :]
    # Editors may normalize the line ending of the comment preamble.
    normalized = EDITOR_FILE_PREAMBLE.rstrip("\n")
    if text.startswith(f"{normalized}\r\n"):
        return text[len(normalized) + 2 :]
    return text
