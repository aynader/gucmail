from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gucmail.errors import ConflictError, LocalStateError
from gucmail.models import Address, DraftData, DraftLocation
from gucmail.services.drafts import (
    AttachmentIntegrityError,
    DraftService,
    DraftSyncCoordinator,
    DraftSyncState,
    ServerDraftSnapshot,
    build_attachment_manifest,
    draft_content_hash,
    validate_attachment_manifest,
)
from gucmail.state import StateStore


@pytest.fixture
def drafts(tmp_path: Path) -> DraftService:
    return DraftService(StateStore(tmp_path / "state.sqlite3"), tmp_path / "recovery")


def test_draft_recovery_is_atomic_and_revisioned(drafts: DraftService) -> None:
    created = drafts.create(
        DraftData(
            reference="dft-one",
            to=[Address("recipient@example.edu")],
            subject="First subject",
            body_text="Body",
        )
    )

    recovery_path = drafts.recovery_path(created.reference)
    envelope = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert envelope["format_version"] == 1
    assert envelope["content_hash"] == draft_content_hash(created)
    assert not list(recovery_path.parent.glob("*.tmp"))

    updated = drafts.edit(
        created.reference,
        expected_revision=created.revision,
        subject="Second subject",
    )
    assert updated.revision == 2
    assert [item.revision for item in drafts.revisions(created.reference)] == [2, 1]
    assert drafts.read_recovery(created.reference).subject == "Second subject"


def test_stale_draft_save_detects_revision_conflict(drafts: DraftService) -> None:
    original = drafts.create(DraftData(reference="dft-conflict", subject="Original"))
    stale = drafts.get(original.reference)
    drafts.edit(original.reference, expected_revision=original.revision, subject="Fresh edit")

    stale.subject = "Stale edit"
    with pytest.raises(ConflictError, match="changed from revision"):
        drafts.save(stale)

    assert drafts.get(original.reference).subject == "Fresh edit"


def test_attachment_manifest_rejects_changed_content(tmp_path: Path) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("version one", encoding="utf-8")
    manifest = build_attachment_manifest([attachment])
    validate_attachment_manifest(manifest)

    attachment.write_text("version two", encoding="utf-8")
    with pytest.raises(AttachmentIntegrityError, match="content changed"):
        validate_attachment_manifest(manifest)


def test_recovery_hash_detects_tampering(drafts: DraftService) -> None:
    draft = drafts.create(DraftData(reference="dft-tamper", body_text="safe"))
    path = drafts.recovery_path(draft.reference)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["draft"]["body_text"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(LocalStateError, match="content hash"):
        drafts.read_recovery(draft.reference)


class _ServerDraftGateway:
    def __init__(self) -> None:
        self.current: DraftData | None = None
        self.saves = 0

    def save_server_draft(self, draft: DraftData) -> DraftData:
        self.saves += 1
        server_id = draft.server_id or "server-draft-1"
        self.current = replace(
            draft,
            location=DraftLocation.SERVER,
            server_id=server_id,
            server_change_key=f"change-{self.saves}",
            updated_at=datetime(2026, 8, 13, 10, self.saves, tzinfo=UTC),
        )
        return self.current

    def get_server_draft(self, server_id: str) -> DraftData:
        assert self.current is not None
        assert self.current.server_id == server_id
        return self.current


def test_draft_sync_creates_then_updates_one_stable_server_draft(drafts: DraftService) -> None:
    gateway = _ServerDraftGateway()
    coordinator = DraftSyncCoordinator(drafts, gateway)
    local = drafts.create(DraftData(reference="stable-local", subject="First"))

    first = coordinator.sync_to_server(local.reference)
    edited = replace(first, subject="Second")
    coordinator.save_local(edited, expected_revision=first.revision)
    second = coordinator.sync_to_server(local.reference)

    assert first.reference == second.reference == "stable-local"
    assert first.server_id == second.server_id == "server-draft-1"
    assert first.server_change_key == "change-1"
    assert second.server_change_key == "change-2"
    assert gateway.saves == 2
    assert coordinator.metadata(local.reference).state is DraftSyncState.SYNCHRONIZED


def test_draft_sync_records_and_resolves_three_way_conflict(drafts: DraftService) -> None:
    gateway = _ServerDraftGateway()
    coordinator = DraftSyncCoordinator(drafts, gateway)
    local = drafts.create(DraftData(reference="conflicted-local", subject="Base"))
    synchronized = coordinator.sync_to_server(local.reference)
    coordinator.save_local(
        replace(synchronized, subject="Local edit"),
        expected_revision=synchronized.revision,
    )
    remote = replace(
        synchronized,
        subject="Server edit",
        server_change_key="change-remote",
        updated_at=datetime(2026, 8, 13, 11, 0, tzinfo=UTC),
    )

    metadata = coordinator.observe_server(
        local.reference,
        ServerDraftSnapshot(
            draft=remote,
            server_id="server-draft-1",
            server_change_key="change-remote",
            modified_at=remote.updated_at,
            client_reference=local.reference,
        ),
    )
    resolved = coordinator.resolve_conflict(local.reference, use="server")

    assert metadata.state is DraftSyncState.CONFLICTED
    assert metadata.remote_draft is not None
    assert resolved.reference == local.reference
    assert resolved.subject == "Server edit"
    assert resolved.location is DraftLocation.SYNCHRONIZED
    assert coordinator.metadata(local.reference).state is DraftSyncState.SYNCHRONIZED
