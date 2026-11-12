from __future__ import annotations

import errno
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from gucmail import config as configuration
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.errors import ConfigurationError


def _store(root: Path) -> ConfigStore:
    return ConfigStore(
        AppPaths(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            config_file=root / "config" / "config.toml",
            state_database=root / "data" / "state.sqlite3",
            recovery_dir=root / "data" / "recovery",
            index_database=root / "data" / "index.sqlite3",
            backup_dir=root / "data" / "backups",
        )
    )


def _windows_denial(code: int) -> OSError:
    error = OSError(errno.EACCES, "synthetic sharing failure")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_atomic_replace_retries_transient_windows_sharing_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    actual_replace = configuration.os.replace
    attempts: list[str] = []
    delays: list[float] = []

    def sharing_replace(source: str, destination: Path) -> None:
        attempts.append(source)
        assert target.read_bytes() == b"original"
        assert Path(source).read_bytes() == b"replacement"
        if len(attempts) <= 2:
            raise _windows_denial(code)
        actual_replace(source, destination)

    monkeypatch.setattr(configuration.os, "replace", sharing_replace)
    monkeypatch.setattr(configuration.time_module, "sleep", delays.append)
    configuration._atomic_write_bytes(target, b"replacement")

    assert target.read_bytes() == b"replacement"
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert delays == [0.02, 0.05]
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


@pytest.mark.parametrize("code", [5, 32, 33])
def test_atomic_replace_stops_at_bound_and_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    attempts: list[str] = []
    delays: list[float] = []

    def denied_replace(source: str, destination: Path) -> None:
        attempts.append(source)
        assert destination == target
        raise _windows_denial(code)

    monkeypatch.setattr(configuration.os, "replace", denied_replace)
    monkeypatch.setattr(configuration.time_module, "sleep", delays.append)
    with pytest.raises(OSError, match="synthetic sharing failure"):
        configuration._atomic_write_bytes(target, b"replacement")

    assert len(attempts) == 6 and len(set(attempts)) == 1
    assert delays == [0.02, 0.05, 0.1, 0.2, 0.4]
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


def test_atomic_replace_does_not_retry_ordinary_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    attempts: list[str] = []

    def denied_replace(source: str, _destination: Path) -> None:
        attempts.append(source)
        raise PermissionError("synthetic ordinary denial")

    def no_retry(_seconds: float) -> None:
        pytest.fail("An ordinary permission error must not be retried.")

    monkeypatch.setattr(configuration.os, "replace", denied_replace)
    monkeypatch.setattr(configuration.time_module, "sleep", no_retry)
    with pytest.raises(PermissionError, match="ordinary denial"):
        configuration._atomic_write_bytes(target, b"replacement")
    assert len(attempts) == 1
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


def test_interrupt_during_replace_retry_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")

    def denied_replace(_source: str, _destination: Path) -> None:
        raise _windows_denial(32)

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(configuration.os, "replace", denied_replace)
    monkeypatch.setattr(configuration.time_module, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        configuration._atomic_write_bytes(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".config.toml.*.tmp"))


def test_loaded_revision_belongs_to_decoded_bytes_during_concurrent_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _store(tmp_path)
    other.save(AppConfig(page_size=17))
    original = other.paths.config_file.read_bytes()
    reader = _store(tmp_path)
    decode = configuration._decode_document
    pending = True

    def decode_while_writer_commits(content: bytes):
        nonlocal pending
        decoded = decode(content)
        if content == original and pending:
            pending = False
            other.save(replace(other.load(), page_size=34))
        return decoded

    monkeypatch.setattr(configuration, "_decode_document", decode_while_writer_commits)
    document = reader.load_document()
    newer = other.paths.config_file.read_bytes()
    assert document.stored_config.page_size == 17
    assert document.revision == hashlib.sha256(original).hexdigest()
    assert newer != original
    with pytest.raises(ConfigurationError, match="changed in another process"):
        reader.save(replace(document.stored_config, page_size=51))
    assert other.paths.config_file.read_bytes() == newer


def test_saved_revision_stays_with_own_commit_after_lock_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _store(tmp_path)
    first.save(AppConfig(page_size=17))
    lock = configuration._interprocess_file_lock
    committed: list[bytes] = []
    pending = True

    @contextmanager
    def lock_then_other_writer(path: Path, **kwargs: float) -> Iterator[None]:
        nonlocal pending
        with lock(path, **kwargs):
            yield
        if pending:
            pending = False
            committed.append(first.paths.config_file.read_bytes())
            other = _store(tmp_path)
            other.save(replace(other.load(), page_size=34))

    monkeypatch.setattr(configuration, "_interprocess_file_lock", lock_then_other_writer)
    first.save(AppConfig(page_size=23))
    newer = first.paths.config_file.read_bytes()
    assert first.revision == hashlib.sha256(committed[0]).hexdigest()
    with pytest.raises(ConfigurationError, match="changed in another process"):
        first.save(AppConfig(page_size=51))
    assert first.paths.config_file.read_bytes() == newer


@pytest.mark.parametrize("source", ["last-known-good", "defaults", "migration"])
def test_recovery_or_migration_cannot_overwrite_concurrent_valid_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    initial = _store(tmp_path)
    initial.save(AppConfig(page_size=17))
    if source == "migration":
        initial.paths.config_file.write_bytes(b"page_size = 17\n")
    else:
        initial.paths.config_file.write_bytes(b"[malformed\n")
        if source == "defaults":
            initial.last_known_good_path.unlink()
    repair = ConfigStore._repair_document
    pending = True
    newer: list[bytes] = []

    def interleaved_repair(store: ConfigStore, content: bytes, **kwargs: object) -> str | None:
        nonlocal pending
        if pending:
            pending = False
            other = _store(tmp_path)
            other.save(replace(other.load(), page_size=34))
            newer.append(other.paths.config_file.read_bytes())
        return repair(store, content, **kwargs)

    monkeypatch.setattr(ConfigStore, "_repair_document", interleaved_repair)
    with pytest.raises(ConfigurationError, match="changed in another process during recovery"):
        _store(tmp_path).load_document()
    assert initial.paths.config_file.read_bytes() == newer[0]
    assert _store(tmp_path).load().page_size == 34


def test_unreadable_primary_is_not_treated_as_missing_for_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(AppConfig(page_size=17))
    original = store.paths.config_file.read_bytes()
    read_bytes = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == store.paths.config_file:
            raise PermissionError("synthetic unreadable primary")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(PermissionError, match="unreadable primary"):
        store.save(AppConfig(page_size=34))
    recovered = _store(tmp_path)
    document = recovered.load_document()
    assert document.stored_config.page_size == 17
    assert any("active in memory" in message for message in recovered.diagnostics.messages)
    assert read_bytes(store.paths.config_file) == original


@pytest.mark.parametrize("source", ["defaults", "migration"])
def test_primary_repair_revision_is_coherent_when_recovery_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    store = _store(tmp_path)
    store.paths.ensure()
    store.paths.config_file.write_bytes(
        b"page_size = 17\n" if source == "migration" else b"[malformed\n"
    )
    atomic_write = configuration._atomic_write_bytes

    def fail_recovery_copy(path: Path, content: bytes) -> None:
        if path == store.last_known_good_path:
            raise OSError("synthetic recovery copy failure")
        atomic_write(path, content)

    monkeypatch.setattr(configuration, "_atomic_write_bytes", fail_recovery_copy)
    document = store.load_document()
    assert document.revision == hashlib.sha256(store.paths.config_file.read_bytes()).hexdigest()
    assert any("recovery copy could not be updated" in m for m in store.diagnostics.messages)
    store.save(replace(document.stored_config, page_size=34))
    assert store.load().page_size == 34
