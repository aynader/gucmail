from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gucmail.errors import ConfirmationRequiredError
from gucmail.models import MessageSummary, SearchQuery
from gucmail.services.cleanup import (
    CleanupCandidate,
    CleanupPlan,
    create_cleanup_plan,
    exclude_from_plan,
    execute_cleanup_plan,
    plan_duplicate_cleanup,
)
from gucmail.services.clusters import cluster_messages
from gucmail.services.extractors import (
    ExtractionPolicy,
    ExtractionStatus,
    extract_attachment_text,
)
from gucmail.services.insights import duplicate_insights, sender_insights, storage_insights
from gucmail.services.messages import MessageService
from gucmail.services.search import SearchPlan


def _messages() -> list[dict[str, object]]:
    complete_content_digest = "a" * 64
    return [
        {
            "reference": "msg-001",
            "subject": "Re: Project update",
            "body_text": "The same synthetic body.",
            "sender": "Tutor <TUTOR@example.test>",
            "received_at": "2026-01-01T10:00:00+00:00",
            "folder": "Inbox",
            "size": 200,
            "is_read": False,
            "attachments": [{"name": "notes.txt", "size": 40}],
            "content_sha256": complete_content_digest,
        },
        {
            "reference": "msg-002",
            "subject": "Project update",
            "body_text": "The  same synthetic body.",
            "sender": {"email": "tutor@example.test"},
            "received_at": "2026-01-02T10:00:00+00:00",
            "folder": "Archive",
            "size": 100,
            "is_read": True,
            "attachments": [{"name": "notes.txt", "size": 40}],
            "content_sha256": complete_content_digest,
        },
        {
            "reference": "msg-003",
            "subject": "A different message",
            "body_text": "Different text.",
            "sender": "other@example.test",
            "received_at": "2026-02-01T10:00:00+00:00",
            "folder": "Inbox",
            "size": 50,
            "is_read": True,
        },
    ]


def test_storage_and_sender_insights_are_deterministic_and_bounded() -> None:
    storage = storage_insights(_messages(), largest_limit=2, max_items=2)
    assert storage.scanned_count == 2
    assert storage.truncated is True
    assert storage.total_bytes == 300
    assert [item.reference for item in storage.largest_messages] == ["msg-001", "msg-002"]
    assert {bucket.label for bucket in storage.by_folder} == {"Inbox", "Archive"}

    senders = sender_insights(_messages())
    assert senders.senders[0].sender == "tutor@example.test"
    assert senders.senders[0].message_count == 2
    assert senders.senders[0].unread_count == 1


def test_duplicate_report_and_cleanup_preserve_one_canonical_copy() -> None:
    report = duplicate_insights(_messages())
    assert len(report.exact_groups) == 1
    group = report.exact_groups[0]
    assert group.references == ("msg-001", "msg-002")
    assert group.reclaimable_bytes == 100

    plan = plan_duplicate_cleanup(_messages(), report=report)
    assert plan.preview_only is True
    assert plan.permanent is False
    assert plan.item_count == 1
    assert plan.candidates[0].reference == "msg-002"
    assert plan.candidates[0].recoverable is True


def test_shallow_duplicate_evidence_is_reported_but_never_executable() -> None:
    shallow = [
        {"reference": "a", "subject": "Re: Status", "preview": "same preview"},
        {"reference": "b", "subject": "Status", "preview": "same preview"},
    ]
    report = duplicate_insights(shallow)
    assert report.exact_groups == ()
    assert len(report.possible_groups) == 1
    assert report.possible_groups[0].kind == "possible"
    assert plan_duplicate_cleanup(shallow, report=report).item_count == 0


def test_cleanup_requires_confirmation_and_explicit_callback() -> None:
    plan = create_cleanup_plan(
        [CleanupCandidate(reference="msg-009", reason="user_selected", size_bytes=42)]
    )
    with pytest.raises(ValueError, match="callback"):
        execute_cleanup_plan(plan, confirmed=True)
    with pytest.raises(ConfirmationRequiredError):
        execute_cleanup_plan(plan, lambda candidate: candidate.reference)

    visited: list[str] = []
    result = execute_cleanup_plan(
        plan,
        lambda candidate: visited.append(candidate.reference),
        confirmed=True,
    )
    assert visited == ["msg-009"]
    assert result.succeeded_count == 1
    assert result.failed_count == 0


@pytest.mark.parametrize(
    ("action", "permanent"),
    [("hard_delete", False), ("move_to_deleted", True), ("unknown", None)],
)
def test_cleanup_action_permanence_invariant_is_closed(action: str, permanent: bool | None) -> None:
    with pytest.raises(ValueError):
        create_cleanup_plan(
            [CleanupCandidate(reference="msg-unsafe", reason="test")],
            action=action,
            permanent=permanent,
        )


def test_cleanup_execution_rejects_a_forged_inconsistent_plan_before_callback() -> None:
    plan = CleanupPlan(
        plan_id="f" * 64,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        candidates=(
            CleanupCandidate(
                reference="msg-forged",
                reason="test",
                action="hard_delete",
                recoverable=True,
            ),
        ),
        action="hard_delete",
        permanent=False,
    )
    visited: list[str] = []
    with pytest.raises(ValueError, match="permanence"):
        execute_cleanup_plan(
            plan,
            lambda candidate: visited.append(candidate.reference),
            confirmed=True,
            allow_permanent=True,
            confirmation_token=plan.confirmation_token,
        )
    assert visited == []


def test_cleanup_exclusions_create_a_new_plan() -> None:
    original = create_cleanup_plan(
        [
            CleanupCandidate(reference="msg-a", reason="duplicate"),
            CleanupCandidate(reference="msg-b", reason="duplicate"),
        ],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    revised = exclude_from_plan(original, ["msg-b"])
    assert original.item_count == 2
    assert [item.reference for item in revised.candidates] == ["msg-a"]
    assert revised.excluded_references == ("msg-b",)


def test_safe_core_attachment_extraction_and_limits() -> None:
    policy = ExtractionPolicy(isolated=False, max_input_bytes=64, max_output_chars=12)
    text = extract_attachment_text("notes.txt", b"hello synthetic world", policy=policy)
    assert text.status is ExtractionStatus.SUCCESS
    assert text.text == "hello synthe"
    assert text.truncated is True

    oversized = extract_attachment_text("notes.txt", b"x" * 65, policy=policy)
    assert oversized.status is ExtractionStatus.TOO_LARGE
    assert oversized.text == ""


def test_attachment_extraction_rejects_active_or_macro_content() -> None:
    policy = ExtractionPolicy.all_supported(isolated=False)
    html = extract_attachment_text(
        "page.html",
        b"<p>Visible</p><script>steal()</script><img src='https://example.test/pixel'>",
        policy=policy,
    )
    assert html.ok
    assert "Visible" in html.text
    assert "steal" not in html.text
    assert "https://" not in html.text

    xml = extract_attachment_text(
        "data.xml",
        b"<!DOCTYPE x [<!ENTITY secret SYSTEM 'file:///private'>]><x>&secret;</x>",
        policy=policy,
    )
    assert xml.status is ExtractionStatus.UNSAFE
    macro = extract_attachment_text("sheet.xlsm", b"not opened", policy=policy)
    assert macro.status is ExtractionStatus.UNSAFE


def test_clustering_is_local_deterministic_and_bounded() -> None:
    first = cluster_messages(_messages(), by="sender", max_items=2, max_clusters=10, seed=7)
    second = cluster_messages(_messages(), by="sender", max_items=2, max_clusters=10, seed=7)
    assert first == second
    assert len(first) == 1
    assert first[0].message_count == 2
    assert first[0].message_references == ("msg-001", "msg-002")

    topic_one = cluster_messages(_messages(), by="topic", seed=123)
    topic_two = cluster_messages(_messages(), by="topic", seed=123)
    assert [cluster.cluster_id for cluster in topic_one] == [
        cluster.cluster_id for cluster in topic_two
    ]


def test_message_service_routes_advanced_search_query_through_planner() -> None:
    class PlannedGateway:
        def __init__(self) -> None:
            self.plan: SearchPlan | None = None

        def search_messages_planned(
            self,
            query: SearchQuery,
            plan: SearchPlan,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[MessageSummary, ...]:
            del query, limit, offset
            self.plan = plan
            return (MessageSummary("planned", "Synthetic", None, None),)

        def search_messages(self, *_args, **_kwargs):
            raise AssertionError("planned gateways must not use the legacy search path")

    gateway = PlannedGateway()
    page = MessageService(gateway).search_messages(  # type: ignore[arg-type]
        SearchQuery(
            recipient="student@example.test",
            body="deadline",
            attachment_name="brief.pdf",
            internet_message_id="<synthetic@example.test>",
        )
    )
    assert page.items[0].reference == "planned"
    assert gateway.plan is not None
    assert {value.field for value in gateway.plan.server_filters} == {
        "body",
        "internet_message_id",
    }
    assert {value.field for value in gateway.plan.residual_filters} == {
        "recipient",
        "attachment",
    }
    assert {"recipients", "attachment_names"}.issubset(gateway.plan.required_fields)
