from __future__ import annotations

import socket
from time import perf_counter

import pytest

from gucmail.command_catalog import CORE_COMMANDS
from gucmail.nlp import (
    CorpusGroup,
    InterpretationKind,
    NaturalLanguageParser,
    generate_command_corpus,
    load_command_corpus,
    normalize_text,
)
from gucmail.palette import PaletteEngine


def test_generated_corpus_is_deterministic_complete_and_unambiguous() -> None:
    first = load_command_corpus()
    second = load_command_corpus()
    registry_ids = tuple(command.command_id for command in CORE_COMMANDS.all())

    assert first == second
    assert first.schema == "gucmail.command-corpus/v1"
    assert tuple(group.command_id for group in first.groups) == registry_ids
    assert all(group.phrases for group in first.groups)

    phrase_owners: dict[str, str] = {}
    for group in first.groups:
        for phrase in group.phrases:
            normalized = normalize_text(phrase).normalized
            assert normalized
            assert normalize_text(normalized).normalized == normalized
            assert phrase_owners.setdefault(normalized, group.command_id) == group.command_id


def test_every_corpus_phrase_round_trips_through_interpretation_and_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("natural-language interpretation must remain offline")

    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_network)
    corpus = load_command_corpus()
    parser = NaturalLanguageParser(corpus=corpus)
    palette = PaletteEngine()

    for group in corpus.groups:
        for phrase in group.phrases:
            result = parser.interpret(phrase)
            suggestions = palette.suggest(phrase, limit=1)

            command = CORE_COMMANDS.get(group.command_id)
            expected_kind = (
                InterpretationKind.INCOMPLETE
                if any(parameter.required for parameter in command.parameters)
                else InterpretationKind.COMMAND
            )
            assert result.kind is expected_kind, (group.command_id, phrase, result)
            if expected_kind is InterpretationKind.COMMAND:
                assert result.invocation is not None
                assert result.invocation.command_id == group.command_id
            else:
                assert result.invocation is None
                assert "missing required parameter" in result.explanation
            assert result.candidates[0].command_id == group.command_id
            assert suggestions
            assert suggestions[0].command.command_id == group.command_id


def test_exact_corpus_interpretation_is_bounded_and_skips_fuzzy_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = load_command_corpus()
    parser = NaturalLanguageParser(corpus=corpus)

    def unexpected_fuzzy_ranking(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exact corpus phrases must not enter fuzzy ranking")

    monkeypatch.setattr(parser, "_rank", unexpected_fuzzy_ranking)
    started = perf_counter()
    results = tuple(parser.interpret(phrase) for group in corpus.groups for phrase in group.phrases)
    elapsed = perf_counter() - started

    assert len(results) == corpus.phrase_count
    # This intentionally generous ceiling catches accidental corpus-wide fuzzy scans while
    # remaining stable on constrained CI workers. The structural assertion above is primary.
    assert elapsed < 5.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  Opn inbxo?!  ", "open inbox"),
        ("WHAT\N{RIGHT SINGLE QUOTATION MARK}S unread", "whats unread"),
        (
            "\uff22\uff2b\uff30 \uff23\uff21\uff2c \uff34\uff2f\uff2d \uff21\uff34\uff34\uff21",
            "backup calendar tomorrow attachment",
        ),
    ],
)
def test_normalization_is_canonical_and_idempotent(text: str, expected: str) -> None:
    normalized = normalize_text(text)

    assert normalized.normalized == expected
    assert normalize_text(normalized.normalized).normalized == expected


def test_corpus_generation_rejects_cross_command_normalization_collisions() -> None:
    first, second = CORE_COMMANDS.all()[:2]
    curated = (
        CorpusGroup(first.command_id, ("Same phrase",)),
        CorpusGroup(second.command_id, ("same   phrase!",)),
    )

    with pytest.raises(ValueError, match="duplicated"):
        generate_command_corpus(CORE_COMMANDS, curated_groups=curated)
