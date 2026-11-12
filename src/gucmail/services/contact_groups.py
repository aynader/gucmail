"""Account-local and personal mailbox contact groups with exact recipient review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ErrorContext,
    ExitCode,
    GUCmailError,
    ItemNotFoundError,
    UnsupportedFeatureError,
)
from gucmail.models import Address, to_primitive
from gucmail.services.compose import normalize_recipient
from gucmail.state import StateStore, iso

MAX_GROUP_MEMBERS = 200
MAX_GROUP_PAGE = 200


class GroupSource(StrEnum):
    LOCAL = "local"
    MAILBOX = "mailbox"


@dataclass(frozen=True, slots=True)
class ContactGroup:
    reference: str
    name: str
    members: tuple[Address, ...]
    source: GroupSource
    revision: str
    members_loaded: bool = True
    unresolved_members: int = 0
    editable: bool = True

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(to_primitive(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ContactGroupPlan:
    name: str
    members: tuple[Address, ...]
    before: ContactGroup | None = None
    delete: bool = False


class ContactGroupVerificationError(GUCmailError):
    exit_code = ExitCode.PARTIAL


def validate_group(name: str, members: tuple[Address, ...]) -> tuple[str, tuple[Address, ...]]:
    name = name.strip()
    if not name or len(name) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ConfigurationError("A group name needs 1-255 characters without control characters.")
    if len(members) > MAX_GROUP_MEMBERS:
        raise ConfigurationError("A contact group may contain at most 200 recipients.")
    normalized = tuple(normalize_recipient(member) for member in members)
    if len({member.email.casefold() for member in normalized}) != len(normalized):
        raise ConfigurationError("The group contains a duplicate recipient address.")
    return name, normalized


class ContactGroupGateway(Protocol):
    def list_contact_groups(self, *, limit: int, offset: int) -> tuple[ContactGroup, ...]: ...
    def get_contact_group(self, reference: str) -> ContactGroup: ...
    def create_contact_group(self, name: str, members: tuple[Address, ...]) -> ContactGroup: ...
    def update_contact_group(
        self, before: ContactGroup, name: str, members: tuple[Address, ...]
    ) -> ContactGroup: ...
    def delete_contact_group(self, before: ContactGroup) -> None: ...


class ContactGroupService:
    def __init__(self, gateway: ContactGroupGateway) -> None:
        self.gateway = gateway

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[ContactGroup, ...]:
        if not 1 <= limit <= MAX_GROUP_PAGE or offset < 0:
            raise ConfigurationError("Use a group page size from 1 to 200 and nonnegative offset.")
        return self.gateway.list_contact_groups(limit=limit, offset=offset)

    def get(self, reference: str) -> ContactGroup:
        return self.gateway.get_contact_group(reference)

    def plan(
        self, name: str, members: tuple[Address, ...], *, before: ContactGroup | None = None
    ) -> ContactGroupPlan:
        name, members = validate_group(name, members)
        if before is not None and (
            not before.members_loaded or before.unresolved_members or not before.editable
        ):
            raise UnsupportedFeatureError(
                "This group contains linked, nested, unresolved or unsupported members. "
                "Inspect it in Outlook before changing membership."
            )
        return ContactGroupPlan(name, members, before)

    def plan_delete(self, before: ContactGroup) -> ContactGroupPlan:
        return ContactGroupPlan(before.name, before.members, before, delete=True)

    def recipients(self, reference: str) -> tuple[Address, ...]:
        group = self.get(reference)
        if not group.members_loaded or group.unresolved_members:
            raise UnsupportedFeatureError(
                "Resolve all group members before composing to this group."
            )
        _, members = validate_group(group.name, group.members)
        if not members:
            raise ConfigurationError("The group has no recipients. Add members before composing.")
        return members

    def apply(self, plan: ContactGroupPlan, *, confirmed: bool = False) -> ContactGroup | None:
        if not confirmed:
            raise ConfirmationRequiredError("Review and confirm the exact group and recipients.")
        if plan.delete:
            if plan.before is None:
                raise ConfigurationError("Deleting a group requires its exact current identity.")
            self.gateway.delete_contact_group(plan.before)
            return None
        checked = self.plan(plan.name, plan.members, before=plan.before)
        if checked.before is None:
            result = self.gateway.create_contact_group(checked.name, checked.members)
        else:
            result = self.gateway.update_contact_group(
                checked.before, checked.name, checked.members
            )
        if (
            result.name != checked.name
            or tuple(m.email.casefold() for m in result.members)
            != tuple(m.email.casefold() for m in checked.members)
            or result.unresolved_members
        ):
            raise ContactGroupVerificationError(
                "The group write completed, but its returned recipients differ from the preview. "
                "Refresh before another change; the write was not repeated.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            )
        return result


class LocalContactGroups:
    """Persistent user-created groups survive disposable-cache clearing."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _map(row: object) -> ContactGroup:
        # sqlite Row supports string indexing but is not a Mapping.
        from typing import Any, cast

        values = cast(Any, row)
        members = tuple(Address(**member) for member in json.loads(values["members_json"]))
        return ContactGroup(
            values["reference"], values["name"], members, GroupSource.LOCAL, str(values["revision"])
        )

    def list_contact_groups(self, *, limit: int, offset: int) -> tuple[ContactGroup, ...]:
        self.state.initialize()
        with self.state.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM local_contact_groups WHERE account_key=? "
                "ORDER BY name COLLATE NOCASE, reference LIMIT ? OFFSET ?",
                (self.state.account_key, limit, offset),
            ).fetchall()
        return tuple(self._map(row) for row in rows)

    def get_contact_group(self, reference: str) -> ContactGroup:
        self.state.initialize()
        with self.state.connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_contact_groups WHERE account_key=? AND reference=?",
                (self.state.account_key, reference),
            ).fetchone()
        if row is None:
            raise ItemNotFoundError("The local contact group was not found in this account.")
        return self._map(row)

    def create_contact_group(self, name: str, members: tuple[Address, ...]) -> ContactGroup:
        name, members = validate_group(name, members)
        reference = "local-group-" + uuid4().hex
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO local_contact_groups "
                "(account_key, reference, name, members_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.state.account_key,
                    reference,
                    name,
                    json.dumps(to_primitive(members)),
                    iso(),
                    iso(),
                ),
            )
        return self.get_contact_group(reference)

    def update_contact_group(
        self, before: ContactGroup, name: str, members: tuple[Address, ...]
    ) -> ContactGroup:
        name, members = validate_group(name, members)
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM local_contact_groups WHERE account_key=? AND reference=?",
                (self.state.account_key, before.reference),
            ).fetchone()
            if row is None or self._map(row).fingerprint != before.fingerprint:
                raise ConflictError(
                    "The local group changed after preview. Refresh before editing."
                )
            connection.execute(
                "UPDATE local_contact_groups SET name=?, members_json=?, revision=revision+1, "
                "updated_at=? WHERE account_key=? AND reference=? AND revision=?",
                (
                    name,
                    json.dumps(to_primitive(members)),
                    iso(),
                    self.state.account_key,
                    before.reference,
                    int(before.revision),
                ),
            )
        return self.get_contact_group(before.reference)

    def delete_contact_group(self, before: ContactGroup) -> None:
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM local_contact_groups WHERE account_key=? AND reference=?",
                (self.state.account_key, before.reference),
            ).fetchone()
            if row is None or self._map(row).fingerprint != before.fingerprint:
                raise ConflictError(
                    "The local group changed after preview. Refresh before deleting."
                )
            connection.execute(
                "DELETE FROM local_contact_groups WHERE account_key=? AND reference=?",
                (self.state.account_key, before.reference),
            )
