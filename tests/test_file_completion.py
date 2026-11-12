from __future__ import annotations

from pathlib import Path

import pytest

from gucmail import file_completion
from gucmail.errors import ConflictError


def _no_links(_source: Path, _target: Path) -> None:
    raise OSError("synthetic hard-link unavailability")


def test_fallback_writes_exclusive_handle_without_replacing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary, target = tmp_path / "owned.tmp", tmp_path / "finished.txt"
    temporary.write_bytes(b"complete content")
    monkeypatch.setattr(file_completion.os, "link", _no_links)

    def no_replace(_source: Path, _target: Path) -> None:
        pytest.fail("Exclusive completion must never replace a closed reservation.")

    monkeypatch.setattr(file_completion.os, "replace", no_replace)
    file_completion.complete_temporary_file(temporary, target, overwrite=False)
    assert target.read_bytes() == b"complete content"
    assert not temporary.exists()


def test_fallback_preserves_destination_that_appears_before_exclusive_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary, target = tmp_path / "owned.tmp", tmp_path / "finished.txt"
    temporary.write_bytes(b"complete content")

    def racing_link(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"other writer")
        raise OSError("synthetic hard-link unavailability")

    monkeypatch.setattr(file_completion.os, "link", racing_link)
    with pytest.raises(ConflictError, match="already exists"):
        file_completion.complete_temporary_file(temporary, target, overwrite=False)
    assert target.read_bytes() == b"other writer"
    assert temporary.read_bytes() == b"complete content"


def test_fallback_reports_changed_target_and_preserves_new_file_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary, target = tmp_path / "owned.tmp", tmp_path / "finished.txt"
    temporary.write_bytes(b"complete content")
    monkeypatch.setattr(file_completion.os, "link", _no_links)
    same_file = file_completion._same_file
    pending = True

    def changed_file(path: Path, identity) -> bool:
        nonlocal pending
        if pending:
            pending = False
            path.rename(tmp_path / "original-completion.txt")
            path.write_bytes(b"other writer")
        return same_file(path, identity)

    monkeypatch.setattr(file_completion, "_same_file", changed_file)
    with pytest.raises(ConflictError, match="new file was preserved"):
        file_completion.complete_temporary_file(temporary, target, overwrite=False)
    assert target.read_bytes() == b"other writer"
    assert temporary.read_bytes() == b"complete content"


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_exclusive_copy_cleans_only_owned_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    temporary, target = tmp_path / "owned.tmp", tmp_path / "finished.txt"
    temporary.write_bytes(b"complete content")
    monkeypatch.setattr(file_completion.os, "link", _no_links)

    def fail_copy(source, destination, *, length: int) -> None:
        assert length == 256 * 1024
        destination.write(source.read(3))
        raise failure("synthetic copy failure")

    monkeypatch.setattr(file_completion.shutil, "copyfileobj", fail_copy)
    with pytest.raises(failure, match="synthetic copy failure"):
        file_completion.complete_temporary_file(temporary, target, overwrite=False)
    assert not target.exists()
    assert temporary.read_bytes() == b"complete content"
