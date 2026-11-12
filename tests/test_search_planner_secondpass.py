from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gucmail.config import AppConfig
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import ConfigurationError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import (
    Address,
    MessageSummary,
    SearchExclusion,
    SearchQuery,
    SearchRanking,
)
from gucmail.services.messages import MessageService
from gucmail.services.search import (
    BooleanNode,
    NotNode,
    build_search_plan,
    evaluate_residual,
    parse_query,
)


def _candidate(*, subject: str = "Synthetic", folder_path: str = "Posteingang") -> Any:
    return SimpleNamespace(
        id=f"ews-{subject}",
        changekey="change-key",
        subject=subject,
        sender=SimpleNamespace(email_address="sender@example.test", name="Sender"),
        author=None,
        datetime_received=datetime(2026, 8, 5, tzinfo=UTC),
        datetime_sent=None,
        is_read=False,
        has_attachments=True,
        importance="Normal",
        categories=("Course", "Urgent"),
        size=2048,
        parent_folder_id=None,
        conversation_id=None,
        gucmail_flag_status=None,
        to_recipients=(SimpleNamespace(email_address="to@example.test", name="To"),),
        cc_recipients=(SimpleNamespace(email_address="cc@example.test", name="Cc"),),
        bcc_recipients=(SimpleNamespace(email_address="bcc@example.test", name="Bcc"),),
        body="Synthetic body",
        message_id="<synthetic@example.test>",
        attachments=(SimpleNamespace(name="brief.pdf", size=10),),
        folder_path=folder_path,
    )


def _gateway() -> EWSGateway:
    account = SimpleNamespace()
    return EWSGateway(AppConfig(), account_factory=lambda: account)


def _inbox_path(_folder: Any, *, parent_path: str | None = None) -> str:
    del parent_path
    return "Inbox"


def test_folder_selector_is_remote_scope_not_a_localized_path_predicate() -> None:
    plan = build_search_plan(
        SearchQuery(folder="fld_opaque", subject="Synthetic"),
        residual_candidate_limit=MAX_PAGE_SIZE,
    )
    assert plan.folder_scope == "fld_opaque"
    assert plan.residual_predicate is None
    assert {item.field for item in plan.server_filters} == {"subject"}

    gateway = _gateway()
    remote_folder = object()
    selected: list[str] = []

    def resolve(reference: str | None) -> object:
        assert reference is not None
        selected.append(reference)
        return remote_folder

    def localized_path(_folder: Any, *, parent_path: str | None = None) -> str:
        del parent_path
        return "Posteingang"

    def candidates(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return (_candidate(),)

    gateway._resolve_folder = resolve  # type: ignore[assignment]
    gateway._folder_path = localized_path  # type: ignore[assignment]
    gateway._bounded_items = candidates  # type: ignore[method-assign]

    results = gateway.search_messages(
        SearchQuery(folder="fld_opaque", subject="Synthetic"),
        limit=5,
    )
    assert selected == ["fld_opaque"]
    assert len(results) == 1

    expert = build_search_plan("folder:Inbox AND subject:Synthetic")
    assert expert.folder_scope == "Inbox"
    assert expert.residual_predicate is None
    assert {item.field for item in expert.server_filters} == {"subject"}


def test_server_only_offset_is_forwarded_once_and_not_double_applied() -> None:
    gateway = _gateway()
    remote_folder = object()
    captured: list[tuple[int, int]] = []

    def resolve(_folder: str | None) -> object:
        return remote_folder

    gateway._resolve_folder = resolve  # type: ignore[assignment]
    gateway._folder_path = _inbox_path  # type: ignore[assignment]

    def bounded(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> tuple[Any, ...]:
        del fields, order_by, filters
        captured.append((limit, offset))
        return (_candidate(subject="Page item"),)

    gateway._bounded_items = bounded  # type: ignore[assignment]
    results = gateway.search_messages(SearchQuery(subject="Page"), limit=5, offset=7)
    assert captured == [(5, 7)]
    assert [item.subject for item in results] == ["Page item"]


def test_residual_search_is_field_minimized_bounded_and_rejects_past_cap() -> None:
    gateway = _gateway()
    remote_folder = object()
    captured: list[tuple[tuple[str, ...], int, int]] = []

    def resolve(_folder: str | None) -> object:
        return remote_folder

    gateway._resolve_folder = resolve  # type: ignore[assignment]
    gateway._folder_path = _inbox_path  # type: ignore[assignment]

    def bounded(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> tuple[Any, ...]:
        del order_by, filters
        captured.append((fields, limit, offset))
        return (_candidate(),)

    gateway._bounded_items = bounded  # type: ignore[assignment]
    results = gateway.search_messages(SearchQuery(to="to@example.test"), limit=5)
    assert len(results) == 1
    fields, candidate_limit, server_offset = captured[-1]
    assert "to_recipients" in fields
    assert "cc_recipients" not in fields
    assert "bcc_recipients" not in fields
    assert "body" not in fields
    assert "attachments" not in fields
    assert candidate_limit == MAX_PAGE_SIZE
    assert server_offset == 0

    with pytest.raises(ConfigurationError, match="only exposes the first"):
        gateway.search_messages(
            SearchQuery(recipient="person@example.test"),
            limit=2,
            offset=MAX_PAGE_SIZE - 1,
        )


def test_multiple_categories_remain_an_exchange_and_filter() -> None:
    gateway = _gateway()
    remote_folder = object()
    captured: list[dict[str, object]] = []

    def resolve(_folder: str | None) -> object:
        return remote_folder

    gateway._resolve_folder = resolve  # type: ignore[assignment]
    gateway._folder_path = _inbox_path  # type: ignore[assignment]

    def bounded(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> tuple[Any, ...]:
        del fields, limit, offset, order_by
        captured.append(dict(filters or {}))
        return (_candidate(),)

    gateway._bounded_items = bounded  # type: ignore[assignment]
    assert gateway.search_messages(SearchQuery(categories=("Course", "Urgent")), limit=5)
    assert captured[-1]["categories__contains"] == ["Course", "Urgent"]


def test_recipient_subfields_are_distinct_and_residual_matching_is_exactly_scoped() -> None:
    query = SearchQuery(
        recipient="any@example.test",
        to="to@example.test",
        cc="cc@example.test",
        bcc="bcc@example.test",
    )
    plan = build_search_plan(query, residual_candidate_limit=MAX_PAGE_SIZE)
    assert {item.field for item in plan.residual_filters} == {"recipient", "to", "cc", "bcc"}
    assert plan.required_fields == frozenset({"recipients", "to", "cc", "bcc"})

    document = {
        "to": [Address("to@example.test")],
        "cc": [Address("cc@example.test")],
        "bcc": [Address("bcc@example.test")],
        "recipients": [Address("any@example.test")],
    }
    assert evaluate_residual(document, plan.residual_predicate)
    assert not evaluate_residual(
        {**document, "cc": [Address("other@example.test")]},
        plan.residual_predicate,
    )


def test_or_and_not_are_truthfully_residual_until_structured_ews_filters_exist() -> None:
    for text in ("subject:alpha OR sender:beta@example.test", "NOT subject:alpha"):
        ast = parse_query(text)
        assert isinstance(ast, (BooleanNode, NotNode))
        plan = build_search_plan(ast, residual_candidate_limit=25)
        assert plan.server_filters == ()
        assert plan.uses_residual
        assert "bounded local evaluation" in plan.execution_summary
        assert any(
            "OR expressions" in reason or "NOT expressions" in reason
            for reason in plan.fallback_reasons
        )


@pytest.mark.parametrize(
    "query",
    [
        SearchQuery(after=datetime(2026, 2, 1), before=datetime(2026, 1, 1)),
        SearchQuery(minimum_size=20, maximum_size=10),
        SearchQuery(attachment_name="brief.pdf", has_attachments=False),
        SearchQuery(folder="   "),
        SearchQuery(fuzzy_threshold=101),
    ],
)
def test_invalid_structured_searches_fail_before_gateway_io(query: SearchQuery) -> None:
    with pytest.raises(ValueError):
        build_search_plan(query)


def test_message_service_exposes_plan_and_refuses_silent_live_fuzzy_matching() -> None:
    class Gateway:
        def search_messages_planned(
            self,
            query: SearchQuery,
            plan: Any,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[MessageSummary, ...]:
            del query, plan, limit, offset
            return (
                MessageSummary(
                    reference="msg-one",
                    subject="Synthetic",
                    sender=Address("sender@example.test"),
                    received_at=datetime(2026, 8, 5, tzinfo=UTC),
                ),
            )

        def search_messages(self, *_args: Any, **_kwargs: Any) -> tuple[MessageSummary, ...]:
            raise AssertionError("the planned path must be used")

    service = MessageService(Gateway())  # type: ignore[arg-type]
    page = service.search_messages(SearchQuery(recipient="student@example.test"), limit=5)
    assert page.plan.residual_candidate_limit == MAX_PAGE_SIZE
    assert page.plan.uses_residual
    assert "checked locally" in page.execution_summary

    with pytest.raises(ConfigurationError, match="local-index feature"):
        service.search_messages(SearchQuery(text="synthetc", fuzzy=True), limit=5)


def test_structured_exclusions_remain_fail_closed_and_bounded() -> None:
    query = SearchQuery(
        subject="Synthetic",
        exclusions=(
            SearchExclusion("sender", "blocked@example.test", "not from blocked"),
            SearchExclusion("attachment_type", "image", "except images"),
        ),
    )
    plan = build_search_plan(query, residual_candidate_limit=MAX_PAGE_SIZE)
    assert plan.uses_residual
    assert {item.field for item in plan.residual_filters} == {
        "sender",
        "attachment_type",
    }
    assert evaluate_residual(
        {
            "subject": "Synthetic",
            "sender": Address("allowed@example.test"),
            "attachment_names": ("brief.pdf",),
        },
        plan.residual_predicate,
    )
    assert not evaluate_residual(
        {
            "subject": "Synthetic",
            "sender": Address("blocked@example.test"),
            "attachment_names": ("brief.pdf",),
        },
        plan.residual_predicate,
    )


def test_adapter_applies_rank_limit_and_thread_ownership_over_one_page() -> None:
    gateway = EWSGateway(
        AppConfig(email="account@example.test"),
        account_factory=lambda: SimpleNamespace(),
    )
    gateway._resolve_folder = lambda _folder: object()  # type: ignore[assignment]
    gateway._folder_path = _inbox_path  # type: ignore[assignment]
    captured: list[tuple[int, int, str | None]] = []

    def bounded(
        _folder: object,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int = 0,
        order_by: str | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> tuple[Any, ...]:
        del fields, filters
        captured.append((limit, offset, order_by))
        newest_external = _candidate(subject="Needs us")
        newest_external.id = "external"
        newest_external.sender = SimpleNamespace(email_address="teacher@example.test", name="T")
        newest_external.conversation_id = "thread-a"
        newest_external.size = 50
        older_ours = _candidate(subject="Our old reply")
        older_ours.id = "ours-old"
        older_ours.sender = SimpleNamespace(email_address="account@example.test", name="Us")
        older_ours.conversation_id = "thread-a"
        older_ours.datetime_received = datetime(2026, 8, 4, tzinfo=UTC)
        older_ours.size = 500
        second_external = _candidate(subject="Needs us 2")
        second_external.id = "external-2"
        second_external.sender = SimpleNamespace(email_address="teacher@example.test", name="T")
        second_external.conversation_id = "thread-b"
        second_external.size = 250
        return newest_external, older_ours, second_external

    gateway._bounded_items = bounded  # type: ignore[assignment]
    results = gateway.search_messages(
        SearchQuery(
            thread_response="waiting_on_me",
            ranking=SearchRanking("size", "descending", limit=1),
        ),
        limit=5,
    )
    assert captured == [(MAX_PAGE_SIZE, 0, "-size")]
    assert [item.subject for item in results] == ["Needs us 2"]


def test_unsupported_advanced_search_semantics_fail_before_io() -> None:
    with pytest.raises(ValueError, match="attachment index"):
        build_search_plan(
            SearchQuery(ranking=SearchRanking("attachment_type_frequency", "descending", 5))
        )
    with pytest.raises(ValueError, match="thread_response"):
        build_search_plan(SearchQuery(thread_response="someone_else"))
