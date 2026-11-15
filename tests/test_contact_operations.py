from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.config import AppConfig
from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import ConfigurationError, LocalStateError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import Address
from gucmail.services.contact_history import MAX_HISTORY_ROWS, ContactHistoryIndex
from gucmail.services.contact_preferences import (
    CONTACT_OPERATION_SETTINGS,
    ContactOperationPreferences,
)
from gucmail.services.contacts import ContactsService, CorrespondenceDirection, CorrespondenceEvent
from gucmail.services.gateway import DeferredGateway
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.contact_history_screen import ContactHistoryScreen
from gucmail.tui.pim_screens import ContactsScreen, ContactsServiceBackend, CorrespondentsScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_contacts_display_preferences import ReadOnlyContacts
from test_settings_secondpass import _context
from waiting import press_button


class MetadataHistory:
    def __init__(self):
        self.calls = []

    def list_correspondence(self, *, since, limit):
        self.calls.append((since, limit))
        return (
            CorrespondenceEvent(
                Address("ada@example.test"),
                datetime(2026, 10, 4, tzinfo=UTC),
                CorrespondenceDirection.INBOUND,
                "message-in",
            ),
            CorrespondenceEvent(
                Address("ada@example.test"),
                datetime(2026, 10, 3, tzinfo=UTC),
                CorrespondenceDirection.OUTBOUND,
                "message-out",
            ),
        )

    def list_messages(self, **kwargs):
        raise AssertionError("Correspondent reports must use the body-free metadata projection")

    def show_message(self, reference):
        raise AssertionError("A correspondent report must never load a message body")


@pytest.mark.parametrize("kind", ["recent", "frequent"])
def test_cli_correspondent_reports_use_bounded_metadata_only(tmp_path, monkeypatch, kind):
    gateway = MetadataHistory()
    monkeypatch.setattr(cli.core, "gateway", lambda ctx: gateway)
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "recipients", kind, "--scan", "7"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0]["address"]["email"] == "ada@example.test"
    assert gateway.calls == [(None, 7)]


@pytest.mark.parametrize(
    "key,args",
    [
        ("contacts.frequent_correspondents", ["recipients", "frequent"]),
        ("contacts.directory_lookup", ["directory", "search", "Ada"]),
        ("contacts.directory_lookup", ["directory", "resolve", "Ada"]),
        ("contacts.duplicate_detection", ["contacts", "duplicates"]),
        ("contacts.correspondence_history", ["recipients", "recent"]),
        ("contacts.correspondence_history", ["recipients", "frequent"]),
        ("contacts.correspondence_history", ["contacts", "correspondence", "contact-1"]),
    ],
)
def test_disabled_cli_operations_do_not_resolve_gateway(tmp_path, monkeypatch, key, args):
    context = _context(tmp_path)
    settings_for(context).set(key, False)
    calls = []
    monkeypatch.setattr(cli.core, "gateway", lambda ctx: calls.append("gateway"))
    result = CliRunner().invoke(
        cli.app, ["--json", "--non-interactive", *args], env=_environment(tmp_path)
    )
    assert result.exit_code != 0 and "disabled in Contacts settings" in result.output
    assert calls == []
    context.close()


def test_operation_preferences_persist_cancel_transfer_account_scope_reset_and_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = settings_for(context, revisions=InMemoryRevisionStore())
    expected = ContactOperationPreferences(False, False, False, False, True, 7)
    session = service.begin()
    for key in CONTACT_OPERATION_SETTINGS:
        session.set(key, getattr(expected, key.split(".")[1]))
    session.apply()
    assert ContactOperationPreferences.from_settings(settings_for(context)) == expected
    before = context.paths.config_file.read_bytes()
    service.begin().set("contacts.directory_lookup", True).cancel()
    assert context.paths.config_file.read_bytes() == before
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert ContactOperationPreferences.from_settings(other) == ContactOperationPreferences()
    target = settings_for(_context(tmp_path / "transfer"))
    target.apply_import(target.plan_import(service.export(setting_ids=CONTACT_OPERATION_SETTINGS)))
    assert ContactOperationPreferences.from_settings(target) == expected
    service.reset("contacts.history_retention_days")
    assert ContactOperationPreferences.from_settings(service).history_retention_days == 30
    service.undo()
    assert ContactOperationPreferences.from_settings(service) == expected
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__DIRECTORY_LOOKUP", "true")
    assert ContactOperationPreferences.from_settings(service).directory_lookup
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        service.set("contacts.history_retention_days", 0)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize("operation", ["directory", "duplicates", "history", "frequent"])
def test_shared_service_disabled_operations_precede_provider_and_supplied_iterable(operation):
    gateway = DeferredGateway(lambda: pytest.fail("Disabled operation resolved the provider"))
    preferences = ContactOperationPreferences(False, False, False, False)
    service = ContactsService(
        gateway, correspondence_gateway=gateway, operation_preferences=lambda: preferences
    )

    def observations():
        pytest.fail("Disabled operation consumed supplied observations")
        yield from ()

    with pytest.raises(ConfigurationError, match="disabled"):
        if operation == "directory":
            service.resolve_directory("Ada")
        elif operation == "duplicates":
            service.duplicates(observations())
        elif operation == "history":
            service.recent_correspondents(observations())
        else:
            service.frequent_correspondents(observations())


@pytest.mark.parametrize("query", ["Ada\nExample", "Ada\x00", "Ada\x1b[31m", "x" * 513])
def test_invalid_directory_query_is_rejected_before_provider(query):
    service = ContactsService(
        DeferredGateway(lambda: pytest.fail("Invalid query reached provider"))
    )
    with pytest.raises(ConfigurationError, match="invalid"):
        service.search_directory(query)


def test_metadata_since_limit_own_exclusion_frequency_off_does_not_disable_recent(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    settings_for(context).set("contacts.frequent_correspondents", False)
    gateway = MetadataHistory()
    own = Address(context.config.email)
    original = gateway.list_correspondence

    def read(**kwargs):
        return (
            *original(**kwargs),
            CorrespondenceEvent(own, datetime.now(UTC), CorrespondenceDirection.OUTBOUND, "own"),
        )

    monkeypatch.setattr(gateway, "list_correspondence", read)
    monkeypatch.setattr(cli.core, "gateway", lambda ctx: gateway)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "--non-interactive",
            "recipients",
            "recent",
            "--scan",
            "1",
            "--since",
            "2026-10-03T00:00:00+00:00",
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert [row["address"]["email"] for row in json.loads(result.output)["data"]] == [
        "ada@example.test"
    ]
    assert gateway.calls == [(datetime(2026, 10, 3, tzinfo=UTC), 1)]
    assert "no message bodies" in result.output


def observation(reference="message-1", address="ada@example.test", when=None):
    return CorrespondenceEvent(
        Address(address, "Name deliberately not retained"),
        when or datetime.now(UTC),
        CorrespondenceDirection.OUTBOUND,
        reference,
    )


def test_history_index_restart_dedup_timezone_query_bound_account_isolation_and_clear(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = StateStore(path, account_key="account-one")
    index = ContactHistoryIndex(state)
    event = observation()
    index.record((event, event))
    reopened = ContactHistoryIndex(StateStore(path, account_key="account-one"))
    result = reopened.query("ADA@example.test", limit=1)
    assert len(result) == 1 and result[0].message_reference == "message-1"
    assert result[0].address.name is None and result[0].occurred_at.tzinfo == UTC
    other = ContactHistoryIndex(StateStore(path, account_key="account-two"))
    other.record((observation("other-account"),))
    assert reopened.status()["observations"] == other.status()["observations"] == 1
    assert reopened.clear() == 1 and reopened.query() == ()
    assert other.query()[0].message_reference == "other-account"
    with pytest.raises(ConfigurationError):
        index.query("ada@example.test' OR 1=1 --")
    assert other.status()["observations"] == 1


def test_history_expiry_row_cap_clearing_cache_and_migration_preserve_unrelated_state(
    tmp_path, monkeypatch
):
    import gucmail.state as state_module

    state = StateStore(tmp_path / "state.sqlite3", account_key="synthetic")
    with monkeypatch.context() as legacy:
        legacy.setattr(state_module, "SCHEMA_VERSION", 9)
        state.initialize()
        with state.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES ('synthetic-unrelated','retained')"
            )
    reopened = StateStore(state.path, account_key="synthetic")
    index = ContactHistoryIndex(reopened)
    now = datetime.now(UTC)
    index.record(
        (
            observation(f"message-{i}", when=now - timedelta(seconds=i))
            for i in range(MAX_HISTORY_ROWS)
        ),
        now=now,
    )
    index.record((observation("newest", when=now + timedelta(seconds=1)),), now=now)
    assert index.status()["observations"] == MAX_HISTORY_ROWS
    assert index.query(limit=1)[0].message_reference == "newest"
    index.record((observation("expired", when=now - timedelta(days=31)),), now=now)
    assert index.status()["observations"] == MAX_HISTORY_ROWS
    index.prune(retention_days=1, now=now + timedelta(days=2))
    assert index.query() == ()
    index.record((observation("after-expiry"),))
    preview = reopened.disposable_cache_counts()
    cleared = reopened.clear_disposable_cache()
    assert cleared == preview
    assert cleared["contact_history"] == 1
    with reopened.connection() as connection:
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key='synthetic-unrelated'"
            ).fetchone()[0]
            == "retained"
        )
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )


def test_history_invalid_metadata_is_atomic_and_corruption_is_recoverable(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    index = ContactHistoryIndex(state)
    with pytest.raises(ConfigurationError):
        index.record((observation(), observation("bad\x1b")))
    assert index.query() == ()
    index.record((observation(),))
    with state.transaction(immediate=True) as connection:
        connection.execute("UPDATE contact_history SET occurred_at='9999-invalid'")
    with pytest.raises(LocalStateError, match="clear it"):
        index.query()
    assert index.clear() == 1


def test_index_opt_in_disable_during_request_and_local_failure_leave_report_usable(tmp_path):
    gateway = MetadataHistory()
    index = ContactHistoryIndex(StateStore(tmp_path / "state.sqlite3"))
    preferences = ContactOperationPreferences()
    service = ContactsService(
        gateway,
        correspondence_gateway=gateway,
        operation_preferences=lambda: preferences,
        history_index=index,
    )
    events = service.read_correspondence(limit=7)
    assert len(events) == 2 and index.status()["observations"] == 0
    preferences = replace(preferences, history_indexing=True)
    # Use fresh timestamps so this test does not depend on the fixture's historical date.
    original = gateway.list_correspondence
    gateway.list_correspondence = lambda **kw: tuple(
        replace(e, occurred_at=datetime.now(UTC)) for e in original(**kw)
    )
    service.read_correspondence(limit=7)
    assert index.status()["observations"] == 2
    index.clear()

    def disable_in_request(**kwargs):
        nonlocal preferences
        preferences = replace(preferences, history_indexing=False)
        return original(**kwargs)

    gateway.list_correspondence = disable_in_request
    preferences = replace(preferences, history_indexing=True)
    service.read_correspondence(limit=7)
    assert index.status()["observations"] == 0
    gateway.list_correspondence = original
    preferences = replace(preferences, history_indexing=True)
    warnings = []
    service.index_warning = warnings.append

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic secret must never appear in warning")

    index.record = locked
    assert len(service.read_correspondence(limit=7)) == 2
    assert len(warnings) == 1 and "secret" not in warnings[0]


def test_backend_privacy_guard_precedes_contact_address_and_context_reads():
    gateway = DeferredGateway(lambda: pytest.fail("Disabled history resolved provider"))
    preferences = ContactOperationPreferences(correspondence_history=False)
    backend = ContactsServiceBackend(
        ContactsService(
            gateway, correspondence_gateway=gateway, operation_preferences=lambda: preferences
        )
    )
    assert not backend.capabilities["correspondence"].available
    with pytest.raises(ConfigurationError, match="disabled"):
        backend.correspondence_history(
            ("contact-one",), limit=100, cancel=Event(), progress=lambda p: None
        )
    with pytest.raises(ConfigurationError, match="disabled"):
        backend.list_correspondents("recent", 100, cancel=Event(), progress=lambda p: None)


def test_default_tui_history_window_fits_adapter_maximum():
    gateway = MetadataHistory()
    service = ContactsService(gateway, correspondence_gateway=gateway)
    assert service.recent_correspondents(limit=100)
    assert gateway.calls == [(None, 200)]


def test_local_history_cli_never_connects_and_clear_requires_confirmation(tmp_path, monkeypatch):
    context = _context(tmp_path)
    service = settings_for(context)
    index = ContactHistoryIndex(context.state)
    index.record((observation(),))
    monkeypatch.setattr(
        cli.core, "gateway", lambda ctx: pytest.fail("Local index resolved gateway")
    )
    runner = CliRunner()

    def invoke(*arguments):
        return runner.invoke(
            cli.app, ["--json", "--non-interactive", *arguments], env=_environment(tmp_path)
        )

    status = invoke("contacts", "history-index")
    assert status.exit_code == 0 and json.loads(status.output)["data"]["observations"] == 1
    assert "ada@example.test" not in status.output
    assert invoke("contacts", "history-index", "--show").exit_code != 0
    service.set("contacts.history_indexing", True)
    shown = invoke("contacts", "history-index", "--show", "--address", "ADA@example.test")
    assert shown.exit_code == 0 and len(json.loads(shown.output)["data"]) == 1
    refused = invoke("contacts", "history-index", "--clear")
    assert refused.exit_code != 0 and index.status()["observations"] == 1
    cleared = invoke("--yes", "contacts", "history-index", "--clear")
    assert cleared.exit_code == 0 and json.loads(cleared.output)["data"]["cleared"] == 1
    assert ContactOperationPreferences.from_settings(service).history_indexing


class HistoryContacts(ReadOnlyContacts):
    def search_directory(self, query, *, limit):
        self.calls.append(("directory", query, limit))
        return (Address("ada@example.test", "Synthetic Ada"),)

    def list_correspondence(self, *, since, limit):
        self.calls.append(("history", since, limit))
        return (observation("in", "shared@example.test"), observation("out", "ada@example.test"))


async def activate(pilot, screen, identifier):
    await press_button(pilot, screen.query_one(identifier, Button))


async def stage(pilot, screen, key, value):
    await wait_for(pilot, lambda: not screen._operation_active)
    assert screen.action_select_setting(key)
    await pilot.pause()
    if isinstance(value, bool):
        screen.query_one("#settings-choice", Select).value = "on" if value else "off"
    else:
        screen.query_one("#settings-editor", Input).value = str(value)
    await activate(pilot, screen, "#settings-stage")
    try:
        await wait_for(pilot, lambda: key in screen._staged and not screen._operation_active)
    except AssertionError as exc:
        raise AssertionError(
            {
                "target": key,
                "selected": screen._selected_setting_id,
                "focus": repr(screen.focused),
                "open": screen._editor_open,
                "choice": screen.query_one("#settings-choice", Select).value,
                "staged": screen._staged,
                "status": str(screen.query_one("#area-state", Static).content),
            }
        ) from exc


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_operation_controls_and_history_index_workflow(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    gateway = HistoryContacts()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("contacts")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ContactsScreen)
                and app.screen.is_mounted
                and len(app.screen._contacts) == 3
                and not app.screen._operation_active
            ),
        )
        contacts = app.screen
        calls = len(gateway.calls)
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        changes = dict(
            zip(CONTACT_OPERATION_SETTINGS, (False, False, False, False, True, 7), strict=True)
        )
        for key, value in changes.items():
            await stage(pilot, screen, key, value)
        await activate(pilot, screen, "#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        assert context.paths.config_file.read_bytes() == before
        assert contacts._operations == ContactOperationPreferences()
        for key, value in changes.items():
            await stage(pilot, screen, key, value)
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(
            pilot, lambda: app.screen is contacts and not contacts._operations.directory_lookup
        )
        assert len(gateway.calls) == calls
        for identifier in (
            "#contacts-directory",
            "#contacts-duplicates",
            "#contacts-correspondence",
            "#contacts-correspondence-mail",
        ):
            assert contacts.query_one(identifier, Button).disabled
        contacts.action_directory()
        contacts.action_find_duplicates()
        contacts.action_correspondence()
        contacts.action_open_correspondence()
        assert len(gateway.calls) == calls
        app._open_correspondents("frequency")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, CorrespondentsScreen)
                and app.screen.is_mounted
                and not app.screen._operation_active
            ),
        )
        await pilot.pause()
        assert "disabled" in str(app.screen.query_one("#area-state", Static).render())
        assert len(gateway.calls) == calls
        app.screen.action_close()
        await wait_for(pilot, lambda: app.screen is contacts)
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        for key in CONTACT_OPERATION_SETTINGS[:4]:
            await stage(pilot, screen, key, True)
        await activate(pilot, screen, "#settings-apply")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        screen.action_close()
        await wait_for(
            pilot, lambda: app.screen is contacts and contacts._operations.directory_lookup
        )
        contacts.query_one("#contacts-search", Input).value = "Ada"
        await activate(pilot, contacts, "#contacts-directory")
        await wait_for(
            pilot,
            lambda: (
                not contacts._operation_active and any(c[0] == "directory" for c in gateway.calls)
            ),
        )
        assert "Synthetic Ada" in str(contacts.query_one("#contacts-detail", Static).render())
        await activate(pilot, contacts, "#contacts-duplicates")
        await wait_for(pilot, lambda: contacts._selected == {"a", "b"})
        await activate(pilot, contacts, "#contacts-correspondence")
        await wait_for(
            pilot,
            lambda: (
                not contacts._operation_active and any(c[0] == "history" for c in gateway.calls)
            ),
        )
        assert ContactHistoryIndex(context.state).status()["observations"] == 2
        await activate(pilot, contacts, "#contacts-history-index")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ContactHistoryScreen)
                and app.screen.is_mounted
                and not app.screen._operation_active
            ),
        )
        history = app.screen
        await wait_for(
            pilot, lambda: history.query_one("#contact-history-table", DataTable).row_count == 2
        )
        await activate(pilot, history, "#contact-history-clear")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is history and not history._operation_active)
        assert ContactHistoryIndex(context.state).status()["observations"] == 2
        await activate(pilot, history, "#contact-history-clear")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await wait_for(
            pilot,
            lambda: (
                app.screen is history
                and not history._operation_active
                and history.query_one("#contact-history-table", DataTable).row_count == 0
            ),
        )
        assert ContactHistoryIndex(context.state).status()["observations"] == 0
        assert ContactOperationPreferences.from_settings(settings_for(context)).history_indexing
        assert gateway.updates == gateway.deletes == [] and context._gateway is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["recent", "frequency"])
async def test_correspondent_privacy_change_hides_existing_and_late_results(
    tmp_path, monkeypatch, order
):
    context = _context(tmp_path)
    gateway = HistoryContacts()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        app.offline = False
        app._open_correspondents(order)
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, CorrespondentsScreen)
                and app.screen.is_mounted
                and bool(app.screen._values)
                and not app.screen._operation_active
            ),
        )
        screen = app.screen
        old = tuple(screen._values.values())
        calls = len(gateway.calls)
        settings_for(context).set("contacts.correspondence_history", False)
        screen.on_screen_resume()
        screen._loaded(old)
        assert screen.query_one("#correspondents-table", DataTable).row_count == 0
        assert "shared@example.test" not in str(
            screen.query_one("#correspondents-detail", Static).render()
        )
        screen.action_open_mail()
        assert len(gateway.calls) == calls


@pytest.mark.asyncio
async def test_contact_privacy_change_rejects_late_directory_and_history_results(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    gateway = HistoryContacts()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        app.offline = False
        app._open_area("contacts")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ContactsScreen)
                and app.screen.is_mounted
                and bool(app.screen._contacts)
                and not app.screen._operation_active
            ),
        )
        screen = app.screen
        screen._directory_loaded((Address("directory-only@example.test"),))
        settings_for(context).set("contacts.directory_lookup", False)
        screen.on_screen_resume()
        screen._directory_loaded((Address("directory-only@example.test"),))
        assert "directory-only@example.test" not in str(
            screen.query_one("#contacts-detail", Static).render()
        )
        events = (observation("mail", "history-only@example.test"),)
        screen._correspondence_loaded(events)
        settings_for(context).set("contacts.correspondence_history", False)
        screen.on_screen_resume()
        screen._correspondence_loaded(events)
        assert "history-only@example.test" not in str(
            screen.query_one("#contacts-detail", Static).render()
        )


def test_ews_history_projection_pushes_since_and_limits_without_body_attachment_or_mutation(
    monkeypatch,
):
    gateway = EWSGateway(
        AppConfig(), account_factory=lambda: pytest.fail("Synthetic projection resolved account")
    )
    reads = []
    since = datetime.now(UTC) - timedelta(days=7)

    class Query:
        def filter(self, **filters):
            reads.append(("filter", filters))
            return self

        def only(self, *fields):
            assert not {"body", "text_body", "mime_content", "attachments", "is_read"}.intersection(
                fields
            )
            reads.append(("fields", fields))
            return self

        def order_by(self, value):
            reads.append(("order", value))
            return self

        def __getitem__(self, selection):
            assert selection.start == 0 and selection.stop == 7
            reads.append(("slice", selection.stop))
            return ()

    Query.__module__ = "exchangelib.queryset"
    monkeypatch.setattr(gateway, "_resolve_folder", lambda reference: SimpleNamespace(id=reference))
    monkeypatch.setattr(gateway, "_query_source", lambda folder: Query())
    monkeypatch.setattr(gateway, "_call", lambda name, action, **kwargs: action())
    assert gateway.list_correspondence(since=since, limit=7) == ()
    assert [row for row in reads if row[0] == "filter"] == [
        ("filter", {"datetime_received__gte": since}),
        ("filter", {"datetime_sent__gte": since}),
    ]
    assert [row for row in reads if row[0] == "slice"] == [("slice", 7), ("slice", 7)]
