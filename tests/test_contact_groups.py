"""Group identity, durable local ownership, native EWS serialization and reviewed composition."""

import copy
import json
from types import SimpleNamespace

import pytest
from exchangelib import Account, DistributionList
from exchangelib.folders import Contacts, Root
from exchangelib.properties import Mailbox, Member
from exchangelib.services import UpdateItem
from exchangelib.version import Build, Version
from textual.app import App
from textual.widgets import Button, Input, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
import gucmail.state as state_module
from gucmail.config import AppConfig
from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ItemNotFoundError,
    LocalStateError,
    UnsupportedFeatureError,
)
from gucmail.ews.adapter import EWSGateway
from gucmail.models import Address
from gucmail.services.contact_groups import (
    ContactGroupService,
    ContactGroupVerificationError,
    GroupSource,
    LocalContactGroups,
)
from gucmail.state import StateStore
from gucmail.tui.app import ComposeScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.contact_groups_screen import ContactGroupEditor, ContactGroupsScreen
from test_cli import _environment
from test_settings_secondpass import _context
from test_tui import FakeGateway


def test_local_groups_migrate_reopen_preserve_cache_and_isolate_accounts(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(state_module, "SCHEMA_VERSION", 8)
        old = StateStore(path, account_key="account-one")
        old.initialize()
        old.save_search("Existing", {"subject": "synthetic"})
    current = StateStore(path, account_key="account-one")
    service = ContactGroupService(LocalContactGroups(current))
    plan = service.plan("Course", (Address("member@example.test", "Synthetic Member"),))
    with pytest.raises(ConfirmationRequiredError):
        service.apply(plan)
    group = service.apply(plan, confirmed=True)
    assert current.health()["schema_version"] == SCHEMA_VERSION
    assert current.get_search("Existing")
    current.clear_disposable_cache()
    reopened = ContactGroupService(LocalContactGroups(StateStore(path, account_key="ACCOUNT-ONE")))
    assert reopened.recipients(group.reference) == group.members
    other = ContactGroupService(LocalContactGroups(StateStore(path, account_key="account-two")))
    assert other.list() == ()
    with pytest.raises(ItemNotFoundError):
        other.get(group.reference)


def test_local_group_compare_and_swap_preserves_newer_membership_and_exact_delete(tmp_path):
    service = ContactGroupService(LocalContactGroups(StateStore(tmp_path / "groups.sqlite")))
    first = service.apply(
        service.plan("Shared name", (Address("one@example.test"),)), confirmed=True
    )
    another = service.apply(
        service.plan("Shared name", (Address("two@example.test"),)), confirmed=True
    )
    stale = service.plan("Changed", (Address("three@example.test"),), before=first)
    updated = service.apply(service.plan("Newer", first.members, before=first), confirmed=True)
    with pytest.raises(ConflictError):
        service.apply(stale, confirmed=True)
    with pytest.raises(ConflictError):
        service.apply(service.plan_delete(first), confirmed=True)
    assert service.get(first.reference) == updated
    service.apply(service.plan_delete(updated), confirmed=True)
    assert service.list() == (another,)


@pytest.mark.parametrize(
    "members",
    [
        (Address("bad"),),
        (Address("one@example.test"), Address("ONE@example.test")),
        tuple(Address(f"person{i}@example.test") for i in range(201)),
    ],
)
def test_invalid_members_fail_before_storage_or_network(tmp_path, members):
    state = StateStore(tmp_path / "not-created.sqlite")
    with pytest.raises(ConfigurationError):
        ContactGroupService(LocalContactGroups(state)).plan("Group", members)
    assert not state.path.exists()


class GroupServer:
    def __init__(self, monkeypatch, path):
        self.account = Account.__new__(Account)
        self.account._version = Version(build=Build(15, 1, 2375, 7))
        self.account.protocol = SimpleNamespace(version=self.account._version)
        self.account.root = Root(account=self.account, id="root-id")
        self.account.contacts = Contacts(root=self.account.root, id="contacts-folder-id")
        self.account.trash = SimpleNamespace(id="trash", name="Deleted Items")
        self.items = {}
        self.writes = []
        self.reads = []
        self.readback_fails = False
        self.response_lost = False
        self.projected = []
        self.account.fetch = self.fetch

        def save(item, **kwargs):
            # Use the installed serializers, not a second handwritten wire model.
            if item.id is not None:
                wire = UpdateItem(account=self.account).get_payload(
                    items=[(item, kwargs["update_fields"])],
                    conflict_resolution=kwargs["conflict_resolution"],
                    message_disposition="SaveOnly",
                    send_meeting_invitations_or_cancellations="SendToNone",
                    suppress_read_receipts=True,
                )
                assert wire.get("ConflictResolution") == "NeverOverwrite"
                assert wire.find(".//{*}FieldURI[@FieldURI='distributionlist:Members']") is not None
            else:
                wire = item.to_xml(version=self.account.version)
                item.id = f"group-{len(self.items) + 1}"
            item.changekey = f"revision-{len(self.writes) + 1}"
            self.writes.append(("save", wire))
            self.items[item.id] = copy.copy(item)
            if self.response_lost:
                raise TimeoutError("synthetic lost response")
            return item

        def move(item, target):
            assert target is self.account.trash
            self.writes.append(("move", item.id))
            self.items.pop(item.id)
            return "deleted-group", "deleted-revision"

        monkeypatch.setattr(DistributionList, "save", save)
        monkeypatch.setattr(DistributionList, "move", move)
        self.state = StateStore(path)
        self.gateway = EWSGateway(
            AppConfig(), account_factory=lambda: self.account, state=self.state
        )
        self.service = ContactGroupService(self.gateway)

        def bounded(folder, *, fields, filters=None, limit, offset=0, **kwargs):
            self.projected.append((fields, filters, limit, offset))
            values = list(self.items.values())[offset : offset + limit]
            return tuple(replace_members(copy.copy(item), None) for item in values)

        self.gateway._bounded_items = bounded

    def fetch(self, *, ids, only_fields):
        self.reads.append((ids, only_fields))
        if self.readback_fails and len(self.writes) >= 2:
            raise TimeoutError("synthetic readback failed")
        result = self.items.get(ids[0][0])
        return [copy.copy(result)] if result else []


def replace_members(item, members):
    item.members = members
    return item


@pytest.fixture
def server(monkeypatch, tmp_path):
    return GroupServer(monkeypatch, tmp_path / "native-state.sqlite")


def test_native_group_real_payload_identity_reopen_stale_protection_and_recoverable_delete(server):
    created = server.service.apply(
        server.service.plan("Personal group", (Address("one@example.test"),)), confirmed=True
    )
    assert created.source is GroupSource.MAILBOX and created.reference.startswith("grp_")
    assert (
        server.writes[0][1].find("{*}Members/{*}Member/{*}Mailbox/{*}EmailAddress").text
        == "one@example.test"
    )
    page = server.service.list(limit=3, offset=0)
    assert page[0].members_loaded is False and page[0].members == ()
    assert server.projected[-1] == (
        ("display_name", "item_class"),
        {"item_class__startswith": "IPM.DistList"},
        3,
        0,
    )
    # Recreate a provider from the persisted local reference, not its old object cache.
    reopened = EWSGateway(
        AppConfig(), account_factory=lambda: server.account, state=StateStore(server.state.path)
    )
    assert reopened.get_contact_group(created.reference) == created
    changed = server.service.apply(
        server.service.plan("Updated", (Address("two@example.test"),), before=created),
        confirmed=True,
    )
    assert changed.reference == created.reference and changed.revision != created.revision
    with pytest.raises(ConflictError):
        server.service.apply(
            server.service.plan("Stale", created.members, before=created), confirmed=True
        )
    assert len(server.writes) == 2
    server.service.apply(server.service.plan_delete(changed), confirmed=True)
    assert server.writes[-1][0] == "move" and len(server.writes) == 3
    assert server.state.resolve_reference(created.reference)["ews_id"] == "deleted-group"


@pytest.mark.parametrize("failure", ["readback", "response_lost"])
def test_native_uncertain_group_update_never_repeats(server, failure):
    created = server.service.apply(
        server.service.plan("Initial", (Address("one@example.test"),)), confirmed=True
    )
    server.readback_fails = failure == "readback"
    server.response_lost = failure == "response_lost"
    plan = server.service.plan("Changed", created.members, before=created)
    with pytest.raises(ContactGroupVerificationError) as error:
        server.service.apply(plan, confirmed=True)
    assert error.value.context.mutation_uncertain and not error.value.context.retry_safe
    assert len(server.writes) == 2


@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
def test_accepted_group_write_tracking_failure_is_uncertain_without_replay(server, operation):
    created = None
    if operation != "create":
        created = server.service.apply(
            server.service.plan("Initial", (Address("one@example.test"),)), confirmed=True
        )
    initial_writes = len(server.writes)
    remember = server.gateway._remember

    def failed_tracking(*args, **kwargs):
        if len(server.writes) > initial_writes:
            raise LocalStateError("synthetic locked reference store")
        return remember(*args, **kwargs)

    server.gateway._remember = failed_tracking
    plan = (
        server.service.plan_delete(created)
        if operation == "delete"
        else server.service.plan("Changed", (Address("two@example.test"),), before=created)
    )
    with pytest.raises(ContactGroupVerificationError) as error:
        server.service.apply(plan, confirmed=True)
    assert error.value.context.mutation_uncertain and error.value.context.retry_safe is False
    assert "tracking or readback failed" in str(error.value)
    assert len(server.writes) == initial_writes + 1


def test_linked_nested_or_unresolved_members_cannot_be_silently_dropped(server):
    created = server.service.apply(
        server.service.plan("Initial", (Address("one@example.test"),)), confirmed=True
    )
    native_id = next(iter(server.items))
    server.items[native_id].members = [
        Member(mailbox=Mailbox(email_address="nested@example.test", mailbox_type="PublicDL"))
    ]
    current = server.service.get(created.reference)
    assert current.unresolved_members == 1 and not current.editable
    with pytest.raises(UnsupportedFeatureError):
        server.service.plan("Changed", current.members, before=current)
    with pytest.raises(UnsupportedFeatureError):
        server.service.recipients(current.reference)
    assert len(server.writes) == 1


@pytest.mark.parametrize("operation", ["read", "edit"])
def test_group_item_never_enters_individual_contact_editing(server, monkeypatch, operation):
    group = DistributionList(
        display_name="Synthetic group",
        item_class="IPM.DistList",
        id="legacy-group",
        changekey="one",
    )
    server.items[group.id] = group
    server.gateway._remember("contact", group, reference="legacy-contact-alias")
    before = server.state.resolve_reference("legacy-contact-alias")
    monkeypatch.setattr(
        server.gateway, "_remember", lambda *_a, **_k: pytest.fail("Group overwritten as Contact")
    )
    with pytest.raises(UnsupportedFeatureError, match="contact-group workflow"):
        if operation == "read":
            server.gateway.get_contact("legacy-contact-alias")
        else:
            server.gateway.update_contact(
                SimpleNamespace(
                    reference="legacy-contact-alias", changes={"display_name": "Wrong edit"}
                )
            )
    assert len(server.reads) == 1 and not server.writes
    assert server.state.resolve_reference("legacy-contact-alias") == before
    assert server.gateway._resolve_item("legacy-contact-alias", "contact")[1] is group
    with pytest.raises(UnsupportedFeatureError):
        server.gateway.delete_contact(
            SimpleNamespace(reference="legacy-contact-alias", hard_delete=False)
        )
    assert not server.writes


def test_cli_groups_local_default_and_explicit_replacement_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "_gateway", lambda *args, **kwargs: pytest.fail("Local groups resolved credentials")
    )
    runner = CliRunner()
    env = _environment(tmp_path)
    for name in ("list", "show", "create", "edit", "delete", "recipients"):
        assert runner.invoke(cli.app, ["contact-groups", name, "--help"], env=env).exit_code == 0
    base = ["contact-groups", "create", "--name", "Synthetic", "--member", "one@example.test"]
    dry = runner.invoke(cli.app, ["--json", *base, "--dry-run"], env=env)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["data"]["dry_run"]
    denied = runner.invoke(cli.app, ["--non-interactive", *base], env=env)
    assert denied.exit_code == 9
    created = runner.invoke(cli.app, ["--json", "--yes", *base], env=env)
    assert created.exit_code == 0, created.output
    reference = json.loads(created.output)["data"]["reference"]
    recipients = runner.invoke(
        cli.app, ["--json", "contact-groups", "recipients", reference], env=env
    )
    assert json.loads(recipients.output)["data"][0]["email"] == "one@example.test"


class _Harness(App):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def on_mount(self):
        self.push_screen(ContactGroupsScreen(self.service))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_mounted_group_form_exact_confirmation_and_cancel(tmp_path, size):
    service = ContactGroupService(LocalContactGroups(StateStore(tmp_path / "groups.sqlite")))
    app = _Harness(service)
    async with app.run_test(size=size) as pilot:

        async def wait_until_ready(condition):
            for _ in range(150):
                if condition():
                    await pilot.pause()
                    return
                await pilot.pause(0.02)
            raise AssertionError("The contact-group workflow did not acknowledge the action.")

        await pilot.pause()
        screen = app.screen
        screen.query_one("#group-create", Button).press()
        await wait_until_ready(
            lambda: isinstance(app.screen, ContactGroupEditor) and app.screen.is_mounted
        )
        assert isinstance(app.screen, ContactGroupEditor)
        await pilot.press("escape")
        await wait_until_ready(lambda: app.screen is screen)
        assert service.list() == ()
        screen.query_one("#group-create", Button).press()
        await wait_until_ready(
            lambda: isinstance(app.screen, ContactGroupEditor) and app.screen.is_mounted
        )
        app.screen.query_one("#group-name", Input).value = "Guided group"
        app.screen.query_one("#group-members", TextArea).load_text(
            "Synthetic <one@example.test>\ntwo@example.test"
        )
        await pilot.press("ctrl+s")
        await wait_until_ready(
            lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        assert isinstance(app.screen, ConfirmationScreen)
        assert (
            "one@example.test" in app.screen.detail_text
            and "two@example.test" in app.screen.detail_text
        )
        await pilot.press("y")
        await wait_until_ready(
            lambda: (
                app.screen is screen and screen.current is not None and not screen._operation_active
            )
        )
        assert screen.current and len(screen.current.members) == 2
        reopened = ContactGroupService(LocalContactGroups(StateStore(tmp_path / "groups.sqlite")))
        assert reopened.get(screen.current.reference).members == screen.current.members


@pytest.mark.asyncio
async def test_actual_app_group_composition_reviews_exact_recipients_then_opens_bcc_draft(tmp_path):
    context = _context(tmp_path)
    service = ContactGroupService(LocalContactGroups(context.state))
    group = service.apply(
        service.plan("Synthetic group", (Address("one@example.test"), Address("two@example.test"))),
        confirmed=True,
    )
    gateway = FakeGateway()
    sends = []
    gateway.send = lambda draft: sends.append(draft)
    app = GUCmailApp(context=context, gateway=gateway)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app._open_area("contact_groups")
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ContactGroupsScreen) and app.screen.current:
                break
        app.screen.query_one("#group-compose", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmationScreen):
                break
        assert isinstance(app.screen, ConfirmationScreen)
        await pilot.press("y")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(app.screen, ComposeScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ComposeScreen)
        assert tuple(app.screen.draft.bcc) == group.members
        assert app.screen.draft.to == [] and not sends
        assert app.screen.query_one("#compose-to", Input).value == ""
        assert app.screen.query_one("#compose-bcc", Input).value == (
            "one@example.test, two@example.test"
        )
