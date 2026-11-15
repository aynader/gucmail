"""Restore outcomes preserve possible creation without replaying writes."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from exchangelib.errors import ErrorAccessDenied

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, GUCmailError, MutationResultUnavailableError
from gucmail.ews.adapter import EWSGateway
from gucmail.services.backup import BackupItem, BackupService, OfflineArchive, RestoreResult
from waiting import rounds


@pytest.mark.parametrize("kind", ["message", "calendar", "contact", "task"])
@pytest.mark.parametrize(
    "failure", ["attachment", "reference", "save_response", "missing_identity"]
)
def test_accepted_ordinary_restore_cannot_lose_uncertainty(monkeypatch, kind, failure):
    accepted = []

    class Remote:
        id = None
        changekey = None

        def save(self, **kwargs):
            accepted.append(kwargs)
            if failure == "save_response":
                raise TimeoutError("synthetic lost response")
            if failure != "missing_identity":
                self.id = "new-owned-item"
                self.changekey = "new-key"

        def attach(self, attachments):
            assert failure == "attachment"
            raise TimeoutError("synthetic post-create attachment timeout")

    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: SimpleNamespace(),
        factory_overrides={
            name: lambda **kwargs: Remote()
            for name in ("Message", "CalendarItem", "Contact", "Task")
        },
    )
    attachments = [object()] if failure == "attachment" else []
    monkeypatch.setattr(gateway, "_restore_attachments", lambda item: attachments)
    if failure == "reference":

        def remember(*args, **kwargs):
            raise OSError("synthetic mapping failure")

        monkeypatch.setattr(gateway, "_remember", remember)
    folder = SimpleNamespace()
    data = {
        "summary": {"subject": "Synthetic"},
        "subject": "Synthetic",
        "start": "2026-10-07T10:00:00+00:00",
        "end": "2026-10-07T11:00:00+00:00",
        "timezone": "UTC",
        "display_name": "Synthetic",
    }

    def restore():
        if kind == "message":
            return gateway._restore_message(SimpleNamespace(attachments=[]), folder, data)
        return getattr(gateway, "_restore_" + ("event" if kind == "calendar" else kind))(
            folder, data, attachments=attachments
        )

    with pytest.raises(GUCmailError) as error:
        restore()
    assert len(accepted) == 1
    assert error.value.context.mutation_uncertain
    assert error.value.context.retry_safe is False
    assert not gateway._objects


@pytest.mark.parametrize("kind", ["message", "calendar", "contact", "task"])
def test_structured_save_rejection_stays_known_noncreation(monkeypatch, kind):
    calls = []

    class Rejected:
        def save(self, **kwargs):
            calls.append(kwargs)
            raise ErrorAccessDenied("synthetic denied create")

    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: SimpleNamespace(),
        factory_overrides={
            name: lambda **kwargs: Rejected()
            for name in ("Message", "CalendarItem", "Contact", "Task")
        },
    )
    monkeypatch.setattr(gateway, "_restore_attachments", lambda item: [])
    data = {
        "summary": {"subject": "Synthetic"},
        "subject": "Synthetic",
        "start": "2026-10-07T10:00:00+00:00",
        "end": "2026-10-07T11:00:00+00:00",
        "timezone": "UTC",
        "display_name": "Synthetic",
    }
    with pytest.raises(GUCmailError) as error:
        if kind == "message":
            gateway._restore_message(SimpleNamespace(attachments=[]), SimpleNamespace(), data)
        else:
            getattr(gateway, "_restore_" + ("event" if kind == "calendar" else kind))(
                SimpleNamespace(), data
            )
    assert len(calls) == 1 and not error.value.context.mutation_uncertain and not gateway._objects


@pytest.mark.parametrize("outcome", ["lost_response", "missing_reference"])
def test_unknown_restore_provider_outcome_stops_and_preserves_uncertainty(tmp_path, outcome):
    service = BackupService(tmp_path / "backups")
    service.create(
        [
            BackupItem(
                kind="message",
                reference="m-one",
                folder_path="Inbox",
                data={"summary": {"subject": "Synthetic"}},
            ),
            BackupItem(
                kind="message",
                reference="m-two",
                folder_path="Inbox",
                data={"summary": {"subject": "Synthetic2"}},
            ),
        ],
        backup_id="synthetic-uncertainty",
    )
    plan = service.plan_restore("synthetic-uncertainty")
    accepted = []

    class Target:
        def restore_item(self, item, destination_folder):
            accepted.append(item.reference)
            if outcome == "lost_response":
                raise ConnectionError("synthetic-private-diagnostic")
            return None

    result = service.restore(
        "synthetic-uncertainty",
        confirmed=True,
        target=Target(),
        confirmation_token=plan.confirmation_token,
    )
    assert isinstance(result, RestoreResult)
    assert len(accepted) == 1 and result.stopped_early
    assert result.failed_count == 1 and result.restored_count == 0
    assert result.mutation_uncertain and result.results[0].mutation_uncertain
    assert "synthetic-private-diagnostic" not in json.dumps(asdict(result))


@pytest.mark.parametrize("field", ["id", "changekey"])
@pytest.mark.parametrize("invalid", [None, "", " ", " bad ", 1, True, "bad\nvalue", "x" * 8193])
def test_returned_identity_is_validated_before_attachments_or_mapping(monkeypatch, field, invalid):
    calls = []

    class Remote:
        id = None
        changekey = None

        def save(self):
            calls.append("save")
            self.id, self.changekey = "created-id", "created-key"
            setattr(self, field, invalid)

        def attach(self, _attachments):
            pytest.fail("Invalid returned identity reached attachment upload")

    gateway = EWSGateway(AppConfig(), account_factory=lambda: pytest.fail("Account accessed"))
    monkeypatch.setattr(
        gateway, "_remember", lambda *a, **kw: pytest.fail("Invalid identity mapped")
    )
    with pytest.raises(MutationResultUnavailableError) as error:
        gateway._save_restored_item(Remote(), "message", object(), attachments=[object()])
    assert calls == ["save"]
    assert error.value.context.mutation_uncertain and error.value.context.retry_safe is False


def test_structured_attachment_rejection_after_save_remains_uncertain():
    calls = []

    class Remote:
        id = None
        changekey = None

        def save(self):
            calls.append("save")
            self.id, self.changekey = "created-id", "created-key"

        def attach(self, _attachments):
            calls.append("attach")
            raise ErrorAccessDenied("synthetic-private-detail")

    gateway = EWSGateway(AppConfig(), account_factory=lambda: pytest.fail("Account accessed"))
    with pytest.raises(MutationResultUnavailableError) as error:
        gateway._save_restored_item(Remote(), "message", object(), attachments=[object()])
    assert calls == ["save", "attach"] and not gateway._objects
    assert error.value.context.mutation_uncertain and not error.value.context.retry_safe
    assert "synthetic-private-detail" not in str(error.value)


def test_existing_item_cannot_be_updated_by_restore():
    remote = SimpleNamespace(id="existing-item", save=lambda: pytest.fail("Existing item updated"))
    gateway = EWSGateway(AppConfig(), account_factory=lambda: pytest.fail("Account accessed"))
    with pytest.raises(ConfigurationError) as error:
        gateway._save_restored_item(remote, "message", object())
    assert not error.value.context.mutation_uncertain and not gateway._objects


def test_local_hydration_failure_reports_no_server_mutation(tmp_path, monkeypatch):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create([BackupItem("m-one", "message", {"summary": {"subject": "Synthetic"}})])
    plan = service.plan_restore(backup.backup_id)

    def broken_hydration(*args):
        raise OSError("synthetic-private-local-failure")

    monkeypatch.setattr(service, "_hydrate_restore_attachments", broken_hydration)

    class Target:
        def restore_item(self, *args):
            pytest.fail("Local failure reached Exchange")

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        confirmation_token=plan.confirmation_token,
        target=Target(),
    )
    assert result.failed_count == 1 and result.stopped_early
    assert not result.mutation_uncertain and not result.server_mutations_performed
    assert "synthetic-private-local-failure" not in json.dumps(asdict(result))


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_local_read_failure_after_creation_preserves_completed_results(
    tmp_path, monkeypatch, continue_on_error
):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two", "m-three")]
    )
    plan = service.plan_restore(backup.backup_id)
    calls = []
    decode = OfflineArchive._decode_item

    def decode_with_late_failure(archive, path, manifest, keys, descriptor):
        item = decode(archive, path, manifest, keys, descriptor)
        if calls and item.reference == "m-two":
            raise OSError("synthetic-private-late-read-failure")
        return item

    monkeypatch.setattr(OfflineArchive, "_decode_item", decode_with_late_failure)

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            return "restored-" + item.reference

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        confirmation_token=plan.confirmation_token,
        target=Target(),
        continue_on_error=continue_on_error,
    )
    assert isinstance(result, RestoreResult)
    assert calls == (["m-one", "m-three"] if continue_on_error else ["m-one"])
    assert result.restored_count == (2 if continue_on_error else 1)
    assert result.failed_count == 1 and not result.mutation_uncertain
    assert result.results[0].restored_reference == "restored-m-one"
    assert result.results[1].source_reference == "m-two"
    assert result.stopped_early is not continue_on_error
    assert result.server_mutations_performed
    assert "synthetic-private-late-read-failure" not in json.dumps(asdict(result))


@pytest.mark.parametrize("field", ["id", "changekey"])
def test_identity_is_revalidated_after_attachment_upload(field):
    calls = []

    class Remote:
        id = None
        changekey = None

        def save(self):
            calls.append("save")
            self.id, self.changekey = "created-id", "created-key"

        def attach(self, _attachments):
            calls.append("attach")
            setattr(self, field, None)

    gateway = EWSGateway(AppConfig(), account_factory=lambda: pytest.fail("Account accessed"))
    with pytest.raises(MutationResultUnavailableError) as error:
        gateway._save_restored_item(Remote(), "message", object(), attachments=[object()])
    assert calls == ["save", "attach"] and not gateway._objects
    assert error.value.context.mutation_uncertain and not error.value.context.retry_safe


@pytest.mark.parametrize("invalid", [None, "", " ", " bad ", 1, True, "bad\nvalue", "x" * 8193])
@pytest.mark.parametrize("continue_on_error", [False, True])
def test_invalid_provider_reference_is_uncertain_and_never_retried(
    tmp_path, invalid, continue_on_error
):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create(
        [BackupItem(reference, "message", {}) for reference in ("m-one", "m-two")]
    )
    plan = service.plan_restore(backup.backup_id)
    calls = []

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            return invalid if item.reference == "m-one" else "m-restored-two"

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        target=Target(),
        confirmation_token=plan.confirmation_token,
        continue_on_error=continue_on_error,
    )
    assert calls == (["m-one", "m-two"] if continue_on_error else ["m-one"])
    assert result.failed_count == 1 and result.restored_count == int(continue_on_error)
    assert result.stopped_early is not continue_on_error
    assert result.mutation_uncertain and result.results[0].mutation_uncertain
    assert result.results[0].error_type == "MutationResultUnavailableError"
    assert result.server_mutations_performed


@pytest.mark.parametrize("uncertain", [False, True])
def test_service_preserves_explicit_provider_outcome(tmp_path, uncertain):
    service = BackupService(tmp_path / "owned-backups")
    backup = service.create([BackupItem("m-one", "message", {})])
    plan = service.plan_restore(backup.backup_id)
    calls = []

    class Target:
        def restore_item(self, item, destination):
            calls.append(item.reference)
            error = MutationResultUnavailableError if uncertain else ConfigurationError
            raise error("synthetic-private-detail")

    result = service.restore(
        backup.backup_id,
        confirmed=True,
        target=Target(),
        confirmation_token=plan.confirmation_token,
    )
    assert calls == ["m-one"] and result.failed_count == 1 and result.stopped_early
    assert result.mutation_uncertain is uncertain
    assert result.results[0].mutation_uncertain is uncertain
    assert "synthetic-private-detail" not in json.dumps(asdict(result))


def _ordinary_restore_gateway(monkeypatch, outcome):
    calls = []

    class Remote:
        id = None
        changekey = None

        def save(self):
            calls.append("save")
            if outcome == "uncertain":
                raise TimeoutError("synthetic-private-detail")
            self.id, self.changekey = "created-owned-id", "created-owned-key"

        def send(self):
            pytest.fail("Restore attempted to send")

    gateway = EWSGateway(
        AppConfig(),
        account_factory=lambda: SimpleNamespace(),
        factory_overrides={"Message": lambda **kwargs: Remote()},
    )
    monkeypatch.setattr(gateway, "_restore_folder", lambda *a: SimpleNamespace())
    monkeypatch.setattr(gateway, "_restore_attachments", lambda *a: [])
    return gateway, calls


@pytest.mark.parametrize("outcome", ["preview", "unconfirmed", "verified", "uncertain"])
def test_cli_ordinary_restore_reports_uncertainty_without_replay(tmp_path, monkeypatch, outcome):
    from tests.test_cli import _environment
    from typer.testing import CliRunner

    from gucmail import cli
    from gucmail.errors import ExitCode

    service = BackupService(tmp_path / "owned-backups")
    backup = service.create([BackupItem("m-one", "message", {"summary": {"subject": "Synthetic"}})])
    gateway, calls = _ordinary_restore_gateway(monkeypatch, outcome)
    resolutions = []
    monkeypatch.setattr(cli.backup, "_backup_service", lambda _ctx: service)

    def target(_ctx):
        resolutions.append(True)
        return gateway

    monkeypatch.setattr(cli.core, "gateway", target)
    args = ["--json", "--non-interactive"]
    if outcome in {"verified", "uncertain"}:
        args += ["--yes"]
    args += ["backup", "restore", backup.backup_id]
    if outcome != "preview":
        args += ["--apply"]
    result = CliRunner().invoke(cli.app, args, env=_environment(tmp_path / "owned-cli"))
    assert "synthetic-private-detail" not in result.output
    data = json.loads(result.output)["data"]
    if outcome == "preview":
        assert result.exit_code == 0 and data["create_count"] == 1
        assert not calls and not resolutions
    elif outcome == "unconfirmed":
        assert result.exit_code != 0 and "--yes" in data["message"]
        assert not calls and not resolutions
    else:
        assert calls == ["save"] and len(resolutions) == 1
        assert data["mutation_uncertain"] is (outcome == "uncertain")
        assert data["results"][0]["mutation_uncertain"] is (outcome == "uncertain")
        assert data["restored_count"] == (1 if outcome == "verified" else 0)
        assert result.exit_code == (int(ExitCode.PARTIAL) if outcome == "uncertain" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("outcome", ["cancel", "verified", "uncertain"])
async def test_normal_backup_restore_preserves_result_and_prevents_replay(
    tmp_path, monkeypatch, size, outcome
):
    from tests.test_tui import FakeGateway, context_for
    from textual.widgets import Button, Input, Static

    from gucmail.tui.app import GUCmailApp, PaletteScreen
    from gucmail.tui.area_common import ConfirmationScreen, ViewState
    from gucmail.tui.backup_screen import BackupScreen, RestorePlanScreen

    context = context_for(tmp_path)
    service = BackupService(context.paths.backup_dir)
    backup = service.create([BackupItem("m-one", "message", {"summary": {"subject": "Synthetic"}})])
    gateway, calls = _ordinary_restore_gateway(monkeypatch, outcome)
    facade = FakeGateway()
    facade.restore_item = gateway.restore_item
    facade.iter_backup_items = lambda *a: pytest.fail("Restore enumerated live source")
    app = GUCmailApp(context, gateway=facade)

    async def wait(pilot, condition):
        for _ in rounds(250):
            await pilot.pause(0.02)
            if condition():
                return
        pytest.fail("Ordinary restore workflow did not settle")

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
        assert not calls
        assert "No changes from this preview." in str(
            plan_screen.query_one("#restore-plan-summary", Static).content
        )
        await activate(pilot, "#restore-execute")
        await wait(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        assert not calls
        await pilot.press("escape" if outcome == "cancel" else "y")
        if outcome == "cancel":
            await wait(pilot, lambda: app.screen is plan_screen)
            assert not calls and not plan_screen.query_one("#restore-execute", Button).disabled
        else:
            await wait(
                pilot,
                lambda: (
                    app.screen is plan_screen
                    and not plan_screen._operation_active
                    and plan_screen.query_one("#restore-execute", Button).disabled
                ),
            )
            assert calls == ["save"]
            detail = str(plan_screen.query_one("#restore-plan-detail", Static).content)
            assert "synthetic-private-detail" not in detail
            if outcome == "uncertain":
                assert "Exchange may have created the failed item" in detail
                assert "Failed reference: m-one" in detail
                assert plan_screen.view_state is ViewState.ERROR
            else:
                assert "Restored: 1" in detail and plan_screen.view_state is ViewState.READY
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["save"]
