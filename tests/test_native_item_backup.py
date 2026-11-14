"""Synthetic protocol, archive, CLI and keyboard checks for native PIM backup streams."""

import base64
from types import SimpleNamespace

import pytest
from exchangelib import Task
from exchangelib.errors import ErrorAccessDenied
from exchangelib.folders import Folder
from exchangelib.services import ExportItems, UploadItems
from exchangelib.version import Build, Version

from gucmail.errors import (
    ConfigurationError,
    MutationResultUnavailableError,
    UnsupportedFeatureError,
)
from gucmail.ews import native_items as module


def account():
    version = Version(build=Build(15, 1, 2375, 7))
    return SimpleNamespace(version=version, protocol=SimpleNamespace(version=version))


@pytest.mark.parametrize("content", [b"opaque synthetic Task", b"\x00\xff\x10\x01"])
def test_native_export_preserves_bytes_and_exact_one_source(monkeypatch, content):
    owner = account()
    source = Task(id="synthetic-source", changekey="synthetic-key", size=20)
    calls = []

    def native_call(service, *, items):
        assert service.account is owner and service.chunk_size == 1
        payload = service.get_payload(items=items)
        ids = payload.xpath('//*[local-name()="ItemId"]')
        assert [(value.get("Id"), value.get("ChangeKey")) for value in ids] == [
            ("synthetic-source", "synthetic-key")
        ]
        calls.append(items)
        return iter(["\n " + base64.b64encode(content).decode() + "\n"])

    monkeypatch.setattr(ExportItems, "call", native_call)
    assert module.NativeItemStreams(owner, max_bytes=128).export_item(source) == content
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "=== Truncated Data ===",
        "abc",
        "AA===",
        "AA==junk",
        "AA==AA==",
        "Zh==",
        "é",
        "A" * 300,
    ],
)
def test_native_export_rejects_incomplete_noncanonical_or_oversized_data(raw):
    with pytest.raises(ConfigurationError):
        module.decode_native_item(raw, max_bytes=64)


@pytest.mark.parametrize("size", [None, 0, -1, True, "20", 129])
def test_native_export_requires_metadata_size_before_request(monkeypatch, size):
    monkeypatch.setattr(
        ExportItems, "call", lambda *_a, **_k: pytest.fail("unexpected ExportItems")
    )
    source = SimpleNamespace(id="synthetic-source", changekey="synthetic-key", size=size)
    with pytest.raises(ConfigurationError, match="size"):
        module.NativeItemStreams(account(), max_bytes=128).export_item(source)


def test_native_restore_is_create_new_ordinary_item_and_exact_payload(monkeypatch):
    owner = account()
    destination = Folder(id="synthetic-destination", changekey="synthetic-folder-key")
    content = b"\x00opaque synthetic item\xff"
    calls = []

    def native_call(service, *, items):
        assert service.account is owner and service.chunk_size == 1
        payload = service.get_payload(items=items)
        item = payload.xpath('//*[local-name()="Item"]')[0]
        assert item.get("CreateAction") == "CreateNew" and item.get("IsAssociated") == "false"
        assert not item.xpath('./*[local-name()="ItemId"]')
        assert (
            base64.b64decode(item.xpath('./*[local-name()="Data"]')[0].text, validate=True)
            == content
        )
        assert item.xpath('./*[local-name()="ParentFolderId"]')[0].get("Id") == destination.id
        calls.append(items)
        return iter([("synthetic-restored", "synthetic-new-key")])

    monkeypatch.setattr(UploadItems, "call", native_call)
    identity = module.NativeItemStreams(owner, max_bytes=128).upload_create(destination, content)
    assert identity.item_id == "synthetic-restored"
    assert "synthetic-restored" not in repr(identity)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "outcome", ["timeout", "empty", "multiple", "missing-key", "missing-id", "invalid-shape"]
)
def test_native_restore_uncertain_outcomes_never_repeat(monkeypatch, outcome):
    calls = []

    def native_call(service, *, items):
        calls.append(items)
        if outcome == "timeout":
            raise TimeoutError("private synthetic server material must be omitted")
        return iter(
            {
                "empty": [],
                "multiple": [("a", "key"), ("b", "key")],
                "missing-key": [("a", None)],
                "missing-id": [(None, "key")],
                "invalid-shape": [SimpleNamespace(id="a")],
            }[outcome]
        )

    monkeypatch.setattr(UploadItems, "call", native_call)
    with pytest.raises(MutationResultUnavailableError) as error:
        module.NativeItemStreams(account(), max_bytes=128).upload_create(
            Folder(id="d", changekey="k"), b"synthetic"
        )
    assert error.value.context.mutation_uncertain and not error.value.context.retry_safe
    assert "private synthetic" not in str(error.value)
    assert len(calls) == 1


def test_structured_native_restore_rejection_is_preserved(monkeypatch):
    def deny(*_args, **_kwargs):
        raise ErrorAccessDenied("synthetic refusal")

    monkeypatch.setattr(UploadItems, "call", deny)
    with pytest.raises(ErrorAccessDenied):
        module.NativeItemStreams(account(), max_bytes=128).upload_create(
            Folder(id="d", changekey="k"), b"synthetic"
        )


@pytest.mark.parametrize("build", [None, Build(14, 0)])
def test_unsupported_native_server_version_fails_before_request(monkeypatch, build):
    owner = account()
    owner.version = SimpleNamespace(build=build)
    monkeypatch.setattr(
        ExportItems, "call", lambda *_a, **_k: pytest.fail("unexpected ExportItems")
    )
    with pytest.raises(UnsupportedFeatureError):
        module.NativeItemStreams(owner, max_bytes=128).export_item(
            Task(id="s", changekey="k", size=20)
        )


@pytest.mark.parametrize("kind", ["calendar", "contact", "task"])
def test_pim_item_attachment_uses_one_native_parent_export_without_flattening(monkeypatch, kind):
    from exchangelib import CalendarItem, Contact, FileAttachment, ItemAttachment

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway

    owner = account()
    gateway = EWSGateway(AppConfig(max_attachment_bytes=128), account_factory=lambda: owner)
    cls = {"calendar": CalendarItem, "contact": Contact, "task": Task}[kind]
    metadata = cls(id="synthetic-source", changekey="metadata-key")
    metadata.attachments = [
        ItemAttachment(name="embedded synthetic item"),
        FileAttachment(name="embedded synthetic file"),
    ]
    sized = cls(id="synthetic-source", changekey="size-key", size=42)
    fetches = []

    def fetch(reference, kinds, fields):
        assert reference == "local-synthetic-pim" and kinds == (
            "event" if kind == "calendar" else kind,
        )
        fetches.append(fields)
        return metadata if fields == ("attachments",) else sized

    monkeypatch.setattr(gateway, "_fetch_item", fetch)
    monkeypatch.setattr(
        ItemAttachment, "item", property(lambda _self: pytest.fail("embedded item dereferenced"))
    )
    monkeypatch.setattr(
        FileAttachment,
        "fp",
        property(lambda _self: pytest.fail("native item file downloaded twice")),
    )
    exports = []

    def export(service, *, items):
        assert (
            service.get_payload(items=items).xpath('//*[local-name()="ItemId"]')[0].get("ChangeKey")
            == "size-key"
        )
        exports.append(items)
        return iter([base64.b64encode(b"opaque complete synthetic PIM item").decode()])

    monkeypatch.setattr(ExportItems, "call", export)
    attachments = gateway._backup_pim_attachments("local-synthetic-pim", kind)
    assert fetches == [("attachments",), ("size",)] and len(exports) == 1
    assert len(attachments) == 1 and attachments[0].data == b"opaque complete synthetic PIM item"
    assert attachments[0].metadata == {
        "role": module.NATIVE_ITEM_ROLE,
        "schema": 1,
        "item_kind": kind,
    }


def native_offline_item(kind="task", *, content=b"opaque synthetic native item", schema=1):
    from gucmail.services.backup import OfflineItem

    return OfflineItem(
        "synthetic-key",
        "synthetic-reference",
        kind,
        "/Synthetic",
        {"subject": "Synthetic PIM item", "recurrence": {"native_history": "opaque"}},
        attachments=(
            {
                "original_filename": "exchange-item.bin",
                "content": content,
                "content_type": module.NATIVE_ITEM_MEDIA_TYPE,
                "metadata": {"role": module.NATIVE_ITEM_ROLE, "schema": schema, "item_kind": kind},
            },
        ),
    )


@pytest.mark.parametrize("kind", ["calendar", "contact", "task"])
def test_native_restore_verifies_fresh_type_and_identity_without_recreating_fields(
    monkeypatch, kind
):
    from exchangelib import CalendarItem, Contact

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway

    owner = account()
    cls = {"calendar": CalendarItem, "contact": Contact, "task": Task}[kind]
    returned = cls(id="new-native-id", changekey="fresh-readback-key")
    fetches = []

    def fetch(**kwargs):
        fetches.append(kwargs)
        assert kwargs == {"ids": [("new-native-id", "upload-key")], "only_fields": ("item_class",)}
        return iter([returned])

    owner.fetch = fetch
    gateway = EWSGateway(AppConfig(max_attachment_bytes=128), account_factory=lambda: owner)
    destination = Folder(id="destination-id", changekey="folder-key")
    folders = []

    def folder(path, requested_kind):
        folders.append((path, requested_kind))
        return destination

    monkeypatch.setattr(gateway, "_restore_folder", folder)
    for old in ("_restore_event", "_restore_contact", "_restore_task"):
        monkeypatch.setattr(gateway, old, lambda *_a, **_k: pytest.fail("native content flattened"))
    uploads = []

    def upload(service, *, items):
        payload = service.get_payload(items=items)
        assert payload.xpath('//*[local-name()="Item"]')[0].get("CreateAction") == "CreateNew"
        uploads.append(items)
        return iter([("new-native-id", "upload-key")])

    monkeypatch.setattr(UploadItems, "call", upload)
    reference = gateway.restore_item(native_offline_item(kind), "Dedicated synthetic restore")
    expected_kind = "event" if kind == "calendar" else kind
    assert gateway._resolve_item(reference, expected_kind)[1] is returned
    assert folders == [("Dedicated synthetic restore", kind)] and len(uploads) == len(fetches) == 1


@pytest.mark.parametrize(
    "outcome",
    ["missing", "wrong-type", "wrong-id", "missing-key", "multiple", "read-error", "state-error"],
)
def test_accepted_native_restore_bad_readback_or_local_state_does_not_replay(monkeypatch, outcome):
    from exchangelib import Message

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway

    owner = account()
    uploads = []

    def upload(_service, *, items):
        uploads.append(items)
        return iter([("new-native-id", "upload-key")])

    monkeypatch.setattr(UploadItems, "call", upload)
    fetches = []

    def fetch(**kwargs):
        fetches.append(kwargs)
        if outcome == "read-error":
            raise TimeoutError("synthetic private diagnostic")
        valid = Task(id="new-native-id", changekey="fresh-key")
        return iter(
            {
                "missing": [],
                "wrong-type": [Message(id="new-native-id")],
                "wrong-id": [Task(id="unrequested-id")],
                "missing-key": [Task(id="new-native-id")],
                "multiple": [valid, valid],
                "state-error": [valid],
            }[outcome]
        )

    owner.fetch = fetch
    gateway = EWSGateway(AppConfig(max_attachment_bytes=128), account_factory=lambda: owner)
    monkeypatch.setattr(
        gateway, "_restore_folder", lambda *_a: Folder(id="destination", changekey="key")
    )
    if outcome == "state-error":

        def fail(*_a, **_k):
            raise OSError("synthetic local failure")

        monkeypatch.setattr(gateway, "_remember", fail)
    with pytest.raises(MutationResultUnavailableError) as error:
        gateway.restore_item(native_offline_item(), "Dedicated synthetic restore")
    assert len(uploads) == len(fetches) == 1 and not error.value.context.retry_safe
    assert error.value.context.mutation_uncertain and not gateway._objects


@pytest.mark.parametrize(
    "problem",
    [
        "schema",
        "schema-bool",
        "wrong-kind",
        "mime",
        "empty",
        "oversized",
        "duplicate",
        "extra-file",
        "old-server",
        "unknown-native-role",
    ],
)
def test_native_restore_preflight_rejects_invalid_archive_before_folder_or_upload(
    monkeypatch, problem
):
    from dataclasses import replace

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway

    owner = account()
    gateway = EWSGateway(AppConfig(max_attachment_bytes=128), account_factory=lambda: owner)
    item = native_offline_item()
    attachment = dict(item.attachments[0])
    metadata = dict(attachment["metadata"])
    if problem.startswith("schema"):
        metadata["schema"] = True if problem == "schema-bool" else 999
    elif problem == "wrong-kind":
        metadata["item_kind"] = "contact"
    elif problem == "mime":
        attachment["content_type"] = "text/plain"
    elif problem == "empty":
        attachment["content"] = b""
    elif problem == "oversized":
        attachment["content"] = b"x" * 129
    elif problem == "old-server":
        owner.version = SimpleNamespace(build=Build(14, 0))
    elif problem == "unknown-native-role":
        metadata["role"] = module.NATIVE_ITEM_ROLE + "_v2"
    attachment["metadata"] = metadata
    attachments = (attachment,)
    if problem == "duplicate":
        attachments += (attachment,)
    elif problem == "extra-file":
        attachments += ({"content": b"synthetic file", "metadata": {}},)
    item = replace(item, attachments=attachments)
    monkeypatch.setattr(
        gateway, "_restore_folder", lambda *_a: pytest.fail("folder created before preflight")
    )
    monkeypatch.setattr(
        UploadItems, "call", lambda *_a, **_k: pytest.fail("native item uploaded before preflight")
    )
    with pytest.raises((ConfigurationError, UnsupportedFeatureError)):
        gateway.restore_item(item, "Dedicated synthetic restore")


@pytest.mark.parametrize("encrypted", [False, True])
def test_native_stream_backup_integrity_offline_plan_and_confirmed_restore(
    tmp_path, monkeypatch, encrypted
):
    from gucmail.config import AppConfig
    from gucmail.errors import ConfirmationRequiredError
    from gucmail.ews.adapter import EWSGateway
    from gucmail.services.backup import BackupAttachment, BackupItem, BackupService

    content = b"\x00opaque private synthetic Task with embedded item\xff"
    service = BackupService(tmp_path / "owned-backups")
    passphrase = b"synthetic-test-only" if encrypted else None
    item = BackupItem(
        "synthetic-original-task",
        "task",
        {"subject": "Synthetic Task"},
        attachments=(
            BackupAttachment(
                "exchange-item.bin",
                content,
                module.NATIVE_ITEM_MEDIA_TYPE,
                metadata={"role": module.NATIVE_ITEM_ROLE, "schema": 1, "item_kind": "task"},
            ),
        ),
    )
    result = service.create([item], passphrase=passphrase)
    archive = service.open_offline(result.backup_id, passphrase=passphrase, verify=True)
    assert (
        archive.read_attachment_bytes(
            item.reference, archive.get_item(item.reference).attachments[0]["object"]
        )
        == content
    )
    plan = service.plan_restore(result.backup_id, passphrase=passphrase)
    assert plan.create_count == 1 and any(
        "opaque Exchange stream" in warning for warning in plan.warnings
    )
    assert any("opaque Exchange stream" in warning for warning in plan.entries[0].warnings)
    owner = account()
    owner.fetch = lambda **_kwargs: iter([Task(id="restored-id", changekey="verified-key")])
    gateway = EWSGateway(AppConfig(), account_factory=lambda: owner)
    monkeypatch.setattr(
        gateway, "_restore_folder", lambda *_a: Folder(id="owned-destination", changekey="k")
    )
    uploads = []

    def upload(service, *, items):
        payload = service.get_payload(items=items)
        assert (
            base64.b64decode(payload.xpath('//*[local-name()="Data"]')[0].text, validate=True)
            == content
        )
        uploads.append(items)
        return iter([("restored-id", "upload-key")])

    monkeypatch.setattr(UploadItems, "call", upload)
    with pytest.raises(ConfirmationRequiredError):
        service.restore(result.backup_id, passphrase=passphrase, target=gateway)
    assert not uploads
    restored = service.restore(
        result.backup_id,
        passphrase=passphrase,
        target=gateway,
        confirmed=True,
        confirmation_token=plan.confirmation_token,
    )
    assert restored.restored_count == 1 and restored.failed_count == 0 and len(uploads) == 1
    assert not restored.mutation_uncertain and not restored.results[0].mutation_uncertain
    hydrated = service._hydrate_restore_attachments(archive, archive.get_item(item.reference))
    assert "opaque private" not in repr(hydrated) and "\\xff" not in repr(hydrated)


@pytest.mark.parametrize("kind", ["calendar", "contact", "task"])
@pytest.mark.parametrize("outcome", ["wrong-type", "wrong-id", "multiple"])
def test_pim_metadata_fetch_rejects_bad_identity_before_cache_remember(monkeypatch, kind, outcome):
    from exchangelib import CalendarItem, Contact, Message

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway

    owner = account()
    cls = {"calendar": CalendarItem, "contact": Contact, "task": Task}[kind]
    source = cls(id="selected-id", changekey="selected-key")
    valid = cls(id="selected-id", changekey="new-key")
    values = {
        "wrong-type": [Message(id="selected-id")],
        "wrong-id": [cls(id="unrequested-id")],
        "multiple": [valid, valid],
    }[outcome]
    fetches = []

    def fetch(**kwargs):
        fetches.append(kwargs)
        assert kwargs == {"ids": [("selected-id", "selected-key")], "only_fields": ("size",)}
        return iter(values)

    owner.fetch = fetch
    gateway = EWSGateway(AppConfig(), account_factory=lambda: owner)
    remembered = gateway._remember("event" if kind == "calendar" else kind, source)
    monkeypatch.setattr(
        gateway, "_remember", lambda *_a, **_k: pytest.fail("invalid readback remembered")
    )
    with pytest.raises(ConfigurationError):
        gateway._fetch_item(remembered, ("event" if kind == "calendar" else kind,), ("size",))
    assert len(fetches) == 1
    assert gateway._resolve_item(remembered, "event" if kind == "calendar" else kind)[1] is source


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("outcome", ["cancel", "verified", "uncertain"])
async def test_normal_backup_palette_plan_confirmation_native_restore_result(
    tmp_path, monkeypatch, size, outcome
):
    from tests.test_tui import FakeGateway, context_for
    from textual.widgets import Button, Input, Static

    from gucmail.config import AppConfig
    from gucmail.ews.adapter import EWSGateway
    from gucmail.services.backup import BackupAttachment, BackupItem, BackupService
    from gucmail.tui.app import GUCmailApp, PaletteScreen
    from gucmail.tui.area_common import ConfirmationScreen, ViewState
    from gucmail.tui.backup_screen import BackupScreen, RestorePlanScreen

    context = context_for(tmp_path)
    content = b"opaque synthetic native Task"
    service = BackupService(context.paths.backup_dir)
    backup = service.create(
        [
            BackupItem(
                "synthetic-native-task",
                "task",
                {"subject": "Synthetic native backup Task"},
                attachments=(
                    BackupAttachment(
                        "exchange-item.bin",
                        content,
                        module.NATIVE_ITEM_MEDIA_TYPE,
                        metadata={
                            "role": module.NATIVE_ITEM_ROLE,
                            "schema": 1,
                            "item_kind": "task",
                        },
                    ),
                ),
            )
        ]
    )
    owner = account()
    fetches = []

    def fetch(**kwargs):
        fetches.append(kwargs)
        if outcome == "uncertain":
            raise TimeoutError("private synthetic material")
        return iter([Task(id="restored-id", changekey="verified-key")])

    owner.fetch = fetch
    native = EWSGateway(AppConfig(), account_factory=lambda: owner)
    monkeypatch.setattr(
        native, "_restore_folder", lambda *_a: Folder(id="destination", changekey="folder-key")
    )
    uploads = []

    def upload(provider, *, items):
        payload = provider.get_payload(items=items)
        assert payload.xpath('//*[local-name()="Item"]')[0].get("CreateAction") == "CreateNew"
        uploads.append(items)
        return iter([("restored-id", "upload-key")])

    monkeypatch.setattr(UploadItems, "call", upload)
    facade = FakeGateway()
    facade.restore_item = native.restore_item
    facade.iter_backup_items = lambda *_a, **_k: pytest.fail(
        "Restore enumerated live source content"
    )
    app = GUCmailApp(context, gateway=facade)

    async def wait(pilot, predicate):
        for _ in range(250):
            await pilot.pause(0.02)
            if predicate():
                return
        pytest.fail(
            "Normal backup workflow did not settle: "
            + str(app.screen)
            + "; uploads="
            + str(len(uploads))
            + "; fetches="
            + str(len(fetches))
            + "; status="
            + str(app.screen.query_one("#area-state", Static).content)
        )

    async def activate(pilot, identifier):
        button = app.screen.query_one(identifier, Button)
        assert not button.disabled
        button.focus()
        await wait(
            pilot, lambda: button.has_focus and app.screen.region.contains_region(button.region)
        )
        await pilot.press("enter")

    async with app.run_test(size=size) as pilot:
        await wait(pilot, lambda: app.ui.online)
        await pilot.press("ctrl+k")
        await wait(pilot, lambda: isinstance(app.screen, PaletteScreen) and app.screen.is_mounted)
        app.screen.query_one("#palette-input", Input).value = "Open Backup Center"
        await pilot.pause()
        await pilot.press("enter")
        await wait(
            pilot,
            lambda: (
                isinstance(app.screen, BackupScreen)
                and app.screen._selected_id == backup.backup_id
                and not app.screen._operation_active
            ),
        )
        await activate(pilot, "#backup-restore-plan")
        await wait(
            pilot, lambda: isinstance(app.screen, RestorePlanScreen) and app.screen.is_mounted
        )
        plan_screen = app.screen
        await wait(
            pilot,
            lambda: (
                "opaque Exchange stream"
                in str(plan_screen.query_one("#restore-plan-detail", Static).content)
            ),
        )
        assert not uploads and not fetches
        assert "No changes from this preview." in str(
            plan_screen.query_one("#restore-plan-summary", Static).content
        )
        await activate(pilot, "#restore-execute")
        await wait(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        assert not uploads
        await pilot.press("escape" if outcome == "cancel" else "y")
        if outcome == "cancel":
            await wait(pilot, lambda: app.screen is plan_screen)
            assert not uploads and not fetches
            assert not plan_screen.query_one("#restore-execute", Button).disabled
            assert "No changes from this preview." in str(
                plan_screen.query_one("#restore-plan-summary", Static).content
            )
        else:
            await wait(
                pilot,
                lambda: (
                    app.screen is plan_screen
                    and not plan_screen._operation_active
                    and plan_screen.query_one("#restore-execute", Button).disabled
                ),
            )
            assert len(uploads) == len(fetches) == 1
            detail = str(plan_screen.query_one("#restore-plan-detail", Static).content)
            status = str(plan_screen.query_one("#area-state", Static).content)
            summary = str(plan_screen.query_one("#restore-plan-summary", Static).content)
            assert "No changes from this preview." not in summary
            assert "Restore attempt finished." in summary
            if outcome == "uncertain":
                assert plan_screen.view_state is ViewState.ERROR
                assert "may have created" in detail and "may exist" in status
                assert "Mailbox items were created." not in status
            else:
                assert plan_screen.view_state is ViewState.READY and "Restored: 1" in detail
            await pilot.press("enter")
            await pilot.pause()
            assert len(uploads) == 1


@pytest.mark.parametrize("outcome", ["preview", "unconfirmed", "verified", "uncertain"])
def test_native_backup_restore_cli_confirmation_exit_code_and_private_json(
    tmp_path, monkeypatch, outcome
):
    import json

    from tests.test_cli import _environment
    from typer.testing import CliRunner

    from gucmail import cli
    from gucmail.config import AppConfig
    from gucmail.errors import ExitCode
    from gucmail.ews.adapter import EWSGateway
    from gucmail.services.backup import BackupAttachment, BackupItem, BackupService

    content = b"opaque synthetic private native Task"
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create(
        [
            BackupItem(
                "synthetic-original",
                "task",
                {"subject": "Synthetic native Task"},
                attachments=(
                    BackupAttachment(
                        "exchange-item.bin",
                        content,
                        module.NATIVE_ITEM_MEDIA_TYPE,
                        metadata={
                            "role": module.NATIVE_ITEM_ROLE,
                            "schema": 1,
                            "item_kind": "task",
                        },
                    ),
                ),
            )
        ]
    )
    monkeypatch.setattr(cli.backup, "_backup_service", lambda _ctx: service)
    owner = account()
    fetches = []

    def fetch(**kwargs):
        fetches.append(kwargs)
        if outcome == "uncertain":
            raise TimeoutError("private synthetic material")
        return iter([Task(id="restored-id", changekey="verified-key")])

    owner.fetch = fetch
    native = EWSGateway(AppConfig(), account_factory=lambda: owner)
    monkeypatch.setattr(
        native, "_restore_folder", lambda *_a: Folder(id="destination", changekey="key")
    )
    gateways = []

    def gateway(_ctx):
        gateways.append(True)
        assert outcome in {"verified", "uncertain"}
        return native

    monkeypatch.setattr(cli.core, "gateway", gateway)
    uploads = []

    def upload(provider, *, items):
        assert (
            provider.get_payload(items=items)
            .xpath('//*[local-name()="Item"]')[0]
            .get("CreateAction")
            == "CreateNew"
        )
        uploads.append(items)
        return iter([("restored-id", "upload-key")])

    monkeypatch.setattr(UploadItems, "call", upload)
    args = ["--json", "--non-interactive"]
    if outcome in {"verified", "uncertain"}:
        args += ["--yes"]
    args += ["backup", "restore", backup.backup_id]
    if outcome != "preview":
        args += ["--apply"]
    result = CliRunner().invoke(cli.app, args, env=_environment(tmp_path / "owned-cli"))
    assert (
        content.decode() not in result.output and "private synthetic material" not in result.output
    )
    data = json.loads(result.output)["data"]
    if outcome == "preview":
        assert result.exit_code == 0 and data["create_count"] == 1
        assert not gateways and not uploads and not fetches
    elif outcome == "unconfirmed":
        assert result.exit_code != 0 and "--yes" in data["message"]
        assert not gateways and not uploads and not fetches
    else:
        assert len(gateways) == len(uploads) == len(fetches) == 1
        assert data["mutation_uncertain"] is (outcome == "uncertain")
        assert data["results"][0]["mutation_uncertain"] is (outcome == "uncertain")
        assert result.exit_code == (int(ExitCode.PARTIAL) if outcome == "uncertain" else 0)


@pytest.mark.parametrize("outcome", ["malformed-stream", "server-denied", "unknown-size"])
def test_native_pim_export_failure_retains_incomplete_archive_without_mutation(
    tmp_path, monkeypatch, outcome
):
    from exchangelib import ItemAttachment

    from gucmail.backup_format import INCOMPLETE_MANIFEST_FILENAME
    from gucmail.config import AppConfig
    from gucmail.errors import GUCmailError
    from gucmail.ews.adapter import EWSGateway
    from gucmail.services.backup import BackupItem, BackupService

    owner = account()
    native = EWSGateway(AppConfig(max_attachment_bytes=128), account_factory=lambda: owner)
    metadata = Task(id="synthetic-source", changekey="synthetic-key")
    metadata.attachments = [ItemAttachment(name="synthetic embedded Task")]
    sized = Task(
        id="synthetic-source",
        changekey="synthetic-key",
        size=None if outcome == "unknown-size" else 42,
    )
    fetches = []

    def fetch(_reference, _kinds, fields):
        fetches.append(fields)
        return metadata if fields == ("attachments",) else sized

    monkeypatch.setattr(native, "_fetch_item", fetch)
    exports = []

    def export(_provider, *, items):
        exports.append(items)
        if outcome == "server-denied":
            raise ErrorAccessDenied("private synthetic server detail")
        return iter(["=== Truncated Data ==="])

    monkeypatch.setattr(ExportItems, "call", export)
    monkeypatch.setattr(
        UploadItems, "call", lambda *_a, **_k: pytest.fail("Backup mutated the mailbox")
    )

    class Source:
        def iter_backup_items(self, _request, _completed):
            yield BackupItem(
                "synthetic-original",
                "task",
                {"subject": "Synthetic Task"},
                attachments=native._backup_pim_attachments("synthetic-original", "task"),
            )

    root = tmp_path / "owned-backups"
    with pytest.raises(GUCmailError) as error:
        BackupService(root).create(Source(), backup_id="synthetic-native-failure")
    assert "private synthetic server detail" not in str(error.value)
    partial = root / "synthetic-native-failure.partial"
    assert partial.is_dir() and (partial / INCOMPLETE_MANIFEST_FILENAME).is_file()
    assert not (root / "synthetic-native-failure").exists()
    assert fetches == [("attachments",), ("size",)]
    assert len(exports) == (0 if outcome == "unknown-size" else 1)
