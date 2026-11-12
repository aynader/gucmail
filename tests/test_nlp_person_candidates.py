"""Mailbox candidate discovery must not change structural search meaning."""

from __future__ import annotations

import pytest

from gucmail.nlp import NaturalLanguageParser


@pytest.mark.parametrize("people", [(), ("Student Affairs", "Synthetic Ada")])
@pytest.mark.parametrize(
    "phrase",
    [
        "mail before June not in Deleted",
        "unread mail not in Deleted before June",
        "mail before June",
        "mail not in Deleted",
    ],
)
def test_search_filters_without_a_person_do_not_invent_a_sender(people, phrase):
    result = NaturalLanguageParser().interpret(phrase, person_candidates=people)
    assert result.invocation is not None
    assert result.invocation.command_id == "search.mail"
    assert result.invocation.get("sender") is None
    if "before June" in phrase:
        assert result.invocation.get("date_range") is not None
    if "not in Deleted" in phrase:
        assert any(item.field == "folder" for item in result.invocation.get("exclusions"))


@pytest.mark.parametrize(
    "phrase",
    [
        "mail from Synthetic Ada before June not in Deleted",
        "Synthetic Ada unread mail before June not in Deleted",
    ],
)
def test_person_is_preserved_with_date_and_folder_exclusion(phrase):
    result = NaturalLanguageParser().interpret(phrase)
    assert result.invocation is not None
    assert result.invocation.get("sender").display == "Synthetic Ada"
    assert result.invocation.get("date_range") is not None
    assert any(item.field == "folder" for item in result.invocation.get("exclusions"))


@pytest.mark.parametrize("people", [(), ("Student Affairs", "Synthetic Ada")])
@pytest.mark.parametrize(
    "phrase,minimum,maximum",
    [
        ("find emails larger than 5 MB", 5_000_000, None),
        ("find emails bigger than 5 MB", 5_000_000, None),
        ("find emails over 5 MB", 5_000_000, None),
        ("find emails smaller than 5 MB", None, 5_000_000),
        ("find emails under 5 MB", None, 5_000_000),
        ("find emails below 5 MB", None, 5_000_000),
    ],
)
def test_size_only_sequence_never_invents_sender_from_loaded_people(
    people, phrase, minimum, maximum
):
    value = NaturalLanguageParser().interpret_sequence(
        phrase + " and then back them up", person_candidates=people
    )
    assert value.complete and value.allowed and value.confirmation_required
    first = value.steps[0].invocation
    assert first is not None and first.command_id == "search.mail"
    assert first.get("sender") is None
    size = first.get("size") or first.get("size_range")
    assert size.minimum_bytes == minimum and size.maximum_bytes == maximum
    assert value.steps[1].invocation.command_id == "backup.back_up_search_results"


@pytest.mark.parametrize("people", [(), ("Student Affairs", "Synthetic Ada")])
@pytest.mark.parametrize("sender", ["student affairs", "ada@example.test"])
def test_explicit_sender_survives_size_sequence_with_or_without_local_people(people, sender):
    value = NaturalLanguageParser().interpret_sequence(
        f"find emails from {sender} larger than 5 MB and then back them up",
        person_candidates=people,
    )
    assert value.complete and value.allowed
    first = value.steps[0].invocation
    assert first is not None
    assert first.get("sender").display.casefold() == sender
    assert first.get("size").minimum_bytes == 5_000_000
