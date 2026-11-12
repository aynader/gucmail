from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import pytest

from gucmail.command_catalog import CORE_COMMANDS
from gucmail.commands import (
    CommandInvocation,
    Exclusion,
    ParameterType,
    Ranking,
    ThreadResponseState,
)
from gucmail.help.reference import DOCUMENTED_NLP_EXAMPLES
from gucmail.nlp import (
    InterpretationKind,
    NaturalLanguageParser,
    PersonEntity,
    extract_date_range,
    extract_future_datetime,
    extract_size_constraint,
    normalize_text,
    resolve_folder,
)
from gucmail.palette import PaletteEngine, interpret_typed_palette_sequence

CAIRO = ZoneInfo("Africa/Cairo")
NOW = datetime(2026, 8, 13, 15, 0, tzinfo=CAIRO)


def _command(parser: NaturalLanguageParser, text: str) -> tuple[str, dict[str, object]]:
    result = parser.interpret(text)
    assert result.invocation is not None, (text, result)
    return result.invocation.command_id, result.invocation.as_dict()


@pytest.mark.parametrize(
    ("text", "expected_command"),
    [
        ("storag sent itms", "storage.show_storage_by_folder"),
        ("run guc mail when windows starts", "help.search"),
        ("scheduel this for monday", "scheduled.schedule_current"),
        ("preveiw even wider please", "layout.preview_wider"),
        ("discrd please test webhook", "integrations.discord_test"),
        ("could you show unread emals by ahmd", "search.mail"),
        ("mail last month unread from Ahmed please", "search.mail"),
        ("student affairs pdf unread mail last month please", "search.mail"),
        ("could you open notifcations setings", "settings.open_notification_settings"),
        ("please back up the inbxo", "backup.create"),
    ],
)
def test_non_exact_perturbations_generalize_without_exact_corpus(
    text: str, expected_command: str
) -> None:
    parser = NaturalLanguageParser(now=NOW)
    assert normalize_text(text).normalized not in parser._exact

    command_id, _parameters = _command(parser, text)

    assert command_id == expected_command


@pytest.mark.parametrize(
    ("text", "expected_command"),
    [
        ("messages not from Student Affairs", "search.mail"),
        ("PDFs except newsletters", "search.mail"),
        ("messages with attachments but not images", "search.mail"),
        ("mail before June not in Deleted", "search.mail"),
        ("everything from Ahmed except calendar invitations", "search.mail"),
        ("oldest unread", "search.mail"),
        ("newest messages from Ahmed", "search.mail"),
        ("messages waiting for my reply", "search.mail"),
        ("threads waiting for them", "search.mail"),
        ("conversations where they sent the last message", "search.mail"),
    ],
)
def test_search_negation_ranking_and_response_language(text: str, expected_command: str) -> None:
    parser = NaturalLanguageParser(now=NOW)

    command_id, _parameters = _command(parser, text)

    assert command_id == expected_command


def test_exclusions_are_typed_and_never_become_positive_slots() -> None:
    parser = NaturalLanguageParser(now=NOW)

    excluded_sender = parser.interpret("messages not from Student Affairs")
    excluded_kind = parser.interpret("PDFs except newsletters")
    excluded_folder = parser.interpret("mail before June not in Deleted")

    assert excluded_sender.entities.person is None
    assert excluded_sender.entities.exclusions[0].field == "sender"
    assert excluded_sender.entities.exclusions[0].value == "student affairs"
    assert excluded_sender.invocation is not None
    assert "sender" not in excluded_sender.invocation.as_dict()
    assert excluded_kind.entities.exclusions[0].field == "text"
    assert excluded_kind.entities.exclusions[0].value == "newsletter"
    assert excluded_folder.entities.folder is None
    assert excluded_folder.entities.exclusions[0].field == "folder"


def test_ranking_and_thread_response_models_are_exposed_in_preview() -> None:
    parser = NaturalLanguageParser(now=NOW)

    oldest = parser.interpret("oldest unread")
    waiting = parser.interpret("messages waiting for my reply")

    assert oldest.entities.ranking is not None
    assert oldest.entities.ranking.field == "received_at"
    assert oldest.entities.ranking.direction == "ascending"
    assert "Order: received_at ascending" in oldest.preview
    assert waiting.entities.thread_response is not None
    assert waiting.entities.thread_response.state == "waiting_on_me"
    assert "Response state: waiting_on_me" in waiting.preview


def test_secondpass_command_preview_examples_are_friendly_and_exact() -> None:
    parser = NaturalLanguageParser(now=datetime(2026, 8, 9, 15, 0, tzinfo=ZoneInfo("Africa/Cairo")))

    search = parser.interpret("mail from ahmed last month unread")
    scheduled = parser.interpret(
        "send this tomorrow morning",
        context={"current_draft": "draft-preview"},
    )

    assert search.invocation is not None
    assert search.invocation.command_id == "search.mail"
    assert "Action: Search Mail" in search.preview
    assert "Sender: Ahmed" in search.preview
    assert "Date: July 1\u201331, 2026" in search.preview
    assert "Read state: Unread" in search.preview
    assert "Scope: All Mail" in search.preview

    assert scheduled.invocation is not None
    assert scheduled.invocation.command_id == "scheduled.schedule_current"
    assert scheduled.entities.when is not None
    assert scheduled.entities.when.local.hour == 8
    assert "Action: Schedule Current Draft" in scheduled.preview
    assert "Date: August 10, 2026" in scheduled.preview
    assert "Time: 08:00" in scheduled.preview
    assert "Time zone: Africa/Cairo" in scheduled.preview
    assert "Action: Send" in scheduled.preview


def test_search_semantics_are_first_class_validated_invocation_slots() -> None:
    result = NaturalLanguageParser(now=NOW).interpret(
        "oldest messages waiting for my reply not from Student Affairs"
    )

    assert result.invocation is not None
    values = result.invocation.as_dict()
    assert values["exclusions"] == (
        Exclusion("sender", "student affairs", "not from student affairs"),
    )
    assert values["ranking"] == Ranking("received_at", "ascending", expression="oldest")
    assert values["thread_response"] == ThreadResponseState("waiting_on_me", "response ownership")
    assert CORE_COMMANDS.validate_invocation(result.invocation) == ()

    specs = {parameter.name: parameter for parameter in CORE_COMMANDS.get("search.mail").parameters}
    assert specs["exclusions"].type is ParameterType.EXCLUSION
    assert specs["exclusions"].multiple
    assert specs["ranking"].type is ParameterType.RANKING
    assert specs["thread_response"].type is ParameterType.THREAD_RESPONSE


def test_attachment_search_carries_the_same_structured_residual_filters() -> None:
    result = NaturalLanguageParser(now=NOW).interpret(
        "find oldest PDF attachments waiting for them except newsletters"
    )

    assert result.invocation is not None
    assert result.invocation.command_id == "attachments.search"
    values = result.invocation.as_dict()
    assert isinstance(values["ranking"], Ranking)
    assert isinstance(values["thread_response"], ThreadResponseState)
    assert isinstance(values["exclusions"], tuple)
    assert isinstance(values["exclusions"][0], Exclusion)
    assert CORE_COMMANDS.validate_invocation(result.invocation) == ()


def test_registry_rejects_untyped_structured_search_slots() -> None:
    invocation = CommandInvocation.from_mapping(
        "search.mail",
        {
            "exclusions": ("not from somebody",),
            "ranking": "oldest",
            "thread_response": "waiting",
        },
    )

    errors = CORE_COMMANDS.validate_invocation(invocation)

    assert len(errors) == 3
    assert all("does not accept" in error for error in errors)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("this weekend", "2026-08-15T00:00:00+03:00", "2026-08-17T00:00:00+03:00"),
        ("last weekend", "2026-08-08T00:00:00+03:00", "2026-08-10T00:00:00+03:00"),
        ("this year", "2026-01-01T00:00:00+02:00", "2027-01-01T00:00:00+02:00"),
        ("last year", "2025-01-01T00:00:00+02:00", "2026-01-01T00:00:00+02:00"),
        ("between May and July", "2026-05-01T00:00:00+03:00", "2026-08-01T00:00:00+03:00"),
        ("on August 5", "2026-08-05T00:00:00+03:00", "2026-08-06T00:00:00+03:00"),
        ("around August 5", "2026-08-04T00:00:00+03:00", "2026-08-07T00:00:00+03:00"),
    ],
)
def test_date_ranges_are_absolute_timezone_aware(text: str, start: str, end: str) -> None:
    result = extract_date_range(text, timezone="Africa/Cairo", now=NOW)

    assert result is not None
    assert result.start is not None
    assert result.end is not None
    assert result.start.isoformat() == start
    assert result.end.isoformat() == end
    assert result.timezone == "Africa/Cairo"


def test_open_ended_month_and_year_boundaries_are_not_fake_bounded_ranges() -> None:
    before = extract_date_range("mail before June", timezone="Africa/Cairo", now=NOW)
    after = extract_date_range("mail after June", timezone="Africa/Cairo", now=NOW)
    since = extract_date_range("mail since January", timezone="Africa/Cairo", now=NOW)
    before_year = extract_date_range("mail before 2025", timezone="Africa/Cairo", now=NOW)

    assert before is not None and before.start is None
    assert before.end is not None and before.end.date().isoformat() == "2026-06-01"
    assert after is not None and after.end is None
    assert after.start is not None and after.start.date().isoformat() == "2026-07-01"
    assert since is not None and since.end is None
    assert since.start is not None and since.start.date().isoformat() == "2026-01-01"
    assert before_year is not None and before_year.start is None
    assert before_year.end is not None and before_year.end.date().isoformat() == "2025-01-01"


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("next Monday", "2026-08-17T00:00:00+03:00", "2026-08-18T00:00:00+03:00"),
        ("Monday afternoon", "2026-08-17T12:00:00+03:00", "2026-08-17T17:00:00+03:00"),
    ],
)
def test_named_weekday_search_ranges_are_explicit(text: str, start: str, end: str) -> None:
    result = extract_date_range(text, timezone="Africa/Cairo", now=NOW)

    assert result is not None
    assert result.start is not None
    assert result.end is not None
    assert result.start.isoformat() == start
    assert result.end.isoformat() == end


def test_between_size_expression_inherits_the_explicit_unit() -> None:
    result = extract_size_constraint("messages between 2 and 5 MB")

    assert result is not None
    assert result.minimum_bytes == 2_000_000
    assert result.maximum_bytes == 5_000_000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomorrow at nine", "2026-08-14T09:00:00+03:00"),
        ("Monday afternoon", "2026-08-17T14:00:00+03:00"),
        ("in 20 minutes", "2026-08-13T15:20:00+03:00"),
        ("after lunch", "2026-08-14T13:00:00+03:00"),
        ("end of day", "2026-08-13T17:00:00+03:00"),
        ("next weekday at 8", "2026-08-14T08:00:00+03:00"),
        ("tonight", "2026-08-13T20:00:00+03:00"),
        ("delay this by an hour", "2026-08-13T16:00:00+03:00"),
    ],
)
def test_future_time_expressions_are_absolute_and_explainable(text: str, expected: str) -> None:
    result = extract_future_datetime(text, timezone="Africa/Cairo", now=NOW)

    assert result is not None
    assert result.local.isoformat() == expected
    assert result.timezone == "Africa/Cairo"
    assert result.explanation


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("Europe/Berlin")),
        datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("Europe/Berlin")),
    ],
)
def test_future_time_rejects_nonexistent_and_ambiguous_dst_wall_times(
    now: datetime,
) -> None:
    with pytest.raises(ValueError, match="Could not resolve future time"):
        extract_future_datetime("tomorrow at 2:30", timezone="Europe/Berlin", now=now)


def test_numeric_date_is_rejected_instead_of_locale_guessed() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        extract_future_datetime("send on 03/04/26", timezone="Africa/Cairo", now=NOW)


def test_fuzzy_folder_resolution_uses_candidate_paths_and_reports_ambiguity() -> None:
    candidates = ("Inbox/Master Applications", "Archive", "Sent Items")

    result = resolve_folder("master aplications", candidates=candidates)

    assert result is not None
    assert result.canonical == "Inbox/Master Applications"
    assert result.confidence > 0.9


@pytest.mark.parametrize(
    ("text", "candidates", "canonical"),
    [
        ("inbox", (), "inbox"),
        ("trash", (), "deleted"),
        ("folder Research", ("Inbox", "Research"), "Research"),
        (
            "in Inbox/Master Applications",
            ("Inbox/Master Applications", "Archive"),
            "Inbox/Master Applications",
        ),
    ],
)
def test_folder_resolution_exact_alias_custom_and_nested(
    text: str,
    candidates: tuple[str, ...],
    canonical: str,
) -> None:
    result = resolve_folder(text, candidates=candidates)

    assert result is not None
    assert result.canonical == canonical


def test_duplicate_folder_display_names_require_a_path_choice() -> None:
    result = resolve_folder(
        "archive",
        candidates=("Projects/Archive", "Private/Archive", "Inbox"),
    )

    assert result is not None
    assert result.ambiguous
    assert result.canonical == "Private/Archive"
    assert result.alternatives == ("Projects/Archive",)


def test_plain_person_name_stays_inside_palette_for_clarification() -> None:
    result = NaturalLanguageParser(now=NOW).interpret(
        "Ahmed",
        person_candidates=("Ahmed Hassan <h@example.test>", "Ahmed Mohamed <m@example.test>"),
    )

    assert result.kind is InterpretationKind.AMBIGUOUS
    assert result.invocation is None
    assert [candidate.command_id for candidate in result.candidates] == [
        "search.mail",
        "contacts.search",
        "compose.new",
        "calendar.search",
    ]
    assert result.entities.person is not None
    assert result.entities.person.ambiguous


def test_current_area_ranks_but_does_not_silently_resolve_person_ambiguity() -> None:
    result = NaturalLanguageParser(now=NOW).interpret(
        "Ahmed",
        context={"current_area": "contacts"},
        person_candidates=("Ahmed Hassan", "Ahmed Mohamed"),
    )

    assert result.kind is InterpretationKind.AMBIGUOUS
    assert result.invocation is None
    assert result.candidates[0].command_id == "contacts.search"


def test_pronoun_without_real_selection_remains_context_gated() -> None:
    result = NaturalLanguageParser(now=NOW).interpret("reply to this")

    assert result.invocation is not None
    assert result.invocation.command_id == "mail.reply"
    assert "reference" not in result.invocation.as_dict()
    assert "Context required: selected_message" in result.preview


@pytest.mark.parametrize(
    ("text", "expected_command", "required_context"),
    [
        ("show more from this sender", "search.search_sender", "selected_message"),
        ("open this sender's contact", "contacts.open_sender_contact", "selected_message"),
        ("find emails related to this", "search.similar_message_search", "selected_message"),
        ("email this contact", "contacts.compose_to_contact", "selected_contact"),
        (
            "meeting with this contact",
            "contacts.schedule_meeting_with_contact",
            "selected_contact",
        ),
        (
            "add this sender to contacts",
            "contacts.add_sender_to_contacts",
            "selected_message",
        ),
    ],
)
def test_contextual_thread_and_contact_phrases_never_invent_people(
    text: str, expected_command: str, required_context: str
) -> None:
    result = NaturalLanguageParser(now=NOW).interpret(text)

    assert result.invocation is not None
    assert result.invocation.command_id == expected_command
    assert result.entities.person is None
    assert "person" not in result.invocation.as_dict()
    assert f"Context required: {required_context}" in result.preview


def test_selected_contact_is_resolved_only_from_supplied_context() -> None:
    result = NaturalLanguageParser(now=NOW).interpret(
        "email this contact",
        context={"selected_contact": "ahmed@example.test"},
    )

    assert result.invocation is not None
    person = result.invocation.as_dict()["person"]
    assert isinstance(person, PersonEntity)
    assert person.email == "ahmed@example.test"
    assert person.source == "context"
    assert "Person: ahmed@example.test <ahmed@example.test>" in result.preview
    assert "Context: selected_contact" in result.preview


def test_folder_destination_is_not_reported_as_a_person() -> None:
    result = NaturalLanguageParser(now=NOW).interpret("move these to Berlin")

    assert result.invocation is not None
    assert result.invocation.command_id == "mail.move"
    assert result.entities.person is None
    assert result.entities.folder is not None
    assert result.entities.folder.display == "Berlin"
    assert "Person: Berlin" not in result.preview


def test_response_state_and_attachment_terms_never_leak_into_person_slots() -> None:
    parser = NaturalLanguageParser(now=NOW)

    for text in (
        "conversations where I sent the last message",
        "messages I sent with no response",
        "emails nobody replied to",
        "messages with word documents",
        "emails with exact phrase official transcript",
    ):
        result = parser.interpret(text)
        assert result.entities.person is None, text
        assert result.invocation is not None
        assert "sender" not in result.invocation.as_dict(), text
        assert "recipient" not in result.invocation.as_dict(), text


def test_search_relations_fill_only_the_correct_person_slot() -> None:
    parser = NaturalLanguageParser(now=NOW)

    sender = parser.interpret("mail from Ahmed with attachments")
    recipient = parser.interpret("emails between me and Ahmed")
    copied = parser.interpret("find messages where Ahmed is cc'd")

    assert sender.invocation is not None
    assert set(sender.invocation.as_dict()) & {"sender", "recipient"} == {"sender"}
    assert recipient.invocation is not None
    assert set(recipient.invocation.as_dict()) & {"sender", "recipient"} == {"recipient"}
    assert copied.invocation is not None
    assert set(copied.invocation.as_dict()) & {"sender", "recipient"} == {"recipient"}


def test_sender_monitoring_variants_extract_a_real_sender_filter() -> None:
    parser = NaturalLanguageParser(now=NOW)

    for text in (
        "watch Student Affairs",
        "notify me only for Student Affairs",
        "notify me when Student Affairs emails",
    ):
        result = parser.interpret(text)
        assert result.invocation is not None
        assert result.invocation.command_id == "monitoring.start"
        sender = result.invocation.as_dict().get("sender")
        assert isinstance(sender, PersonEntity)
        assert sender.display == "Student Affairs"


def test_non_exact_interpretation_meets_the_local_latency_target() -> None:
    parser = NaturalLanguageParser(now=NOW)
    phrases = (
        "unread frm ahmd this wk please",
        "storag sent itms please",
        "messages not from Student Affairs this month",
        "meeting Ahmed tomorrow 2 to 3",
        "run guc mail when windows starts please",
    )
    started = perf_counter()
    results = tuple(parser.interpret(text) for _ in range(4) for text in phrases)
    elapsed = perf_counter() - started

    assert all(result.invocation is not None for result in results)
    assert elapsed / len(results) < 0.1


@pytest.mark.parametrize(
    ("text", "commands", "handoff"),
    [
        (
            "find unread emails from student affairs and mark them read",
            ("search.mail", "mail.mark_read"),
            "message_results",
        ),
        (
            "find emails larger than 5 MB and then back them up",
            ("search.mail", "backup.back_up_search_results"),
            "search_results",
        ),
        (
            "find all PDFs from Ahmed then save them to Downloads",
            ("attachments.search", "attachments.download_all_attachments"),
            "attachment_results",
        ),
    ],
)
def test_allowlisted_chains_are_bounded_preview_only_with_result_handoff(
    text: str, commands: tuple[str, str], handoff: str
) -> None:
    sequence = NaturalLanguageParser(now=NOW).interpret_sequence(text, maximum_steps=99)

    assert sequence.allowed
    assert sequence.complete
    assert sequence.confirmation_required
    assert sequence.handoffs == (handoff,)
    actual_commands = tuple(
        step.invocation.command_id for step in sequence.steps if step.invocation
    )
    assert actual_commands == commands
    assert len(sequence.steps) == 2
    assert "Preview" in sequence.explanation


def test_palette_exposes_the_stable_sequence_interpretation_route() -> None:
    text = "find unread mail from Ahmed and mark them read"

    direct = PaletteEngine(now=NOW).interpret_sequence(text)
    public = interpret_typed_palette_sequence(text, now=NOW, maximum_steps=99)

    assert public == direct
    assert public.allowed
    assert public.complete
    assert public.handoffs == ("message_results",)
    assert tuple(step.invocation.command_id for step in public.steps if step.invocation) == (
        "search.mail",
        "mail.mark_read",
    )
    assert public.confirmation_required

    capped = interpret_typed_palette_sequence(text, now=NOW, maximum_steps=1)
    assert not capped.allowed
    assert not capped.complete
    assert not capped.handoffs
    assert len(capped.steps) == 1


def test_arbitrary_or_destructive_chain_is_not_split_or_executed() -> None:
    sequence = NaturalLanguageParser(now=NOW).interpret_sequence(
        "delete everything then send every message"
    )

    assert not sequence.allowed
    assert not sequence.complete
    assert not sequence.handoffs
    assert len(sequence.steps) == 1


def test_unknown_text_has_explicit_non_mutating_search_preview() -> None:
    result = NaturalLanguageParser(now=NOW).interpret("HCTL RDS certificate")

    assert result.kind is InterpretationKind.SEARCH_FALLBACK
    assert result.invocation is not None
    assert result.invocation.command_id == "search.mail"
    assert result.preview[-1] == "Mutation: never"


def test_cleanup_language_never_infers_deletion() -> None:
    parser = NaturalLanguageParser(now=NOW)

    for text in ("clean inbox", "remove old mail", "delete all mail from somebody maybe"):
        result = parser.interpret(text)
        assert result.invocation is not None
        assert result.invocation.command_id == "cleanup.analyze"


def test_selected_invitation_context_resolves_short_responses_and_cancel_is_strict() -> None:
    parser = NaturalLanguageParser(now=NOW)
    context = {"selected_event": "event-1", "current_area": "calendar"}

    for text, command_id in (
        ("accept", "calendar.accept_invitation"),
        ("tentative", "calendar.tentatively_accept"),
        ("decline", "calendar.decline_invitation"),
    ):
        result = parser.interpret(text, context=context)
        assert result.invocation is not None
        assert result.invocation.command_id == command_id
        assert result.candidates[0].command_id == command_id

    without_context = parser.interpret("accept")
    assert without_context.invocation is None

    cancellation = parser.interpret("cancel meeting", context=context)
    assert cancellation.invocation is not None
    assert cancellation.invocation.command_id == "calendar.cancel_meeting"
    assert cancellation.preview[-1] == "Confirmation: required before execution"


def test_every_quoted_packaged_help_nlp_example_has_a_regression_fixture() -> None:
    root = Path(__file__).parents[1] / "src" / "gucmail" / "help" / "pages"
    pages = (root / "palette" / "overview.md", root / "tutorials" / "palette.md")
    quote_pattern = (
        r"\N{LEFT DOUBLE QUOTATION MARK}"
        r"([^\N{RIGHT DOUBLE QUOTATION MARK}]+)"
        r"\N{RIGHT DOUBLE QUOTATION MARK}"
    )
    quoted = {
        phrase.rstrip(".")
        for page in pages
        for phrase in re.findall(quote_pattern, page.read_text(encoding="utf-8"))
    }
    fixtures = {
        example.text: (
            InterpretationKind(example.interpretation_kind),
            example.command_id,
        )
        for example in DOCUMENTED_NLP_EXAMPLES
    }

    assert quoted == set(fixtures)
    parser = NaturalLanguageParser(now=NOW)
    for phrase, (kind, command_id) in fixtures.items():
        result = parser.interpret(phrase)
        assert result.kind is kind, phrase
        assert (result.invocation.command_id if result.invocation else None) == command_id, phrase


@pytest.mark.parametrize(
    ("phrase", "command_id"),
    [
        ("open scheduled mail", "scheduled.open"),
        ("open backups", "backup.open"),
        ("open local search", "search.search_all_data"),
        ("resume draft", "draft.recover"),
        ("open draft manager", "drafts.open_drafts"),
        ("open drafts", "mail.open_drafts"),
    ],
)
def test_walkthrough_navigation_intents(phrase: str, command_id: str) -> None:
    result = NaturalLanguageParser().interpret(
        phrase, folder_candidates=("inbox", "Inbox", "drafts", "Drafts")
    )
    assert result.invocation is not None
    assert result.invocation.command_id == command_id
    if phrase == "open local search":
        assert not result.invocation.as_dict().get("query")


def test_walkthrough_search_fields_and_folder_aliases() -> None:
    parser = NaturalLanguageParser()
    subject = parser.interpret("search subject PTY")
    assert subject.invocation is not None
    assert subject.invocation.command_id == "search.mail"
    assert subject.invocation.as_dict() == {"subject": "PTY"}
    folder = parser.interpret("search deleted items for PTY")
    assert folder.invocation is not None
    assert folder.invocation.command_id == "search.mail"
    assert folder.invocation.as_dict()["query"] == "PTY"
    assert folder.invocation.as_dict()["folder"].canonical == "deleted"
    resolved = resolve_folder("open inbox", candidates=("Inbox", "inbox"))
    assert resolved is not None and not resolved.alternatives
