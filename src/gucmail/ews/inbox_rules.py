"""Typed native inbox-rule operations that preserve Outlook's rule blob."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from datetime import date, datetime
from itertools import islice
from typing import Any, cast

from gucmail.errors import (
    ConflictError,
    ErrorContext,
    ItemNotFoundError,
    NetworkError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.services.inbox_rules import (
    MAX_INBOX_RULES,
    InboxRule,
    InboxRuleVerificationError,
    SimpleInboxRule,
    inventory_fingerprint,
    validate_simple_rule,
)


def _values(element: Any) -> Any:
    if element is None or isinstance(element, (str, int, bool, float)):
        return element
    if isinstance(element, (datetime, date)):
        return element.isoformat()
    if isinstance(element, (tuple, list)):
        return [_values(value) for value in element]
    return {
        field.name: _values(getattr(element, field.name, None))
        for field in element.FIELDS
        if getattr(element, field.name, None) is not None
    }


class NativeInboxRules:
    def __init__(
        self,
        account: Callable[[], Any],
        call: Callable[..., Any],
        resolve_folder: Callable[[str], Any],
    ) -> None:
        self.account = account
        self.call = call
        self.resolve_folder = resolve_folder

    def _read(self) -> tuple[Any, ...]:
        from exchangelib.services import GetInboxRules

        def read() -> tuple[Any, ...]:
            result = tuple(
                islice(GetInboxRules(account=self.account()).call(), MAX_INBOX_RULES + 1)
            )
            if len(result) > MAX_INBOX_RULES:
                raise UnsupportedFeatureError(
                    "The rule policy exceeds the 1000-rule safety limit. No rule was changed."
                )
            for rule in result:
                if isinstance(rule, Exception):
                    raise rule
                if rule is None or not rule.id:
                    raise UnsupportedFeatureError(
                        "Exchange returned a rule without a stable identity."
                    )
            if len({rule.id for rule in result}) != len(result):
                raise UnsupportedFeatureError("Exchange returned duplicate rule identities.")
            return result

        return cast(tuple[Any, ...], self.call("read inbox rules", read, read_only=True))

    def _map(self, rule: Any) -> InboxRule:
        from lxml import etree

        wire = etree.tostring(rule.to_xml(version=self.account().version), method="c14n")
        if len(wire) > 256 * 1024:
            raise UnsupportedFeatureError("The rule exceeds the 256 KiB safety limit.")
        return InboxRule(
            reference=str(rule.id),
            name=str(rule.display_name),
            priority=int(rule.priority),
            enabled=bool(rule.is_enabled),
            unsupported=bool(rule.is_not_supported),
            in_error=bool(rule.is_in_error),
            conditions=_values(rule.conditions) or {},
            exceptions=_values(rule.exceptions) or {},
            actions=_values(rule.actions) or {},
            fingerprint=hashlib.sha256(wire).hexdigest(),
        )

    def list(self) -> tuple[InboxRule, ...]:
        return tuple(self._map(rule) for rule in self._read())

    def _fresh(self, before: InboxRule) -> Any:
        for rule in self._read():
            if rule.id == before.reference:
                if self._map(rule).fingerprint != before.fingerprint:
                    raise ConflictError(
                        "The inbox rule changed after preview. Refresh before applying."
                    )
                return rule
        raise ItemNotFoundError("The selected inbox rule no longer exists. Refresh the list.")

    def _write(self, service: Any, rule: Any) -> None:
        try:
            self.call(
                "update inbox rule",
                lambda: service(account=self.account()).get(
                    rule=rule, remove_outlook_rule_blob=False
                ),
            )
        except NetworkError as exc:
            if isinstance(exc, ThrottledError):
                raise
            raise InboxRuleVerificationError(
                "The rule update lost its response and may have completed. "
                "Refresh the Exchange policy before another change; the write was not retried.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            ) from exc

    def _readback(self) -> tuple[InboxRule, ...]:
        try:
            return self.list()
        except Exception as exc:
            raise self._unverified() from exc

    @staticmethod
    def _unverified() -> InboxRuleVerificationError:
        return InboxRuleVerificationError(
            "Exchange accepted the rule update, but the resulting policy could not be verified. "
            "Refresh and inspect the policy before another change; the write was not retried.",
            context=ErrorContext(retry_safe=False, mutation_uncertain=True),
        )

    def create(self, request: SimpleInboxRule, expected_inventory: str) -> InboxRule:
        from exchangelib.properties import Actions, Conditions, Rule
        from exchangelib.services import CreateInboxRule

        validate_simple_rule(request)
        before = self.list()
        if inventory_fingerprint(before) != expected_inventory:
            raise ConflictError("The inbox policy changed after preview. Refresh before creating.")
        if any(rule.name.casefold() == request.name.casefold() for rule in before):
            raise ConflictError("A rule with this name already exists. Choose a unique name.")
        if len(before) >= MAX_INBOX_RULES:
            raise UnsupportedFeatureError("The rule policy is at the safety limit.")
        actions = Actions(
            assign_categories=list(request.categories) or None,
            mark_as_read=request.mark_read or None,
            stop_processing_rules=request.stop_processing or None,
            move_to_folder=(
                self.resolve_folder(request.move_to_folder) if request.move_to_folder else None
            ),
        )
        rule = Rule(
            display_name=request.name,
            priority=request.priority,
            is_enabled=False,
            conditions=Conditions(
                contains_sender_strings=list(request.sender_contains) or None,
                contains_subject_strings=list(request.subject_contains) or None,
                has_attachments=request.has_attachments,
            ),
            actions=actions,
        )
        # Validate the real library payload before sending. No account helper defaults.
        rule.to_xml(version=self.account().version)
        expected = self._map_without_id(rule)
        self._write(CreateInboxRule, rule)
        known = {existing.reference for existing in before}
        candidates = [
            result
            for result in self._readback()
            if result.reference not in known and result.name == request.name
        ]
        if len(candidates) != 1 or self._content(candidates[0]) != expected:
            raise self._unverified()
        return candidates[0]

    @staticmethod
    def _content(rule: InboxRule) -> tuple[Any, ...]:
        return (
            rule.name,
            rule.enabled,
            rule.conditions,
            rule.exceptions,
            rule.actions,
        )

    def _map_without_id(self, rule: Any) -> tuple[Any, ...]:
        # Priority can be renumbered by Exchange; only the requested behavior is compared.
        return self._content(self._map(rule))

    def set_enabled(self, before: InboxRule, enabled: bool) -> InboxRule:
        from exchangelib.services import SetInboxRule

        original = self._fresh(before)
        if original.is_not_supported or original.is_in_error:
            raise UnsupportedFeatureError("Inspect unsupported or errored rules in Outlook first.")
        if bool(original.is_enabled) == enabled:
            return self._map(original)
        rule = copy.copy(original)
        rule.is_enabled = enabled
        expected = self._map_without_id(rule)
        self._write(SetInboxRule, rule)
        for result in self._readback():
            if result.reference == before.reference and self._content(result) == expected:
                return result
        raise self._unverified()

    def delete(self, before: InboxRule) -> None:
        from exchangelib.services import DeleteInboxRule

        rule = self._fresh(before)
        # Rule.delete() can temporarily enable and rewrite a disabled rule. Never use it.
        self._write(DeleteInboxRule, rule)
        if any(result.reference == before.reference for result in self._readback()):
            raise self._unverified()
