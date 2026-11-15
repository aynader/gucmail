from __future__ import annotations

import json
from dataclasses import replace

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import Address, ContactInfo, to_primitive
from gucmail.services.contacts import ContactsService, find_duplicate_groups
from gucmail.services.contacts_display import (
    CONTACT_DISPLAY_SETTINGS,
    ContactsDisplayPreferences,
)
from gucmail.services.interchange import export_vcards
from gucmail.services.pim import PimPage
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import ContactEditorScreen, ContactsScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_pim import ContactsMemoryGateway
from test_settings_secondpass import _context
from waiting import press_button


def contacts():
    return (
        ContactInfo(
            "a",
            "A Alias",
            given_name="Zoe",
            family_name="Alpha",
            company="Zeta",
            emails=(Address("shared@example.test"),),
            categories=("synthetic",),
        ),
        ContactInfo(
            "b",
            "Z Alias",
            given_name="Ada",
            family_name="Zulu",
            company="Alpha",
            emails=(Address("shared@example.test"),),
            categories=("synthetic",),
        ),
        ContactInfo("c", "Fallback Only", company="Beta", categories=("synthetic",)),
    )


class ReadOnlyContacts(ContactsMemoryGateway):
    def __init__(self):
        super().__init__(contacts())
        self.calls = []

    def list_contacts(self, *, folder_reference, limit):
        self.calls.append(("list", folder_reference, limit))
        return tuple(self.contacts.values())[:limit]

    def search_contacts_page(self, query, *, folder_reference, limit, offset=0):
        self.calls.append(("page", query, folder_reference, limit, offset))
        return PimPage(
            tuple(self.contacts.values())[:limit], offset, 7, offset + 7, "Synthetic page."
        )

    def get_contact(self, reference):
        self.calls.append(("get", reference))
        return super().get_contact(reference)


def configure(context, sort="display_name", format="stored", threshold=70):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in zip(CONTACT_DISPLAY_SETTINGS, (sort, format, threshold), strict=True):
        session.set(key, value)
    session.apply()
    return service


def invoke(root, arguments, *, human=False, extra_env=None):
    env = _environment(root)
    env.update(extra_env or {})
    return CliRunner().invoke(
        cli.app,
        ["--no-color" if human else "--json", "--non-interactive", "contacts", *arguments],
        env=env,
    )


def test_account_preferences_stage_cancel_apply_reopen_transfer_reset_undo_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = configure(context, "company", "family_given", 81)
    expected = ContactsDisplayPreferences("company", "family_given", 81)
    assert ContactsDisplayPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    service.begin().set("contacts.display_name_format", "given_family").cancel()
    assert context.paths.config_file.read_bytes() == before
    assert ContactsDisplayPreferences.from_settings(settings_for(context)) == expected
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert ContactsDisplayPreferences.from_settings(other) == ContactsDisplayPreferences()
    target = settings_for(_context(tmp_path / "transfer"))
    target.apply_import(target.plan_import(service.export(setting_ids=CONTACT_DISPLAY_SETTINGS)))
    assert ContactsDisplayPreferences.from_settings(target) == expected
    for key in CONTACT_DISPLAY_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert ContactsDisplayPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__SORT_ORDER", "family_name")
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__DISPLAY_NAME_FORMAT", "given_family")
    monkeypatch.setenv("GUCMAIL_SETTING_CONTACTS__DUPLICATE_THRESHOLD", "80")
    assert ContactsDisplayPreferences.from_settings(service) == ContactsDisplayPreferences(
        "family_name", "given_family", 80
    )
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("contacts.sort_order", "birthday"),
        ("contacts.display_name_format", "nickname"),
        ("contacts.duplicate_threshold", 0),
        ("contacts.duplicate_threshold", 101),
        ("contacts.duplicate_threshold", "not-a-number"),
    ],
)
def test_invalid_preferences_preserve_saved_bytes(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, value)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize("format", ["stored", "given_family", "family_given"])
@pytest.mark.parametrize("sort", ["display_name", "family_name", "company"])
def test_cli_bare_list_and_search_use_saved_page_sort_without_changing_metadata(
    tmp_path, monkeypatch, sort, format
):
    context = _context(tmp_path)
    configure(context, sort, format)
    gateway = ReadOnlyContacts()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    original = to_primitive(gateway.contacts)
    exported = export_vcards(gateway.contacts.values())
    before = context.paths.config_file.read_bytes()
    expected = {
        "company": ["b", "c", "a"],
        "family_name": ["c", "a", "b"],
        "display_name": ["b", "c", "a"] if format == "given_family" else ["a", "c", "b"],
    }[sort]
    result = invoke(tmp_path, [])
    assert result.exit_code == 0, result.output
    assert [c["reference"] for c in json.loads(result.output)["data"]] == expected
    for options in (["list"], ["search", "--category", "synthetic"]):
        result = invoke(tmp_path, [*options, "--limit", "3", "--offset", "11", "--page"])
        assert result.exit_code == 0, result.output
        page = json.loads(result.output)["data"]
        assert [c["reference"] for c in page["items"]] == expected
        assert (page["offset"], page["scanned"], page["next_offset"], page["residual"]) == (
            11,
            7,
            18,
            "Synthetic page.",
        )
        assert all(
            c["display_name"] == original[c["reference"]]["display_name"] for c in page["items"]
        )
    assert len(gateway.calls) == 3
    assert gateway.calls[0] == ("list", None, 200)
    assert all(call[-2:] == (3, 11) for call in gateway.calls[1:])
    assert to_primitive(gateway.contacts) == original
    assert export_vcards(gateway.contacts.values()) == exported
    assert context.paths.config_file.read_bytes() == before


def test_human_name_format_fallback_sanitization_original_json_and_fuzzy_order(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    configure(context, "company", "family_given")
    gateway = ReadOnlyContacts()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["show", "b"], human=True)
    assert result.exit_code == 0 and "Zulu, Ada" in result.output and "Z Alias" in result.output
    result = invoke(tmp_path, ["list", "--limit", "3"], human=True)
    assert result.exit_code == 0 and "Zulu, Ada" in result.output and "Alpha, Zoe" in result.output
    result = invoke(tmp_path, ["show", "b"])
    assert json.loads(result.output)["data"]["display_name"] == "Z Alias"
    result = invoke(tmp_path, ["search", "A Alias", "--fuzzy", "--threshold", "0", "--page"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["items"][0]["item"]["reference"] == "a"
    assert data["items"][0]["exact"] and data["scanned"] == 7 and data["next_offset"] == 7
    policy = ContactsDisplayPreferences(display_name_format="family_given")
    assert (
        policy.label(ContactInfo("missing", "Stored", given_name=" ", family_name=None)) == "Stored"
    )
    assert (
        policy.label(ContactInfo("unicode", "Stored", given_name="أحمد", family_name="مثال"))
        == "مثال, أحمد"
    )
    hostile = replace(gateway.contacts["a"], given_name="Zoe\x1b[31m", family_name="Alpha\x00")
    assert "\x1b" not in policy.label(hostile) and "\x00" not in policy.label(hostile)
    assert hostile.given_name.endswith("\x1b[31m")


def test_cli_duplicate_threshold_cutoff_explicit_override_and_no_automatic_mutation(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = configure(context, threshold=80)
    gateway = ReadOnlyContacts()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    before = to_primitive(gateway.contacts)
    result = invoke(tmp_path, ["duplicates"])
    assert result.exit_code == 0, result.output
    groups = json.loads(result.output)["data"]
    assert len(groups) == 1 and groups[0]["candidates"][0]["score"] == 80
    assert groups[0]["candidates"][0]["reasons"] == ["shared email address"]
    service.set("contacts.duplicate_threshold", 81)
    result = invoke(tmp_path, ["duplicates"])
    assert result.exit_code == 0 and json.loads(result.output)["data"] == []
    saved = context.paths.config_file.read_bytes()
    result = invoke(
        tmp_path,
        ["duplicates", "--threshold", "80"],
        extra_env={"GUCMAIL_SETTING_CONTACTS__DUPLICATE_THRESHOLD": "invalid"},
    )
    assert result.exit_code == 0 and len(json.loads(result.output)["data"]) == 1
    assert context.paths.config_file.read_bytes() == saved
    assert len(gateway.calls) == 3 and all(c[0] == "list" for c in gateway.calls)
    assert gateway.updates == gateway.deletes == [] and to_primitive(gateway.contacts) == before


@pytest.mark.parametrize("value", [0, 101, True, 1.5])
def test_invalid_duplicate_threshold_precedes_gateway_and_iterable_access(value):
    class NoAccess:
        def list_contacts(self, **kwargs):
            pytest.fail("Invalid duplicate threshold must not access the provider.")

    def contacts_generator():
        pytest.fail("Invalid duplicate threshold must not enumerate supplied contacts.")
        yield ContactInfo("unreachable", "Unused")

    with pytest.raises(ConfigurationError, match="integer between 1 and 100"):
        ContactsService(NoAccess()).duplicates(threshold=value)
    with pytest.raises(ConfigurationError, match="integer between 1 and 100"):
        find_duplicate_groups(contacts_generator(), threshold=value)


def test_cli_invalid_preferences_or_arguments_do_not_resolve_gateway(tmp_path, monkeypatch):
    _context(tmp_path)
    calls = []
    monkeypatch.setattr(cli.core, "gateway", lambda *_: calls.append(True))
    for arguments in (
        ["duplicates", "--threshold", "0"],
        ["duplicates", "--threshold", "101"],
        ["search"],
    ):
        assert invoke(tmp_path, arguments).exit_code != 0
    for key in CONTACT_DISPLAY_SETTINGS:
        assert (
            invoke(
                tmp_path,
                ["list"],
                extra_env={"GUCMAIL_SETTING_" + key.upper().replace(".", "__"): "invalid"},
            ).exit_code
            != 0
        )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_cancel_apply_relabel_selection_duplicates_and_original_editor(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    gateway = ReadOnlyContacts()
    before_contacts = to_primitive(gateway.contacts)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("contacts")
        await wait_for(
            pilot, lambda: isinstance(app.screen, ContactsScreen) and app.screen.is_mounted
        )
        contacts_screen = app.screen
        await wait_for(
            pilot,
            lambda: len(contacts_screen._contacts) == 3 and not contacts_screen._operation_active,
        )
        table = contacts_screen.query_one("#contacts-table", DataTable)
        table.focus()
        await wait_for(pilot, lambda: table.has_focus)
        await pilot.press("down", "down")
        await wait_for(pilot, lambda: contacts_screen._highlighted == "b")
        await pilot.press("space")
        await wait_for(pilot, lambda: contacts_screen._selected == {"b"})
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                contacts_screen._complete_detail_reference == "b"
                and not contacts_screen._operation_active
            ),
        )
        before = context.paths.config_file.read_bytes()
        calls = len(gateway.calls)
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen

        async def activate(identifier):
            await press_button(pilot, screen.query_one(identifier, Button))

        async def stage():
            for key, value in zip(
                CONTACT_DISPLAY_SETTINGS, ("company", "family_given", 81), strict=True
            ):
                assert screen.action_select_setting(key)
                await pilot.pause()
                if isinstance(value, int):
                    screen.query_one("#settings-editor", Input).value = str(value)
                else:
                    await wait_for(pilot, lambda: bool(screen.query("#settings-choice #label")))
                    screen.query_one("#settings-choice", Select).value = value
                await activate("#settings-stage")
                await wait_for(pilot, lambda key=key: key in screen._staged)

        await stage()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged)
        assert context.paths.config_file.read_bytes() == before
        assert contacts_screen._display == ContactsDisplayPreferences()
        await stage()
        await activate("#settings-apply")
        await wait_for(
            pilot,
            lambda: (
                not screen._staged
                and not screen.mutation_pending
                and contacts_screen._display.duplicate_threshold == 81
            ),
        )
        screen.action_close()
        await wait_for(pilot, lambda: app.screen is contacts_screen)
        assert [c.reference for c in contacts_screen._contacts] == ["b", "c", "a"]
        assert contacts_screen._highlighted == "b" and contacts_screen._selected == {"b"}
        assert str(table.get_cell("b", "name")) == "Zulu, Ada"
        assert "Stored name: Z Alias" in str(
            contacts_screen.query_one("#contacts-detail", Static).render()
        )
        assert len(gateway.calls) == calls
        contacts_screen.action_find_duplicates()
        assert "threshold 81" in str(contacts_screen.query_one("#area-state", Static).render())
        contacts_screen.action_edit()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ContactEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        await wait_for(pilot, lambda: editor.query_one("#contact-edit-name", Input).is_mounted)
        assert editor.query_one("#contact-edit-name", Input).value == "Z Alias"
        settings_for(context).set("contacts.display_name_format", "given_family")
        assert editor.query_one("#contact-edit-name", Input).value == "Z Alias"
        editor.action_cancel()
        await pilot.pause()
        contacts_screen.action_refresh()
        await wait_for(
            pilot,
            lambda: (
                not contacts_screen._operation_active
                and str(table.get_cell("b", "name")) == "Ada Zulu"
            ),
        )
        assert contacts_screen._selected == {"b"} and contacts_screen._highlighted == "b"
        contacts_screen.action_close()
        await pilot.pause()
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
        assert app.screen._display == ContactsDisplayPreferences("company", "given_family", 81)
        assert gateway.updates == gateway.deletes == []
        assert to_primitive(gateway.contacts) == before_contacts and context._gateway is None
