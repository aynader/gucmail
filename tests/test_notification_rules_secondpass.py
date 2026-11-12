from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time

import pytest

from gucmail.models import Address, Importance
from gucmail.notification_events import (
    EventDisclosurePolicy,
    MailEventType,
    NewMailEvent,
    PrivacyMode,
    event_json,
    event_payload,
    stable_event_id,
)
from gucmail.notification_rules import (
    AllCondition,
    AnyCondition,
    BooleanCondition,
    BooleanField,
    ChoiceCondition,
    ChoiceField,
    InMemoryNotificationRuleRepository,
    MatchState,
    NotCondition,
    NotificationRule,
    NotificationRuleEngine,
    NotificationRuleSimulator,
    RuleAction,
    RuleActionKind,
    RuleEvaluationContext,
    SizeCondition,
    TextCondition,
    TextField,
    TextOperator,
    TimeWindowCondition,
    WeekdayCondition,
    export_rules,
    import_rules,
)


def _event(**changes: object) -> NewMailEvent:
    event = NewMailEvent(
        event_id="mail-synthetic-001",
        event_type=MailEventType.NEW_MESSAGE,
        occurred_at=datetime(2026, 8, 3, 10, 15, tzinfo=UTC),  # Monday
        detected_at=datetime(2026, 8, 3, 10, 16, tzinfo=UTC),
        account_id="synthetic-account",
        folder="Inbox/Course A",
        message_reference="m-local-001",
        sender=Address("advisor@dept.example.test", "Academic Advisor"),
        reply_to=(Address("reply@dept.example.test", "Advising Desk"),),
        recipients=(Address("student@campus.example.test", "Test Student"),),
        cc=(Address("team@collab.example.test", "Project Team"),),
        subject="Exam timetable — final version",
        body_preview="The updated exam timetable is attached.",
        importance=Importance.HIGH,
        is_read=False,
        has_attachments=True,
        attachment_count=1,
        attachment_filenames=("timetable.PDF",),
        size_bytes=2_500_000,
        categories=("Course", "Exams"),
        conversation_id="conversation-server-id",
        internet_message_id="<synthetic-001@example.test>",
        sender_in_contacts=True,
    )
    return replace(event, **changes)


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (
            TextCondition(TextField.FOLDER, TextOperator.EXACT, ("inbox/course a",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(
                TextField.SENDER,
                TextOperator.EXACT,
                ("nobody@example.test", "advisor@dept.example.test"),
            ),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.SENDER_DOMAIN, TextOperator.EXACT, ("dept.example.test",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.RECIPIENT, TextOperator.CONTAINS, ("campus.example",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.CC, TextOperator.GLOB, ("*@collab.example.test",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("timetable",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.SUBJECT, TextOperator.NOT_CONTAINS, ("cancelled",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(
                TextField.SUBJECT,
                TextOperator.EXACT,
                ("Exam timetable — final version",),
            ),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.SUBJECT, TextOperator.REGEX, (r"^exam\s+time",)),
            MatchState.MATCH,
        ),
        (
            BooleanCondition(BooleanField.HAS_ATTACHMENTS, True),
            MatchState.MATCH,
        ),
        (BooleanCondition(BooleanField.IS_READ, False), MatchState.MATCH),
        (BooleanCondition(BooleanField.SENDER_IN_CONTACTS, True), MatchState.MATCH),
        (
            TextCondition(TextField.ATTACHMENT_FILENAME, TextOperator.GLOB, ("*.pdf",)),
            MatchState.MATCH,
        ),
        (
            TextCondition(TextField.ATTACHMENT_EXTENSION, TextOperator.EXACT, (".pdf",)),
            MatchState.MATCH,
        ),
        (ChoiceCondition(ChoiceField.IMPORTANCE, ("high",)), MatchState.MATCH),
        (ChoiceCondition(ChoiceField.CATEGORY, ("exams",)), MatchState.MATCH),
        (SizeCondition(minimum_bytes=1_000_000, maximum_bytes=3_000_000), MatchState.MATCH),
        (TimeWindowCondition(time(9), time(12), "UTC"), MatchState.MATCH),
        (WeekdayCondition(frozenset({0}), "UTC"), MatchState.MATCH),
    ],
)
def test_all_supported_leaf_conditions(
    condition: object,
    expected: MatchState,
) -> None:
    trace = condition.evaluate(_event(), RuleEvaluationContext())  # type: ignore[attr-defined]
    assert trace.outcome is expected
    assert "Exam timetable" not in trace.detail


def test_body_and_external_filters_are_explicit_and_fail_closed() -> None:
    body = TextCondition(TextField.BODY_PREVIEW, TextOperator.CONTAINS, ("updated",))
    assert body.evaluate(_event(), RuleEvaluationContext()).outcome is MatchState.UNKNOWN
    enabled = RuleEvaluationContext(body_matching_enabled=True)
    assert body.evaluate(_event(), enabled).outcome is MatchState.MATCH

    external = BooleanCondition(BooleanField.SENDER_EXTERNAL, True)
    context = RuleEvaluationContext(internal_domains=frozenset({"campus.example.test"}))
    assert external.evaluate(_event(), context).outcome is MatchState.MATCH
    internal = replace(_event(), sender=Address("staff@sub.campus.example.test"))
    assert external.evaluate(internal, context).outcome is MatchState.NO_MATCH


def test_missing_fields_are_unknown_and_unknown_propagates_through_boolean_ast() -> None:
    missing = replace(
        _event(),
        sender=None,
        attachment_filenames=None,
        categories=None,
        size_bytes=None,
    )
    sender = TextCondition(TextField.SENDER, TextOperator.CONTAINS, ("advisor",))
    attachment = TextCondition(TextField.ATTACHMENT_EXTENSION, TextOperator.EXACT, (".pdf",))
    category = ChoiceCondition(ChoiceField.CATEGORY, ("exams",))
    size = SizeCondition(minimum_bytes=1)
    context = RuleEvaluationContext()
    assert sender.evaluate(missing, context).outcome is MatchState.UNKNOWN
    assert attachment.evaluate(missing, context).outcome is MatchState.UNKNOWN
    assert category.evaluate(missing, context).outcome is MatchState.UNKNOWN
    assert size.evaluate(missing, context).outcome is MatchState.UNKNOWN
    assert NotCondition(sender).evaluate(missing, context).outcome is MatchState.UNKNOWN
    assert (
        AllCondition((sender, BooleanCondition(BooleanField.IS_READ, False)))
        .evaluate(missing, context)
        .outcome
        is MatchState.UNKNOWN
    )
    assert (
        AnyCondition((sender, BooleanCondition(BooleanField.IS_READ, True)))
        .evaluate(missing, context)
        .outcome
        is MatchState.UNKNOWN
    )


def test_nested_and_or_not_and_stop_processing_are_deterministic() -> None:
    nested = AllCondition(
        (
            TextCondition(TextField.FOLDER, TextOperator.CONTAINS, ("inbox",)),
            AnyCondition(
                (
                    TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("exam",)),
                    TextCondition(TextField.SENDER, TextOperator.CONTAINS, ("office",)),
                )
            ),
            NotCondition(TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("cancelled",))),
        )
    )
    first = NotificationRule(
        "urgent-exams",
        "Urgent exams",
        nested,
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),),
        enabled=True,
        stop_processing=True,
        priority=100,
        order=10,
    )
    lower = NotificationRule(
        "fallback",
        "Fallback",
        BooleanCondition(BooleanField.IS_READ, False),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "console"),),
        enabled=True,
        priority=0,
        order=0,
    )
    decision = NotificationRuleEngine((lower, first)).evaluate(_event())
    assert decision.matched_rule_ids == ("urgent-exams",)
    assert decision.immediate_providers == ("desktop",)
    assert decision.stopped_by_rule_id == "urgent-exams"
    assert decision.traces[0].evaluation is not None
    assert decision.traces[0].evaluation.children


def test_suppress_action_overrides_routes() -> None:
    route = NotificationRule(
        "route",
        "Route",
        BooleanCondition(BooleanField.IS_READ, False),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),),
        enabled=True,
        priority=10,
    )
    suppress = NotificationRule(
        "suppress",
        "Suppress",
        TextCondition(TextField.SENDER_DOMAIN, TextOperator.GLOB, ("*.example.test",)),
        (RuleAction(RuleActionKind.SUPPRESS),),
        enabled=True,
        priority=5,
    )
    decision = NotificationRuleEngine((route, suppress)).evaluate(_event())
    assert decision.matched_rule_ids == ("route", "suppress")
    assert decision.suppressed is True
    assert decision.immediate_providers == ()


def test_rule_crud_duplicate_enable_disable_and_reorder() -> None:
    base = NotificationRule(
        "base",
        "Base",
        BooleanCondition(BooleanField.IS_READ, False),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),),
        order=0,
    )
    other = replace(base, rule_id="other", name="Other", order=1)
    repository = InMemoryNotificationRuleRepository((base, other))
    assert repository.set_enabled("BASE", enabled=True).enabled is True
    assert repository.set_enabled("base", enabled=False).enabled is False
    duplicate = repository.duplicate_rule("base", new_id="copy", new_name="Copy")
    assert duplicate.enabled is False
    assert duplicate.condition == base.condition
    reordered = repository.reorder(("copy", "other", "base"))
    assert {rule.rule_id: rule.order for rule in reordered} == {
        "copy": 0,
        "other": 1,
        "base": 2,
    }
    edited = replace(repository.get_rule("copy"), name="Edited")  # type: ignore[arg-type]
    repository.save_rule(edited)
    assert repository.get_rule("copy") == edited
    assert repository.delete_rule("copy") is True
    assert repository.delete_rule("copy") is False


def test_rule_export_import_round_trip_and_strict_validation() -> None:
    rule = NotificationRule(
        "portable",
        "Portable",
        AllCondition(
            (
                TextCondition(TextField.FOLDER, TextOperator.EXACT, ("Inbox",)),
                WeekdayCondition(frozenset({0, 1, 2, 3, 4}), "UTC"),
            )
        ),
        (
            RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),
            RuleAction(RuleActionKind.ROUTE_DIGEST, "digest"),
        ),
        enabled=True,
        description="Synthetic portable rule",
    )
    encoded = export_rules((rule,))
    assert import_rules(encoded) == (rule,)
    malformed = json.loads(encoded)
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        import_rules(json.dumps(malformed))
    malformed = json.loads(encoded)
    malformed["rules"].append(malformed["rules"][0])
    with pytest.raises(ValueError, match="duplicate"):
        import_rules(json.dumps(malformed))
    with pytest.raises(ValueError, match="regular expression"):
        TextCondition(TextField.SUBJECT, TextOperator.REGEX, ("(",))


def test_simulator_explains_routes_and_provider_disclosure_without_dispatching() -> None:
    rule = NotificationRule(
        "simulate",
        "Simulator",
        TextCondition(TextField.SUBJECT, TextOperator.CONTAINS, ("exam",)),
        (
            RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "desktop"),
            RuleAction(RuleActionKind.ROUTE_DIGEST, "private-digest"),
        ),
        enabled=True,
    )
    simulator = NotificationRuleSimulator(
        NotificationRuleEngine((rule,)),
        provider_policies={
            "desktop": EventDisclosurePolicy(
                PrivacyMode.SENDER_AND_SUBJECT,
                redact_addresses=False,
            ),
            "private-digest": EventDisclosurePolicy(),
        },
    )
    result = simulator.simulate((_event(), replace(_event(), event_id="other", subject="News")))
    assert result.total_events == 2
    assert result.matching_events == 1
    assert result.notification_events == 1
    assert result.provider_counts == {"desktop": 1, "private-digest": 1}
    assert "sender" in result.provider_exposed_fields["desktop"]
    assert "subject" not in result.provider_exposed_fields["private-digest"]
    assert result.examples[0].traces[0].matched is True


def test_event_privacy_serialization_is_sanitized_and_fail_closed() -> None:
    hostile = replace(
        _event(),
        subject="\x1b[31mExam for student@campus.example.test",
        attachment_filenames=("\x1b]0;bad\x07notes.pdf",),
    )
    private = event_payload(hostile)
    assert private == {
        "event_id": "mail-synthetic-001",
        "event_type": "new_message",
        "detected_at": "2026-08-03T10:16:00+00:00",
        "summary": "New GUC email",
    }
    full = event_payload(
        hostile,
        EventDisclosurePolicy(
            PrivacyMode.FULL,
            redact_addresses=True,
            include_attachment_names=True,
            include_server_identifiers=True,
        ),
    )
    assert "\x1b" not in json.dumps(full)
    assert "student@campus.example.test" not in str(full)
    assert full["attachment_filenames"] == ["notes.pdf"]
    assert full["conversation_id"] == "conversation-server-id"
    assert json.loads(event_json(hostile))["summary"] == "New GUC email"
    with pytest.raises(ValueError, match="unknown event disclosure"):
        EventDisclosurePolicy(field_allowlist=frozenset({"future_private_field"}))


def test_event_id_is_stable_across_detection_time_and_opaque() -> None:
    values = {
        "account_id": "synthetic-account",
        "event_type": MailEventType.NEW_MESSAGE,
        "occurred_at": datetime(2026, 8, 3, 10, tzinfo=UTC),
        "message_reference": "m-local-001",
    }
    first = stable_event_id(**values)
    second = stable_event_id(**values)
    assert first == second
    assert "synthetic-account" not in first
    assert "m-local-001" not in first
    assert first != stable_event_id(**{**values, "message_reference": "m-local-002"})


def test_profile_without_rules_or_routing_notifies_every_enabled_provider() -> None:
    """Live finding: an attached desktop provider received nothing without rules."""

    from gucmail.notification_policies import (
        MonitoringProfile,
        NotificationRouter,
        ProviderPolicy,
        RoutingMatrix,
    )
    from gucmail.notification_rules import RuleDecision

    profile = MonitoringProfile(
        "primary",
        "Primary mailbox",
        ("Inbox",),
        providers=(ProviderPolicy("desktop-main", True), ProviderPolicy("off", False)),
        routing_matrix=RoutingMatrix(("desktop-main", "off")),
    )
    decision = RuleDecision((), (), (), (), False, None, ())
    plan = NotificationRouter().plan(_event(), decision, profile)
    assert plan.immediate_providers == ("desktop-main",)
