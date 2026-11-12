"""Reviewed Exchange inbox policies, distinct from local notification rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ExitCode,
    GUCmailError,
    ItemNotFoundError,
    UnsupportedFeatureError,
)

MAX_INBOX_RULES = 1_000


@dataclass(frozen=True, slots=True)
class InboxRule:
    reference: str
    name: str
    priority: int
    enabled: bool
    unsupported: bool
    in_error: bool
    conditions: dict[str, Any]
    exceptions: dict[str, Any]
    actions: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SimpleInboxRule:
    name: str
    priority: int = 0
    sender_contains: tuple[str, ...] = ()
    subject_contains: tuple[str, ...] = ()
    has_attachments: bool | None = None
    categories: tuple[str, ...] = ()
    mark_read: bool = False
    move_to_folder: str | None = None
    stop_processing: bool = False


class InboxRuleOperation(StrEnum):
    CREATE = "create"
    ENABLE = "enable"
    DISABLE = "disable"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class InboxRulePlan:
    operation: InboxRuleOperation
    before: InboxRule | None = None
    creation: SimpleInboxRule | None = None
    inventory_fingerprint: str | None = None


class InboxRuleVerificationError(GUCmailError):
    exit_code = ExitCode.PARTIAL


def inventory_fingerprint(rules: tuple[InboxRule, ...]) -> str:
    values = sorted((rule.reference, rule.fingerprint) for rule in rules)
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


def validate_simple_rule(rule: SimpleInboxRule) -> SimpleInboxRule:
    def text(value: str, label: str, maximum: int) -> None:
        if (
            not value.strip()
            or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ConfigurationError(f"{label} must be nonempty text without control characters.")

    text(rule.name, "Rule name", 255)
    if isinstance(rule.priority, bool) or not 0 <= rule.priority <= 10_000:
        raise ConfigurationError("Rule priority must be between 0 and 10000.")
    for values, label in (
        (rule.sender_contains, "Sender text"),
        (rule.subject_contains, "Subject text"),
        (rule.categories, "Category"),
    ):
        if len(values) > 20:
            raise ConfigurationError(f"At most 20 {label.lower()} values are supported.")
        for value in values:
            text(value, label, 255)
    if rule.has_attachments is not None and type(rule.has_attachments) is not bool:
        raise ConfigurationError("Attachment condition must be true or false.")
    if not (rule.sender_contains or rule.subject_contains or rule.has_attachments is not None):
        raise ConfigurationError("A simple inbox rule needs at least one matching condition.")
    if rule.move_to_folder is not None:
        text(rule.move_to_folder, "Destination folder", 2_048)
    if not (rule.mark_read or rule.categories or rule.move_to_folder or rule.stop_processing):
        raise ConfigurationError("Choose at least one action for the inbox rule.")
    return rule


class InboxRuleGateway(Protocol):
    def list_inbox_rules(self) -> tuple[InboxRule, ...]: ...
    def create_inbox_rule(self, rule: SimpleInboxRule, expected_inventory: str) -> InboxRule: ...
    def set_inbox_rule_enabled(self, rule: InboxRule, enabled: bool) -> InboxRule: ...
    def delete_inbox_rule(self, rule: InboxRule) -> None: ...


class InboxRuleService:
    def __init__(self, gateway: InboxRuleGateway) -> None:
        self.gateway = gateway

    def list(self, *, limit: int = 100, offset: int = 0) -> tuple[InboxRule, ...]:
        if not 1 <= limit <= MAX_INBOX_RULES or offset < 0:
            raise ConfigurationError("Use a rule limit from 1 to 1000 and a nonnegative offset.")
        return self.gateway.list_inbox_rules()[offset : offset + limit]

    def get(self, reference: str) -> InboxRule:
        for rule in self.gateway.list_inbox_rules():
            if rule.reference == reference:
                return rule
        raise ItemNotFoundError("The exact inbox rule was not found. Refresh the rule list.")

    def plan_create(self, rule: SimpleInboxRule) -> InboxRulePlan:
        validate_simple_rule(rule)
        rules = self.gateway.list_inbox_rules()
        if any(existing.name.casefold() == rule.name.casefold() for existing in rules):
            raise ConfigurationError("Choose a unique rule name so creation can be verified.")
        return InboxRulePlan(
            InboxRuleOperation.CREATE,
            creation=rule,
            inventory_fingerprint=inventory_fingerprint(rules),
        )

    def plan(
        self, reference: str, operation: InboxRuleOperation, *, current: InboxRule | None = None
    ) -> InboxRulePlan:
        operation = InboxRuleOperation(operation)
        if operation is InboxRuleOperation.CREATE:
            raise ConfigurationError("Use the simple-rule creation workflow.")
        before = current or self.get(reference)
        if before.reference != reference:
            raise ConfigurationError("The preview does not match the selected rule.")
        if operation is not InboxRuleOperation.DELETE and (before.unsupported or before.in_error):
            raise UnsupportedFeatureError(
                "Exchange marks this rule unsupported or in error. "
                "Inspect it in Outlook before editing."
            )
        return InboxRulePlan(operation, before=before)

    def apply(self, plan: InboxRulePlan, *, confirmed: bool = False) -> InboxRule | None:
        if not confirmed:
            raise ConfirmationRequiredError("Review and confirm the exact Exchange inbox rule.")
        if plan.operation is InboxRuleOperation.CREATE:
            if plan.creation is None or plan.inventory_fingerprint is None:
                raise ConfigurationError("The creation preview is incomplete.")
            validate_simple_rule(plan.creation)
            return self.gateway.create_inbox_rule(plan.creation, plan.inventory_fingerprint)
        if plan.before is None:
            raise ConfigurationError("The mutation preview is incomplete.")
        # Revalidate even a caller-constructed plan. Provider checks the fresh exact fingerprint.
        self.plan(plan.before.reference, plan.operation, current=plan.before)
        if plan.operation is InboxRuleOperation.DELETE:
            self.gateway.delete_inbox_rule(plan.before)
            return None
        return self.gateway.set_inbox_rule_enabled(
            plan.before, plan.operation is InboxRuleOperation.ENABLE
        )
