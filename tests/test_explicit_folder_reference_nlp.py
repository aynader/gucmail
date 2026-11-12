"""Explicit local folder identities must not be lost in fuzzy candidate resolution."""

import pytest

from gucmail.nlp import NaturalLanguageParser, resolve_folder
from gucmail.palette import universal_palette_registry


@pytest.mark.parametrize("prefix", ["Open Folder", "go to folder", "show folder", "open"])
@pytest.mark.parametrize("id_prefix", ["", "id:"])
def test_explicit_folder_reference_remains_exact_with_unrelated_display_candidates(
    prefix, id_prefix
):
    reference = "fld_abcdefghijklmnop"
    parser = NaturalLanguageParser(universal_palette_registry())
    result = parser.interpret(
        f"{prefix} {id_prefix}{reference}",
        folder_candidates=("/Mailbox/Posteingang", "/Mailbox/Archiv"),
    )
    assert result.invocation is not None
    assert result.invocation.command_id == "folders.open"
    folder = result.invocation.get("folder")
    assert folder is not None
    assert folder.canonical == reference
    assert not folder.alternatives


@pytest.mark.parametrize(
    "text",
    [
        "Open Folder ews:owned-remote-id",
        "Open Folder msg_abcdefghijklmnop",
        "Open Folder fld_abcdefghijklmnop-extra",
        "Open Folder fld_abcdefghijklmnop; delete folder",
        "Open Folder fld_abcdefghijklmnop and delete it",
    ],
)
def test_explicit_navigation_does_not_accept_raw_message_or_chained_identity_as_folder(text):
    folder = resolve_folder(text, candidates=("/Mailbox/Posteingang", "/Mailbox/Archiv"))
    assert folder is None or folder.canonical != "fld_abcdefghijklmnop"
