from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gucmail.errors import ConfigurationError
from gucmail.services.index import IndexDocument, SearchIndex


def test_full_text_index_supports_upsert_folder_filter_and_clear(tmp_path) -> None:
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(
        IndexDocument(
            reference="msg_alpha",
            subject="Synthetic enrollment reminder",
            sender="registrar@example.test",
            body="Please review the synthetic form before Friday.",
            attachment_names="form.pdf",
            folder_path="Inbox/University",
            received_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            size=1024,
        )
    )
    index.upsert(
        IndexDocument(
            reference="msg_beta",
            subject="Synthetic shopping receipt",
            sender="shop@example.test",
            body="A fictional purchase confirmation.",
            folder_path="Inbox/Receipts",
        )
    )

    hits = index.search("enrollment form", folder="University")
    assert [hit.reference for hit in hits] == ["msg_alpha"]
    assert "[" in hits[0].snippet
    assert index.stats().documents == 2
    records = index.records()
    assert [record.reference for record in records] == ["msg_alpha", "msg_beta"]
    assert records[0].body == "Please review the synthetic form before Friday."
    assert index.record("msg_alpha").attachment_names == "form.pdf"

    replacement = IndexDocument(
        reference="msg_alpha",
        subject="Updated synthetic reminder",
        body="The enrollment phrase was deliberately removed.",
        folder_path="Inbox/University",
    )
    index.upsert(replacement)
    assert index.search("shopping")[0].reference == "msg_beta"
    verification = index.verify()
    assert verification.valid
    assert verification.documents == verification.full_text_rows == 2
    assert index.optimize().documents == 2
    assert index.delete("msg_beta")
    assert not index.delete("msg_beta")
    assert index.clear() == 1
    assert index.stats().documents == 0


def test_fuzzy_search_is_bounded_and_fts_operators_are_quoted(tmp_path) -> None:
    index = SearchIndex(tmp_path / "index.sqlite3")
    index.upsert(
        IndexDocument(
            reference="msg_typo",
            subject="Synthetic accommodation request",
            body="Example content only.",
        )
    )

    hits = index.search("acomodation", fuzzy=True, fuzzy_threshold=65)
    assert hits and hits[0].reference == "msg_typo"
    # User input is tokenized and quoted instead of becoming an FTS operator expression.
    assert index.search('" OR NOT ( accommodation') == ()

    with pytest.raises(ConfigurationError):
        index.search("x", limit=501)
    with pytest.raises(ConfigurationError):
        index.search(" ")
    with pytest.raises(ConfigurationError, match="not present"):
        index.record("missing")
