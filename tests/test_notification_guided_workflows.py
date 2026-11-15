"""Normal guided editing, durable AST readback, runtime routing, and bounded sampling."""

from copy import deepcopy
from datetime import UTC, datetime
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Select, Static, TabbedContent, TextArea

from gucmail.models import Address, MessageSummary
from gucmail.notification_policies import MonitoringProfile
from gucmail.notification_rules import (
    NotificationRuleEngine,
    action_to_data,
    condition_from_data,
    condition_to_data,
)
from gucmail.services.messages import MessageService
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.notification_rule_editing import edit_condition_tree
from gucmail.services.notification_simulation import NotificationSimulationService
from gucmail.state import StateStore
from gucmail.tui.notification_rule_builder import RuleBuilder, RuleValueForm
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen, RuleEditorScreen
from test_notification_settings_screen_secondpass import (
    NotificationSettingsHarness,
    _event,
    _service,
)

LEAF = {"type": "text", "field": "subject", "operator": "contains", "values": ["exam"]}


def test_tree_edits_are_atomic_and_keep_nested_logic_equivalent():
    root = condition_to_data(condition_from_data(LEAF))
    original = deepcopy(root)
    root = edit_condition_tree(root, (), "or")
    root = edit_condition_tree(
        root,
        (),
        "add",
        {
            "type": "boolean",
            "field": "has_attachments",
            "expected": True,
        },
    )
    root = edit_condition_tree(root, (1,), "not")
    root = edit_condition_tree(root, (), "and")
    root = edit_condition_tree(
        root,
        (),
        "add",
        {
            "type": "text",
            "field": "folder",
            "operator": "exact",
            "values": ["Inbox"],
        },
    )
    snapshot = deepcopy(root)
    with pytest.raises(ValueError):
        edit_condition_tree(root, (1,), "replace", {"type": "unknown"})
    assert root == snapshot
    assert original["type"] == "text"
    root = edit_condition_tree(root, (1,), "duplicate")
    root = edit_condition_tree(root, (2,), "up")
    root = edit_condition_tree(root, (2,), "remove")
    assert condition_to_data(condition_from_data(root)) == snapshot


@pytest.mark.parametrize("path", [(-1,), (True,), (2,), (0, -1), ("0",)])
def test_invalid_condition_selection_does_not_edit_another_child(path):
    root = {"type": "and", "children": [deepcopy(LEAF), deepcopy(LEAF)]}
    original = deepcopy(root)
    with pytest.raises(ValueError, match="no longer exists"):
        edit_condition_tree(root, path, "replace", {"type": "boolean", "field": "is_read"})
    assert root == original


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (70, 20)])
async def test_guided_tree_actions_save_reopen_and_execute_same_runtime_rule(tmp_path, size):
    service = _service(tmp_path / "state.sqlite3")
    app = App()
    async with app.run_test(size=size) as pilot:

        async def wait_for_form():
            for _ in range(150):
                if (
                    isinstance(app.screen, RuleValueForm)
                    and app.screen.is_mounted
                    and all(
                        any(label.is_mounted for label in select.query("#label"))
                        for select in app.screen.query(Select)
                    )
                    and len(list(app.screen.query(Select))) >= 3
                ):
                    return
                await pilot.pause(0.02)
            raise AssertionError("The rule value form did not finish mounting its selectors.")

        editor = RuleEditorScreen(None, next_order=1, provider_id="desktop-main")
        await app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#notification-rule-id", Input).value = "guided"
        editor.query_one("#notification-rule-name", Input).value = "Guided exam routing"
        editor.query_one("#notification-rule-enabled", Checkbox).value = True
        builder = editor.query_one(RuleBuilder)
        builder.query_one("#rule-tree-add", Button).press()
        await pilot.pause()
        await wait_for_form()
        assert isinstance(app.screen, RuleValueForm)
        app.screen.query_one("#rule-value-field", Select).value = "subject"
        app.screen.query_one("#rule-value-values", TextArea).load_text("exam")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is editor
        assert builder.condition["type"] == "and"
        builder.path = (1,)
        builder.query_one("#rule-tree-not", Button).press()
        await pilot.pause()
        assert builder.condition["children"][1]["type"] == "not"
        builder.path = (1,)
        builder.query_one("#rule-tree-not", Button).press()
        await pilot.pause()
        builder.query_one("#rule-action-add", Button).press()
        await pilot.pause()
        await wait_for_form()
        assert isinstance(app.screen, RuleValueForm)
        app.screen.query_one("#rule-value-kind", Select).value = "route_digest"
        app.screen.query_one("#rule-value-provider_id", Input).value = "desktop-main"
        await pilot.press("ctrl+s")
        await pilot.pause()
        builder.action_index = 1
        builder.query_one("#rule-action-up", Button).press()
        await pilot.pause()
        rule = editor.build_value()
        assert [action.kind.value for action in rule.actions] == ["route_digest", "route_immediate"]
        assert builder.region.width <= size[0]
        service.rules("campus").save_rule(rule)
        reopened = NotificationConfigurationService(
            StateStore(tmp_path / "state.sqlite3", account_key="settings-test")
        )
        saved = next(
            value
            for value in reopened.profiles.get_profile("campus").rules
            if value.rule_id == "guided"
        )
        assert condition_to_data(saved.condition) == builder.condition
        assert [action_to_data(action) for action in saved.actions] == builder.actions
        decision = NotificationRuleEngine((saved,)).evaluate(_event())
        assert decision.matched_rule_ids == ("guided",)
        assert [action.kind.value for action in decision.actions] == [
            "route_digest",
            "route_immediate",
        ]
        await pilot.press("escape")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {
            "type": "text",
            "field": "subject",
            "operator": "regex",
            "values": ["literal; value", "exam, review"],
        },
        {"type": "boolean", "field": "has_attachments", "expected": False},
        {"type": "choice", "field": "importance", "values": ["high"], "negate": True},
        {"type": "size", "minimum_bytes": 10, "maximum_bytes": 100},
        {"type": "time", "start": "22:00:00", "end": "08:00:00", "timezone": "Africa/Cairo"},
        {"type": "weekday", "weekdays": [0, 2, 6], "timezone": "UTC"},
    ],
)
async def test_each_typed_condition_form_preserves_canonical_value(value):
    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        form = RuleValueForm(value)
        await app.push_screen(form)
        await pilot.pause()
        assert form.build_value() == condition_to_data(condition_from_data(value))
        await pilot.press("escape")


class _MetadataGateway:
    def __init__(self):
        self.calls = []

    def list_messages(self, **kwargs):
        self.calls.append(kwargs)
        return tuple(
            MessageSummary(
                f"{kwargs['folder']}-{index}",
                "Exam notices",
                Address("synthetic@example.test"),
                datetime.now(UTC),
                folder_path=kwargs["folder"],
                preview="Must not enter a sample",
            )
            for index in range(kwargs["limit"])
        )


def test_recent_sampling_has_fixed_budget_no_content_and_no_mutation(tmp_path):
    gateway = _MetadataGateway()
    service = NotificationSimulationService(MessageService(gateway), account_id="synthetic")
    profile = MonitoringProfile("many", "Many folders", tuple(f"Inbox/{i}" for i in range(20)))
    result = service.recent(profile)
    assert len(gateway.calls) == 10
    assert sum(call["limit"] for call in gateway.calls) == 50
    assert len(result.events) == 50
    assert result.omitted_folder_count == 10
    assert all(event.body_preview is None and event.recipients is None for event in result.events)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        service.recent(profile, cancel=cancelled)
    assert len(gateway.calls) == 10


@pytest.mark.asyncio
async def test_recent_mail_button_runs_worker_dry_run_without_provider_delivery(tmp_path):
    service = _service(tmp_path / "state.db")
    gateway = _MetadataGateway()
    sampler = NotificationSimulationService(MessageService(gateway), account_id="synthetic")
    delivered = []
    screen = NotificationSettingsScreen(
        service,
        webhook_delivery=lambda *args: delivered.append(args),
        recent_simulation_source=lambda profile, cancel, _: sampler.recent(profile, cancel=cancel),
    )
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(100, 35)) as pilot:
        # Press only after the settings load; otherwise the load result replaces the report.
        for _ in range(300):
            await pilot.pause(0.05)
            if screen._profiles and not screen._operation_active:
                break
        screen.query_one("#notification-tabs", TabbedContent).active = "notification-rules-tab"
        screen.query_one("#notification-rule-recent", Button).press()
        for _ in range(300):  # sampling and the dry run are two worker steps
            await pilot.pause(0.05)
            if "Recent metadata sample" in str(
                screen.query_one("#notification-rule-detail", Static).content
            ):
                break
        report = str(screen.query_one("#notification-rule-detail", Static).content)
        assert "Events: 50" in report
        assert "DRY RUN" in report
        assert len(gateway.calls) == 2
        assert not delivered
