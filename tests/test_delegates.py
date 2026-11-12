"""Delegate request scope, metadata fidelity, bounds and shared user workflows."""

from __future__ import annotations

import json
from dataclasses import replace
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from exchangelib.errors import ErrorAccessDenied, ErrorServerBusy
from exchangelib.properties import DelegatePermissions, DelegateUser, UserId
from exchangelib.services import GetDelegate
from exchangelib.util import TNS
from exchangelib.version import Build, Version
from lxml import etree
from textual.app import App
from textual.widgets import Button, DataTable, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.command_catalog import CORE_COMMANDS
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, GUCmailError, ThrottledError, UnsupportedFeatureError
from gucmail.ews.adapter import EWSGateway
from gucmail.ews.capabilities import CapabilityRegistry
from gucmail.ews.delegates import delegate_reader, map_delegate, map_delegate_collection
from gucmail.models import (
    AccountAccess,
    ImplementationStatus,
    SafetyClass,
    ServerSupport,
    VerificationLevel,
)
from gucmail.services.delegates import DelegateService
from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ViewState
from gucmail.tui.delegates_screen import DelegatesScreen
from test_cli import _environment
from test_settings_secondpass import _context
from test_tui import FakeGateway

VERSION = Version(build=Build(15, 1, 2375, 7))


def _native(index=0):
    return DelegateUser(
        user_id=UserId(
            primary_smtp_address=f"delegate{index}@example.invalid",
            display_name=f"Synthetic delegate {index}",
            sid=f"S-1-5-21-{index}",
        ),
        delegate_permissions=DelegatePermissions(
            inbox_folder_permission_level="Reviewer",
            calendar_folder_permission_level="Editor",
        ),
        receive_copies_of_meeting_messages=True,
        view_private_items=False,
    )


def _account():
    return SimpleNamespace(
        primary_smtp_address="owner@example.invalid",
        version=VERSION,
        protocol=SimpleNamespace(version=VERSION),
    )


def test_real_library_payload_and_response_mapping_target_only_configured_mailbox(monkeypatch):
    account = _account()
    factories = []
    requests = []

    def factory():
        factories.append(account)
        return account

    def transport(service, payload_func, *, items, mailbox, include_permissions):
        assert list(items) == [None] and include_permissions is True
        payload = payload_func(user_ids=[None], mailbox=mailbox, include_permissions=True)
        requests.append(etree.tostring(payload))
        assert payload.get("IncludePermissions") == "true"
        assert payload.find("{*}Mailbox/{*}EmailAddress").text == "owner@example.invalid"
        assert payload.find("{*}UserIds") is None
        return iter([_native().to_xml(version=VERSION)])

    monkeypatch.setattr(GetDelegate, "_chunked_get_elements", transport)
    gateway = EWSGateway(AppConfig(), account_factory=factory)
    service = DelegateService(gateway)
    assert not requests and not factories
    for _ in range(2):
        page = service.list()
        row = page.entries[0]
        assert row.email_address == "delegate0@example.invalid"
        assert row.security_identifier == "S-1-5-21-0"
        assert {value.folder: value.level for value in row.permissions} == {
            "calendar": "Editor",
            "tasks": "None",
            "inbox": "Reviewer",
            "contacts": "None",
            "notes": "None",
            "journal": "None",
        }
        assert row.receive_meeting_copies is True and row.view_private_items is False
    assert len(requests) == 2 and len(factories) == 1


def test_absent_wire_fields_are_not_fabricated_as_explicit_none_or_false():
    element = _native().to_xml(version=VERSION)
    permissions = element.find(f"{{{TNS}}}DelegatePermissions")
    permissions.remove(permissions.find(f"{{{TNS}}}TasksFolderPermissionLevel"))
    element.remove(element.find(f"{{{TNS}}}ViewPrivateItems"))
    element.remove(element.find(f"{{{TNS}}}ReceiveCopiesOfMeetingMessages"))
    reader = delegate_reader(account=_account())
    row = map_delegate(reader._elem_to_obj(element))
    levels = {value.folder: value.level for value in row.permissions}
    assert levels["tasks"] is None and levels["notes"] == "None"
    assert row.view_private_items is None and row.receive_meeting_copies is None


def test_missing_permission_element_remains_unreported():
    element = _native().to_xml(version=VERSION)
    element.remove(element.find(f"{{{TNS}}}DelegatePermissions"))
    row = map_delegate(delegate_reader(account=_account())._elem_to_obj(element))
    assert all(value.level is None for value in row.permissions)


@pytest.mark.parametrize(
    "wire,expected", [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_xml_boolean_forms_preserve_the_explicit_reported_value(wire, expected):
    element = _native().to_xml(version=VERSION)
    element.find(f"{{{TNS}}}ViewPrivateItems").text = wire
    row = map_delegate(delegate_reader(account=_account())._elem_to_obj(element))
    assert row.view_private_items is expected


@pytest.mark.parametrize("wire", ["", "on", "off", "yes", "True", "secret-invalid"])
def test_invalid_wire_boolean_is_rejected_without_copying_the_value(wire):
    element = _native().to_xml(version=VERSION)
    element.find(f"{{{TNS}}}ViewPrivateItems").text = wire
    with pytest.raises(GUCmailError, match="invalid delegate metadata") as error:
        delegate_reader(account=_account())._elem_to_obj(element)
    assert "secret-invalid" not in str(error.value)


def test_empty_collection_is_valid_and_does_not_imply_no_folder_sharing():
    gateway = SimpleNamespace(list_delegates=lambda: ())
    page = DelegateService(gateway).list()
    assert page.entries == () and page.total == 0 and page.next_offset is None


def test_whole_collection_bound_stops_consumption_and_closes_generator():
    consumed = []
    closed = []

    def entries():
        try:
            for index in range(2000):
                consumed.append(index)
                yield _native(index)
        finally:
            closed.append(True)

    with pytest.raises(UnsupportedFeatureError, match="1000-entry"):
        map_delegate_collection(entries())
    assert len(consumed) == 1001 and closed == [True]


@pytest.mark.parametrize(
    "limit,offset", [(0, 0), (1001, 0), (1, -1), (1, 1001), (True, 0), (1, False)]
)
def test_invalid_windows_do_not_resolve_gateway(limit, offset):
    def forbidden():
        raise AssertionError("Invalid pagination must not contact Exchange")

    with pytest.raises(ConfigurationError):
        DelegateService(SimpleNamespace(list_delegates=forbidden)).list(limit=limit, offset=offset)


def test_local_pagination_reports_total_and_next_offset():
    entries = tuple(map_delegate(_native(i)) for i in range(5))
    page = DelegateService(SimpleNamespace(list_delegates=lambda: entries)).list(limit=2, offset=2)
    assert page.total == 5 and page.next_offset == 4
    assert [row.email_address for row in page.entries] == [
        "delegate2@example.invalid",
        "delegate3@example.invalid",
    ]
    assert page.pagination == "local_slice_of_single_ews_collection"


@pytest.mark.parametrize(
    "malformed",
    [
        SimpleNamespace(user_id=None),
        SimpleNamespace(user_id=SimpleNamespace(display_name="Only a name")),
        SimpleNamespace(user_id=SimpleNamespace(primary_smtp_address="x" * 1025)),
        SimpleNamespace(user_id=SimpleNamespace(sid="S-1"), view_private_items="false"),
    ],
)
def test_malformed_metadata_fails_without_echoing_provider_data(malformed):
    with pytest.raises(GUCmailError, match="invalid delegate metadata") as error:
        map_delegate(malformed)
    assert "x" * 50 not in str(error.value)


@pytest.mark.parametrize(
    "error,expected",
    [
        (ErrorAccessDenied("sensitive provider details"), UnsupportedFeatureError),
        (ErrorServerBusy("sensitive provider details"), ThrottledError),
        (TimeoutError("sensitive provider details"), GUCmailError),
    ],
)
def test_failure_is_single_read_without_initial_retry_or_sensitive_diagnostics(error, expected):
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        raise error

    gateway = EWSGateway(
        AppConfig(),
        account_factory=_account,
        factory_overrides={
            "GetDelegate": lambda **kwargs: SimpleNamespace(call=call),
        },
    )
    with pytest.raises(expected) as raised:
        gateway.list_delegates()
    assert len(calls) == 1 and calls[0] == {"user_ids": None, "include_permissions": True}
    assert "sensitive provider details" not in str(raised.value)
    assert raised.value.context.mutation_uncertain is False


def test_registry_exposes_only_read_with_unprobed_server_and_account_axes():
    registry = CapabilityRegistry()
    capability = registry.get("GetDelegate")
    assert capability.implementation_status is ImplementationStatus.IMPLEMENTED
    assert capability.safety is SafetyClass.READ_ONLY and capability.dry_run is False
    assert capability.verification is VerificationLevel.FIXTURE_TESTED
    assert capability.server_support is ServerSupport.NOT_PROBED
    assert capability.account_access is AccountAccess.NOT_PROBED
    for operation in ("AddDelegate", "UpdateDelegate", "RemoveDelegate", "UpdateFolderPermissions"):
        assert registry.get(operation).implementation_status is ImplementationStatus.NOT_IMPLEMENTED
    command = CORE_COMMANDS.get("ews.delegates")
    assert command.cli_path == ("ews", "delegates") and command.requires_connection
    assert not command.offline and not command.changes_server and not command.changes_local
    assert not command.confirmation_required


class Gateway(FakeGateway):
    def __init__(self, count=5):
        self.entries = tuple(map_delegate(_native(index)) for index in range(count))
        self.reads = 0
        self.thread_ids = []

    def list_delegates(self):
        self.reads += 1
        self.thread_ids.append(get_ident())
        return self.entries


def test_cli_json_human_help_and_bad_arguments_share_read_only_service(monkeypatch, tmp_path):
    gateway = Gateway()
    monkeypatch.setattr(cli, "_gateway", lambda *args, **kwargs: gateway)
    runner = CliRunner()
    env = _environment(tmp_path)
    assert runner.invoke(cli.app, ["ews", "delegates", "--help"], env=env).exit_code == 0
    assert gateway.reads == 0
    bad = runner.invoke(cli.app, ["ews", "delegates", "--limit", "1001"], env=env)
    assert bad.exit_code == 2 and gateway.reads == 0
    result = runner.invoke(
        cli.app,
        ["--json", "--non-interactive", "ews", "delegates", "--limit", "2", "--offset", "2"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["total"] == 5 and data["next_offset"] == 4
    assert data["entries"][0]["email_address"] == "delegate2@example.invalid"
    result = runner.invoke(cli.app, ["--no-color", "ews", "delegates"], env=env)
    assert result.exit_code == 0 and "Synthetic delegate" in result.output
    assert gateway.reads == 2


class Harness(App):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def on_mount(self):
        self.push_screen(DelegatesScreen(self.service, page_size=2))


async def _loaded(pilot, screen):
    for _ in range(50):
        await pilot.pause(0.05)
        if screen.view_state in {ViewState.READY, ViewState.ERROR}:
            return
    raise AssertionError("Delegate view did not finish loading")


async def _tab_to(pilot, app, identifier):
    # Actual keyboard traversal; no focus() or synthetic Button.press().
    for _ in range(25):
        await pilot.press("tab")
        if app.focused is not None and app.focused.id == identifier:
            return
    raise AssertionError(f"Keyboard could not reach {identifier}")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_keyboard_details_local_pages_and_refresh_do_not_write(size):
    gateway = Gateway()
    ui_thread = get_ident()
    app = Harness(DelegateService(gateway))
    async with app.run_test(size=size) as pilot:
        screen = app.screen
        await _loaded(pilot, screen)
        assert gateway.reads == 1 and gateway.thread_ids[0] != ui_thread
        table = screen.query_one("#delegates-table", DataTable)
        assert table.row_count == 2
        assert "Calendar: Editor" in str(screen.query_one("#delegates-details", Static).content)
        await _tab_to(pilot, app, "delegates-next")
        await pilot.press("enter")
        await pilot.pause()
        assert screen.page_offset == 2 and gateway.reads == 1
        assert "delegate2@example.invalid" in str(
            screen.query_one("#delegates-details", Static).content
        )
        await _tab_to(pilot, app, "delegates-next")
        await pilot.press("enter")
        await pilot.pause()
        assert screen.page_offset == 4 and table.row_count == 1
        assert screen.query_one("#delegates-next", Button).disabled
        await _tab_to(pilot, app, "delegates-previous")
        await pilot.press("enter")
        await pilot.pause()
        assert screen.page_offset == 2 and gateway.reads == 1
        await pilot.press("r")
        await _loaded(pilot, screen)
        assert screen.page_offset == 0 and gateway.reads == 2
        assert all(
            "delete" not in (button.id or "") and "edit" not in (button.id or "")
            for button in screen.query(Button)
        )
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DelegatesScreen)


@pytest.mark.asyncio
async def test_reader_failure_has_visible_manual_retry_without_claiming_empty_mailbox():
    class Denied(Gateway):
        def list_delegates(self):
            self.reads += 1
            raise UnsupportedFeatureError(
                "Exchange does not permit reading delegates for this account."
            )

    gateway = Denied()
    app = Harness(DelegateService(gateway))
    async with app.run_test() as pilot:
        screen = app.screen
        await _loaded(pilot, screen)
        assert screen.view_state is ViewState.ERROR and gateway.reads == 1
        assert "does not permit" in str(screen.query_one("#area-state", Static).content)
        assert screen.query_one("#area-retry", Button).display
        assert "no delegates" not in str(screen.query_one("#delegates-details", Static).content)


@pytest.mark.asyncio
async def test_cancelled_slow_read_keeps_ui_responsive_and_rejects_late_result():
    entered = Event()
    release = Event()

    class Slow(Gateway):
        def list_delegates(self):
            entered.set()
            assert release.wait(10)
            return self.entries

    app = Harness(DelegateService(Slow()))
    try:
        async with app.run_test() as pilot:
            screen = app.screen
            for _ in range(30):
                await pilot.pause(0.05)
                if entered.is_set():
                    break
            assert entered.is_set() and screen.view_state is ViewState.LOADING
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert screen.view_state is ViewState.CANCELLED
            release.set()
            await pilot.pause(0.2)
            assert screen.entries == () and screen.view_state is ViewState.CANCELLED
    finally:
        release.set()


@pytest.mark.asyncio
async def test_main_app_binds_delegate_action_and_offline_gate_without_early_reads(tmp_path):
    gateway = Gateway()
    app = GUCmailApp(context=_context(tmp_path), gateway=gateway)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert gateway.reads == 0
        assert "ews.delegates" in app._palette_handlers()
        app._open_area("delegates")
        await pilot.pause()
        screen = app.screen
        await _loaded(pilot, screen)
        assert isinstance(screen, DelegatesScreen) and gateway.reads == 1
        await pilot.press("escape")
        await pilot.pause()
        app.offline = True
        app._open_area("delegates")
        await pilot.pause()
        assert not isinstance(app.screen, DelegatesScreen) and gateway.reads == 1


@pytest.mark.asyncio
async def test_normal_palette_keyboard_route_resolves_and_reads_once(tmp_path):
    context = _context(tmp_path)
    context.config = replace(context.config, mode="advanced")
    gateway = Gateway()
    app = GUCmailApp(context=context, gateway=gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press(*"Show Mailbox Delegates")
        await pilot.pause()
        assert app.screen._interpretation.invocation.command_id == "ews.delegates"
        assert gateway.reads == 0
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, DelegatesScreen) and app.screen.view_state is ViewState.READY:
                break
        assert isinstance(app.screen, DelegatesScreen)
        assert app.screen.view_state is ViewState.READY and gateway.reads == 1
