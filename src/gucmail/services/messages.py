"""High-level, bounded message reads and preview-first mailbox mutations."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    ExitCode,
    GUCmailError,
    ItemNotFoundError,
    MutationExecutionError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.models import Importance, MessageDetail, MessageSummary, SearchQuery
from gucmail.services._base import (
    MutationExecution,
    MutationOutcome,
    MutationPlan,
    MutationRisk,
    PageRequest,
    ServicePage,
    bounded_page,
    make_mutation_plan,
    normalize_targets,
    require_confirmation,
    validate_reference,
)
from gucmail.services.search import QueryExpression, SearchPlan, build_search_plan, parse_query
from gucmail.state import StateStore

_ORDER_FIELDS = {
    "received_at",
    "datetime_received",
    "sent_at",
    "datetime_sent",
    "subject",
    "sender",
    "size",
    "importance",
}
_CATEGORY_MODES = {"set", "add", "remove"}


@dataclass(frozen=True, slots=True)
class SearchServicePage(ServicePage[MessageSummary]):
    """A normal service page plus a truthful server/residual execution plan."""

    plan: SearchPlan

    @property
    def execution_summary(self) -> str:
        return self.plan.execution_summary


class MessageGateway(Protocol):
    """Provider boundary used by both the command interface and the TUI."""

    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 50,
        offset: int = 0,
        order_by: str = "-datetime_received",
    ) -> Sequence[MessageSummary]: ...

    def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail: ...

    def search_messages(
        self,
        query: SearchQuery,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[MessageSummary]: ...

    def mark_read(self, reference: str, read: bool = True) -> Any: ...

    def flag(self, reference: str, flagged: bool = True) -> Any: ...

    def categorize(self, reference: str, categories: Sequence[str], mode: str = "set") -> Any: ...

    def set_importance(self, reference: str, importance: Importance) -> Any: ...

    def move_message(self, reference: str, destination: str = "trash") -> Any: ...

    def copy_message(self, reference: str, destination: str) -> Any: ...

    def delete_message(self, reference: str, permanent: bool = False) -> Any: ...


class MessageService:
    """Mailbox message use cases with no dependency on ``exchangelib`` types."""

    def __init__(self, gateway: MessageGateway, *, state: StateStore | None = None) -> None:
        self.gateway = gateway
        self.state = state

    def list_messages(
        self,
        folder: str = "inbox",
        *,
        limit: int = 25,
        offset: int = 0,
        order_by: str = "-datetime_received",
    ) -> ServicePage[MessageSummary]:
        request = PageRequest(limit=limit, offset=offset)
        folder_reference = validate_reference(folder, label="folder")
        normalized_order = self.validate_order(order_by)
        items = tuple(
            self.gateway.list_messages(
                folder=folder_reference,
                limit=request.limit,
                offset=request.offset,
                order_by=normalized_order,
            )
        )
        return bounded_page(items, request)

    def show_message(self, reference: str, *, mark_read: bool = False) -> MessageDetail:
        return self.gateway.show_message(
            validate_reference(reference, label="message reference"),
            mark_read=bool(mark_read),
        )

    def search_messages(
        self,
        query: SearchQuery,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchServicePage:
        if not isinstance(query, SearchQuery):
            raise ConfigurationError("search_messages requires a SearchQuery value.")
        if query.fuzzy:
            raise ConfigurationError(
                "Fuzzy matching is a local-index feature and is not silently emulated by "
                "the live EWS search. Use the local search index for fuzzy results."
            )
        request = PageRequest(limit=limit, offset=offset)
        # One Exchange page is the hard bound for residual evaluation.  Keeping the
        # advertised plan equal to the provider's real cap prevents a UI from
        # claiming that 500 candidates were checked when only 200 were fetched.
        plan = build_search_plan(query, residual_candidate_limit=MAX_PAGE_SIZE)
        planned_search = getattr(self.gateway, "search_messages_planned", None)
        if callable(planned_search):
            values = planned_search(
                query,
                plan,
                limit=request.limit,
                offset=request.offset,
            )
        else:
            values = self.gateway.search_messages(
                query,
                limit=request.limit,
                offset=request.offset,
            )
        items = tuple(values)
        page = bounded_page(items, request)
        return SearchServicePage(
            items=page.items,
            offset=page.offset,
            limit=page.limit,
            has_more=page.has_more,
            next_offset=page.next_offset,
            plan=plan,
        )

    def search_expression(
        self,
        query: str | QueryExpression,
        *,
        timezone: str = "UTC",
        limit: int = 25,
        offset: int = 0,
    ) -> SearchServicePage:
        """Run expert syntax through the exact bounded server/residual plan.

        A provider must explicitly implement the expression-plan boundary. Falling
        back to ``SearchQuery(text=...)`` would silently reinterpret field syntax,
        Boolean operators, and negation as ordinary body text.
        """

        request = PageRequest(limit=limit, offset=offset)
        expression = parse_query(query, timezone=timezone) if isinstance(query, str) else query
        plan = build_search_plan(expression, residual_candidate_limit=MAX_PAGE_SIZE)
        planned_search = getattr(self.gateway, "search_messages_expression_planned", None)
        if not callable(planned_search):
            raise ConfigurationError(
                "This mailbox provider does not expose the planned advanced-search boundary."
            )
        values = planned_search(
            expression,
            plan,
            limit=request.limit,
            offset=request.offset,
        )
        page = bounded_page(tuple(values), request)
        return SearchServicePage(
            items=page.items,
            offset=page.offset,
            limit=page.limit,
            has_more=page.has_more,
            next_offset=page.next_offset,
            plan=plan,
        )

    def plan_mark_read(self, references: Sequence[str], *, read: bool = True) -> MutationPlan:
        targets = normalize_targets(list(references))
        operation = "mark_read" if read else "mark_unread"
        return make_mutation_plan(
            operation=operation,
            targets=targets,
            summary=f"Mark {len(targets)} message(s) as {'read' if read else 'unread'}.",
            arguments={"read": bool(read)},
            requires_confirmation=len(targets) > 1,
        )

    def plan_flag(self, references: Sequence[str], *, flagged: bool = True) -> MutationPlan:
        targets = normalize_targets(list(references))
        operation = "flag" if flagged else "unflag"
        return make_mutation_plan(
            operation=operation,
            targets=targets,
            summary=f"{'Flag' if flagged else 'Unflag'} {len(targets)} message(s).",
            arguments={"flagged": bool(flagged)},
            requires_confirmation=len(targets) > 1,
        )

    def plan_categorize(
        self,
        references: Sequence[str],
        categories: Sequence[str],
        *,
        mode: str = "set",
    ) -> MutationPlan:
        targets = normalize_targets(list(references))
        normalized_mode = mode.strip().lower()
        if normalized_mode not in _CATEGORY_MODES:
            raise ConfigurationError("Category mode must be set, add, or remove.")
        normalized_categories = self._normalize_categories(categories)
        if not normalized_categories and normalized_mode != "set":
            raise ConfigurationError("Add and remove category operations need a category.")
        return make_mutation_plan(
            operation="categorize",
            targets=targets,
            summary=(
                f"{normalized_mode.title()} categories on {len(targets)} message(s): "
                + (", ".join(normalized_categories) or "clear all")
            ),
            arguments={"categories": normalized_categories, "mode": normalized_mode},
            requires_confirmation=len(targets) > 1,
        )

    def plan_importance(
        self, references: Sequence[str], *, importance: Importance | str
    ) -> MutationPlan:
        targets = normalize_targets(list(references))
        try:
            normalized = Importance(str(importance).strip().casefold())
        except ValueError as exc:
            raise ConfigurationError("Importance must be low, normal, or high.") from exc
        return make_mutation_plan(
            operation="set_importance",
            targets=targets,
            summary=f"Set importance to {normalized.value} on {len(targets)} message(s).",
            arguments={"importance": normalized.value},
            requires_confirmation=len(targets) > 1,
        )

    def plan_move(self, references: Sequence[str], destination: str) -> MutationPlan:
        targets = normalize_targets(list(references))
        folder = validate_reference(destination, label="destination folder")
        return make_mutation_plan(
            operation="move",
            targets=targets,
            summary=f"Move {len(targets)} message(s) to {folder!r}.",
            arguments={"destination": folder},
            risk=MutationRisk.HIGH_IMPACT,
            requires_confirmation=True,
        )

    def plan_copy(self, references: Sequence[str], destination: str) -> MutationPlan:
        targets = normalize_targets(list(references))
        folder = validate_reference(destination, label="destination folder")
        return make_mutation_plan(
            operation="copy",
            targets=targets,
            summary=f"Copy {len(targets)} message(s) to {folder!r}.",
            arguments={"destination": folder},
            requires_confirmation=len(targets) > 1,
        )

    def plan_delete(
        self,
        references: Sequence[str],
        *,
        permanent: bool = False,
        destination: str | None = None,
    ) -> MutationPlan:
        targets = normalize_targets(list(references))
        if destination is not None:
            if permanent:
                raise ConfigurationError("Permanent deletion cannot also move mail to a folder.")
            folder = validate_reference(destination, label="Delete destination folder")
            return make_mutation_plan(
                operation="move",
                targets=targets,
                summary=f"Move {len(targets)} message(s) to {folder} using the Delete preference.",
                arguments={"destination": folder},
                risk=MutationRisk.HIGH_IMPACT,
                requires_confirmation=True,
            )
        if permanent:
            summary = (
                f"Permanently delete {len(targets)} message(s). This operation cannot be undone."
            )
            risk = MutationRisk.DESTRUCTIVE
        else:
            summary = f"Move {len(targets)} message(s) to Deleted Items."
            risk = MutationRisk.HIGH_IMPACT
        return make_mutation_plan(
            operation="delete",
            targets=targets,
            summary=summary,
            arguments={"permanent": bool(permanent)},
            risk=risk,
            requires_confirmation=True,
        )

    def execute_mutation(
        self,
        plan: MutationPlan,
        *,
        dry_run: bool = False,
        confirmation_scope: str | None = None,
    ) -> MutationExecution:
        """Execute exactly the previewed targets; dry-run never touches the gateway."""

        if dry_run:
            return MutationExecution(plan=plan, dry_run=True, executed=False)
        require_confirmation(plan, confirmation_scope)
        if plan.operation not in {
            "mark_read",
            "mark_unread",
            "flag",
            "unflag",
            "categorize",
            "set_importance",
            "move",
            "copy",
            "delete",
        }:
            raise ConfigurationError(f"Unknown message mutation: {plan.operation}")
        results: list[Any] = []
        operation_id = uuid4().hex
        outcomes = [MutationOutcome(ref, "not_started") for ref in plan.targets]
        if self.state is not None:
            self.state.begin_message_operation(
                operation_id, plan.operation, plan.confirmation_scope, plan.targets
            )
        for index, reference in enumerate(plan.targets):
            dispatched = acknowledged = False
            try:
                if self.state is not None:
                    self.state.record_message_target(operation_id, index, "dispatching")
                dispatched = True
                result = self._execute_target(plan, reference)
                acknowledged = True
                results.append(result)
                outcomes[index] = MutationOutcome(reference, "completed")
                result_reference = (
                    result
                    if isinstance(result, str)
                    else result.get("reference")
                    if isinstance(result, dict)
                    else getattr(result, "reference", None)
                )
                outcomes[index] = MutationOutcome(
                    reference,
                    "completed",
                    result_reference if isinstance(result_reference, str) else None,
                )
                if self.state is not None:
                    self.state.record_message_target(
                        operation_id,
                        index,
                        "completed",
                        result_reference=outcomes[index].result_reference,
                    )
            except BaseException as exc:
                if not acknowledged:
                    definite = isinstance(
                        exc,
                        (
                            AuthenticationError,
                            ConfigurationError,
                            ConflictError,
                            ItemNotFoundError,
                            ThrottledError,
                            UnsupportedFeatureError,
                        ),
                    ) or (isinstance(exc, GUCmailError) and exc.context.retry_safe is True)
                    uncertain = dispatched and (
                        not definite
                        or (isinstance(exc, GUCmailError) and exc.context.mutation_uncertain)
                    )
                    status = "uncertain" if uncertain else "failed" if dispatched else "not_started"
                    outcomes[index] = MutationOutcome(
                        reference, status, error_type=type(exc).__name__
                    )
                    if self.state is not None and dispatched:
                        # If this fails, the durable dispatch marker remains unconfirmed.
                        with contextlib.suppress(GUCmailError, sqlite3.Error, OSError):
                            self.state.record_message_target(
                                operation_id, index, status, error_type=type(exc).__name__
                            )
                counts = {
                    status: sum(item.status == status for item in outcomes)
                    for status in ("completed", "failed", "uncertain", "not_started")
                }
                raise MutationExecutionError(
                    f"{plan.operation.replace('_', ' ').capitalize()} stopped: "
                    f"{counts['completed']} completed, {counts['failed']} failed, "
                    f"{counts['uncertain']} uncertain, {counts['not_started']} not started."
                    + (
                        " The accepted effect could not be saved in the recovery journal."
                        if acknowledged
                        else ""
                    ),
                    operation_id=operation_id,
                    operation=plan.operation,
                    outcomes=tuple(
                        {
                            "reference": item.reference,
                            "status": item.status,
                            "result_reference": item.result_reference,
                            "error_type": item.error_type,
                        }
                        for item in outcomes
                    ),
                    cause_type=type(exc).__name__,
                    journal_available=self.state is not None,
                    cause_exit_code=(
                        exc.exit_code
                        if isinstance(exc, GUCmailError)
                        else ExitCode.INTERRUPTED
                        if isinstance(exc, KeyboardInterrupt)
                        else ExitCode.SERVER
                    ),
                ) from exc
        return MutationExecution(
            plan=plan,
            dry_run=False,
            executed=True,
            results=tuple(results),
            operation_id=operation_id,
            outcomes=tuple(outcomes),
        )

    def _execute_target(self, plan: MutationPlan, reference: str) -> Any:
        parameters = plan.parameters
        if plan.operation in {"mark_read", "mark_unread"}:
            return self.gateway.mark_read(reference, read=bool(parameters["read"]))
        if plan.operation in {"flag", "unflag"}:
            return self.gateway.flag(reference, flagged=bool(parameters["flagged"]))
        if plan.operation == "categorize":
            categories = tuple(str(item) for item in parameters["categories"])
            return self.gateway.categorize(reference, categories, mode=str(parameters["mode"]))
        if plan.operation == "set_importance":
            return self.gateway.set_importance(reference, Importance(str(parameters["importance"])))
        if plan.operation == "move":
            return self.gateway.move_message(reference, str(parameters["destination"]))
        if plan.operation == "copy":
            return self.gateway.copy_message(reference, str(parameters["destination"]))
        if plan.operation == "delete":
            return self.gateway.delete_message(reference, permanent=bool(parameters["permanent"]))
        raise ConfigurationError(f"Unknown message mutation: {plan.operation}")

    @staticmethod
    def validate_order(order_by: str) -> str:
        value = order_by.strip()
        field = value[1:] if value.startswith("-") else value
        if field not in _ORDER_FIELDS:
            raise ConfigurationError(
                "Unsupported message ordering. Use received date, sent date, subject, sender, "
                "size, or importance."
            )
        return value

    @staticmethod
    def _normalize_categories(categories: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in categories:
            category = raw.strip()
            if not category:
                continue
            if len(category) > 255:
                raise ConfigurationError("A category name may not exceed 255 characters.")
            if any(ord(character) < 32 or ord(character) == 127 for character in category):
                raise ConfigurationError("Category names may not contain control characters.")
            key = category.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(category)
        return tuple(normalized)


Page = ServicePage

__all__ = ["MessageGateway", "MessageService", "Page"]
