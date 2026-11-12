"""Bounded, confirmation-scoped raw-message export for TUI and other clients."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from gucmail.errors import ConfigurationError, ConflictError
from gucmail.security import safe_filename
from gucmail.services._base import (
    MutationExecution,
    MutationPlan,
    make_mutation_plan,
    normalize_targets,
    require_confirmation,
)


class RawMessageGateway(Protocol):
    def raw_message(self, reference: str) -> bytes: ...


class MessageExportService:
    """Export an exact bounded reference set without overwriting existing files."""

    def __init__(
        self,
        gateway: RawMessageGateway,
        *,
        maximum_message_bytes: int = 100 * 1024 * 1024,
        maximum_total_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        if maximum_message_bytes < 1 or maximum_total_bytes < maximum_message_bytes:
            raise ValueError("Message export byte limits are invalid.")
        self.gateway = gateway
        self.maximum_message_bytes = maximum_message_bytes
        self.maximum_total_bytes = maximum_total_bytes

    def plan(
        self,
        references: tuple[str, ...] | list[str],
        destination: Path | str,
        *,
        subjects: Mapping[str, str] | None = None,
    ) -> MutationPlan:
        targets = normalize_targets(list(references))
        directory = Path(destination).expanduser().resolve(strict=False)
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ConfigurationError("The message export destination must be a real directory.")
        labels = subjects or {}
        filenames: list[str] = []
        for index, reference in enumerate(targets, 1):
            subject = safe_filename(labels.get(reference, "message"), fallback="message")
            stem = Path(subject).stem[:100] or "message"
            # The stable local reference makes same-subject exports collision resistant while
            # retaining a human-readable filename.
            reference_label = safe_filename(reference, fallback=str(index))[-24:]
            filenames.append(f"{stem} -- {reference_label}.eml")
        return make_mutation_plan(
            operation="export_raw_messages",
            targets=targets,
            summary=(
                f"Export exactly {len(targets)} selected message(s) as raw .eml files to "
                f"{directory}. Existing files will not be overwritten."
            ),
            arguments={"destination": str(directory), "filenames": tuple(filenames)},
            requires_confirmation=True,
        )

    def execute(
        self,
        plan: MutationPlan,
        *,
        confirmation_scope: str | None,
    ) -> MutationExecution:
        if plan.operation != "export_raw_messages":
            raise ConfigurationError("The supplied plan is not a raw-message export plan.")
        require_confirmation(plan, confirmation_scope)
        values = dict(plan.arguments)
        destination = Path(str(values.get("destination", ""))).resolve(strict=False)
        raw_filenames = values.get("filenames")
        if not isinstance(raw_filenames, tuple) or len(raw_filenames) != len(plan.targets):
            raise ConfigurationError("The message export plan is incomplete.")
        if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
            raise ConfigurationError("The message export destination must be a real directory.")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve(strict=True)
        paths = tuple(
            (destination / str(filename)).resolve(strict=False) for filename in raw_filenames
        )
        if any(path.parent != destination for path in paths):
            raise ConfigurationError("A message export path would escape its destination.")
        existing = tuple(path for path in paths if path.exists() or path.is_symlink())
        if existing:
            raise ConflictError(
                "Message export refused because a destination already exists: " + str(existing[0])
            )

        created: list[Path] = []
        total_bytes = 0
        try:
            for reference, path in zip(plan.targets, paths, strict=True):
                payload = self.gateway.raw_message(reference)
                if not isinstance(payload, bytes):
                    raise ConfigurationError("The mailbox provider returned invalid raw MIME data.")
                if len(payload) > self.maximum_message_bytes:
                    raise ConfigurationError(
                        f"Raw message {reference!r} exceeds the configured export size limit."
                    )
                total_bytes += len(payload)
                if total_bytes > self.maximum_total_bytes:
                    raise ConfigurationError("The selected messages exceed the total export limit.")
                self._write_new_file(path, payload)
                created.append(path)
        except BaseException:
            for path in reversed(created):
                with suppress(OSError):
                    path.unlink()
            raise
        return MutationExecution(
            plan=plan,
            dry_run=False,
            executed=True,
            results=tuple(paths),
        )

    @staticmethod
    def _write_new_file(path: Path, payload: bytes) -> None:
        """Create one private file exclusively; never replace a collision."""

        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)


__all__ = ["MessageExportService", "RawMessageGateway"]
