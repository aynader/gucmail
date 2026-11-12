"""Bounded, traversal-safe attachment downloads with atomic completion."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from gucmail.constants import DEFAULT_MAX_ATTACHMENT_BYTES, MAX_PAGE_SIZE
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ItemNotFoundError,
)
from gucmail.file_completion import complete_temporary_file
from gucmail.models import AttachmentInfo
from gucmail.security import is_dangerous_attachment, safe_filename, unique_download_path
from gucmail.services._base import (
    PageRequest,
    ServicePage,
    bounded_page,
    make_mutation_plan,
    validate_reference,
)

DEFAULT_CHUNK_SIZE = 256 * 1024
MAX_ATTACHMENT_METADATA_SCAN = 2_000


class CollisionPolicy(StrEnum):
    RENAME = "rename"
    ERROR = "error"
    OVERWRITE = "overwrite"


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    attachment_reference: str
    bytes_downloaded: int
    total_bytes: int | None


@dataclass(frozen=True, slots=True)
class AttachmentSavePlan:
    message_reference: str
    attachment: AttachmentInfo
    directory: Path
    target: Path
    collision_policy: CollisionPolicy
    overwrite: bool
    requires_confirmation: bool
    confirmation_scope: str
    expected_sha256: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SavedAttachment:
    plan: AttachmentSavePlan
    path: Path | None
    bytes_written: int
    sha256: str | None
    dry_run: bool
    warnings: tuple[str, ...] = ()


class AttachmentGateway(Protocol):
    def list_attachments(
        self,
        message_reference: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AttachmentInfo]: ...

    def open_attachment(
        self, message_reference: str, attachment_reference: str
    ) -> BinaryIO | contextlib.AbstractContextManager[BinaryIO]: ...


class AttachmentService:
    def __init__(
        self,
        gateway: AttachmentGateway,
        *,
        max_download_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        collision_policy: CollisionPolicy | str = CollisionPolicy.RENAME,
    ) -> None:
        if max_download_bytes < 1:
            raise ConfigurationError("max_download_bytes must be positive.")
        if not 4096 <= chunk_size <= 4 * 1024 * 1024:
            raise ConfigurationError("Attachment chunks must be between 4 KiB and 4 MiB.")
        self.gateway = gateway
        self.max_download_bytes = max_download_bytes
        self.chunk_size = chunk_size
        self.collision_policy = self._collision_policy(collision_policy)

    def _collision_policy(self, value: CollisionPolicy | str | None) -> CollisionPolicy:
        if value is None:
            return self.collision_policy
        try:
            return CollisionPolicy(value)
        except ValueError as exc:
            raise ConfigurationError(
                "Collision policy must be rename, error, or overwrite."
            ) from exc

    def list_attachments(
        self,
        message_reference: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ServicePage[AttachmentInfo]:
        message = validate_reference(message_reference, label="message reference")
        request = PageRequest(limit=limit, offset=offset)
        items = tuple(
            self.gateway.list_attachments(message, limit=request.limit, offset=request.offset)
        )
        return bounded_page(items, request)

    def plan_save(
        self,
        message_reference: str,
        attachment_reference: str,
        destination_directory: Path,
        *,
        filename: str | None = None,
        collision: CollisionPolicy | str | None = None,
        expected_sha256: str | None = None,
        attachment: AttachmentInfo | None = None,
        reserved_targets: Sequence[Path] = (),
    ) -> AttachmentSavePlan:
        message = validate_reference(message_reference, label="message reference")
        reference = validate_reference(attachment_reference, label="attachment reference")
        policy = self._collision_policy(collision)
        metadata = attachment or self._find_attachment(message, reference)
        if metadata.reference != reference:
            raise ConfigurationError("Attachment metadata does not match the requested reference.")
        if metadata.size is not None and metadata.size > self.max_download_bytes:
            raise ConfigurationError(
                f"Attachment {metadata.name!r} exceeds the configured download limit."
            )

        directory = Path(destination_directory).expanduser().resolve(strict=False)
        if directory.exists() and not directory.is_dir():
            raise ConfigurationError("Attachment destination is not a directory.")
        requested_name = filename if filename is not None else metadata.name
        normalized_name = safe_filename(requested_name)
        lexical_candidate = directory / normalized_name
        if lexical_candidate.is_symlink():
            raise ConfigurationError("Refusing to replace an attachment destination symlink.")
        candidate = lexical_candidate.resolve(strict=False)
        if candidate.parent != directory:
            raise ConfigurationError("The attachment target would escape its destination.")

        warnings: list[str] = []
        if normalized_name != requested_name:
            warnings.append(f"Unsafe filename was normalized to {normalized_name!r}.")
        dangerous = metadata.dangerous or is_dangerous_attachment(normalized_name)
        if dangerous:
            warnings.append(
                "This file type can execute code. It will be saved only and never opened."
            )
        if metadata.is_item_attachment:
            warnings.append(
                "This is an attached Exchange item; the gateway determines its exported format."
            )

        if policy is CollisionPolicy.ERROR and candidate.exists():
            raise ConflictError(f"Destination file already exists: {candidate.name}")
        if policy is CollisionPolicy.RENAME:
            target = unique_download_path(directory, normalized_name, reserved=reserved_targets)
            overwrite = False
        else:
            if os.path.normcase(str(candidate)) in {
                os.path.normcase(str(path)) for path in reserved_targets
            }:
                raise ConflictError(
                    f"Multiple attachments would use the same destination: {candidate.name}. "
                    "Choose rename collisions or save them individually with distinct names."
                )
            target = candidate
            overwrite = policy is CollisionPolicy.OVERWRITE
        requires_confirmation = overwrite and target.exists()

        digest = _validate_digest(expected_sha256) if expected_sha256 else None
        scope = make_mutation_plan(
            operation="attachment_save",
            targets=(message, reference),
            summary=f"Save attachment to {target}.",
            arguments={
                "target": str(target),
                "overwrite": overwrite,
                "expected_sha256": digest,
            },
            requires_confirmation=requires_confirmation,
        ).confirmation_scope
        return AttachmentSavePlan(
            message_reference=message,
            attachment=metadata,
            directory=directory,
            target=target,
            collision_policy=policy,
            overwrite=overwrite,
            requires_confirmation=requires_confirmation,
            confirmation_scope=scope,
            expected_sha256=digest,
            warnings=tuple(warnings),
        )

    def execute_save(
        self,
        plan: AttachmentSavePlan,
        *,
        dry_run: bool = False,
        confirmation_scope: str | None = None,
        calculate_sha256: bool = False,
        progress: Callable[[DownloadProgress], None] | None = None,
    ) -> SavedAttachment:
        if dry_run:
            return SavedAttachment(
                plan=plan,
                path=None,
                bytes_written=0,
                sha256=None,
                dry_run=True,
                warnings=plan.warnings,
            )
        if plan.requires_confirmation and (
            confirmation_scope is None
            or not hmac.compare_digest(confirmation_scope, plan.confirmation_scope)
        ):
            raise ConfirmationRequiredError(
                "Overwriting an existing attachment requires confirmation for that exact path."
            )
        resolved_directory = plan.directory.resolve(strict=False)
        resolved_target = plan.target.resolve(strict=False)
        if resolved_directory != plan.directory:
            raise ConflictError("The attachment destination changed after its preview was created.")
        if plan.target.is_symlink() or resolved_target != plan.target:
            raise ConflictError("The attachment target changed after its preview was created.")
        if resolved_target.parent != resolved_directory:
            raise ConfigurationError("The attachment target no longer stays in its destination.")
        if resolved_target.exists() and (not plan.overwrite or not plan.requires_confirmation):
            raise ConflictError(
                f"Destination appeared after the preview was created: {resolved_target.name}"
            )
        if plan.attachment.size is not None and plan.attachment.size > self.max_download_bytes:
            raise ConfigurationError("The attachment now exceeds the configured download limit.")

        resolved_directory.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{resolved_target.name}.", suffix=".part", dir=resolved_directory
        )
        temporary = Path(temporary_name)
        downloaded = 0
        digest = hashlib.sha256()
        need_digest = calculate_sha256 or plan.expected_sha256 is not None
        warnings = list(plan.warnings)
        try:
            with os.fdopen(fd, "wb") as destination, contextlib.ExitStack() as stack:
                opened = self.gateway.open_attachment(
                    plan.message_reference, plan.attachment.reference
                )
                if hasattr(opened, "__enter__") and hasattr(opened, "__exit__"):
                    source = stack.enter_context(opened)
                else:
                    source = cast(BinaryIO, opened)
                    close = getattr(source, "close", None)
                    if callable(close):
                        stack.callback(close)
                if progress:
                    progress(
                        DownloadProgress(
                            plan.attachment.reference,
                            0,
                            plan.attachment.size,
                        )
                    )
                while True:
                    chunk = source.read(self.chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ConfigurationError("Attachment gateway returned non-byte content.")
                    downloaded += len(chunk)
                    if downloaded > self.max_download_bytes:
                        raise ConfigurationError(
                            "Attachment exceeded the configured limit while downloading."
                        )
                    destination.write(chunk)
                    if need_digest:
                        digest.update(chunk)
                    if progress:
                        progress(
                            DownloadProgress(
                                plan.attachment.reference,
                                downloaded,
                                plan.attachment.size,
                            )
                        )
                destination.flush()
                os.fsync(destination.fileno())

            checksum = digest.hexdigest() if need_digest else None
            if plan.expected_sha256 and not hmac.compare_digest(
                checksum or "", plan.expected_sha256
            ):
                raise ConflictError("Downloaded attachment did not match the expected SHA-256.")
            if plan.attachment.size is not None and downloaded != plan.attachment.size:
                warnings.append(
                    "The server-reported attachment size differed from the downloaded size."
                )
            # A target absent at review never receives overwrite authorization, even
            # when the selected policy is overwrite. Exclusive completion closes the
            # race where another process creates it while the content is streaming.
            _commit_download(
                temporary,
                resolved_target,
                overwrite=plan.overwrite and plan.requires_confirmation,
            )
            return SavedAttachment(
                plan=plan,
                path=resolved_target,
                bytes_written=downloaded,
                sha256=checksum if calculate_sha256 or plan.expected_sha256 else None,
                dry_run=False,
                warnings=tuple(warnings),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def plan_save_all(
        self,
        message_reference: str,
        destination_directory: Path,
        *,
        limit: int = 100,
        collision: CollisionPolicy | str | None = None,
    ) -> tuple[AttachmentSavePlan, ...]:
        """Prepare the entire bounded batch before any content is downloaded."""

        policy = self._collision_policy(collision)
        page = self.list_attachments(message_reference, limit=limit)
        plans: list[AttachmentSavePlan] = []
        for attachment in page.items:
            plans.append(
                self.plan_save(
                    message_reference,
                    attachment.reference,
                    destination_directory,
                    collision=policy,
                    attachment=attachment,
                    reserved_targets=tuple(plan.target for plan in plans),
                )
            )
        return tuple(plans)

    def save_all(
        self,
        message_reference: str,
        destination_directory: Path,
        *,
        limit: int = 100,
        collision: CollisionPolicy | str | None = None,
        dry_run: bool = False,
        calculate_sha256: bool = False,
        progress: Callable[[DownloadProgress], None] | None = None,
    ) -> tuple[SavedAttachment, ...]:
        request = PageRequest(limit=limit, offset=0)
        if request.limit > MAX_PAGE_SIZE:
            raise ConfigurationError("Too many attachments requested.")
        plans = self.plan_save_all(
            message_reference,
            destination_directory,
            limit=request.limit,
            collision=collision,
        )
        if not dry_run and any(plan.requires_confirmation for plan in plans):
            raise ConfirmationRequiredError(
                "Review all prepared paths and confirm each overwrite before saving this batch."
            )
        saved: list[SavedAttachment] = []
        for plan in plans:
            saved.append(
                self.execute_save(
                    plan,
                    dry_run=dry_run,
                    confirmation_scope=None,
                    calculate_sha256=calculate_sha256,
                    progress=progress,
                )
            )
        return tuple(saved)

    def _find_attachment(self, message_reference: str, attachment_reference: str) -> AttachmentInfo:
        offset = 0
        while offset < MAX_ATTACHMENT_METADATA_SCAN:
            limit = min(MAX_PAGE_SIZE, MAX_ATTACHMENT_METADATA_SCAN - offset)
            page = tuple(
                self.gateway.list_attachments(
                    message_reference,
                    limit=limit,
                    offset=offset,
                )
            )
            if len(page) > limit:
                page = page[:limit]
            matches = [item for item in page if item.reference == attachment_reference]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ConflictError("Attachment reference is ambiguous.")
            if len(page) < limit:
                break
            offset += len(page)
        if offset >= MAX_ATTACHMENT_METADATA_SCAN:
            raise ConfigurationError("Attachment lookup reached its safe metadata scan limit.")
        raise ItemNotFoundError(f"Attachment not found: {attachment_reference}")


def _validate_digest(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ConfigurationError("Expected SHA-256 must contain exactly 64 hexadecimal digits.")
    return normalized


def _commit_download(temporary: Path, target: Path, *, overwrite: bool) -> None:
    complete_temporary_file(temporary, target, overwrite=overwrite)


Page = ServicePage

__all__ = [
    "AttachmentGateway",
    "AttachmentSavePlan",
    "AttachmentService",
    "CollisionPolicy",
    "DownloadProgress",
    "Page",
    "SavedAttachment",
]
