"""Contact, deduplication, and correspondent workflows independent of EWS."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, runtime_checkable

from gucmail.errors import ConfigurationError
from gucmail.models import Address, ContactInfo
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts_display import validate_duplicate_threshold
from gucmail.services.gateway import resolve
from gucmail.services.interchange import export_vcards as render_vcards
from gucmail.services.interchange import parse_vcards
from gucmail.services.pim import (
    MAX_PIM_PAGE_SIZE,
    BatchFailure,
    BatchOperationResult,
    BatchSuccess,
    PimPage,
    bounded_pim_limit,
)

if TYPE_CHECKING:
    from gucmail.services.contact_history import ContactHistoryIndex


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()
MaybeUnset: TypeAlias = Any | _Unset


class CorrespondenceDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


@dataclass(frozen=True, slots=True)
class ContactQuery:
    text: str | None = None
    email: str | None = None
    company: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class ContactCreatePlan:
    contact: ContactInfo
    folder_reference: str | None = None
    allow_duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ContactPatch:
    """Explicit contact changes; ``UNSET`` preserves and ``None`` clears a field."""

    display_name: MaybeUnset = UNSET
    given_name: MaybeUnset = UNSET
    middle_name: MaybeUnset = UNSET
    family_name: MaybeUnset = UNSET
    nickname: MaybeUnset = UNSET
    emails: MaybeUnset = UNSET
    phones: MaybeUnset = UNSET
    addresses: MaybeUnset = UNSET
    company: MaybeUnset = UNSET
    department: MaybeUnset = UNSET
    job_title: MaybeUnset = UNSET
    birthday: MaybeUnset = UNSET
    notes: MaybeUnset = UNSET
    categories: MaybeUnset = UNSET

    def changes(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("display_name", self.display_name),
                ("given_name", self.given_name),
                ("middle_name", self.middle_name),
                ("family_name", self.family_name),
                ("nickname", self.nickname),
                ("emails", self.emails),
                ("phones", self.phones),
                ("addresses", self.addresses),
                ("company", self.company),
                ("department", self.department),
                ("job_title", self.job_title),
                ("birthday", self.birthday),
                ("notes", self.notes),
                ("categories", self.categories),
            )
            if value is not UNSET
        }


@dataclass(frozen=True, slots=True)
class ContactUpdatePlan:
    reference: str
    changes: Mapping[str, Any]

    @property
    def update_fields(self) -> tuple[str, ...]:
        return tuple(self.changes)


@dataclass(frozen=True, slots=True)
class ContactDeletePlan:
    reference: str
    hard_delete: bool = False


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    first: ContactInfo
    second: ContactInfo
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    contacts: tuple[ContactInfo, ...]
    candidates: tuple[DuplicateCandidate, ...]

    @property
    def maximum_score(self) -> int:
        return max((candidate.score for candidate in self.candidates), default=0)


@dataclass(frozen=True, slots=True)
class MergeConflict:
    field: str
    choices: tuple[Any, ...]
    selected: Any


@dataclass(frozen=True, slots=True)
class MergePreview:
    primary_reference: str
    source_references: tuple[str, ...]
    merged: ContactInfo
    conflicts: tuple[MergeConflict, ...]


@dataclass(frozen=True, slots=True)
class ContactMergePlan:
    preview: MergePreview
    update: ContactUpdatePlan
    deletes: tuple[ContactDeletePlan, ...]


@dataclass(frozen=True, slots=True)
class ContactImportPlan:
    contacts: tuple[ContactInfo, ...]
    duplicate_candidates: tuple[DuplicateCandidate, ...]
    folder_reference: str | None = None
    allow_duplicates: bool = False


@dataclass(frozen=True, slots=True)
class CorrespondenceEvent:
    address: Address
    occurred_at: datetime
    direction: CorrespondenceDirection
    message_reference: str | None = None


@dataclass(frozen=True, slots=True)
class CorrespondentSummary:
    address: Address
    count: int
    first_seen: datetime
    last_seen: datetime
    inbound_count: int
    outbound_count: int


@runtime_checkable
class ContactsGateway(Protocol):
    """Provider-neutral mailbox contact persistence contract."""

    def list_contacts(
        self, *, folder_reference: str | None, limit: int
    ) -> Sequence[ContactInfo]: ...

    def search_contacts(
        self,
        query: ContactQuery,
        *,
        folder_reference: str | None,
        limit: int,
    ) -> Sequence[ContactInfo]: ...

    def get_contact(self, reference: str) -> ContactInfo: ...

    def create_contact(self, plan: ContactCreatePlan) -> ContactInfo: ...

    def update_contact(self, plan: ContactUpdatePlan) -> ContactInfo: ...

    def delete_contact(self, plan: ContactDeletePlan) -> None: ...


@runtime_checkable
class PagedContactsGateway(Protocol):
    def search_contacts_page(
        self, query: ContactQuery, *, folder_reference: str | None, limit: int, offset: int = 0
    ) -> PimPage[ContactInfo]: ...


@runtime_checkable
class CorrespondenceGateway(Protocol):
    """Bounded local-mail-history reader used for recipient suggestions."""

    def list_correspondence(
        self, *, since: datetime | None, limit: int
    ) -> Sequence[CorrespondenceEvent]: ...


@runtime_checkable
class DirectoryGateway(Protocol):
    """Bounded Exchange directory lookup, separate from mailbox contacts."""

    def search_directory(self, query: str, *, limit: int = 25) -> Sequence[Address]: ...


def normalize_email(value: str) -> str:
    """Return an email key suitable for local matching, not for message delivery."""

    normalized = value.strip()
    if normalized.lower().startswith("mailto:"):
        normalized = normalized[7:]
    if (
        not normalized
        or len(normalized) > 320
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
        or any(char.isspace() or char == "\x00" for char in normalized)
    ):
        raise ConfigurationError(f"Invalid email address: {value!r}")
    local, domain = normalized.rsplit("@", 1)
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ConfigurationError(f"Invalid email address: {value!r}")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ConfigurationError(f"Invalid email address: {value!r}") from exc
    if (
        not ascii_domain
        or ascii_domain.startswith(".")
        or ascii_domain.endswith(".")
        or ".." in ascii_domain
    ):
        raise ConfigurationError(f"Invalid email address: {value!r}")
    return f"{local.casefold()}@{ascii_domain.casefold()}"


def normalize_contact(contact: ContactInfo) -> ContactInfo:
    display_name = " ".join(contact.display_name.split())
    if not display_name:
        raise ConfigurationError("A contact needs a display name.")
    seen_emails: set[str] = set()
    emails: list[Address] = []
    for address in contact.emails:
        key = normalize_email(address.email)
        if key not in seen_emails:
            emails.append(replace(address, email=address.email.strip()))
            seen_emails.add(key)
    phones = _normalize_labeled_values(contact.phones, field="phone")
    addresses = _normalize_labeled_values(contact.addresses, field="postal address")
    categories = tuple(dict.fromkeys(item.strip() for item in contact.categories if item.strip()))
    return replace(
        contact,
        display_name=display_name,
        emails=tuple(emails),
        phones=phones,
        addresses=addresses,
        categories=categories,
    )


def contact_search_text(contact: ContactInfo) -> str:
    """Return all bounded user-facing fields used by full contact search."""

    return " ".join(
        (
            contact.display_name,
            contact.given_name or "",
            contact.middle_name or "",
            contact.family_name or "",
            contact.nickname or "",
            " ".join(
                " ".join(
                    part for part in (address.name, address.email, address.mailbox_type) if part
                )
                for address in contact.emails
            ),
            " ".join(f"{label} {value}" for label, value in contact.phones.items()),
            " ".join(f"{label} {value}" for label, value in contact.addresses.items()),
            contact.company or "",
            contact.department or "",
            contact.job_title or "",
            contact.birthday.isoformat() if contact.birthday else "",
            contact.notes or "",
            " ".join(contact.categories),
        )
    )


def _normalize_labeled_values(values: Mapping[str, str], *, field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_label, raw_value in values.items():
        label = " ".join(str(raw_label).split()).casefold()
        value = " ".join(str(raw_value).split())
        if not label or not value:
            raise ConfigurationError(f"A contact {field} label and value may not be empty.")
        if any(char in label + value for char in "\r\n\x00"):
            raise ConfigurationError(f"A contact {field} contains an invalid control character.")
        if label in result:
            raise ConfigurationError(f"Duplicate contact {field} label: {label!r}")
        result[label] = value
    return result


def duplicate_score(first: ContactInfo, second: ContactInfo) -> DuplicateCandidate:
    """Score two contacts using explainable identifiers; no fuzzy transmission occurs."""

    first = normalize_contact(first)
    second = normalize_contact(second)
    score = 0
    reasons: list[str] = []
    first_emails = {normalize_email(item.email) for item in first.emails}
    second_emails = {normalize_email(item.email) for item in second.emails}
    if first_emails & second_emails:
        score += 80
        reasons.append("shared email address")
    first_phones = {_phone_key(value) for value in first.phones.values() if _phone_key(value)}
    second_phones = {_phone_key(value) for value in second.phones.values() if _phone_key(value)}
    if first_phones & second_phones:
        score += 65
        reasons.append("shared phone number")
    if _name_key(first.display_name) == _name_key(second.display_name):
        score += 30
        reasons.append("same display name")
    elif _structured_name_key(first) and _structured_name_key(first) == _structured_name_key(
        second
    ):
        score += 35
        reasons.append("same given and family name")
    if first.company and second.company and _name_key(first.company) == _name_key(second.company):
        score += 5
        reasons.append("same company")
    return DuplicateCandidate(first, second, min(score, 100), tuple(reasons))


def find_duplicate_groups(
    contacts: Iterable[ContactInfo], *, threshold: int = 70
) -> tuple[DuplicateGroup, ...]:
    """Build transitive duplicate groups from pairwise explainable scores."""

    validate_duplicate_threshold(threshold)
    values = tuple(normalize_contact(contact) for contact in contacts)
    parents = list(range(len(values)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    pair_indexes: list[tuple[int, int, DuplicateCandidate]] = []
    for left in range(len(values)):
        for right in range(left + 1, len(values)):
            candidate = duplicate_score(values[left], values[right])
            if candidate.score >= threshold:
                pair_indexes.append((left, right, candidate))
                union(left, right)
    members: dict[int, list[ContactInfo]] = defaultdict(list)
    for index, contact in enumerate(values):
        members[find(index)].append(contact)
    groups: list[DuplicateGroup] = []
    for _root, contacts_in_group in members.items():
        if len(contacts_in_group) < 2:
            continue
        refs = {contact.reference for contact in contacts_in_group}
        group_candidates = tuple(
            candidate
            for _, _, candidate in pair_indexes
            if candidate.first.reference in refs and candidate.second.reference in refs
        )
        groups.append(DuplicateGroup(tuple(contacts_in_group), group_candidates))
    return tuple(
        sorted(
            groups,
            key=lambda group: (-group.maximum_score, group.contacts[0].display_name.casefold()),
        )
    )


def preview_merge(
    contacts: Iterable[ContactInfo], *, primary_reference: str | None = None
) -> MergePreview:
    """Merge without mutation, retaining all multi-value fields and reporting conflicts."""

    values = tuple(normalize_contact(contact) for contact in contacts)
    if len(values) < 2:
        raise ConfigurationError("A contact merge preview requires at least two contacts.")
    references = [contact.reference for contact in values]
    if len(set(references)) != len(references):
        raise ConfigurationError("A merge preview cannot contain the same contact twice.")
    if primary_reference is None:
        primary = values[0]
    else:
        try:
            primary = next(contact for contact in values if contact.reference == primary_reference)
        except StopIteration as exc:
            raise ConfigurationError(
                f"Primary contact {primary_reference!r} is not in the merge."
            ) from exc
    ordered = (primary, *(contact for contact in values if contact.reference != primary.reference))
    conflicts: list[MergeConflict] = []

    def choose(field_name: str) -> Any:
        choices = tuple(
            dict.fromkeys(
                value
                for contact in ordered
                if (value := getattr(contact, field_name)) not in (None, "")
            )
        )
        selected = choices[0] if choices else None
        if len(choices) > 1:
            conflicts.append(MergeConflict(field_name, choices, selected))
        return selected

    emails: list[Address] = []
    email_keys: set[str] = set()
    for contact in ordered:
        for email in contact.emails:
            key = normalize_email(email.email)
            if key not in email_keys:
                emails.append(email)
                email_keys.add(key)
    phones = _merge_labeled_maps(ordered, "phones", conflicts)
    addresses = _merge_labeled_maps(ordered, "addresses", conflicts)
    categories = tuple(
        dict.fromkeys(category for contact in ordered for category in contact.categories)
    )
    merged = replace(
        primary,
        display_name=choose("display_name") or primary.display_name,
        given_name=choose("given_name"),
        middle_name=choose("middle_name"),
        family_name=choose("family_name"),
        nickname=choose("nickname"),
        emails=tuple(emails),
        phones=phones,
        addresses=addresses,
        company=choose("company"),
        department=choose("department"),
        job_title=choose("job_title"),
        birthday=choose("birthday"),
        notes=choose("notes"),
        categories=categories,
    )
    return MergePreview(
        primary_reference=primary.reference,
        source_references=tuple(contact.reference for contact in ordered),
        merged=normalize_contact(merged),
        conflicts=tuple(conflicts),
    )


def _merge_labeled_maps(
    contacts: Sequence[ContactInfo],
    field_name: str,
    conflicts: list[MergeConflict],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for contact in contacts:
        values = getattr(contact, field_name)
        for label, value in values.items():
            if value in result.values():
                continue
            if label not in result:
                result[label] = value
                continue
            conflicts.append(
                MergeConflict(f"{field_name}.{label}", (result[label], value), result[label])
            )
            index = 2
            while f"{label}_{index}" in result:
                index += 1
            result[f"{label}_{index}"] = value
    return result


def _name_key(value: str) -> str:
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE).casefold()


def _structured_name_key(contact: ContactInfo) -> str:
    if not contact.given_name or not contact.family_name:
        return ""
    return _name_key(f"{contact.given_name}\0{contact.family_name}")


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def summarize_correspondents(
    events: Iterable[CorrespondenceEvent],
    *,
    known_contacts: Iterable[ContactInfo] = (),
    excluded_addresses: Iterable[str] = (),
) -> tuple[CorrespondentSummary, ...]:
    """Aggregate bounded, caller-supplied history without exposing it externally."""

    excluded = {normalize_email(value) for value in excluded_addresses}
    names: dict[str, str] = {}
    for contact in known_contacts:
        normalized = normalize_contact(contact)
        for address in normalized.emails:
            names[normalize_email(address.email)] = normalized.display_name
    buckets: dict[str, list[CorrespondenceEvent]] = defaultdict(list)
    addresses: dict[str, Address] = {}
    for event in events:
        if event.occurred_at.tzinfo is None:
            raise ConfigurationError("Correspondence timestamps must include a time zone.")
        key = normalize_email(event.address.email)
        if key in excluded:
            continue
        try:
            CorrespondenceDirection(event.direction)
        except ValueError as exc:
            raise ConfigurationError(
                f"Unknown correspondence direction: {event.direction!r}"
            ) from exc
        buckets[key].append(event)
        existing_name = names.get(key) or event.address.name
        addresses[key] = Address(
            event.address.email.strip(), existing_name, event.address.mailbox_type
        )
    result: list[CorrespondentSummary] = []
    for key, observations in buckets.items():
        observations.sort(key=lambda item: item.occurred_at.astimezone(UTC))
        result.append(
            CorrespondentSummary(
                address=addresses[key],
                count=len(observations),
                first_seen=observations[0].occurred_at,
                last_seen=observations[-1].occurred_at,
                inbound_count=sum(
                    CorrespondenceDirection(item.direction) is CorrespondenceDirection.INBOUND
                    for item in observations
                ),
                outbound_count=sum(
                    CorrespondenceDirection(item.direction) is CorrespondenceDirection.OUTBOUND
                    for item in observations
                ),
            )
        )
    return tuple(result)


class ContactsService:
    """Application service for mailbox contacts and privacy-local recipient history."""

    def __init__(
        self,
        gateway: ContactsGateway,
        *,
        correspondence_gateway: CorrespondenceGateway | None = None,
        default_folder: str | None = None,
        default_limit: int = 200,
        maximum_history_observations: int = MAX_PIM_PAGE_SIZE,
        operation_preferences: Callable[[], ContactOperationPreferences] | None = None,
        history_index: ContactHistoryIndex | None = None,
        index_warning: Callable[[str], None] | None = None,
    ) -> None:
        if default_limit <= 0 or maximum_history_observations <= 0:
            raise ConfigurationError("Contact and history limits must be positive.")
        self.gateway = gateway
        self.correspondence_gateway = correspondence_gateway
        self.operation_preferences = operation_preferences or ContactOperationPreferences
        self.history_index = history_index
        self.index_warning = index_warning
        self.default_folder = default_folder
        self.default_limit = bounded_pim_limit(
            None,
            default=default_limit,
            subject="Contact",
        )
        self.maximum_history_observations = maximum_history_observations

    def list(
        self, *, folder_reference: str | None = None, limit: int | None = None
    ) -> tuple[ContactInfo, ...]:
        values = self.gateway.list_contacts(
            folder_reference=folder_reference or self.default_folder,
            limit=self._limit(limit),
        )
        return tuple(
            sorted(
                (normalize_contact(contact) for contact in values),
                key=lambda contact: (contact.display_name.casefold(), contact.reference),
            )
        )

    def search(
        self,
        query: ContactQuery | str,
        *,
        folder_reference: str | None = None,
        limit: int | None = None,
    ) -> tuple[ContactInfo, ...]:
        resolved = ContactQuery(text=query.strip()) if isinstance(query, str) else query
        if not any((resolved.text, resolved.email, resolved.company, resolved.category)):
            raise ConfigurationError("Contact search needs at least one criterion.")
        values = self.gateway.search_contacts(
            resolved,
            folder_reference=folder_reference or self.default_folder,
            limit=self._limit(limit),
        )
        return tuple(normalize_contact(contact) for contact in values)

    def page(
        self,
        query: ContactQuery | str = "",
        *,
        folder_reference: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PimPage[ContactInfo]:
        """Browse or search one candidate window with explicit continuation."""
        if offset < 0:
            raise ConfigurationError("Contact offset cannot be negative.")
        resolved = ContactQuery(text=query.strip()) if isinstance(query, str) else query
        bounded = self._limit(limit)
        gateway = resolve(self.gateway)
        if isinstance(gateway, PagedContactsGateway):
            page = gateway.search_contacts_page(
                resolved,
                folder_reference=folder_reference or self.default_folder,
                limit=bounded,
                offset=offset,
            )
            return replace(page, items=tuple(normalize_contact(item) for item in page.items))
        if offset:
            raise ConfigurationError("This contact provider does not support continuation.")
        values = (
            self.search(resolved, folder_reference=folder_reference, limit=bounded)
            if any((resolved.text, resolved.email, resolved.company, resolved.category))
            else self.list(folder_reference=folder_reference, limit=bounded)
        )
        return PimPage(
            values, 0, len(values), residual="This provider exposes a single bounded view."
        )

    def get(self, reference: str) -> ContactInfo:
        if not reference.strip():
            raise ConfigurationError("A contact reference is required.")
        return normalize_contact(self.gateway.get_contact(reference))

    def search_directory(self, query: str, *, limit: int = 25) -> tuple[Address, ...]:
        self.operation_preferences().require("directory")
        if len(query) > 512 or any(ord(character) < 32 for character in query):
            raise ConfigurationError("Directory search text is empty or invalid.")
        value = " ".join(query.split())
        if not value or any(character in value for character in "\r\n\x00"):
            raise ConfigurationError("Directory search text is empty or invalid.")
        if limit < 1 or limit > 100:
            raise ConfigurationError("Directory results must be limited to between 1 and 100.")
        gateway = resolve(self.gateway)
        if not isinstance(gateway, DirectoryGateway):
            raise ConfigurationError("This account gateway does not provide directory lookup.")
        return tuple(gateway.search_directory(value, limit=limit))

    def resolve_directory(self, query: str) -> Address:
        results = self.search_directory(query, limit=2)
        if not results:
            raise ConfigurationError("No Exchange directory entry matches that value.")
        if len(results) != 1:
            raise ConfigurationError(
                "The Exchange directory result is ambiguous; choose a specific address."
            )
        return results[0]

    def plan_create(
        self,
        contact: ContactInfo,
        *,
        folder_reference: str | None = None,
        allow_duplicate: bool = False,
    ) -> ContactCreatePlan:
        return ContactCreatePlan(
            normalize_contact(contact),
            folder_reference or self.default_folder,
            allow_duplicate,
        )

    def create(
        self,
        contact: ContactInfo,
        *,
        folder_reference: str | None = None,
        allow_duplicate: bool = False,
    ) -> ContactInfo:
        return normalize_contact(
            self.gateway.create_contact(
                self.plan_create(
                    contact,
                    folder_reference=folder_reference,
                    allow_duplicate=allow_duplicate,
                )
            )
        )

    def plan_update(
        self,
        reference: str,
        patch: ContactPatch,
        *,
        current: ContactInfo | None = None,
    ) -> ContactUpdatePlan:
        if not reference.strip():
            raise ConfigurationError("A contact reference is required.")
        changes = patch.changes()
        if not changes:
            raise ConfigurationError("No contact fields were supplied for update.")
        current = self.get(reference) if current is None else normalize_contact(current)
        updated = normalize_contact(replace(current, **changes))
        changes = {name: getattr(updated, name) for name in changes}
        return ContactUpdatePlan(reference, dict(changes))

    def update(
        self,
        reference: str,
        patch: ContactPatch,
        *,
        current: ContactInfo | None = None,
    ) -> ContactInfo:
        return normalize_contact(
            self.gateway.update_contact(self.plan_update(reference, patch, current=current))
        )

    def plan_delete(self, reference: str, *, hard_delete: bool = False) -> ContactDeletePlan:
        if not reference.strip():
            raise ConfigurationError("A contact reference is required.")
        return ContactDeletePlan(reference, hard_delete)

    def delete(self, plan: ContactDeletePlan) -> None:
        self.gateway.delete_contact(plan)

    def duplicates(
        self,
        contacts: Iterable[ContactInfo] | None = None,
        *,
        threshold: int = 70,
        folder_reference: str | None = None,
        limit: int | None = None,
    ) -> tuple[DuplicateGroup, ...]:
        self.operation_preferences().require("duplicates")
        validate_duplicate_threshold(threshold)
        values = (
            tuple(contacts)
            if contacts is not None
            else self.list(folder_reference=folder_reference, limit=limit)
        )
        return find_duplicate_groups(values, threshold=threshold)

    def merge_preview(
        self, contacts: Iterable[ContactInfo], *, primary_reference: str | None = None
    ) -> MergePreview:
        return preview_merge(contacts, primary_reference=primary_reference)

    def plan_merge(self, preview: MergePreview) -> ContactMergePlan:
        changes = {
            field_name: getattr(preview.merged, field_name)
            for field_name in (
                "display_name",
                "given_name",
                "middle_name",
                "family_name",
                "nickname",
                "emails",
                "phones",
                "addresses",
                "company",
                "department",
                "job_title",
                "birthday",
                "notes",
                "categories",
            )
        }
        update = ContactUpdatePlan(preview.primary_reference, dict(changes))
        deletes = tuple(
            ContactDeletePlan(reference)
            for reference in preview.source_references
            if reference != preview.primary_reference
        )
        return ContactMergePlan(preview, update, deletes)

    def apply_merge(self, plan: ContactMergePlan) -> ContactInfo:
        updated = self.gateway.update_contact(plan.update)
        for deletion in plan.deletes:
            self.gateway.delete_contact(deletion)
        return normalize_contact(updated)

    def apply_merge_result(
        self,
        plan: ContactMergePlan,
        *,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[ContactInfo]:
        """Apply a merge while retaining enough step detail to reconcile partial work."""

        completed: list[BatchSuccess[ContactInfo]] = []
        failures: list[BatchFailure] = []
        skipped: list[str] = []
        try:
            updated = normalize_contact(self.gateway.update_contact(plan.update))
        except Exception as exc:
            failures.append(
                BatchFailure.from_exception(
                    index=0,
                    operation="update primary contact",
                    reference=plan.update.reference,
                    error=exc,
                    retry_safe=False,
                )
            )
            skipped.extend(deletion.reference for deletion in plan.deletes)
            return BatchOperationResult(
                attempted=1 + len(plan.deletes),
                failures=tuple(failures),
                skipped_references=tuple(skipped),
            )
        completed.append(
            BatchSuccess(
                index=0,
                operation="update primary contact",
                reference=updated.reference,
                value=updated,
            )
        )
        for index, deletion in enumerate(plan.deletes, start=1):
            try:
                self.gateway.delete_contact(deletion)
            except Exception as exc:
                failures.append(
                    BatchFailure.from_exception(
                        index=index,
                        operation="remove merged contact",
                        reference=deletion.reference,
                        error=exc,
                        retry_safe=False,
                    )
                )
                if not continue_on_error:
                    skipped.extend(item.reference for item in plan.deletes[index:])
                    break
            else:
                completed.append(
                    BatchSuccess(
                        index=index,
                        operation="remove merged contact",
                        reference=deletion.reference,
                    )
                )
        return BatchOperationResult(
            attempted=1 + len(plan.deletes),
            completed=tuple(completed),
            failures=tuple(failures),
            skipped_references=tuple(skipped),
        )

    def import_vcards(
        self,
        value: str,
        *,
        existing: Iterable[ContactInfo] | None = None,
        folder_reference: str | None = None,
        allow_duplicates: bool = False,
        duplicate_threshold: int = 70,
    ) -> ContactImportPlan:
        imported = tuple(normalize_contact(contact) for contact in parse_vcards(value))
        existing_values = (
            self.list(folder_reference=folder_reference, limit=MAX_PIM_PAGE_SIZE)
            if existing is None
            else tuple(normalize_contact(contact) for contact in existing)
        )
        # vCard UID values are not Exchange item IDs and are not guaranteed to be
        # unique.  Duplicate or mailbox-colliding references made the old
        # reference-based duplicate blocker skip *every* copy, including the first
        # imported record.  Preserve a UID when possible and deterministically
        # disambiguate only collisions before scoring.
        imported = _unique_import_references(imported, existing=existing_values)
        imported_refs = {contact.reference for contact in imported}
        candidates = tuple(
            candidate
            for group in find_duplicate_groups(
                (*existing_values, *imported), threshold=duplicate_threshold
            )
            for candidate in group.candidates
            if candidate.first.reference in imported_refs
            or candidate.second.reference in imported_refs
        )
        return ContactImportPlan(
            imported,
            candidates,
            folder_reference or self.default_folder,
            allow_duplicates,
        )

    def apply_import(self, plan: ContactImportPlan) -> tuple[ContactInfo, ...]:
        blocked_refs = _blocked_import_references(plan)
        created: list[ContactInfo] = []
        for contact in plan.contacts:
            if contact.reference in blocked_refs:
                continue
            created.append(
                self.gateway.create_contact(
                    ContactCreatePlan(contact, plan.folder_reference, plan.allow_duplicates)
                )
            )
        return tuple(created)

    def apply_import_result(
        self,
        plan: ContactImportPlan,
        *,
        continue_on_error: bool = False,
    ) -> BatchOperationResult[ContactInfo]:
        """Apply a contact import with explicit duplicate skips and partial failures."""

        blocked_refs = _blocked_import_references(plan)
        completed: list[BatchSuccess[ContactInfo]] = []
        failures: list[BatchFailure] = []
        skipped: list[str] = []
        for index, contact in enumerate(plan.contacts):
            if contact.reference in blocked_refs:
                skipped.append(contact.reference)
                continue
            try:
                created = normalize_contact(
                    self.gateway.create_contact(
                        ContactCreatePlan(
                            contact,
                            plan.folder_reference,
                            plan.allow_duplicates,
                        )
                    )
                )
            except Exception as exc:
                failures.append(
                    BatchFailure.from_exception(
                        index=index,
                        operation="create contact",
                        reference=contact.reference,
                        error=exc,
                        retry_safe=False,
                    )
                )
                if not continue_on_error:
                    skipped.extend(item.reference for item in plan.contacts[index + 1 :])
                    break
            else:
                completed.append(
                    BatchSuccess(
                        index=index,
                        operation="create contact",
                        reference=created.reference,
                        value=created,
                    )
                )
        return BatchOperationResult(
            attempted=len(plan.contacts),
            completed=tuple(completed),
            failures=tuple(failures),
            skipped_references=tuple(dict.fromkeys(skipped)),
        )

    def export_vcards(
        self, contacts: ContactInfo | Iterable[ContactInfo], *, version: str = "4.0"
    ) -> str:
        if isinstance(contacts, ContactInfo):
            return render_vcards(normalize_contact(contacts), version=version)
        return render_vcards(
            tuple(normalize_contact(contact) for contact in contacts), version=version
        )

    def export_contact(self, reference: str, *, version: str = "4.0") -> str:
        return self.export_vcards(self.get(reference), version=version)

    def recent_correspondents(
        self,
        observations: Iterable[CorrespondenceEvent] | None = None,
        *,
        known_contacts: Iterable[ContactInfo] = (),
        excluded_addresses: Iterable[str] = (),
        since: datetime | None = None,
        limit: int = 20,
    ) -> tuple[CorrespondentSummary, ...]:
        self.operation_preferences().require("history")
        summaries = self._correspondents(
            observations,
            known_contacts=known_contacts,
            excluded_addresses=excluded_addresses,
            since=since,
            requested_limit=limit,
        )
        return tuple(
            sorted(
                summaries,
                key=lambda item: (
                    -item.last_seen.astimezone(UTC).timestamp(),
                    item.address.email.casefold(),
                ),
            )[:limit]
        )

    def frequent_correspondents(
        self,
        observations: Iterable[CorrespondenceEvent] | None = None,
        *,
        known_contacts: Iterable[ContactInfo] = (),
        excluded_addresses: Iterable[str] = (),
        since: datetime | None = None,
        minimum_count: int = 1,
        limit: int = 20,
    ) -> tuple[CorrespondentSummary, ...]:
        self.operation_preferences().require("frequent")
        if minimum_count <= 0:
            raise ConfigurationError("Minimum correspondence count must be positive.")
        summaries = self._correspondents(
            observations,
            known_contacts=known_contacts,
            excluded_addresses=excluded_addresses,
            since=since,
            requested_limit=limit,
        )
        eligible = (item for item in summaries if item.count >= minimum_count)
        return tuple(
            sorted(
                eligible,
                key=lambda item: (
                    -item.count,
                    -item.last_seen.astimezone(UTC).timestamp(),
                    item.address.email.casefold(),
                ),
            )[:limit]
        )

    infer_correspondents = frequent_correspondents

    def correspondence_history(
        self,
        addresses: Iterable[Address | str],
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[CorrespondenceEvent, ...]:
        self.operation_preferences().require("history")
        if self.correspondence_gateway is None:
            raise ConfigurationError("No local correspondence-history gateway is configured.")
        if since is not None and since.tzinfo is None:
            raise ConfigurationError("Correspondence history start must include a time zone.")
        bounded_limit = bounded_pim_limit(limit, default=100, subject="Correspondence")
        keys = {
            normalize_email(address.email if isinstance(address, Address) else address)
            for address in addresses
        }
        if not keys:
            raise ConfigurationError("Correspondence history needs at least one contact address.")
        scan_limit = min(self.maximum_history_observations, max(bounded_limit * 10, 200))
        observations = self.read_correspondence(
            since=since,
            limit=scan_limit,
        )
        result = [
            event
            for event in observations
            if normalize_email(event.address.email) in keys
            and (since is None or event.occurred_at >= since)
        ]
        return tuple(
            sorted(
                result,
                key=lambda event: (
                    -event.occurred_at.astimezone(UTC).timestamp(),
                    event.message_reference or "",
                ),
            )[:bounded_limit]
        )

    def _correspondents(
        self,
        observations: Iterable[CorrespondenceEvent] | None,
        *,
        known_contacts: Iterable[ContactInfo],
        excluded_addresses: Iterable[str],
        since: datetime | None,
        requested_limit: int,
    ) -> tuple[CorrespondentSummary, ...]:
        if requested_limit <= 0:
            raise ConfigurationError("Correspondent result limit must be positive.")
        if since is not None and since.tzinfo is None:
            raise ConfigurationError("Correspondent history start must include a time zone.")
        if observations is None:
            if self.correspondence_gateway is None:
                raise ConfigurationError("No local correspondence-history gateway is configured.")
            observation_limit = min(
                self.maximum_history_observations, max(requested_limit * 50, 200)
            )
            observations = self.read_correspondence(
                since=since,
                limit=observation_limit,
            )
        bounded: list[CorrespondenceEvent] = []
        for observation in observations:
            if observation.occurred_at.tzinfo is None:
                raise ConfigurationError("Correspondence timestamps must include a time zone.")
            if since is not None and observation.occurred_at < since:
                continue
            bounded.append(observation)
            if len(bounded) >= self.maximum_history_observations:
                break
        return summarize_correspondents(
            bounded,
            known_contacts=known_contacts,
            excluded_addresses=excluded_addresses,
        )

    def read_correspondence(
        self, *, since: datetime | None = None, limit: int = MAX_PIM_PAGE_SIZE
    ) -> tuple[CorrespondenceEvent, ...]:
        preferences = self.operation_preferences()
        preferences.require("history")
        if since is not None and since.tzinfo is None:
            raise ConfigurationError("Correspondence history start must include a time zone.")
        bounded = bounded_pim_limit(limit, default=MAX_PIM_PAGE_SIZE, subject="Correspondence")
        if self.correspondence_gateway is None:
            raise ConfigurationError("No bounded correspondence-history gateway is configured.")
        events = tuple(self.correspondence_gateway.list_correspondence(since=since, limit=bounded))[
            :bounded
        ]
        if preferences.history_indexing and self.history_index is not None:
            try:
                # Recheck privacy after a potentially slow provider request. A preference
                # disabled during the request must not lead to new local recording.
                current = self.operation_preferences()
                if current.history_indexing and current.correspondence_history:
                    self.history_index.record(events, retention_days=current.history_retention_days)
            except Exception:
                warning = (
                    "The metadata report succeeded, but local contact history indexing failed."
                )
                if self.index_warning is not None:
                    self.index_warning(warning)
                else:
                    logging.getLogger(__name__).warning(warning)
        return events[:bounded]

    def _limit(self, value: int | None) -> int:
        return bounded_pim_limit(
            value,
            default=self.default_limit,
            subject="Contact",
        )


def _blocked_import_references(plan: ContactImportPlan) -> set[str]:
    """Choose imported duplicates to skip while retaining the first imported record."""

    if plan.allow_duplicates:
        return set()
    imported_refs = {contact.reference for contact in plan.contacts}
    blocked: set[str] = set()
    for candidate in plan.duplicate_candidates:
        first_imported = candidate.first.reference in imported_refs
        second_imported = candidate.second.reference in imported_refs
        if first_imported and second_imported:
            blocked.add(candidate.second.reference)
        elif first_imported:
            blocked.add(candidate.first.reference)
        elif second_imported:
            blocked.add(candidate.second.reference)
    return blocked


def _unique_import_references(
    contacts: Sequence[ContactInfo],
    *,
    existing: Sequence[ContactInfo],
) -> tuple[ContactInfo, ...]:
    """Return import-preview contacts with references unique in this mailbox page.

    References are reconciliation tokens only; the EWS adapter assigns a stable
    mailbox reference after creation.  Suffixing a colliding vCard UID therefore
    does not modify any contact field that is persisted to Exchange.
    """

    used = {contact.reference for contact in existing}
    result: list[ContactInfo] = []
    for contact in contacts:
        base = contact.reference.strip() or "vcard-import"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}#import-{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(
            contact if candidate == contact.reference else replace(contact, reference=candidate)
        )
    return tuple(result)


__all__ = [
    "UNSET",
    "ContactCreatePlan",
    "ContactDeletePlan",
    "ContactImportPlan",
    "ContactMergePlan",
    "ContactPatch",
    "ContactQuery",
    "ContactUpdatePlan",
    "ContactsGateway",
    "ContactsService",
    "CorrespondenceDirection",
    "CorrespondenceEvent",
    "CorrespondenceGateway",
    "CorrespondentSummary",
    "DirectoryGateway",
    "DuplicateCandidate",
    "DuplicateGroup",
    "MergeConflict",
    "MergePreview",
    "contact_search_text",
    "duplicate_score",
    "find_duplicate_groups",
    "normalize_contact",
    "normalize_email",
    "preview_merge",
    "summarize_correspondents",
]
