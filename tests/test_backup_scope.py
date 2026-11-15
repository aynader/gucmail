"""Archive chains preserve the selected scope and reject unsafe continuations."""

from __future__ import annotations

import json
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.config import AppConfig
from gucmail.errors import BackupIntegrityError, ItemNotFoundError, NetworkError
from gucmail.ews.adapter import EWSGateway
from gucmail.services.backup import (
    BackupEstimate,
    BackupItem,
    BackupKind,
    BackupRequest,
    BackupSelection,
    BackupService,
)
from gucmail.tui.area_backends import BackupAreaBackend
from gucmail.tui.area_common import ConfirmationScreen, PublicOperationError
from gucmail.tui.backup_screen import BackupCreateRequest, BackupScreen
from waiting import rounds, wait_until


class Snapshot:
    def __init__(self, items=(), *, identity="synthetic-source-a", fail=False):
        self.items = items
        self.backup_source_identity = identity
        self.fail = fail
        self.requests = []
        self.estimates = []

    def iter_backup_items(self, request, completed_item_keys):
        self.requests.append(request)
        yield from self.items
        if self.fail:
            raise NetworkError("Synthetic interrupted snapshot")

    def estimate_backup(self, request):
        self.estimates.append(request)
        return BackupEstimate(len(self.items), 0, 100, 0, False)


def item(reference="one", subject="before"):
    return BackupItem(reference, "message", {"subject": subject}, folder_path="Localized/Inbox")


@pytest.mark.parametrize(
    "selection",
    [
        BackupSelection(folders=("fld_opaque",), item_kinds=("message",)),
        BackupSelection(messages=("one",), search="a resolved search"),
        BackupSelection(messages=("one",), cluster="synthetic-cluster"),
    ],
)
def test_incremental_inherits_exact_scope_and_exclusions(tmp_path, selection):
    service = BackupService(tmp_path)
    request = BackupRequest(selection=selection, exclusions=("excluded-*",))
    parent = service.create(Snapshot((item(),)), request, backup_id="parent")
    current = Snapshot((item(subject="after"),))
    child = service.incremental(current, parent.backup_id, sync_state={})
    assert current.requests[0].selection == selection
    assert current.requests[0].exclusions == request.exclusions
    assert child.manifest.selection == parent.manifest.selection
    assert service.open_offline(child.backup_id).get_item("one").data["subject"] == "after"
    assert child.manifest.sync_state["source_identity"] == "synthetic-source-a"


@pytest.mark.parametrize("change", ["folders", "messages", "item_kinds", "exclusions"])
def test_scope_change_is_rejected_before_snapshot_starts(tmp_path, change):
    service = BackupService(tmp_path)
    selection = BackupSelection(folders=("Inbox",))
    parent = service.create(Snapshot((item(),)), BackupRequest(selection=selection))
    source = Snapshot()
    kwargs = (
        {"exclusions": ("private-*",)}
        if change == "exclusions"
        else {"selection": replace(selection, **{change: ("different",)})}
    )
    with pytest.raises(BackupIntegrityError, match="scope differs"):
        service.incremental(source, parent.backup_id, sync_state={}, **kwargs)
    assert source.requests == []
    assert len(service.list(include_incomplete=True)) == 1


def test_older_mixed_scope_chain_remains_browsable_but_cannot_continue(tmp_path):
    service = BackupService(tmp_path)
    parent = service.create([item()], selection=BackupSelection(messages=("one",)))
    # Reproduce a legacy chain that expanded from selected messages to the mailbox.
    old = service.create([item("two")], parent_backup_id=parent.backup_id)
    assert len(tuple(service.open_offline(old.backup_id).iter_items())) == 2
    with pytest.raises(BackupIntegrityError, match="older archive chain changed scope"):
        service.incremental(Snapshot(), old.backup_id, sync_state={})


def test_account_mismatch_blocks_incremental_and_interrupted_resume(tmp_path):
    service = BackupService(tmp_path)
    parent = service.create(Snapshot((item(), item("missing"))))
    other = Snapshot(identity="synthetic-source-b")
    with pytest.raises(BackupIntegrityError, match="different mailbox"):
        service.incremental(other, parent.backup_id, sync_state={})
    assert other.requests == []
    with pytest.raises(NetworkError, match="interrupted"):
        service.incremental(
            Snapshot((item(subject="changed"),), fail=True),
            parent.backup_id,
            sync_state={},
            backup_id="interrupted",
        )
    checkpoint = service.inspect("interrupted", allow_incomplete=True)
    assert not any(entry.deleted for entry in checkpoint.items)
    with pytest.raises(BackupIntegrityError, match="different mailbox"):
        service.resume("interrupted", other)
    assert other.requests == []
    result = service.resume("interrupted", Snapshot((item(subject="changed"),)))
    assert result.manifest.statistics["deleted_item_count"] == 1
    assert {value.reference for value in service.open_offline(result.backup_id).iter_items()} == {
        "one"
    }


def test_empty_selected_backup_cannot_expand_to_the_mailbox():
    for kind in (BackupKind.SELECTED_MESSAGES, BackupKind.SEARCH_RESULTS, BackupKind.CLUSTER):
        with pytest.raises(ValueError, match="resolved message references"):
            BackupRequest(kind=kind)
    with pytest.raises(ValueError, match="Resolve search or cluster"):
        BackupRequest(selection=BackupSelection(search="unresolved"))


def test_source_identity_uses_actual_mailbox_and_contains_no_address():
    account = SimpleNamespace(primary_smtp_address="Actual@example.test")
    gateway = EWSGateway(AppConfig(email="different@example.test"), account_factory=lambda: account)
    first = gateway.backup_source_identity
    assert first.startswith("ews:") and len(first) == 68 and "@" not in first
    account.primary_smtp_address = "actual@EXAMPLE.TEST"
    assert gateway.backup_source_identity == first
    account.primary_smtp_address = "another@example.test"
    assert gateway.backup_source_identity != first


@pytest.mark.parametrize("kind", ["message", "draft"])
def test_selected_deletion_requires_resolved_reference_and_missing_server_item(monkeypatch, kind):
    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())

    def missing(*args):
        raise ItemNotFoundError("Synthetic absent item")

    monkeypatch.setattr(gateway, "_resolve_item", missing)
    with pytest.raises(ItemNotFoundError):
        gateway._backup_message("msg_unknown", allow_missing=True)

    def resolve(reference, *allowed_kinds):
        assert kind in allowed_kinds
        return kind, object()

    monkeypatch.setattr(gateway, "_resolve_item", resolve)
    monkeypatch.setattr(gateway, "show_message", missing)
    assert gateway._backup_message("known", allow_missing=True) is None
    with pytest.raises(ItemNotFoundError):
        gateway._backup_message("known")
    # A disappearing attachment/MIME download cannot be interpreted as message deletion.
    monkeypatch.setattr(gateway, "show_message", lambda _: SimpleNamespace(attachments=()))
    monkeypatch.setattr(gateway, "raw_message", missing)
    with pytest.raises(ItemNotFoundError):
        gateway._backup_message("known", allow_missing=True)


def test_backend_uses_explicit_parent_and_encrypted_scope_for_estimate(tmp_path):
    service = BackupService(tmp_path)
    selection = BackupSelection(messages=("one",))
    source = Snapshot((item(),))
    phrase = "synthetic-passphrase-only"
    first = service.create(source, selection=selection, passphrase=phrase, backup_id="selected")
    service.create(source, backup_id="unrelated-newer")
    backend = BackupAreaBackend(tmp_path, source_factory=lambda: source)
    request = BackupCreateRequest(
        str(tmp_path), "incremental", encrypted=True, parent_backup_id=first.backup_id
    )
    context = {"cancel": Event(), "progress": lambda _: None}
    estimate = backend.estimate_backup(request, passphrase=phrase, **context)
    assert estimate.item_count == 1 and source.estimates[-1].selection == selection
    result = backend.create_backup(request, passphrase=phrase, **context)
    child = service.inspect(result.backup_id, passphrase=phrase)
    assert child.parent_backup_id == first.backup_id
    assert child.encrypted and child.selection == first.manifest.selection
    with pytest.raises(PublicOperationError, match="Choose a complete parent"):
        backend.create_backup(replace(request, parent_backup_id=None), passphrase=phrase, **context)


def test_cli_incremental_keeps_selected_parent_scope(tmp_path, monkeypatch):
    service = BackupService(tmp_path / "backups")
    selection = BackupSelection(messages=("one",))
    source = Snapshot((item(),))
    parent = service.create(source, selection=selection)
    monkeypatch.setattr(cli.backup, "_backup_service", lambda _: service)
    monkeypatch.setattr(cli.core, "gateway", lambda _: source)
    env = {
        f"GUCMAIL_{name}_DIR": str(tmp_path / name) for name in ("CONFIG", "DATA", "CACHE", "LOG")
    }
    result = CliRunner().invoke(
        cli.app, ["--json", "backup", "incremental", parent.backup_id], env=env
    )
    assert result.exit_code == 0, result.output
    assert source.requests[-1].selection == selection
    assert json.loads(result.stdout)["data"]["parent_backup_id"] == parent.backup_id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_tui_reviews_selected_parent_and_rejects_other_destination(tmp_path, size):
    service = BackupService(tmp_path)
    service.create(Snapshot((item(),)), backup_id="chosen-parent")
    backend = BackupAreaBackend(tmp_path, source_factory=Snapshot)
    screen = BackupScreen(backend, default_destination=str(tmp_path), default_kind="incremental")
    app = App()
    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        await wait_until(
            pilot,
            lambda: screen.selected_backup_id and not screen._operation_active,
            interval=0.02,
            pause_first=True,
        )
        request = screen._parse_request()
        assert request.parent_backup_id == "chosen-parent"
        assert screen.query_one("#backup-encrypt", Checkbox).disabled
        for button_id in ("backup-create", "backup-estimate"):
            button = screen.query_one(f"#{button_id}", Button)
            assert button.region.bottom <= screen.size.height
            assert button.region.y >= 0
        create = screen.query_one("#backup-create", Button)
        settled = 0
        previous_region = None
        for _ in rounds(250):
            await pilot.pause(0.02)
            region = create.region
            if (
                create.is_mounted
                and not create.disabled
                and region.width > 0
                and region.height > 0
                and screen.region.contains_region(region)
                and app.get_widget_at(region.x, region.y)[0] is create
            ):
                settled = settled + 1 if region == previous_region else 1
                previous_region = region
                if settled >= 2:
                    break
            else:
                settled = 0
        assert settled >= 2, "Create backup did not become a settled visible mouse target"
        assert await pilot.click("#backup-create")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "chosen-parent" in str(
            app.screen.query_one("#operational-confirm-detail", Static).render()
        )
        await pilot.press("n")
        screen.query_one("#backup-destination", Input).value = str(tmp_path / "another")
        with pytest.raises(PublicOperationError, match="parent's repository"):
            screen._parse_request()
