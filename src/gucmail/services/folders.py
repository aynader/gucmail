"""Safe folder navigation and preview-first folder mutations."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Any, Protocol

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError, ConflictError, ItemNotFoundError
from gucmail.models import FolderInfo
from gucmail.services._base import (
    MutationExecution,
    MutationPlan,
    MutationRisk,
    PageRequest,
    ServicePage,
    bounded_page,
    make_mutation_plan,
    require_confirmation,
    validate_reference,
)

ROOT_TARGET = "<mailbox-root>"
MAX_FOLDER_DEPTH = 64
MAX_SIBLINGS_SCANNED = 2_000


class FolderGateway(Protocol):
    """Minimal bounded folder API implemented by an Exchange adapter or a test fake."""

    def list_folders(
        self,
        parent_reference: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[FolderInfo]: ...

    def get_folder(self, reference: str) -> FolderInfo: ...

    def create_folder(self, parent_reference: str | None, name: str) -> FolderInfo: ...

    def rename_folder(self, reference: str, new_name: str) -> FolderInfo: ...

    def move_folder(
        self,
        reference: str,
        destination_reference: str | None,
    ) -> FolderInfo: ...

    def delete_folder(self, reference: str, permanent: bool = False) -> Any: ...

    def empty_folder(self, reference: str, permanent: bool = False) -> Any: ...


def parse_folder_path(path: str) -> tuple[str, ...]:
    r"""Parse ``/`` paths with ``\/`` and ``\\`` escapes and no traversal tokens.

    Exchange folder display names are opaque labels, not local filesystem paths.  Treating
    ``.`` or ``..`` specially would therefore be both surprising and unsafe, so they are
    rejected. A literal slash in a display name is written as ``\/``.
    """

    if not isinstance(path, str):
        raise ConfigurationError("Folder path must be text.")
    text = path.strip()
    if text == "/":
        return ()
    if not text:
        raise ConfigurationError("Folder path may not be empty.")
    if text.startswith("/"):
        text = text[1:]
    if not text:
        raise ConfigurationError("Folder path contains an empty component.")

    components: list[str] = []
    current: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            if character not in {"/", "\\"}:
                raise ConfigurationError(
                    "A folder path backslash may escape only '/' or another backslash."
                )
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "/":
            components.append(_validate_component("".join(current)))
            current = []
        else:
            current.append(character)
    if escaped:
        raise ConfigurationError("Folder path ends with an incomplete escape.")
    components.append(_validate_component("".join(current)))
    if len(components) > MAX_FOLDER_DEPTH:
        raise ConfigurationError(f"Folder paths may contain at most {MAX_FOLDER_DEPTH} levels.")
    return tuple(components)


def format_folder_path(components: Sequence[str], *, absolute: bool = False) -> str:
    checked = tuple(_validate_component(component) for component in components)
    encoded = "/".join(item.replace("\\", "\\\\").replace("/", "\\/") for item in checked)
    if not encoded:
        return "/"
    return f"/{encoded}" if absolute else encoded


def validate_folder_name(name: str) -> str:
    """Validate one opaque display name (slashes are allowed and escaped in paths)."""

    if not isinstance(name, str):
        raise ConfigurationError("Folder name must be text.")
    return _validate_component(unicodedata.normalize("NFC", name.strip()))


def _validate_component(component: str) -> str:
    # Existing Exchange display names are opaque: normalization could select a
    # different name. New-name validation applies its own explicit NFC policy.
    value = component
    if not value or value.isspace():
        raise ConfigurationError("Folder path contains an empty component.")
    if value in {".", ".."}:
        raise ConfigurationError("Folder paths may not contain '.' or '..' components.")
    if len(value) > 255:
        raise ConfigurationError("A folder path component may not exceed 255 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError("Folder names may not contain control characters.")
    return value


class FolderService:
    def __init__(
        self,
        gateway: FolderGateway,
        *,
        resolution_page_size: int = MAX_PAGE_SIZE,
        max_siblings_scanned: int = MAX_SIBLINGS_SCANNED,
    ) -> None:
        self.gateway = gateway
        self.resolution_page_size = PageRequest(limit=resolution_page_size).limit
        if max_siblings_scanned < 1:
            raise ConfigurationError("max_siblings_scanned must be positive.")
        self.max_siblings_scanned = max_siblings_scanned

    def list_folders(
        self,
        parent_reference: str | None = None,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> ServicePage[FolderInfo]:
        request = PageRequest(limit=limit, offset=offset)
        parent = (
            validate_reference(parent_reference, label="parent folder reference")
            if parent_reference is not None
            else None
        )
        folders = tuple(
            self.gateway.list_folders(
                parent_reference=parent,
                limit=request.limit,
                offset=request.offset,
            )
        )
        return bounded_page(folders, request)

    def resolve(self, selector: str) -> FolderInfo:
        """Resolve either ``id:<opaque-reference>`` or a safely parsed display path."""

        value = validate_reference(selector, label="folder selector")
        if value.casefold().startswith("id:"):
            reference = validate_reference(value[3:], label="folder reference")
            return self.gateway.get_folder(reference)
        components = parse_folder_path(value)
        if not components:
            raise ConfigurationError("The mailbox root is not an actionable folder.")

        direct_resolution = getattr(self.gateway, "resolve_folder_path", None)
        if callable(direct_resolution):
            resolved = direct_resolution(value)
            if not isinstance(resolved, FolderInfo):
                raise ConfigurationError("Folder resolution returned invalid folder metadata.")
            if (
                resolved.distinguished_role == "msgfolderroot"
                or resolved.folder_class == "IPF.Root"
            ):
                raise ConfigurationError("The mailbox root is not an actionable folder.")
            return resolved

        parent: str | None = None
        selected: FolderInfo | None = None
        for depth, component in enumerate(components):
            matches: list[FolderInfo] = []
            offset = 0
            scanned = 0
            while scanned < self.max_siblings_scanned:
                limit = min(
                    self.resolution_page_size,
                    self.max_siblings_scanned - scanned,
                )
                page = tuple(
                    self.gateway.list_folders(
                        parent_reference=parent,
                        limit=limit,
                        offset=offset,
                    )
                )
                if len(page) > limit:
                    page = page[:limit]
                for folder in page:
                    names = {folder.name.casefold()}
                    if depth == 0 and folder.distinguished_role:
                        names.add(folder.distinguished_role.casefold())
                    if component.casefold() in names:
                        matches.append(folder)
                scanned += len(page)
                offset += len(page)
                if len(page) < limit:
                    break
                if not page:
                    break
            if not matches and scanned >= self.max_siblings_scanned:
                raise ConfigurationError(
                    "Folder resolution reached its safe sibling scan limit. Use id:<reference> "
                    "or narrow the path."
                )
            if not matches:
                partial = format_folder_path(components[: depth + 1])
                raise ItemNotFoundError(f"Folder not found: {partial}")
            unique = {match.reference: match for match in matches}
            if len(unique) != 1:
                partial = format_folder_path(components[: depth + 1])
                raise ConflictError(
                    f"Folder path is ambiguous: {partial}. Use id:<reference> instead."
                )
            selected = next(iter(unique.values()))
            parent = selected.reference
        if selected is None:  # Defensive; non-empty components always select or raise above.
            raise ItemNotFoundError("Folder not found.")
        return selected

    def plan_create(self, name: str, *, parent: str | None = None) -> MutationPlan:
        folder_name = validate_folder_name(name)
        parent_reference = self.resolve(parent).reference if parent is not None else None
        target = parent_reference or ROOT_TARGET
        return make_mutation_plan(
            operation="folder_create",
            targets=(target,),
            summary=f"Create folder {folder_name!r} below {target!r}.",
            arguments={"name": folder_name, "parent_reference": parent_reference},
        )

    def plan_rename(self, folder: str, new_name: str) -> MutationPlan:
        current = self.resolve(folder)
        normalized = validate_folder_name(new_name)
        if current.name == normalized:
            raise ConfigurationError("The folder already has that name.")
        return make_mutation_plan(
            operation="folder_rename",
            targets=(current.reference,),
            summary=f"Rename folder {current.name!r} to {normalized!r}.",
            arguments={"new_name": normalized},
        )

    def plan_move(self, folder: str, destination: str) -> MutationPlan:
        """Plan moving one folder below another without trusting display paths as identity."""

        source = self.resolve(folder)
        self._reject_standard_folder(source, "moved")
        if parse_folder_path(destination) == ():
            return make_mutation_plan(
                operation="folder_move",
                targets=(source.reference,),
                summary=f"Move folder {source.path!r} below the mailbox root.",
                arguments={"destination_reference": None},
                risk=MutationRisk.HIGH_IMPACT,
                requires_confirmation=True,
            )
        target = self.resolve(destination)
        if source.reference == target.reference:
            raise ConfigurationError("A folder cannot be moved into itself.")
        if source.parent_reference == target.reference:
            raise ConfigurationError("The folder is already below that destination.")
        self._reject_descendant_destination(source, target)
        return make_mutation_plan(
            operation="folder_move",
            targets=(source.reference,),
            summary=f"Move folder {source.path!r} below {target.path!r}.",
            arguments={"destination_reference": target.reference},
            risk=MutationRisk.HIGH_IMPACT,
            requires_confirmation=True,
        )

    @staticmethod
    def _reject_standard_folder(folder: FolderInfo, action: str) -> None:
        # Exchange refuses to delete or move distinguished folders (Inbox, Sent
        # Items, ...); refuse before planning so no confirmation is ever offered.
        if folder.distinguished_role:
            raise ConfigurationError(
                f"{folder.name!r} is a standard Exchange folder and cannot be {action}."
            )

    def plan_delete(self, folder: str, *, permanent: bool = False) -> MutationPlan:
        target = self.resolve(folder)
        self._reject_standard_folder(target, "deleted")
        return make_mutation_plan(
            operation="folder_delete",
            targets=(target.reference,),
            summary=(
                f"Permanently delete folder {target.path!r} and its contents."
                if permanent
                else f"Delete folder {target.path!r}."
            ),
            arguments={"permanent": bool(permanent)},
            risk=MutationRisk.DESTRUCTIVE if permanent else MutationRisk.HIGH_IMPACT,
            requires_confirmation=True,
        )

    def plan_empty(self, folder: str, *, permanent: bool = False) -> MutationPlan:
        target = self.resolve(folder)
        return make_mutation_plan(
            operation="folder_empty",
            targets=(target.reference,),
            summary=(
                f"Permanently remove every item from folder {target.path!r}."
                if permanent
                else f"Remove every item from folder {target.path!r}."
            ),
            arguments={"permanent": bool(permanent)},
            risk=MutationRisk.DESTRUCTIVE if permanent else MutationRisk.HIGH_IMPACT,
            requires_confirmation=True,
        )

    def execute_mutation(
        self,
        plan: MutationPlan,
        *,
        dry_run: bool = False,
        confirmation_scope: str | None = None,
    ) -> MutationExecution:
        if dry_run:
            return MutationExecution(plan=plan, dry_run=True, executed=False)
        require_confirmation(plan, confirmation_scope)
        parameters = plan.parameters
        reference = plan.targets[0]
        if plan.operation == "folder_create":
            result = self.gateway.create_folder(
                parameters.get("parent_reference"), str(parameters["name"])
            )
        elif plan.operation == "folder_rename":
            result = self.gateway.rename_folder(reference, str(parameters["new_name"]))
        elif plan.operation == "folder_move":
            result = self.gateway.move_folder(
                reference,
                (
                    str(parameters["destination_reference"])
                    if parameters["destination_reference"] is not None
                    else None
                ),
            )
        elif plan.operation == "folder_delete":
            result = self.gateway.delete_folder(reference, permanent=bool(parameters["permanent"]))
        elif plan.operation == "folder_empty":
            result = self.gateway.empty_folder(reference, permanent=bool(parameters["permanent"]))
        else:
            raise ConfigurationError(f"Unknown folder mutation: {plan.operation}")
        return MutationExecution(
            plan=plan,
            dry_run=False,
            executed=True,
            results=(result,),
        )

    def _reject_descendant_destination(
        self,
        source: FolderInfo,
        destination: FolderInfo,
    ) -> None:
        """Reject cycles using bounded canonical ancestry, never path-prefix guesses."""

        current = destination
        visited: set[str] = set()
        for _depth in range(MAX_FOLDER_DEPTH + 1):
            if current.reference == source.reference:
                raise ConfigurationError("A folder cannot be moved below one of its descendants.")
            if current.reference in visited:
                raise ConflictError("The destination folder hierarchy contains a cycle.")
            visited.add(current.reference)
            if current.parent_reference is None:
                return
            current = self.gateway.get_folder(current.parent_reference)
        raise ConfigurationError(
            f"Folder ancestry may not exceed {MAX_FOLDER_DEPTH} levels during a move."
        )


Page = ServicePage

__all__ = [
    "FolderGateway",
    "FolderService",
    "Page",
    "format_folder_path",
    "parse_folder_path",
    "validate_folder_name",
]
