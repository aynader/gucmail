from __future__ import annotations

import contextlib
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from gucmail.errors import ConfirmationRequiredError, ConflictError, UnsupportedFeatureError
from gucmail.services.backup import (
    BackupAttachment,
    BackupCancelledError,
    BackupIntegrityError,
    BackupItem,
    BackupRequest,
    BackupService,
    OfflineArchive,
    OfflineItem,
    PrunePlan,
    RestorePlan,
    RestoreResult,
)
from gucmail.tui.area_backends import BackupAreaBackend
from gucmail.tui.area_common import PublicOperationError


def _synthetic_items() -> tuple[BackupItem, ...]:
    return (
        BackupItem(
            reference="msg_synthetic_one",
            kind="message",
            folder_path="Inbox/Synthetic",
            data={
                "subject": "Synthetic course notice",
                "sender": "teacher@example.test",
                "body_text": "This is generated test content.",
            },
            attachments=(
                BackupAttachment(
                    "example.txt",
                    b"synthetic attachment data",
                    content_type="text/plain",
                ),
            ),
        ),
        BackupItem(
            reference="contact_synthetic_one",
            kind="contact",
            folder_path="Contacts",
            data={"display_name": "Example Person", "email": "person@example.test"},
        ),
    )


def test_backup_round_trip_verify_search_and_portable_export(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    result = service.create(_synthetic_items(), backup_id="full-synthetic")

    assert result.item_count == 2
    report = service.verify(result.backup_id)
    assert report.valid and report.complete

    archive = service.open_offline(result.backup_id)
    assert archive.offline and archive.read_only
    assert archive.get_item("msg_synthetic_one").folder_path == "Inbox/Synthetic"
    assert archive.search("course notice")[0].item.reference == "msg_synthetic_one"
    assert archive.folders()[0]["source"] == "backup"

    exported = service.export(result.backup_id, tmp_path / "portable.gucmail.zip")
    assert exported.is_file()
    with zipfile.ZipFile(exported) as bundle:
        assert "export.json" in bundle.namelist()


def test_backup_listing_pushes_down_a_bounded_result_limit(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    for number in range(3):
        service.create(
            _synthetic_items(),
            backup_id=f"bounded-synthetic-{number}",
        )

    assert len(service.list(limit=2, inspect_corrupt_sizes=False)) == 2
    with pytest.raises(ValueError, match="between 1 and 10000"):
        service.list(limit=0)


def test_encrypted_incremental_chain_and_recoverable_prune(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    passphrase = "synthetic-passphrase-only"
    full = service.create(
        _synthetic_items(),
        backup_id="full-encrypted",
        passphrase=passphrase,
    )
    assert service.verify(full.backup_id, passphrase=passphrase).valid

    incremental = service.incremental(
        (
            *_synthetic_items(),
            BackupItem(
                reference="msg_synthetic_two",
                kind="message",
                folder_path="Inbox/Synthetic",
                data={"subject": "Synthetic incremental item"},
            ),
        ),
        full.backup_id,
        sync_state={"strategy": "synthetic-fixture"},
        backup_id="incremental-encrypted",
        passphrase=passphrase,
    )
    archive = service.open_offline(incremental.backup_id, passphrase=passphrase)
    assert {item.reference for item in archive.iter_items()} == {
        "msg_synthetic_one",
        "contact_synthetic_one",
        "msg_synthetic_two",
    }

    planned = service.prune(keep_last=1)
    assert isinstance(planned, PrunePlan)
    # The parent is protected because the retained incremental backup depends on it.
    assert not planned.candidates


def test_prune_protects_interrupted_incremental_parent_and_resume(tmp_path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    service = BackupService(tmp_path / "backups", clock=lambda: now)
    service.create(_synthetic_items(), backup_id="parent")
    with pytest.raises(BackupCancelledError):
        service.incremental(
            _synthetic_items(),
            "parent",
            sync_state={},
            backup_id="unfinished",
            cancelled=lambda: True,
        )
    now += timedelta(days=1)
    service.create(_synthetic_items(), backup_id="newer")

    plan = service.prune(keep_last=1)
    assert "parent" in plan.protected_ancestors
    assert "parent" not in plan.candidates
    service.execute_prune(plan, confirmation_token=plan.confirmation_token)
    resumed = service.resume("unfinished", _synthetic_items())
    assert service.verify(resumed.backup_id).valid
    assert len(service.open_offline(resumed.backup_id).list_items()) == 2


def test_prune_rechecks_repository_after_writer_lock(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    service = BackupService(tmp_path / "backups", clock=lambda: now)
    service.create(_synthetic_items(), backup_id="parent")
    now += timedelta(days=1)
    service.create(_synthetic_items(), backup_id="newer")
    plan = service.prune(keep_last=1)
    assert plan.candidates == ("parent",)
    original_lock = service._writer_lock

    @contextlib.contextmanager
    def interleaved_lock(operation):
        if operation == "prune":
            with pytest.raises(BackupCancelledError):
                service.incremental(
                    _synthetic_items(),
                    "parent",
                    sync_state={},
                    backup_id="unfinished",
                    cancelled=lambda: True,
                )
        with original_lock(operation):
            yield

    monkeypatch.setattr(service, "_writer_lock", interleaved_lock)
    with pytest.raises(ConflictError, match="changed after the prune plan"):
        service.execute_prune(plan, confirmation_token=plan.confirmation_token)
    assert (service.root / "parent").is_dir()
    assert service.verify(service.resume("unfinished", _synthetic_items()).backup_id).valid


def test_prune_refuses_checkpoint_with_unknown_parent(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    service.create(_synthetic_items(), backup_id="base")
    (service.root / "interrupted.partial").mkdir()
    with pytest.raises(BackupIntegrityError, match="no readable manifest"):
        service.prune(keep_last=1)


def test_incremental_full_snapshot_emits_change_and_deletion_tombstones(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    parent = service.create(
        (
            BackupItem("unchanged", "message", {"subject": "same"}),
            BackupItem("changed", "message", {"subject": "before"}),
            BackupItem("removed", "message", {"subject": "gone"}),
        ),
        backup_id="parent-snapshot",
    )
    incremental = service.incremental(
        (
            BackupItem("unchanged", "message", {"subject": "same"}),
            BackupItem("changed", "message", {"subject": "after"}),
            BackupItem("added", "message", {"subject": "new"}),
        ),
        parent.backup_id,
        sync_state={"strategy": "caller-claimed-incremental"},
        backup_id="parent-diff",
    )

    assert incremental.manifest.sync_state["strategy"] == "full_snapshot_parent_diff"
    assert incremental.manifest.sync_state["native_change_tracking"] is False
    assert incremental.manifest.statistics["deleted_item_count"] == 1
    assert len(incremental.manifest.items) == 3  # changed, added, and removed tombstone
    archive = service.open_offline(incremental.backup_id)
    assert {item.reference for item in archive.iter_items()} == {
        "unchanged",
        "changed",
        "added",
    }
    assert archive.get_item("changed").data["subject"] == "after"


class _EstimateOnlySource:
    def __init__(self) -> None:
        self.content_enumerated = False

    def estimate_backup(self, request: BackupRequest) -> dict[str, int]:
        return {
            "item_count": 3,
            "attachment_count": 1,
            "estimated_bytes": 4096,
            "unknown_size_count": 1,
        }

    def iter_backup_items(self, request: BackupRequest, completed: frozenset[str]):
        self.content_enumerated = True
        raise AssertionError("estimate must not retrieve backup content")


class _UnsafeEstimateSource:
    def __init__(self) -> None:
        self.content_enumerated = False

    def iter_backup_items(self, request: BackupRequest, completed: frozenset[str]):
        self.content_enumerated = True
        raise AssertionError("estimate must not retrieve backup content")


def test_provider_estimate_is_metadata_only_and_never_falls_back_to_content(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    safe = _EstimateOnlySource()
    estimate = service.estimate(safe, encrypted=True)
    assert estimate.item_count == 3
    assert estimate.encrypted is True
    assert safe.content_enumerated is False

    unsafe = _UnsafeEstimateSource()
    with pytest.raises(UnsupportedFeatureError, match="metadata-only"):
        service.estimate(unsafe)
    assert unsafe.content_enumerated is False


class _SyntheticRestoreTarget:
    def __init__(self) -> None:
        self.items: list[tuple[OfflineItem, str]] = []

    def restore_item(self, item: OfflineItem, destination_folder: str) -> str:
        self.items.append((item, destination_folder))
        return f"restored-{item.reference}"


class _SyntheticBackupGateway(_SyntheticRestoreTarget):
    def iter_backup_items(
        self,
        request: BackupRequest,
        completed_item_keys: frozenset[str],
    ):
        del request, completed_item_keys
        return ()


def test_restore_requires_plan_token_and_hydrates_verified_attachments(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    service.create(_synthetic_items(), backup_id="restore-synthetic")
    plan = service.restore("restore-synthetic")
    assert isinstance(plan, RestorePlan)
    target = _SyntheticRestoreTarget()

    with pytest.raises(ConfirmationRequiredError):
        service.restore(
            "restore-synthetic",
            confirmed=True,
            target=target,
            confirmation_token="wrong-token",
        )

    result = service.restore(
        "restore-synthetic",
        confirmed=True,
        target=target,
        confirmation_token=plan.confirmation_token,
    )
    assert isinstance(result, RestoreResult)
    assert result.restored_count == 2
    restored_message = next(item for item, _folder in target.items if item.kind == "message")
    assert restored_message.attachments[0]["content"] == b"synthetic attachment data"


def test_incremental_restore_confirmation_binds_parent_content(tmp_path) -> None:
    service = BackupService(tmp_path / "backups")
    original = BackupItem("one", "message", {"subject": "original"})
    service.create([original], backup_id="parent")
    service.incremental([original], "parent", sync_state={}, backup_id="child")
    plan = service.plan_restore("child")

    # An independently valid replacement parent has the same archive ID and
    # item identity, but different content. The child manifest is unchanged.
    replacement = BackupService(tmp_path / "replacement")
    replacement.create([BackupItem("one", "message", {"subject": "changed"})], backup_id="parent")
    (tmp_path / "backups" / "parent").rename(tmp_path / "original-parent")
    (tmp_path / "replacement" / "parent").rename(tmp_path / "backups" / "parent")

    target = _SyntheticRestoreTarget()
    with pytest.raises(ConfirmationRequiredError):
        service.restore(
            "child", confirmed=True, target=target, confirmation_token=plan.confirmation_token
        )
    assert target.items == []


@pytest.mark.parametrize("swap_boundary", ["descriptors", "attachments"])
def test_restore_uses_confirmed_chain_during_parent_swap(
    tmp_path, monkeypatch, swap_boundary
) -> None:
    service = BackupService(tmp_path / "backups")
    original = BackupItem(
        "one",
        "message",
        {"subject": "original"},
        attachments=(BackupAttachment("proof.txt", b"original bytes"),),
    )
    changed = BackupItem(
        "one",
        "message",
        {"subject": "changed"},
        attachments=(BackupAttachment("proof.txt", b"changed bytes"),),
    )
    service.create([original], backup_id="parent")
    service.incremental([original], "parent", sync_state={}, backup_id="child")
    replacement = BackupService(tmp_path / "replacement")
    replacement.create([changed], backup_id="parent")
    # Retain all old immutable objects at their original names in the replacement
    # so a pinned descriptor can still verify and read exactly the reviewed data.
    shutil.copytree(
        tmp_path / "backups" / "parent" / "objects",
        tmp_path / "replacement" / "parent" / "objects",
        dirs_exist_ok=True,
    )
    plan = service.plan_restore("child")
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        assert not swapped
        (tmp_path / "backups" / "parent").rename(tmp_path / "old-parent")
        (tmp_path / "replacement" / "parent").rename(tmp_path / "backups" / "parent")
        swapped = True

    if swap_boundary == "descriptors":
        digest = OfflineArchive._chain_digest
        calls = 0

        def swap_after_execution_check(archive):
            nonlocal calls
            result = digest(archive)
            calls += 1
            if calls == 2:  # restore's fresh plan, then its execution check
                swap_parent()
            return result

        monkeypatch.setattr(OfflineArchive, "_chain_digest", swap_after_execution_check)
    else:
        hydrate = service._hydrate_restore_attachments

        def swap_before_hydration(archive, item):
            swap_parent()
            return hydrate(archive, item)

        monkeypatch.setattr(service, "_hydrate_restore_attachments", swap_before_hydration)

    target = _SyntheticRestoreTarget()
    result = service.restore(
        "child", confirmed=True, target=target, confirmation_token=plan.confirmation_token
    )
    assert swapped
    assert isinstance(result, RestoreResult)
    assert result.restored_count == 1
    restored = target.items[0][0]
    assert restored.data["subject"] == "original"
    assert restored.attachments[0]["content"] == b"original bytes"


def test_tui_backup_backend_executes_only_the_confirmed_preview(tmp_path) -> None:
    root = tmp_path / "backups"
    BackupService(root).create(_synthetic_items(), backup_id="tui-restore")
    gateway = _SyntheticBackupGateway()
    backend = BackupAreaBackend(root, source_factory=lambda: gateway)
    cancel = Event()

    def progress(_value) -> None:
        return None

    plan = backend.create_restore_plan(
        "tui-restore",
        (),
        cancel=cancel,
        progress=progress,
    )
    assert plan.create_count == 2
    assert plan.confirmation_token
    assert plan.confirmation_token not in repr(plan)

    with pytest.raises(PublicOperationError, match="explicit confirmation"):
        backend.execute_restore(
            plan,
            confirmed=False,
            cancel=cancel,
            progress=progress,
        )
    assert gateway.items == []

    result = backend.execute_restore(
        plan,
        confirmed=True,
        cancel=cancel,
        progress=progress,
    )
    assert result.restored_count == 2
    assert result.failed_count == 0
    assert result.server_mutations_performed is True
    assert len(gateway.items) == 2


def test_tui_backup_backend_accepts_an_ephemeral_encrypted_restore_passphrase(tmp_path) -> None:
    root = tmp_path / "backups"
    passphrase = "synthetic-one-operation-secret"
    BackupService(root).create(
        _synthetic_items(),
        backup_id="tui-encrypted-restore",
        passphrase=passphrase,
    )
    gateway = _SyntheticBackupGateway()
    backend = BackupAreaBackend(root, source_factory=lambda: gateway)
    cancel = Event()

    def progress(_value) -> None:
        return None

    with pytest.raises(PublicOperationError, match="secure passphrase prompt"):
        backend.create_restore_plan(
            "tui-encrypted-restore",
            (),
            cancel=cancel,
            progress=progress,
        )

    plan = backend.create_restore_plan(
        "tui-encrypted-restore",
        (),
        passphrase=passphrase,
        cancel=cancel,
        progress=progress,
    )
    assert plan.encrypted is True
    result = backend.execute_restore(
        plan,
        confirmed=True,
        passphrase=passphrase,
        cancel=cancel,
        progress=progress,
    )
    assert result.restored_count == 2


def test_tui_backup_backend_exports_prunes_and_opens_validated_repository(tmp_path) -> None:
    root = tmp_path / "backups"
    service = BackupService(root)
    for number in range(3):
        service.create(_synthetic_items(), backup_id=f"repository-{number}")
    opened = []
    backend = BackupAreaBackend(root, folder_opener=opened.append)
    cancel = Event()

    def progress(_value) -> None:
        return None

    destination = tmp_path / "portable" / "mailbox.zip"
    exported = backend.export_backup(
        "repository-2",
        str(destination),
        cancel=cancel,
        progress=progress,
    )
    assert exported == str(destination.resolve())
    assert destination.is_file()
    with pytest.raises(PublicOperationError, match="already exists"):
        backend.export_backup(
            "repository-2",
            str(destination),
            cancel=cancel,
            progress=progress,
        )

    plan = backend.plan_prune(1, cancel=cancel, progress=progress)
    assert len(plan.retained) == 1
    assert len(plan.candidates) == 2
    assert plan.confirmation_token not in repr(plan)
    with pytest.raises(PublicOperationError, match="explicit confirmation"):
        backend.execute_prune(
            plan,
            confirmed=False,
            cancel=cancel,
            progress=progress,
        )
    result = backend.execute_prune(
        plan,
        confirmed=True,
        cancel=cancel,
        progress=progress,
    )
    assert result.pruned_ids == plan.candidates
    assert result.bytes_moved > 0

    opened_path = backend.open_backup_folder(cancel=cancel, progress=progress)
    assert opened == [root.resolve()]
    assert opened_path == str(root.resolve())
