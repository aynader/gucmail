"""Cooperative archive reads stop without partial plans or restore dispatch."""

import hashlib
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest

from gucmail.backup_format import canonical_json_bytes
from gucmail.errors import BackupIntegrityError, ExitCode
from gucmail.services import backup as module
from gucmail.services.backup import (
    BackupItem,
    BackupReadCancelledError,
    BackupService,
    OfflineArchive,
)
from gucmail.services.restore_journal import RestoreJournal
from gucmail.state import StateStore
from gucmail.tui.area_backends import BackupAreaBackend
from gucmail.tui.area_common import PublicOperationError


def prepared(tmp_path, *, count=24, encrypted=False):
    state = StateStore(tmp_path / "state.sqlite", account_key="synthetic-read-cancel")
    service = BackupService(tmp_path / "backups", state=state)
    key = "synthetic-archive-key" if encrypted else None
    archive = service.create((
        BackupItem(f"synthetic-{i}", ("message", "contact", "task", "draft")[i % 4],
                   {"subject": f"دعوة [bold]literal[/bold] {i}", "body_text": "Owned local text"},
                   folder_path=f"Synthetic/Course-{i % 5}")
        for i in range(count)
    ), backup_id="synthetic-read-cancel", passphrase=key)
    return state, service, archive.backup_id, key


def hashes(service):
    return {p.relative_to(service.root): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in service.root.rglob("*") if p.is_file()}


def assert_no_restore(state):
    assert not RestoreJournal(state).operations()


@pytest.mark.parametrize("encrypted", [False, True])
def test_uncancelled_plan_keeps_exact_canonical_digests_and_entries(tmp_path, encrypted):
    state, service, identifier, key = prepared(tmp_path, encrypted=encrypted)
    before = hashes(service)
    original = service.plan_restore(identifier, passphrase=key)
    checked = service.plan_restore(identifier, passphrase=key, cancelled=Event())
    assert asdict(checked) == asdict(original)
    archive = service.open_offline(identifier, passphrase=key)
    assert checked.manifest_digest == hashlib.sha256(
        canonical_json_bytes(archive.manifest.to_dict())
    ).hexdigest()
    assert checked.chain_digest == hashlib.sha256(canonical_json_bytes(
        [manifest.to_dict() for _, manifest, _ in archive._chain()]
    )).hexdigest()
    core = {
        "backup_id": checked.backup_id, "manifest_digest": checked.manifest_digest,
        "chain_digest": checked.chain_digest, "destination_root": checked.destination_root,
        "entries": [asdict(entry) for entry in checked.entries],
        "allow_completed_duplicates": checked.allow_completed_duplicates,
    }
    assert checked.confirmation_token == hashlib.sha256(canonical_json_bytes(core)).hexdigest()[:16]
    assert checked.create_count == 24
    assert before == hashes(service)
    assert_no_restore(state)


def test_pre_cancelled_service_does_not_open_archive(tmp_path, monkeypatch):
    state, service, identifier, _ = prepared(tmp_path)
    cancel = Event()
    cancel.set()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pre-cancelled planning opened its archive")

    monkeypatch.setattr(service, "_open_manifest", forbidden)
    with pytest.raises(BackupReadCancelledError) as caught:
        service.plan_restore(identifier, cancelled=cancel)
    assert caught.value.exit_code == ExitCode.INTERRUPTED
    assert caught.value.context.retry_safe and not caught.value.context.mutation_uncertain
    assert_no_restore(state)


@pytest.mark.parametrize("encrypted", [False, True])
def test_backend_cancel_after_third_real_decode_returns_no_plan(tmp_path, monkeypatch, encrypted):
    state, service, identifier, key = prepared(tmp_path, count=1000, encrypted=encrypted)
    before = hashes(service)
    cancel = Event()
    decoded = []
    original = OfflineArchive._decode_item

    def decode(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        decoded.append(result.reference)
        if len(decoded) == 3:
            cancel.set()
        return result

    def forbidden():
        raise AssertionError("Read cancellation initialized its restore provider")

    monkeypatch.setattr(OfflineArchive, "_decode_item", decode)
    backend = BackupAreaBackend(service.root, state=state, source_factory=forbidden)
    with pytest.raises(PublicOperationError, match="cancelled safely"):
        backend.create_restore_plan(identifier, (), passphrase=key, cancel=cancel,
                                    progress=lambda _: None)
    assert len(decoded) == 3
    assert before == hashes(service)
    assert_no_restore(state)
    cancel.clear()
    monkeypatch.setattr(OfflineArchive, "_decode_item", original)
    fresh = BackupService(service.root, StateStore(state.path, account_key=state.account_key))
    assert fresh.plan_restore(identifier, passphrase=key).create_count == 1000


def test_file_hash_cancellation_stops_after_one_bounded_read_and_closes_handle(
    tmp_path, monkeypatch,
):
    path = tmp_path / "owned-large-object"
    path.write_bytes(b"local-only" * 600_000)
    cancel, reads, opened = Event(), [], []
    original = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *values):
            return self.handle.__exit__(*values)

        def read(self, size):
            result = self.handle.read(size)
            reads.append(len(result))
            cancel.set()
            return result

    def open_owned(self, *args, **kwargs):
        handle = original(self, *args, **kwargs)
        if self == path:
            opened.append(handle)
            return Reader(handle)
        return handle

    monkeypatch.setattr(Path, "open", open_owned)
    with pytest.raises(BackupReadCancelledError):
        BackupService._sha256_path(path, cancelled=cancel)
    assert reads == [1024 * 1024]
    assert opened[0].closed
    # A cancelled read neither rewrites nor removes its input.
    with original(path, "rb") as source:
        assert hashlib.sha256(source.read()).hexdigest() == hashlib.sha256(
            b"local-only" * 600_000
        ).hexdigest()


@pytest.mark.parametrize("phase", ["chain", "manifest", "confirmation"])
def test_cancel_during_real_digest_cannot_return_a_confirmation(tmp_path, monkeypatch, phase):
    state, service, identifier, _ = prepared(tmp_path)
    before = hashes(service)
    cancel, triggered = Event(), []
    original = module.canonical_json_bytes

    def serialize(value):
        result = original(value)
        matches = (
            (phase == "chain" and isinstance(value, list))
            or (phase == "manifest" and isinstance(value, dict) and "objects" in value)
            or (phase == "confirmation" and isinstance(value, dict) and "entries" in value)
        )
        if matches:
            triggered.append(True)
            cancel.set()
        return result

    monkeypatch.setattr(module, "canonical_json_bytes", serialize)
    with pytest.raises(BackupReadCancelledError):
        service.plan_restore(identifier, cancelled=cancel)
    assert triggered and before == hashes(service)
    assert_no_restore(state)


def test_verification_cancel_is_not_reported_as_corruption(tmp_path, monkeypatch):
    _state, service, identifier, _ = prepared(tmp_path)
    cancel = Event()
    original = BackupService._sha256_path
    calls = []

    def hash_then_cancel(path, **kwargs):
        result = original(path, **kwargs)
        calls.append(path)
        cancel.set()
        return result

    monkeypatch.setattr(BackupService, "_sha256_path", staticmethod(hash_then_cancel))
    with pytest.raises(BackupReadCancelledError):
        service.verify(identifier, cancelled=cancel)
    assert len(calls) == 1
    monkeypatch.setattr(BackupService, "_sha256_path", staticmethod(original))
    assert service.verify(identifier).valid
    victim = calls[0]
    victim.write_bytes(victim.read_bytes() + b"owned damage")
    report = service.verify(identifier)
    assert not report.valid and report.errors
    with pytest.raises(BackupIntegrityError):
        service.plan_restore(identifier, cancelled=Event())


def test_cancel_while_loading_parent_does_not_decode_later_items(tmp_path, monkeypatch):
    state, service, parent, _ = prepared(tmp_path)
    child = service.incremental((
        BackupItem("new-item", "message", {"subject": "Synthetic incremental record"}),
    ), parent, sync_state={}, backup_id="synthetic-child")
    cancel, reads = Event(), []
    original = service._read_manifest_auto

    def read_manifest(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.parent.name == parent:
            reads.append(path)
            cancel.set()
        return result

    monkeypatch.setattr(service, "_read_manifest_auto", read_manifest)
    with pytest.raises(BackupReadCancelledError):
        service.plan_restore(child.backup_id, cancelled=cancel)
    assert len(reads) == 1
    assert_no_restore(state)


def test_metadata_merge_observes_ongoing_cancellation(tmp_path, monkeypatch):
    _state, service, identifier, _ = prepared(tmp_path, count=100)
    active, checks = [], []
    original = OfflineArchive._merged_descriptors

    def merging(self):
        active.append(True)
        try:
            return original(self)
        finally:
            active.clear()

    def cancel():
        if active:
            checks.append(True)
        return len(checks) >= 5

    monkeypatch.setattr(OfflineArchive, "_merged_descriptors", merging)
    with pytest.raises(BackupReadCancelledError):
        service.plan_restore(identifier, cancelled=cancel)
    assert len(checks) == 5


def test_view_conversion_late_cancel_cannot_return_an_actionable_plan(tmp_path, monkeypatch):
    from gucmail.tui import area_backends

    _state, service, identifier, _ = prepared(tmp_path)
    plan = service.plan_restore(identifier)
    cancel, calls = Event(), []
    original = area_backends.RestorePlanEntryView

    def entry(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 3:
            cancel.set()
        return result

    monkeypatch.setattr(area_backends, "RestorePlanEntryView", entry)
    with pytest.raises(PublicOperationError, match="cancelled safely"):
        BackupAreaBackend._restore_plan(plan, encrypted=False, cancel=cancel)
    assert len(calls) == 3


def test_cancellation_during_entry_serialization_returns_no_confirmation(tmp_path, monkeypatch):
    state, service, identifier, _ = prepared(tmp_path)
    cancel, serialized = Event(), []
    original = module.asdict

    def serialize(value):
        result = original(value)
        if isinstance(value, module.RestorePlanEntry):
            serialized.append(value.item_key)
            if len(serialized) == 3:
                cancel.set()
        return result

    monkeypatch.setattr(module, "asdict", serialize)
    with pytest.raises(BackupReadCancelledError):
        service.plan_restore(identifier, cancelled=cancel)
    assert len(serialized) == 3
    assert_no_restore(state)


def test_cancel_during_statistics_stops_before_remaining_descriptors(tmp_path):
    _state, service, identifier, _ = prepared(tmp_path)
    manifest = service.open_offline(identifier).manifest
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) == 3

    with pytest.raises(BackupReadCancelledError):
        service._statistics(manifest.objects, manifest.items, cancelled=cancelled)
    assert len(checks) == 3 and len(manifest.objects) > 3


def test_cli_translates_actual_cancelled_planning_to_exit_130_without_a_plan(
    tmp_path, monkeypatch,
):
    import json

    from typer.testing import CliRunner

    import gucmail.cli as cli

    state, service, identifier, _ = prepared(tmp_path)
    before = hashes(service)
    cancel, decoded = Event(), []
    plan_restore, decode = service.plan_restore, OfflineArchive._decode_item

    def plan(*args, **kwargs):
        return plan_restore(*args, **kwargs, cancelled=cancel)

    def item(self, *args, **kwargs):
        result = decode(self, *args, **kwargs)
        decoded.append(result.reference)
        if len(decoded) == 3:
            cancel.set()
        return result

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Cancelled CLI planning requested a provider")

    for name in ("CONFIG", "DATA", "CACHE", "LOG"):
        monkeypatch.setenv(f"GUCMAIL_{name}_DIR", str(tmp_path / name))
    monkeypatch.setattr(service, "plan_restore", plan)
    monkeypatch.setattr(OfflineArchive, "_decode_item", item)
    monkeypatch.setattr(cli.backup, "_backup_service", lambda _: service)
    monkeypatch.setattr(cli.core, "gateway", forbidden)
    result = CliRunner().invoke(cli.app, ["--json", "backup", "restore-plan", identifier])
    assert result.exit_code == 130, result.output
    payload = json.loads(result.stdout)["data"]
    assert payload["error"] == "BackupReadCancelledError"
    assert payload["retry_safe"] is True and payload["mutation_uncertain"] is False
    assert "confirmation_token" not in payload and "entries" not in payload
    assert len(decoded) == 3 and before == hashes(service)
    assert_no_restore(state)
