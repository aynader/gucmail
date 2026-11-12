from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gucmail.errors import ConfigurationError
from gucmail.models import DraftData
from gucmail.services.drafts import DraftService
from gucmail.services.editor import (
    EDITOR_FILE_PREAMBLE,
    ExternalDraftEditor,
    ExternalEditorError,
    build_editor_arguments,
    parse_editor_command,
)
from gucmail.state import StateStore


def _drafts(tmp_path: Path) -> DraftService:
    return DraftService(StateStore(tmp_path / "state.sqlite3"), tmp_path / "recovery")


def test_external_editor_uses_argv_without_shell_and_rehydrates_durable_draft(
    tmp_path: Path,
) -> None:
    drafts = _drafts(tmp_path)
    draft = drafts.create(DraftData(reference="dft-editor", body_text="Before"))
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(arguments: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        path = Path(arguments[-1])
        path.write_text(f"{EDITOR_FILE_PREAMBLE}After", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    result = ExternalDraftEditor(
        drafts,
        ("synthetic-editor", "--safe", "{file}"),
        runner=runner,
        temporary_directory=tmp_path / "editor-temp",
    ).edit(draft.reference)

    assert result.draft.body_text == "After"
    assert drafts.get(draft.reference).body_text == "After"
    assert calls and calls[0][1]["shell"] is False
    assert calls[0][0][:2] == ("synthetic-editor", "--safe")
    assert result.recovery_path.is_file()


def test_external_editor_failure_preserves_recovery_and_rejects_control_arguments(
    tmp_path: Path,
) -> None:
    drafts = _drafts(tmp_path)
    draft = drafts.create(DraftData(reference="dft-editor-fail", body_text="Recover me"))

    with pytest.raises(ExternalEditorError):
        ExternalDraftEditor(
            drafts,
            ("synthetic-editor",),
            runner=lambda _arguments, **_kwargs: SimpleNamespace(returncode=7),
            temporary_directory=tmp_path / "editor-temp",
        ).edit(draft.reference)

    assert list((tmp_path / "recovery").glob("*.json"))
    with pytest.raises(ConfigurationError):
        parse_editor_command(("editor", "bad\nargument"))
    assert build_editor_arguments(("editor", "{file}"), Path("draft.txt")) == (
        "editor",
        "draft.txt",
    )
