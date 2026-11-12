from __future__ import annotations

import json
from pathlib import Path

import pytest

from gucmail.tui.theme_repository import (
    CustomThemeRepository,
    ThemeRepositoryBusyError,
    ThemeRepositoryConflictError,
    ThemeRepositoryError,
)
from gucmail.tui.themes import ThemeDefinition, ThemeRegistry, export_theme


def _custom(
    name: str = "Repository Test",
    custom_id: str | None = None,
) -> ThemeDefinition:
    registry = ThemeRegistry()
    return registry.duplicate(
        "builtin.default-dark",
        name=name,
        custom_id=custom_id,
    )


def test_custom_theme_repository_crud_import_export_and_revisions(tmp_path: Path) -> None:
    path = tmp_path / "appearance" / "custom-themes.json"
    repository = CustomThemeRepository(path)
    assert repository.load().themes == ()
    assert repository.load().revision == 0

    created = repository.add(_custom(custom_id="custom.repository-test"))
    assert created.id == "custom.repository-test"
    assert repository.load().revision == 1

    edited = repository.edit(
        created.id,
        palette_overrides={"primary_accent": "#00ffaa"},
        style_overrides={"row_density": "compact"},
        expected_revision=1,
    )
    assert edited.palette.primary_accent == "#00ffaa"
    assert edited.style.row_density == "compact"
    renamed = repository.rename(edited.id, "Repository Renamed", expected_revision=2)
    assert renamed.name == "Repository Renamed"

    export_path = tmp_path / "shared" / "theme.json"
    assert repository.export_file(renamed.id, export_path) == export_path
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["id"] == renamed.id
    with pytest.raises(ThemeRepositoryError, match="already exists"):
        repository.export_file(renamed.id, export_path)

    removed = repository.delete(renamed.id, expected_revision=3)
    assert removed.id == renamed.id
    assert repository.load().themes == ()
    imported = repository.import_file(export_path, expected_revision=4)
    assert imported.name == "Repository Renamed"
    with pytest.raises(ThemeRepositoryError, match="already exists"):
        repository.import_file(export_path)

    reset = repository.reset(expected_revision=5)
    assert reset.revision == 6
    assert reset.themes == ()


def test_custom_theme_repository_recovers_backup_and_quarantines_primary(
    tmp_path: Path,
) -> None:
    repository = CustomThemeRepository(tmp_path / "custom-themes.json")
    first = _custom("First", "custom.first")
    second = _custom("Second", "custom.second")
    repository.save((first,))
    repository.save((first, second), expected_revision=1)
    assert repository.backup_path.exists()

    repository.path.write_bytes(b"{definitely not JSON")
    recovered = repository.load()
    assert recovered.recovered
    assert recovered.recovered_from == repository.backup_path
    assert recovered.revision == 1
    assert [theme.id for theme in recovered.themes] == ["custom.first"]
    assert len(recovered.quarantined) == 1
    assert recovered.quarantined[0].exists()
    assert b"definitely not JSON" in recovered.quarantined[0].read_bytes()

    # Recovery rewrites a fully validated primary copy and remains usable.
    loaded_again = repository.load()
    assert not loaded_again.recovered
    assert loaded_again.themes == recovered.themes


def test_custom_theme_repository_quarantines_both_bad_copies_without_data_loss(
    tmp_path: Path,
) -> None:
    repository = CustomThemeRepository(tmp_path / "custom-themes.json")
    repository.path.write_text("bad primary", encoding="utf-8")
    repository.backup_path.write_text("bad backup", encoding="utf-8")

    snapshot = repository.load()
    assert snapshot.themes == ()
    assert snapshot.revision == 0
    assert len(snapshot.quarantined) == 2
    assert all(path.exists() for path in snapshot.quarantined)
    assert not repository.path.exists()
    assert not repository.backup_path.exists()


def test_custom_theme_repository_rejects_stale_writes_and_lock_contention(
    tmp_path: Path,
) -> None:
    repository = CustomThemeRepository(
        tmp_path / "custom-themes.json",
        lock_timeout=0.05,
    )
    repository.save((_custom("First", "custom.first"),))
    with pytest.raises(ThemeRepositoryConflictError, match="another process"):
        repository.save((), expected_revision=0)

    repository.lock_path.write_text("synthetic-other-process", encoding="ascii")
    with pytest.raises(ThemeRepositoryBusyError, match="another process"):
        repository.save(())


def test_custom_theme_repository_bounds_and_rejects_executable_or_unknown_data(
    tmp_path: Path,
) -> None:
    repository = CustomThemeRepository(
        tmp_path / "custom-themes.json",
        max_bytes=16 * 1024,
        max_themes=1,
    )
    one = _custom("One", "custom.one")
    two = _custom("Two", "custom.two")
    with pytest.raises(ThemeRepositoryError, match="limited to 1"):
        repository.save((one, two))

    source = tmp_path / "not-a-theme.json"
    source.write_text('__import__("os").system("echo unsafe")', encoding="utf-8")
    with pytest.raises(ThemeRepositoryError, match="valid JSON"):
        repository.import_file(source)

    payload = json.loads(export_theme(one))
    payload["palette"]["unexpected"] = "field"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ThemeRepositoryError, match=r"Palette fields|fields"):
        repository.import_file(source)
