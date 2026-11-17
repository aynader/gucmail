"""Offline-first notification and monitoring configuration for Textual.

This module is deliberately a standalone application surface.  It edits the same
``NotificationConfigurationService`` consumed by the watcher and CLI, performs no
mailbox operations, and keeps every destructive local or outbound change behind an
explicit confirmation. Provider delivery is injected so the screen remains testable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from threading import Event
from typing import Any, ClassVar, TypeVar, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Label,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from gucmail.models import Address, Importance, NotificationEvent
from gucmail.notification_events import (
    EVENT_PAYLOAD_FIELDS,
    EventDisclosurePolicy,
    NewMailEvent,
    PrivacyMode,
)
from gucmail.notification_policies import (
    DigestCadence,
    DigestSchedule,
    MonitoringProfile,
    ProviderPolicy,
    QuietHoursBehavior,
    QuietHoursSchedule,
    QuietHoursWindow,
    RouteMode,
    RoutingMatrix,
    import_profiles,
)
from gucmail.notification_providers import validate_discord_webhook_url
from gucmail.notification_rules import (
    Condition,
    NotificationRule,
    action_from_data,
    action_to_data,
    condition_from_data,
    condition_to_data,
    import_rules,
)
from gucmail.security import sanitize_terminal
from gucmail.services.notification_configuration import (
    NotificationConfigurationService,
    PersistenceSnapshot,
    ProviderConfigurationMetadata,
)
from gucmail.services.notification_simulation import NotificationSample
from gucmail.services.notification_switch import NotificationSwitch, SwitchResult
from gucmail.services.notifications import (
    EVENT_FIELDS,
    DeliveryResult,
    DeliveryState,
)
from gucmail.services.webhooks import WebhookConfig, WebhookRegistry, WebhookRegistryLoadResult
from gucmail.tui.area_common import (
    AreaAction,
    OperationalScreen,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.events import (
    AutomaticRepliesRequested,
    InboxRulesRequested,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.notification_rule_builder import RuleBuilder

_STRATEGIES = (
    ("Automatic (recommended)", "automatic"),
    ("Streaming, when supported", "streaming"),
    ("Pull notifications", "pull"),
    ("Incremental sync", "incremental_sync"),
    ("Polling", "bounded_poll"),
)
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")
WebhookDelivery = Callable[[WebhookConfig, NotificationEvent], DeliveryResult]
RecentSimulationSource = Callable[[MonitoringProfile, Event, ProgressReporter], NotificationSample]


def _identifier(value: str, *, label: str) -> str:
    normalized = value.strip().casefold().replace(" ", "-")
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(
            f"{label} must use 1-200 lowercase letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _json_object(value: str, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, Any], decoded)


def _json_list(value: str, *, label: str) -> list[Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"{label} must be a JSON list")
    return decoded


def _selected(select: Select[str], *, default: str = "") -> str:
    value = select.value
    return value if isinstance(value, str) else default


def _integer(value: str, *, label: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return result


def _float(value: str, *, label: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return result


def _clock(value: str, *, label: str) -> time:
    try:
        result = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must use HH:MM or HH:MM:SS") from exc
    if result.tzinfo is not None:
        raise ValueError(f"{label} must be a local wall-clock time")
    return result


def _weekdays(value: str) -> frozenset[int]:
    aliases = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    result: set[int] = set()
    for raw in value.split(","):
        token = raw.strip().casefold()
        if not token:
            continue
        if token.isdigit() and 0 <= int(token) <= 6:
            result.add(int(token))
        elif token in aliases:
            result.add(aliases[token])
        else:
            raise ValueError(f"unknown weekday: {raw.strip()}")
    if not result:
        raise ValueError("select at least one weekday")
    return frozenset(result)


def _weekday_text(values: frozenset[int]) -> str:
    return ", ".join(_WEEKDAY_NAMES[value] for value in sorted(values))


def _safe_public_error(error: BaseException) -> PublicOperationError:
    if isinstance(error, PublicOperationError):
        return error
    if isinstance(error, KeyError):
        return PublicOperationError("The selected configuration item no longer exists.")
    if isinstance(error, (TypeError, ValueError)):
        return PublicOperationError(str(error), retryable=False)
    return PublicOperationError(
        "The local notification configuration could not be updated. "
        "Open Diagnostics for a redacted persistence report."
    )


def preview_provider_metadata_document(
    document: str | bytes,
) -> tuple[ProviderConfigurationMetadata, ...]:
    """Fully validate a secret-free provider metadata import without persisting it."""

    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("provider metadata document is too large")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("provider metadata document is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema",
        "version",
        "providers",
    }:
        raise ValueError("provider metadata document has an invalid shape")
    if decoded["schema"] != "gucmail.notification-provider-metadata" or decoded["version"] != 1:
        raise ValueError("unsupported provider metadata document schema or version")
    raw_providers = decoded["providers"]
    if not isinstance(raw_providers, list) or len(raw_providers) > 1_000:
        raise ValueError("provider metadata document has an invalid provider list")
    providers: list[ProviderConfigurationMetadata] = []
    expected_keys = {
        "provider_id",
        "kind",
        "name",
        "enabled",
        "secret_ref",
        "metadata",
    }
    for value in raw_providers:
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ValueError("provider metadata entry has an invalid shape")
        if value["secret_ref"] is not None:
            raise ValueError(
                "provider metadata imports through the TUI cannot contain secret references"
            )
        if not isinstance(value["enabled"], bool) or not isinstance(value["metadata"], dict):
            raise ValueError("provider enabled and metadata values have invalid types")
        providers.append(
            ProviderConfigurationMetadata(
                value["provider_id"],
                value["kind"],
                value["name"],
                value["enabled"],
                None,
                value["metadata"],
            )
        )
    identifiers = [provider.provider_id.casefold() for provider in providers]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("provider metadata document contains duplicate identifiers")
    return tuple(providers)


@dataclass(frozen=True, slots=True)
class ProfileEdit:
    profile_id: str
    name: str
    folders: tuple[str, ...]
    poll_interval_seconds: float
    strategy: str | None
    enabled: bool
    notify_existing: bool


_EditorResult = TypeVar("_EditorResult")


class NotificationEditorScreen(ModalScreen[_EditorResult | None]):
    """Dismiss an editor consistently, including while a text field has focus."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel")
    ]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        event.widget.scroll_visible(animate=False, force=True, immediate=True)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 100, "narrow-editor")
        self.call_after_refresh(self._reveal_focus)

    def _reveal_focus(self) -> None:
        if self.focused is not None:
            self.focused.scroll_visible(animate=False, force=True, immediate=True)


class ProfileEditorScreen(NotificationEditorScreen[ProfileEdit]):
    """Create or edit the watcher-facing fields of one named profile."""

    DEFAULT_CSS = """
    ProfileEditorScreen { align: center middle; }
    #notification-profile-dialog {
        width: 95%; max-width: 86; height: auto; max-height: 92%;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-profile-dialog Input, #notification-profile-dialog Select { margin-bottom: 1; }
    #notification-profile-checks { height: 3; }
    #notification-profile-checks Checkbox { margin-right: 2; }
    #notification-profile-error { color: $error; min-height: 1; height: auto; }
    #notification-profile-actions { height: 3; align-horizontal: right; }
    #notification-profile-actions Button { margin-left: 1; }
    """

    def __init__(self, profile: MonitoringProfile | None, *, strategy: str | None = None) -> None:
        super().__init__()
        self.profile = profile
        self.strategy = strategy

    def compose(self) -> ComposeResult:
        profile = self.profile
        with VerticalScroll(id="notification-profile-dialog"):
            yield Label(
                "Edit monitoring profile" if profile else "Set up monitoring profile", markup=False
            )
            yield Label("Profile ID")
            yield Input(
                value=profile.profile_id if profile else "",
                placeholder="Profile ID, e.g. important-only",
                id="notification-profile-id",
                disabled=profile is not None,
            )
            yield Label("Name")
            yield Input(
                value=profile.name if profile else "",
                placeholder="Friendly name",
                id="notification-profile-name",
            )
            yield Label("Folders (comma separated)")
            yield Input(
                value=", ".join(profile.folders) if profile else "Inbox",
                placeholder="Folders, comma separated",
                id="notification-profile-folders",
            )
            yield Label("Poll interval (seconds; minimum 60)")
            yield Input(
                value=f"{profile.poll_interval_seconds:g}" if profile else "60",
                placeholder="Poll interval in seconds (safe minimum 60)",
                id="notification-profile-poll",
            )
            yield Label("Monitoring strategy")
            yield Select(
                _STRATEGIES,
                value=self.strategy or "automatic",
                allow_blank=False,
                id="notification-profile-strategy",
            )
            with Horizontal(id="notification-profile-checks"):
                yield Checkbox(
                    "Enable monitoring",
                    value=profile.enabled if profile else False,
                    id="notification-profile-enabled",
                )
                yield Checkbox(
                    "Notify existing items on first run",
                    value=profile.notify_existing if profile else False,
                    id="notification-profile-existing",
                )
            yield Static(
                "Notify-existing is intentionally off by default to prevent a first-run burst.",
                markup=False,
            )
            yield Static("", id="notification-profile-error", markup=False)
            with Horizontal(id="notification-profile-actions"):
                yield Button("Cancel", id="notification-profile-cancel")
                yield Button("Save profile", id="notification-profile-save", variant="primary")

    def build_value(self) -> ProfileEdit:
        profile_id = _identifier(
            self.query_one("#notification-profile-id", Input).value,
            label="profile ID",
        )
        name = self.query_one("#notification-profile-name", Input).value.strip()
        if not name or len(name) > 300:
            raise ValueError("profile name must be between 1 and 300 characters")
        folders = _csv(self.query_one("#notification-profile-folders", Input).value)
        if not folders:
            raise ValueError("choose at least one monitored folder")
        poll = _float(
            self.query_one("#notification-profile-poll", Input).value,
            label="poll interval",
            minimum=60,
            maximum=86_400,
        )
        strategy_value = _selected(
            self.query_one("#notification-profile-strategy", Select),
            default="automatic",
        )
        strategy = None if strategy_value == "automatic" else strategy_value
        return ProfileEdit(
            profile_id,
            name,
            folders,
            poll,
            strategy,
            self.query_one("#notification-profile-enabled", Checkbox).value,
            self.query_one("#notification-profile-existing", Checkbox).value,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "notification-profile-cancel":
            self.dismiss(None)
            return
        if event.button.id != "notification-profile-save":
            return
        try:
            value = self.build_value()
        except ValueError as exc:
            self.query_one("#notification-profile-error", Static).update(
                sanitize_terminal(str(exc), max_length=500)
            )
            return
        self.dismiss(value)


class RuleEditorScreen(NotificationEditorScreen[NotificationRule]):
    """Guided condition tree and ordered actions with optional advanced serialization."""

    DEFAULT_CSS = """
    RuleEditorScreen { align: center middle; }
    #notification-rule-dialog {
        width: 97%; max-width: 112; height: 95%; max-height: 45;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-rule-row { height: 4; }
    #notification-rule-row Vertical { width: 1fr; margin-right: 1; }
    #notification-rule-flags { layout: grid; grid-size: 6; grid-rows: 3; height: 6; }
    #notification-rule-flags Checkbox { column-span: 3; }
    #notification-rule-flags Button { column-span: 2; }
    #notification-rule-flags Checkbox, #notification-rule-flags Button {
        width: 1fr; min-width: 0;
    }
    #notification-rule-editors { height: 16; }
    #notification-rule-editors Vertical { width: 1fr; margin-right: 1; }
    #notification-rule-condition, #notification-rule-actions { height: 1fr; min-height: 10; }
    #notification-rule-error { color: $error; min-height: 1; height: auto; }
    #notification-rule-actions-row { height: 3; align-horizontal: right; }
    #notification-rule-actions-row Button { margin-left: 1; }
    RuleEditorScreen.narrow-editor #notification-rule-flags { grid-size: 2; height: 9; }
    RuleEditorScreen.narrow-editor #notification-rule-flags Checkbox,
    RuleEditorScreen.narrow-editor #notification-rule-flags Button { column-span: 1; }
    """

    def __init__(
        self,
        rule: NotificationRule | None,
        *,
        next_order: int,
        provider_id: str,
        initial_id: str = "",
        initial_name: str = "",
        initial_condition: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.rule = rule
        self.next_order = next_order
        self.provider_id = provider_id
        self.initial_id = initial_id
        self.initial_name = initial_name
        self.initial_condition = initial_condition

    def compose(self) -> ComposeResult:
        rule = self.rule
        default_condition: dict[str, Any] = {
            "type": "text",
            "field": "folder",
            "operator": "exact",
            "values": ["Inbox"],
            "case_sensitive": False,
        }
        default_actions = [{"kind": "route_immediate", "provider_id": self.provider_id}]
        condition = (
            condition_to_data(rule.condition)
            if rule
            else self.initial_condition or default_condition
        )
        actions = [action_to_data(action) for action in rule.actions] if rule else default_actions
        with VerticalScroll(id="notification-rule-dialog"):
            yield Label("Notification rule")
            with Horizontal(id="notification-rule-row"):
                with Vertical():
                    yield Label("Rule ID")
                    yield Input(
                        value=rule.rule_id if rule else self.initial_id,
                        placeholder="Rule ID",
                        id="notification-rule-id",
                        disabled=rule is not None,
                    )
                with Vertical():
                    yield Label("Name")
                    yield Input(
                        value=rule.name if rule else self.initial_name,
                        placeholder="Rule name",
                        id="notification-rule-name",
                    )
                with Vertical():
                    yield Label("Priority")
                    yield Input(
                        value=str(rule.priority if rule else 0),
                        type="integer",
                        placeholder="Priority",
                        id="notification-rule-priority",
                    )
            yield Label("Description (optional)")
            yield Input(
                value=rule.description or "" if rule else "",
                placeholder="Description (optional)",
                id="notification-rule-description",
            )
            with Horizontal(id="notification-rule-flags"):
                yield Checkbox(
                    "Enabled",
                    value=rule.enabled if rule else False,
                    id="notification-rule-enabled",
                )
                yield Checkbox(
                    "Stop after match",
                    value=rule.stop_processing if rule else False,
                    id="notification-rule-stop",
                )
                yield Button("Inbox starter", id="notification-rule-template-inbox")
                yield Button("Nested AND/OR starter", id="notification-rule-template-nested")
                yield Button("Suppress starter", id="notification-rule-template-suppress")
            yield RuleBuilder(condition, actions, provider_id=self.provider_id)
            with Collapsible(title="Advanced: edit rule JSON", collapsed=True):
                with Horizontal(id="notification-rule-editors"):
                    with Vertical():
                        yield Label("Condition")
                        yield TextArea(
                            json.dumps(condition, ensure_ascii=False, indent=2),
                            id="notification-rule-condition",
                        )
                    with Vertical():
                        yield Label("Ordered actions")
                        yield TextArea(
                            json.dumps(actions, ensure_ascii=False, indent=2),
                            id="notification-rule-actions",
                        )
                yield Button("Apply JSON to guided controls", id="notification-rule-apply-json")
            yield Static(
                "Saving validates the complete AST. Body matching remains fail-closed unless "
                "explicitly enabled by the watcher context.",
                markup=False,
            )
            yield Static("", id="notification-rule-error", markup=False)
            with Horizontal(id="notification-rule-actions-row"):
                yield Button("Cancel", id="notification-rule-cancel")
                yield Button(
                    "Save disabled" if not rule else "Save rule",
                    id="notification-rule-save",
                    variant="primary",
                )

    def build_value(self) -> NotificationRule:
        rule_id = _identifier(self.query_one("#notification-rule-id", Input).value, label="rule ID")
        name = self.query_one("#notification-rule-name", Input).value.strip()
        if not name or len(name) > 300:
            raise ValueError("rule name must be between 1 and 300 characters")
        condition = condition_from_data(
            _json_object(
                self.query_one("#notification-rule-condition", TextArea).text, label="condition"
            )
        )
        raw_actions = _json_list(
            self.query_one("#notification-rule-actions", TextArea).text,
            label="actions",
        )
        actions = tuple(action_from_data(value) for value in raw_actions)
        description = self.query_one("#notification-rule-description", Input).value.strip() or None
        return NotificationRule(
            rule_id,
            name,
            condition,
            actions,
            self.query_one("#notification-rule-enabled", Checkbox).value,
            self.query_one("#notification-rule-stop", Checkbox).value,
            _integer(
                self.query_one("#notification-rule-priority", Input).value,
                label="priority",
                minimum=-1_000,
                maximum=1_000,
            ),
            self.rule.order if self.rule else self.next_order,
            description,
        )

    def _template(self, condition: dict[str, Any], actions: list[dict[str, Any]]) -> None:
        self.query_one("#notification-rule-condition", TextArea).load_text(
            json.dumps(condition, ensure_ascii=False, indent=2)
        )
        self.query_one("#notification-rule-actions", TextArea).load_text(
            json.dumps(actions, ensure_ascii=False, indent=2)
        )
        builder = self.query_one(RuleBuilder)
        builder.condition = condition_to_data(condition_from_data(condition))
        builder.actions = [action_to_data(action_from_data(value)) for value in actions]
        builder.refresh_values()

    @on(RuleBuilder.Changed)
    def _guided_changed(self, event: RuleBuilder.Changed) -> None:
        event.stop()
        self.query_one("#notification-rule-condition", TextArea).load_text(
            json.dumps(event.condition, ensure_ascii=False, indent=2)
        )
        self.query_one("#notification-rule-actions", TextArea).load_text(
            json.dumps(event.actions, ensure_ascii=False, indent=2)
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "notification-rule-cancel":
            self.dismiss(None)
            return
        if button_id == "notification-rule-apply-json":
            try:
                value = self.build_value()
                self._template(
                    condition_to_data(value.condition),
                    [action_to_data(action) for action in value.actions],
                )
            except (TypeError, ValueError) as exc:
                self.query_one("#notification-rule-error", Static).update(
                    sanitize_terminal(str(exc), max_length=700)
                )
            return
        if button_id == "notification-rule-template-inbox":
            self._template(
                {
                    "type": "text",
                    "field": "folder",
                    "operator": "exact",
                    "values": ["Inbox"],
                    "case_sensitive": False,
                },
                [{"kind": "route_immediate", "provider_id": self.provider_id}],
            )
            return
        if button_id == "notification-rule-template-nested":
            self._template(
                {
                    "type": "and",
                    "children": [
                        {
                            "type": "text",
                            "field": "folder",
                            "operator": "exact",
                            "values": ["Inbox"],
                            "case_sensitive": False,
                        },
                        {
                            "type": "or",
                            "children": [
                                {
                                    "type": "text",
                                    "field": "subject",
                                    "operator": "contains",
                                    "values": ["exam"],
                                    "case_sensitive": False,
                                },
                                {
                                    "type": "choice",
                                    "field": "importance",
                                    "values": ["high"],
                                    "negate": False,
                                },
                            ],
                        },
                    ],
                },
                [{"kind": "route_immediate", "provider_id": self.provider_id}],
            )
            return
        if button_id == "notification-rule-template-suppress":
            self._template(
                {
                    "type": "text",
                    "field": "sender_domain",
                    "operator": "glob",
                    "values": ["newsletter.*"],
                    "case_sensitive": False,
                },
                [{"kind": "suppress", "provider_id": None, "value": None}],
            )
            return
        if button_id != "notification-rule-save":
            return
        try:
            value = self.build_value()
        except (TypeError, ValueError) as exc:
            self.query_one("#notification-rule-error", Static).update(
                sanitize_terminal(str(exc), max_length=700)
            )
            return
        self.dismiss(value)


class QuietHoursEditorScreen(NotificationEditorScreen[QuietHoursWindow]):
    """Editor for one weekday-aware quiet-hours window."""

    DEFAULT_CSS = """
    QuietHoursEditorScreen { align: center middle; }
    #notification-quiet-dialog {
        width: 96%; max-width: 92; height: auto; max-height: 94%;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-quiet-dialog Input, #notification-quiet-dialog Select { margin-bottom: 1; }
    #notification-quiet-error { color: $error; min-height: 1; height: auto; }
    #notification-quiet-actions { height: 3; align-horizontal: right; }
    #notification-quiet-actions Button { margin-left: 1; }
    """

    def __init__(self, window: QuietHoursWindow | None) -> None:
        super().__init__()
        self.window = window

    def compose(self) -> ComposeResult:
        value = self.window
        with VerticalScroll(id="notification-quiet-dialog"):
            yield Label("Quiet hours · local schedule")
            yield Label("Window ID")
            yield Input(
                value=value.window_id if value else "",
                placeholder="Window ID",
                id="notification-quiet-id",
                disabled=value is not None,
            )
            yield Label("Start HH:MM")
            yield Input(
                value=value.start.isoformat(timespec="minutes") if value else "23:00",
                placeholder="Start HH:MM",
                id="notification-quiet-start",
            )
            yield Label("End HH:MM")
            yield Input(
                value=value.end.isoformat(timespec="minutes") if value else "08:00",
                placeholder="End HH:MM",
                id="notification-quiet-end",
            )
            yield Label("IANA timezone")
            yield Input(
                value=value.timezone if value else "Africa/Cairo",
                placeholder="IANA timezone",
                id="notification-quiet-timezone",
            )
            yield Label("Weekdays")
            yield Input(
                value=_weekday_text(value.weekdays)
                if value
                else "Mon, Tue, Wed, Thu, Fri, Sat, Sun",
                placeholder="Weekdays",
                id="notification-quiet-weekdays",
            )
            yield Label("During quiet hours")
            yield Select(
                tuple(
                    (item.value.replace("_", " ").title(), item.value)
                    for item in QuietHoursBehavior
                ),
                value=value.behavior.value if value else QuietHoursBehavior.QUEUE.value,
                allow_blank=False,
                id="notification-quiet-behavior",
            )
            yield Checkbox(
                "Enabled", value=value.enabled if value else True, id="notification-quiet-enabled"
            )
            yield Label("Bypass importance: high, normal, low")
            yield Input(
                value=", ".join(item.value for item in value.bypass_importance)
                if value
                else "high",
                placeholder="Bypass importance: high, normal, low",
                id="notification-quiet-importance",
            )
            yield Label("Bypass sender addresses")
            yield Input(
                value=", ".join(value.bypass_senders) if value else "",
                placeholder="Bypass sender addresses",
                id="notification-quiet-senders",
            )
            yield Label("Bypass sender domains")
            yield Input(
                value=", ".join(value.bypass_sender_domains) if value else "",
                placeholder="Bypass sender domains",
                id="notification-quiet-domains",
            )
            yield Label("Bypass matching rule IDs")
            yield Input(
                value=", ".join(value.bypass_rule_ids) if value else "",
                placeholder="Bypass matching rule IDs",
                id="notification-quiet-rules",
            )
            yield Static(
                "Cross-midnight windows and daylight-saving transitions are "
                "resolved by the policy engine.",
                markup=False,
            )
            yield Static("", id="notification-quiet-error", markup=False)
            with Horizontal(id="notification-quiet-actions"):
                yield Button("Cancel", id="notification-quiet-cancel")
                yield Button("Save window", id="notification-quiet-save", variant="primary")

    def build_value(self) -> QuietHoursWindow:
        importance = frozenset(
            Importance(item.casefold())
            for item in _csv(self.query_one("#notification-quiet-importance", Input).value)
        )
        return QuietHoursWindow(
            _identifier(self.query_one("#notification-quiet-id", Input).value, label="window ID"),
            _clock(self.query_one("#notification-quiet-start", Input).value, label="start"),
            _clock(self.query_one("#notification-quiet-end", Input).value, label="end"),
            self.query_one("#notification-quiet-timezone", Input).value.strip(),
            _weekdays(self.query_one("#notification-quiet-weekdays", Input).value),
            QuietHoursBehavior(_selected(self.query_one("#notification-quiet-behavior", Select))),
            importance,
            frozenset(_csv(self.query_one("#notification-quiet-senders", Input).value)),
            frozenset(_csv(self.query_one("#notification-quiet-domains", Input).value)),
            frozenset(_csv(self.query_one("#notification-quiet-rules", Input).value)),
            self.query_one("#notification-quiet-enabled", Checkbox).value,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "notification-quiet-cancel":
            self.dismiss(None)
        elif event.button.id == "notification-quiet-save":
            try:
                self.dismiss(self.build_value())
            except (TypeError, ValueError) as exc:
                self.query_one("#notification-quiet-error", Static).update(
                    sanitize_terminal(str(exc), max_length=500)
                )


class DigestEditorScreen(NotificationEditorScreen[DigestSchedule]):
    """Editor for interval and wall-clock digest schedules."""

    DEFAULT_CSS = """
    DigestEditorScreen { align: center middle; }
    #notification-digest-dialog {
        width: 96%; max-width: 86; height: auto; max-height: 94%;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-digest-dialog Input, #notification-digest-dialog Select { margin-bottom: 1; }
    #notification-digest-error { color: $error; min-height: 1; height: auto; }
    #notification-digest-actions { height: 3; align-horizontal: right; }
    #notification-digest-actions Button { margin-left: 1; }
    """

    def __init__(self, schedule: DigestSchedule | None) -> None:
        super().__init__()
        self.schedule = schedule

    def compose(self) -> ComposeResult:
        value = self.schedule
        cadence = value.cadence if value else DigestCadence.MORNING
        with VerticalScroll(id="notification-digest-dialog"):
            yield Label("Digest schedule · generated locally")
            yield Label("Schedule ID")
            yield Input(
                value=value.schedule_id if value else "",
                placeholder="Schedule ID",
                id="notification-digest-id",
                disabled=value is not None,
            )
            yield Label("Name")
            yield Input(
                value=value.name if value else "Morning digest",
                placeholder="Name",
                id="notification-digest-name",
            )
            yield Label("Delivery frequency")
            yield Select(
                tuple((item.value.replace("_", " ").title(), item.value) for item in DigestCadence),
                value=cadence.value,
                allow_blank=False,
                id="notification-digest-cadence",
            )
            yield Label("IANA timezone")
            yield Input(
                value=value.timezone if value else "Africa/Cairo",
                placeholder="IANA timezone",
                id="notification-digest-timezone",
            )
            yield Label("Daily delivery HH:MM")
            yield Input(
                value=value.delivery_time.isoformat(timespec="minutes")
                if value and value.delivery_time
                else "08:00",
                placeholder="Daily delivery HH:MM",
                id="notification-digest-time",
            )
            yield Label("Custom interval minutes")
            yield Input(
                value=str(value.interval_minutes or 30) if value else "30",
                placeholder="Custom interval minutes",
                id="notification-digest-interval",
            )
            yield Label("Maximum events per digest")
            yield Input(
                value=str(value.maximum_events) if value else "500",
                placeholder="Maximum events per digest",
                id="notification-digest-maximum",
            )
            yield Checkbox(
                "Enabled", value=value.enabled if value else True, id="notification-digest-enabled"
            )
            yield Static(
                "Interval schedules use 30/hourly presets or 5-1440 custom minutes. "
                "Daily schedules use the local delivery time.",
                markup=False,
            )
            yield Static("", id="notification-digest-error", markup=False)
            with Horizontal(id="notification-digest-actions"):
                yield Button("Cancel", id="notification-digest-cancel")
                yield Button("Save digest", id="notification-digest-save", variant="primary")

    def build_value(self) -> DigestSchedule:
        cadence = DigestCadence(_selected(self.query_one("#notification-digest-cadence", Select)))
        delivery_time = (
            _clock(self.query_one("#notification-digest-time", Input).value, label="delivery time")
            if cadence
            in {
                DigestCadence.MORNING,
                DigestCadence.EVENING,
                DigestCadence.DAILY,
                DigestCadence.CUSTOM_DAILY,
            }
            else None
        )
        interval = (
            _integer(
                self.query_one("#notification-digest-interval", Input).value,
                label="interval",
                minimum=5,
                maximum=1_440,
            )
            if cadence is DigestCadence.CUSTOM_INTERVAL
            else None
        )
        return DigestSchedule(
            _identifier(
                self.query_one("#notification-digest-id", Input).value, label="schedule ID"
            ),
            self.query_one("#notification-digest-name", Input).value.strip(),
            cadence,
            self.query_one("#notification-digest-timezone", Input).value.strip(),
            delivery_time,
            interval,
            _integer(
                self.query_one("#notification-digest-maximum", Input).value,
                label="maximum events",
                minimum=1,
                maximum=10_000,
            ),
            enabled=self.query_one("#notification-digest-enabled", Checkbox).value,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "notification-digest-cancel":
            self.dismiss(None)
        elif event.button.id == "notification-digest-save":
            try:
                self.dismiss(self.build_value())
            except (TypeError, ValueError) as exc:
                self.query_one("#notification-digest-error", Static).update(
                    sanitize_terminal(str(exc), max_length=500)
                )


@dataclass(frozen=True, slots=True)
class ProviderEdit:
    metadata: ProviderConfigurationMetadata
    policy: ProviderPolicy


class ProviderFilterEditorScreen(NotificationEditorScreen[Condition]):
    """Edit an additional provider predicate using the runtime condition schema."""

    DEFAULT_CSS = """
    ProviderFilterEditorScreen { align: center middle; }
    #notification-provider-filter-dialog {
        width: 96%; max-width: 96; height: 95%; max-height: 40;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-provider-filter-error { height: auto; color: $error; }
    #notification-provider-filter-actions { height: 3; }
    """

    def __init__(self, condition: Condition | None) -> None:
        super().__init__()
        self.condition = condition

    def compose(self) -> ComposeResult:
        initial = (
            condition_to_data(self.condition)
            if self.condition is not None
            else {
                "type": "text",
                "field": "folder",
                "operator": "exact",
                "values": ["Inbox"],
            }
        )
        with VerticalScroll(id="notification-provider-filter-dialog"):
            yield Label("Additional provider filter")
            yield Static(
                "Only matching events can reach this provider after rule routing. "
                "Unavailable fields do not match. Use filter keeps these edits in "
                "the provider form; Save provider applies them.",
                markup=False,
            )
            yield RuleBuilder(initial, [], provider_id="", condition_only=True)
            yield Static("", id="notification-provider-filter-error", markup=False)
            with HorizontalScroll(id="notification-provider-filter-actions"):
                yield Button("Cancel", id="notification-provider-filter-cancel")
                yield Button(
                    "Use filter", id="notification-provider-filter-save", variant="primary"
                )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "notification-provider-filter-cancel":
            self.dismiss(None)
        elif event.button.id == "notification-provider-filter-save":
            try:
                condition = condition_from_data(self.query_one(RuleBuilder).condition)
            except (TypeError, ValueError) as exc:
                self.query_one("#notification-provider-filter-error", Static).update(
                    sanitize_terminal(str(exc), max_length=600)
                )
            else:
                self.dismiss(condition)


class ProviderEditorScreen(NotificationEditorScreen[ProviderEdit]):
    """Edit non-secret provider metadata and fail-closed per-provider privacy."""

    DESKTOP_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "title",
            "duration",
            "sound",
            "group_by_folder",
            "show_attachment_indicator",
            "show_importance_indicator",
        }
    )

    DEFAULT_CSS = """
    ProviderEditorScreen { align: center middle; }
    #notification-provider-dialog {
        width: 97%; max-width: 96; height: 95%; max-height: 44;
        border: round $border; background: $surface; padding: 1 2;
    }
    #notification-provider-dialog Input, #notification-provider-dialog Select { margin-bottom: 1; }
    #notification-desktop-options { height: auto; }
    #notification-provider-flags { layout: grid; grid-size: 2; grid-rows: 3; height: 6; }
    #notification-provider-flags Checkbox { width: 1fr; min-width: 0; }
    #notification-provider-metadata { height: 7; }
    #notification-provider-filter-controls { height: 3; }
    #notification-provider-filter-summary { height: auto; }
    #notification-provider-error { color: $error; min-height: 1; height: auto; }
    #notification-provider-actions { height: 3; align-horizontal: right; }
    #notification-provider-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        metadata: ProviderConfigurationMetadata | None,
        policy: ProviderPolicy | None,
        *,
        digest_ids: tuple[str, ...],
    ) -> None:
        super().__init__()
        self.metadata = metadata
        self.policy = policy
        self.digest_ids = digest_ids
        self.filter_condition = policy.filter_condition if policy else None
        options = metadata.metadata if metadata and metadata.kind.casefold() == "desktop" else {}
        self.desktop_options: dict[str, Any] = {
            "title": "GUC Mail",
            "duration": "short",
            "sound": True,
            "group_by_folder": True,
            "show_attachment_indicator": True,
            "show_importance_indicator": True,
        }
        invalid = []
        for key, default in self.desktop_options.items():
            value = options.get(key, default)
            valid = (
                isinstance(value, str)
                and bool(value.strip())
                and len(value) <= 120
                and sanitize_terminal(value) == value
                if key == "title"
                else value in ("short", "long")
                if key == "duration"
                else type(value) is bool
            )
            if valid:
                self.desktop_options[key] = value
            else:
                invalid.append(key)
                if type(default) is bool:
                    self.desktop_options[key] = False
        self.desktop_warning = (
            "Saved desktop options are invalid: "
            + ", ".join(sorted(invalid))
            + ". Review the controls; Save replaces these values. Cancel preserves them."
            if invalid
            else ""
        )

    def compose(self) -> ComposeResult:
        metadata = self.metadata
        policy = self.policy
        privacy = (
            policy.privacy
            if policy
            else EventDisclosurePolicy(
                # Desktop toasts stay on this PC, so new ones show sender and subject.
                PrivacyMode.SENDER_AND_SUBJECT
                if metadata is None or metadata.kind == "desktop"
                else PrivacyMode.PRIVATE
            )
        )
        digest_options = (
            ("No digest schedule", ""),
            *tuple((item, item) for item in self.digest_ids),
        )
        with VerticalScroll(id="notification-provider-dialog"):
            yield Label("Provider & privacy · secrets remain outside this editor")
            yield Label("Provider ID")
            yield Input(
                value=metadata.provider_id if metadata else policy.provider_id if policy else "",
                placeholder="Provider ID",
                id="notification-provider-id",
                disabled=metadata is not None or policy is not None,
            )
            yield Label("Friendly name")
            yield Input(
                value=metadata.name if metadata else "",
                placeholder="Friendly name",
                id="notification-provider-name",
            )
            yield Label("Provider type")
            yield Select(
                (
                    ("Windows desktop", "desktop"),
                    ("Console", "console"),
                    ("JSON / NDJSON", "json"),
                    ("Discord", "discord"),
                    ("Generic webhook", "webhook"),
                    ("Slack-compatible", "slack"),
                    ("Microsoft Teams", "teams"),
                    ("Telegram", "telegram"),
                    ("Local command hook (expert)", "command"),
                ),
                value=metadata.kind if metadata else "desktop",
                allow_blank=False,
                id="notification-provider-kind",
            )
            with Vertical(id="notification-desktop-options"):
                yield Static(self.desktop_warning, markup=False, id="notification-desktop-warning")
                yield Label("Generic notification title")
                yield Input(
                    value=self.desktop_options["title"],
                    id="notification-desktop-title",
                )
                yield Label("Duration")
                yield Select(
                    (("Short", "short"), ("Long", "long")),
                    value=self.desktop_options["duration"],
                    allow_blank=False,
                    id="notification-desktop-duration",
                )
                yield Checkbox(
                    "Allow sound",
                    value=self.desktop_options["sound"],
                    id="notification-desktop-sound",
                )
                yield Checkbox(
                    "Group by folder",
                    value=self.desktop_options["group_by_folder"],
                    id="notification-desktop-group",
                )
                yield Checkbox(
                    "Attachment indicator",
                    value=self.desktop_options["show_attachment_indicator"],
                    id="notification-desktop-attachment",
                )
                yield Checkbox(
                    "Importance indicator",
                    value=self.desktop_options["show_importance_indicator"],
                    id="notification-desktop-importance",
                )
            yield Label("Privacy mode")
            yield Select(
                tuple((item.label, item.value) for item in PrivacyMode),
                value=privacy.mode.value,
                allow_blank=False,
                id="notification-provider-privacy",
            )
            yield Label("Optional payload field allowlist")
            yield Input(
                value=", ".join(sorted(privacy.field_allowlist)) if privacy.field_allowlist else "",
                placeholder="Optional payload field allowlist",
                id="notification-provider-allowlist",
            )
            yield Label("Digest schedule")
            yield Select(
                digest_options,
                value=policy.digest_schedule_id if policy and policy.digest_schedule_id else "",
                allow_blank=False,
                id="notification-provider-digest",
            )
            yield Label("Maximum notifications per burst")
            yield Input(
                value=str(policy.maximum_notifications_per_burst) if policy else "5",
                placeholder="Maximum notifications per burst",
                id="notification-provider-burst",
            )
            with Horizontal(id="notification-provider-flags"):
                yield Checkbox(
                    "Enabled",
                    value=policy.enabled if policy else False,
                    id="notification-provider-enabled",
                )
                yield Checkbox(
                    "Redact addresses",
                    value=privacy.redact_addresses,
                    id="notification-provider-redact",
                )
                yield Checkbox(
                    "Attachment names",
                    value=privacy.include_attachment_names,
                    id="notification-provider-attachments",
                )
                yield Checkbox(
                    "Server identifiers",
                    value=privacy.include_server_identifiers,
                    id="notification-provider-identifiers",
                )
            yield Label("Additional provider filter")
            yield Static(
                self._filter_summary(), id="notification-provider-filter-summary", markup=False
            )
            with HorizontalScroll(id="notification-provider-filter-controls"):
                yield Button("Edit filter", id="notification-provider-edit-filter")
                yield Button("Remove filter", id="notification-provider-remove-filter")
            with Collapsible(title="Advanced: non-secret metadata JSON", collapsed=True):
                yield TextArea(
                    json.dumps(
                        {
                            key: value
                            for key, value in metadata.metadata.items()
                            if metadata.kind.casefold() != "desktop" or key not in self.DESKTOP_KEYS
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    if metadata
                    else "{}",
                    id="notification-provider-metadata",
                )
            yield Static(
                "Webhook URLs, tokens, HMAC keys, and credentials are never accepted "
                "or displayed here. "
                "Configure them through the secure provider setup flow.",
                markup=False,
            )
            yield Static("", id="notification-provider-error", markup=False)
            with Horizontal(id="notification-provider-actions"):
                yield Button("Cancel", id="notification-provider-cancel")
                yield Button("Save provider", id="notification-provider-save", variant="primary")

    def build_value(self) -> ProviderEdit:
        provider_id = _identifier(
            self.query_one("#notification-provider-id", Input).value, label="provider ID"
        )
        name = self.query_one("#notification-provider-name", Input).value.strip()
        if not name:
            raise ValueError("provider name is required")
        allowlist_values = frozenset(
            _csv(self.query_one("#notification-provider-allowlist", Input).value)
        )
        unknown = allowlist_values - EVENT_PAYLOAD_FIELDS
        if unknown:
            raise ValueError(f"unknown payload fields: {', '.join(sorted(unknown))}")
        privacy = EventDisclosurePolicy(
            PrivacyMode(_selected(self.query_one("#notification-provider-privacy", Select))),
            allowlist_values or None,
            self.query_one("#notification-provider-redact", Checkbox).value,
            self.query_one("#notification-provider-attachments", Checkbox).value,
            self.query_one("#notification-provider-identifiers", Checkbox).value,
        )
        metadata_payload = _json_object(
            self.query_one("#notification-provider-metadata", TextArea).text, label="metadata"
        )
        kind = _selected(self.query_one("#notification-provider-kind", Select))
        if kind == "desktop":
            if self.DESKTOP_KEYS & metadata_payload.keys():
                raise ValueError(
                    "Use the desktop controls for title, duration, sound and indicators."
                )
            title = self.query_one("#notification-desktop-title", Input).value
            if not title.strip() or len(title) > 120 or sanitize_terminal(title) != title:
                raise ValueError("The generic desktop title needs 1-120 printable characters.")
            metadata_payload.update(
                title=title,
                duration=_selected(self.query_one("#notification-desktop-duration", Select)),
                sound=self.query_one("#notification-desktop-sound", Checkbox).value,
                group_by_folder=self.query_one("#notification-desktop-group", Checkbox).value,
                show_attachment_indicator=self.query_one(
                    "#notification-desktop-attachment", Checkbox
                ).value,
                show_importance_indicator=self.query_one(
                    "#notification-desktop-importance", Checkbox
                ).value,
            )
        enabled = self.query_one("#notification-provider-enabled", Checkbox).value
        metadata = ProviderConfigurationMetadata(
            provider_id,
            kind,
            name,
            enabled,
            self.metadata.secret_ref if self.metadata else None,
            metadata_payload,
        )
        digest = _selected(self.query_one("#notification-provider-digest", Select)) or None
        policy = ProviderPolicy(
            provider_id,
            enabled,
            privacy,
            filter_condition=self.filter_condition,
            digest_schedule_id=digest,
            maximum_notifications_per_burst=_integer(
                self.query_one("#notification-provider-burst", Input).value,
                label="burst limit",
                minimum=1,
                maximum=100,
            ),
        )
        return ProviderEdit(metadata, policy)

    def on_mount(self) -> None:
        self._desktop_visibility()

    @on(Select.Changed, "#notification-provider-kind")
    def _desktop_kind_changed(self) -> None:
        self._desktop_visibility()
        if self.policy is None:
            kind = _selected(self.query_one("#notification-provider-kind", Select))
            self.query_one("#notification-provider-privacy", Select).value = (
                PrivacyMode.SENDER_AND_SUBJECT.value
                if kind == "desktop"
                else PrivacyMode.PRIVATE.value
            )

    def _desktop_visibility(self) -> None:
        self.query_one("#notification-desktop-options").display = (
            _selected(self.query_one("#notification-provider-kind", Select)) == "desktop"
        )

    def _filter_summary(self) -> str:
        return (
            "An additional filter is active. Edit filter to inspect its conditions."
            if self.filter_condition is not None
            else "No additional filter. Every event routed here may reach this provider."
        )

    def _filter_resolved(self, condition: Condition | None) -> None:
        if condition is not None:
            self.filter_condition = condition
            self.query_one("#notification-provider-filter-summary", Static).update(
                self._filter_summary()
            )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "notification-provider-edit-filter":
            self.app.push_screen(
                ProviderFilterEditorScreen(self.filter_condition), self._filter_resolved
            )
        elif event.button.id == "notification-provider-remove-filter":
            self.filter_condition = None
            self.query_one("#notification-provider-filter-summary", Static).update(
                self._filter_summary()
            )
        elif event.button.id == "notification-provider-cancel":
            self.dismiss(None)
        elif event.button.id == "notification-provider-save":
            try:
                self.dismiss(self.build_value())
            except (TypeError, ValueError) as exc:
                self.query_one("#notification-provider-error", Static).update(
                    sanitize_terminal(str(exc), max_length=600)
                )


@dataclass(frozen=True, slots=True)
class WebhookSetup:
    config: WebhookConfig
    provider_kind: str
    signing_secret: str | None = field(default=None, repr=False, compare=False)


class WebhookSetupScreen(NotificationEditorScreen[WebhookSetup]):
    """Masked, secret-safe setup for one disabled outbound webhook."""

    DEFAULT_CSS = """
    WebhookSetupScreen { align: center middle; }
    #webhook-setup-dialog {
        width: 96%; max-width: 92; height: auto; max-height: 94%;
        border: round $border; background: $surface; padding: 1 2;
    }
    #webhook-setup-dialog Input, #webhook-setup-dialog Select { margin-bottom: 1; }
    #webhook-setup-error { color: $error; min-height: 1; height: auto; }
    #webhook-setup-actions { height: 3; align-horizontal: right; }
    #webhook-setup-actions Button { margin-left: 1; }
    """

    _LABELS: ClassVar[dict[str, str]] = {
        "webhook": "Generic HTTPS webhook",
        "discord": "Discord incoming webhook",
        "slack": "Slack-compatible incoming webhook",
        "teams": "Microsoft Teams workflow / webhook",
    }

    def __init__(self, provider_kind: str = "webhook") -> None:
        super().__init__()
        if provider_kind not in self._LABELS:
            raise ValueError("unsupported webhook provider kind")
        self.provider_kind = provider_kind

    def compose(self) -> ComposeResult:
        default_id = {
            "webhook": "webhook-main",
            "discord": "discord-main",
            "slack": "slack-main",
            "teams": "teams-main",
        }[self.provider_kind]
        safe_fields = (
            "event_id,event_type,folder,message_reference,sender,subject,"
            "occurred_at,importance,has_attachments,open_hint"
        )
        with VerticalScroll(id="webhook-setup-dialog"):
            yield Label(f"Configure {self._LABELS[self.provider_kind]}", markup=False)
            yield Static(
                "The destination is masked and copied to the OS secret store. The new "
                "definition is disabled until you explicitly resume it; setup sends nothing.",
                markup=False,
            )
            yield Label("Stable provider ID")
            yield Input(value=default_id, placeholder="Stable provider ID", id="webhook-name")
            yield Label("Secret HTTPS destination URL")
            yield Input(
                "",
                placeholder="Secret HTTPS destination URL",
                password=True,
                id="webhook-url",
            )
            yield Label("HTTP method")
            yield Select(
                (("POST", "POST"), ("PUT", "PUT"), ("PATCH", "PATCH")),
                value="POST",
                allow_blank=False,
                id="webhook-method",
                disabled=self.provider_kind != "webhook",
            )
            yield Label("Allowed payload fields, comma separated")
            yield Input(
                safe_fields,
                placeholder="Allowed payload fields, comma separated",
                id="webhook-fields",
            )
            yield Label("Maximum retries (0-10)")
            yield Input(
                "3",
                placeholder="Maximum retries (0-10)",
                id="webhook-retries",
            )
            yield Label("Optional HMAC secret (16+ bytes)")
            yield Input(
                "",
                placeholder="Optional HMAC secret (16+ bytes)",
                password=True,
                id="webhook-hmac-secret",
            )
            yield Static("", id="webhook-setup-error", markup=False)
            with Horizontal(id="webhook-setup-actions"):
                yield Button("Cancel", id="webhook-setup-cancel")
                yield Button("Store disabled", id="webhook-setup-save", variant="primary")

    def _clear_secrets(self) -> None:
        self.query_one("#webhook-url", Input).value = ""
        self.query_one("#webhook-hmac-secret", Input).value = ""

    def build_value(self) -> WebhookSetup:
        name = _identifier(self.query_one("#webhook-name", Input).value, label="webhook ID")
        url = self.query_one("#webhook-url", Input).value.strip()
        if not url:
            raise ValueError("webhook destination is required")
        if self.provider_kind == "discord":
            validate_discord_webhook_url(url)
        allowlist = frozenset(_csv(self.query_one("#webhook-fields", Input).value))
        unknown = allowlist - EVENT_FIELDS
        if unknown:
            raise ValueError(f"unknown payload fields: {', '.join(sorted(unknown))}")
        signing_secret = self.query_one("#webhook-hmac-secret", Input).value or None
        if signing_secret is not None and len(signing_secret.encode("utf-8")) < 16:
            raise ValueError("HMAC secret must contain at least 16 bytes")
        return WebhookSetup(
            WebhookConfig(
                name=name,
                url=url,
                method=_selected(self.query_one("#webhook-method", Select), default="POST"),
                field_allowlist=allowlist or None,
                enabled=False,
                max_retries=_integer(
                    self.query_one("#webhook-retries", Input).value,
                    label="maximum retries",
                    minimum=0,
                    maximum=10,
                ),
            ),
            self.provider_kind,
            signing_secret,
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "webhook-setup-cancel":
            self._clear_secrets()
            self.dismiss(None)
            return
        if event.button.id != "webhook-setup-save":
            return
        try:
            value = self.build_value()
        except (TypeError, ValueError) as exc:
            self.query_one("#webhook-setup-error", Static).update(
                sanitize_terminal(str(exc), max_length=500)
            )
            return
        self._clear_secrets()
        self.dismiss(value)


class NotificationConfirmationScreen(ModalScreen[bool]):
    """Confirmation dialog compatible with Textual versions without CSS ``min()``."""

    DEFAULT_CSS = """
    NotificationConfirmationScreen { align: center middle; }
    #operational-confirm-dialog {
        width: 92%; max-width: 72; height: auto; max-height: 80%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    #operational-confirm-title { text-style: bold; height: auto; margin-bottom: 1; }
    #operational-confirm-detail { height: auto; max-height: 12; }
    #operational-confirm-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #operational-confirm-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "reject", "Cancel", show=False),
        Binding("n", "reject", "Cancel", show=False),
        Binding("y", "accept", "Confirm", show=False),
    ]

    def __init__(self, title: str, detail: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.title_text = sanitize_terminal(title, max_length=120)
        self.detail_text = sanitize_terminal(detail, max_length=2_000)
        self.confirm_label = sanitize_terminal(confirm_label, max_length=40)

    def compose(self) -> ComposeResult:
        with Vertical(id="operational-confirm-dialog"):
            yield Label(self.title_text, id="operational-confirm-title", markup=False)
            yield Static(self.detail_text, id="operational-confirm-detail", markup=False)
            with Horizontal(id="operational-confirm-actions"):
                yield Button("Cancel", id="operational-confirm-no")
                yield Button(
                    self.confirm_label,
                    id="operational-confirm-yes",
                    variant="warning",
                )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "operational-confirm-yes":
            self.dismiss(True)
        elif event.button.id == "operational-confirm-no":
            self.dismiss(False)

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True, slots=True)
class NotificationSettingsSnapshot:
    profiles: PersistenceSnapshot[MonitoringProfile]
    active_id: str | None
    selected_id: str | None
    strategy: str
    providers: PersistenceSnapshot[ProviderConfigurationMetadata]
    webhooks: WebhookRegistryLoadResult | None
    webhook_error: PublicOperationError | None = None


class NotificationSettingsScreen(OperationalScreen):
    """Comprehensive local notification configuration and diagnostics surface."""

    DEFAULT_CSS = """
    NotificationSettingsScreen { layout: vertical; }
    #notification-settings-root { width: 100%; height: 100%; padding: 1 2; }
    #notification-settings-title { height: 2; text-style: bold; }
    #notification-switch-row { height: 3; }
    #notification-switch-label { width: auto; padding: 1 1 0 0; text-style: bold; }
    #notification-switch-status { width: 1fr; padding: 1 0 0 2; }
    #notification-switch-steps { height: auto; margin-bottom: 1; }
    #notification-advanced-label { height: 1; color: $text-muted; }
    #notification-profile-toolbar { height: 3; }
    #notification-profile-select { width: 1fr; margin-right: 1; }
    #notification-profile-toolbar Button { margin-right: 1; }
    #notification-tabs { height: 1fr; }
    #notification-tabs > ContentSwitcher { height: 1fr; }
    #notification-tabs TabPane { height: 1fr; }
    .notification-pane { width: 100%; height: 100%; padding: 1; }
    .notification-summary { height: auto; min-height: 3; margin-bottom: 1; }
    .notification-table { height: 1fr; min-height: 8; }
    .notification-actions { layout: grid; grid-rows: 3; height: auto; min-height: 3; }
    .notification-actions Button { width: 1fr; min-width: 0; margin-right: 1; }
    .notification-detail {
        height: auto; min-height: 4;
        border-top: solid $panel; padding: 1;
    }
    #notification-simulation-row { height: 3; }
    #notification-simulation-row Input { width: 1fr; margin-right: 1; }
    #notification-transfer-controls { height: 3; }
    #notification-transfer-controls Select { width: 1fr; margin-right: 1; }
    #notification-transfer-payload { height: 1fr; min-height: 10; }
    #notification-settings-footer { height: 3; align-horizontal: right; }
    #notification-settings-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "new_profile", "New profile", show=False),
        Binding("ctrl+s", "edit_profile", "Edit profile", show=False),
    ]

    def __init__(
        self,
        service: NotificationConfigurationService,
        *,
        webhook_registry: WebhookRegistry | None = None,
        webhook_delivery: WebhookDelivery | None = None,
        initial_profile_id: str | None = None,
        simulation_events: tuple[NewMailEvent, ...] = (),
        recent_simulation_source: RecentSimulationSource | None = None,
        automatic_replies_available: bool = False,
        desktop_setup: Callable[[], None] | None = None,
        switch: NotificationSwitch | None = None,
    ) -> None:
        super().__init__(area_name="notification-settings", offline=True)
        self.switch = switch
        self._switch_syncing = False
        self.service = service
        self.webhook_registry = webhook_registry
        self.webhook_delivery = webhook_delivery
        self.initial_profile_id = initial_profile_id
        self.simulation_events = simulation_events[:100]
        self.recent_simulation_source = recent_simulation_source
        self.automatic_replies_available = automatic_replies_available
        self.desktop_setup = desktop_setup
        self._profiles: dict[str, MonitoringProfile] = {}
        self._selected_profile_id: str | None = None
        self._selected_rule_id: str | None = None
        self._selected_quiet_id: str | None = None
        self._selected_digest_id: str | None = None
        self._selected_provider_id: str | None = None
        self._webhooks: dict[str, WebhookConfig] = {}
        self._webhook_kinds: dict[str, str] = {}
        self._provider_metadata: dict[str, ProviderConfigurationMetadata] = {}
        self._selected_strategy = "automatic"
        self._refresh_controls: dict[Button, bool] = {}
        self._pending_configuration_action: Callable[[], None] | None = None
        self._configuration_notice: str | None = None
        self._selected_webhook_name: str | None = None
        self._selected_route: tuple[str, str] | None = None
        self._import_preview_valid = False
        self._import_preview_fingerprint: str | None = None
        self._rule_detail_override: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="notification-settings-root"):
            yield Label("Notifications", id="notification-settings-title")
            if self.switch is not None:
                with Horizontal(id="notification-switch-row"):
                    yield Label("Notify me about new mail", id="notification-switch-label")
                    yield Switch(value=False, id="notification-switch")
                    yield Static("", id="notification-switch-status", markup=False)
                yield Static("", id="notification-switch-steps", markup=False)
                yield Label(
                    "More: rules, quiet hours, where alerts go, webhooks",
                    id="notification-advanced-label",
                )
            with Horizontal(id="notification-profile-toolbar"):
                yield Select((), prompt="Choose a profile", id="notification-profile-select")
                yield Button("New profile", id="notification-profile-new", variant="primary")
                yield Button("Edit", id="notification-profile-edit", disabled=True)
                yield Button("Make active", id="notification-profile-activate", disabled=True)
                yield Button(
                    "Delete", id="notification-profile-delete", variant="warning", disabled=True
                )
            yield Static("Loading local notification configuration…", id="area-state", markup=False)
            with TabbedContent(id="notification-tabs"):
                with TabPane("Overview", id="notification-overview-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield Static(
                            "",
                            id="notification-overview",
                            classes="notification-summary",
                            markup=False,
                        )
                        yield Button(
                            "Enable / disable profile",
                            id="notification-profile-toggle",
                            disabled=True,
                        )
                        yield Button(
                            "Automatic replies",
                            id="notification-automatic-replies",
                            disabled=not self.automatic_replies_available,
                        )
                        yield Button(
                            "Inbox rules",
                            id="notification-inbox-rules",
                            disabled=not self.automatic_replies_available,
                        )
                with TabPane("Rules", id="notification-rules-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield DataTable(
                            id="notification-rules-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        yield Static(
                            "Select a rule.",
                            id="notification-rule-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(id="notification-simulation-row"):
                            yield Input(
                                value="Inbox",
                                placeholder="Synthetic folder",
                                id="notification-sim-folder",
                            )
                            yield Input(
                                value="advisor@example.test",
                                placeholder="Synthetic sender",
                                id="notification-sim-sender",
                            )
                            yield Input(
                                value="Final exam schedule",
                                placeholder="Synthetic subject",
                                id="notification-sim-subject",
                            )
                        with Horizontal(classes="notification-actions"):
                            yield Button("New", id="notification-rule-new")
                            yield Button("Edit", id="notification-rule-edit")
                            yield Button("Duplicate", id="notification-rule-duplicate")
                            yield Button("Enable / disable", id="notification-rule-toggle")
                            yield Button("Move up", id="notification-rule-up")
                            yield Button("Move down", id="notification-rule-down")
                            yield Button(
                                "Simulate selected rule",
                                id="notification-rule-simulate",
                                variant="primary",
                            )
                            yield Button("Delete", id="notification-rule-delete", variant="warning")
                        yield Button(
                            "Simulate recent mail (read-only)",
                            id="notification-rule-recent",
                            disabled=self.recent_simulation_source is None,
                        )
                with TabPane("Quiet hours", id="notification-quiet-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield DataTable(
                            id="notification-quiet-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        yield Static(
                            "Quiet hours never contact Exchange.",
                            id="notification-quiet-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button("Add window", id="notification-quiet-new")
                            yield Button("Edit", id="notification-quiet-edit")
                            yield Button("Enable / disable", id="notification-quiet-toggle")
                            yield Button(
                                "Delete", id="notification-quiet-delete", variant="warning"
                            )
                with TabPane("Digests", id="notification-digest-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield DataTable(
                            id="notification-digest-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        yield Static(
                            "Digest summaries are generated locally.",
                            id="notification-digest-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button("Add schedule", id="notification-digest-new")
                            yield Button("Edit", id="notification-digest-edit")
                            yield Button("Enable / disable", id="notification-digest-toggle")
                            yield Button(
                                "Delete", id="notification-digest-delete", variant="warning"
                            )
                with TabPane("Destinations", id="notification-provider-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield DataTable(
                            id="notification-provider-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        yield Static(
                            "Provider secrets are intentionally hidden.",
                            id="notification-provider-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button("Add provider", id="notification-provider-new")
                            yield Button(
                                "Desktop setup…",
                                id="notification-desktop-setup",
                                disabled=self.desktop_setup is None,
                            )
                            yield Button("Edit policy", id="notification-provider-edit")
                            yield Button("Enable / disable", id="notification-provider-toggle")
                            yield Button(
                                "Remove from profile",
                                id="notification-provider-remove",
                                variant="warning",
                            )
                with TabPane("Webhooks", id="notification-webhook-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield Static(
                            "Secret destinations are never displayed. Setup and lifecycle "
                            "changes are local; Test sends only a confirmed synthetic event.",
                            classes="notification-summary",
                            markup=False,
                        )
                        yield DataTable(
                            id="notification-webhook-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        yield Static(
                            "No webhook selected.",
                            id="notification-webhook-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button("Add generic", id="notification-webhook-add")
                            yield Button("Add Discord", id="notification-webhook-add-discord")
                            yield Button("Add Slack", id="notification-webhook-add-slack")
                            yield Button("Add Teams", id="notification-webhook-add-teams")
                            yield Button("Test selected", id="notification-webhook-test")
                            yield Button("Pause", id="notification-webhook-pause")
                            yield Button(
                                "Resume",
                                id="notification-webhook-resume",
                                variant="primary",
                            )
                            yield Button(
                                "Remove",
                                id="notification-webhook-remove",
                                variant="warning",
                            )
                with TabPane("Routing", id="notification-routing-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield Static(
                            "Cycle each rule/provider destination: Off → Immediate → Digest → Off.",
                            classes="notification-summary",
                            markup=False,
                        )
                        yield DataTable(
                            id="notification-routing-table",
                            classes="notification-table",
                            cursor_type="row",
                            zebra_stripes=True,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button(
                                "Cycle selected route",
                                id="notification-route-cycle",
                                variant="primary",
                            )
                            yield Button("Disable selected route", id="notification-route-off")
                with TabPane("Import/export", id="notification-transfer-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        with Horizontal(id="notification-transfer-controls"):
                            yield Select(
                                (
                                    ("All profiles", "profiles"),
                                    ("Selected profile rules", "rules"),
                                    ("Provider metadata (no secrets)", "providers"),
                                ),
                                value="profiles",
                                allow_blank=False,
                                id="notification-transfer-kind",
                            )
                            yield Checkbox(
                                "Replace existing on import", id="notification-transfer-replace"
                            )
                        yield TextArea(
                            "",
                            id="notification-transfer-payload",
                            placeholder="Generate an export or paste a JSON document to import.",
                        )
                        yield Static(
                            "Imports are parsed and previewed before any local state changes.",
                            id="notification-transfer-detail",
                            classes="notification-detail",
                            markup=False,
                        )
                        with Horizontal(classes="notification-actions"):
                            yield Button(
                                "Generate redacted export",
                                id="notification-export",
                                variant="primary",
                            )
                            yield Button("Preview import", id="notification-import-preview")
                            yield Button(
                                "Apply import",
                                id="notification-import-apply",
                                disabled=True,
                                variant="warning",
                            )
                with TabPane("Diagnostics", id="notification-diagnostics-tab"):  # noqa: SIM117
                    with VerticalScroll(classes="notification-pane"):
                        yield Static(
                            "",
                            id="notification-diagnostics",
                            classes="notification-detail",
                            markup=False,
                        )
                        yield Button(
                            "Run local diagnostics",
                            id="notification-diagnostics-run",
                            variant="primary",
                        )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="notification-settings-footer"):
                yield Button("Retry", id="area-retry")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Refresh", id="notification-refresh")
                yield Button("Close", classes="area-close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        self._layout_action_rows()
        self.query_one("#notification-rules-table", DataTable).add_columns(
            "On", "Priority", "Rule", "Condition", "Actions"
        )
        self.query_one("#notification-quiet-table", DataTable).add_columns(
            "On", "Days", "Window", "Behavior", "Exceptions"
        )
        self.query_one("#notification-digest-table", DataTable).add_columns(
            "On", "Digest", "Cadence", "Delivery", "Limit"
        )
        self.query_one("#notification-provider-table", DataTable).add_columns(
            "On", "Provider", "Kind", "Privacy", "Burst", "Secret"
        )
        self.query_one("#notification-webhook-table", DataTable).add_columns(
            "On", "Webhook", "Kind", "Method", "Fields", "Signing"
        )
        self.query_one("#notification-routing-table", DataTable).add_columns(
            "Rule", "Provider", "Mode"
        )
        if self.switch is not None:
            self.query_one("#notification-switch-steps", Static).display = False
            self._show_switch(self.switch.notifications_on())
        self.call_after_refresh(self.action_refresh)

    # --- the one switch -------------------------------------------------------------

    def _show_switch(self, on: bool, result: SwitchResult | None = None) -> None:
        self._switch_syncing = True
        switch = self.query_one("#notification-switch", Switch)
        switch.value = on
        self._switch_syncing = False
        status = result.summary if result is not None else ("On" if on else "Off")
        self.query_one("#notification-switch-status", Static).update(status)
        steps = self.query_one("#notification-switch-steps", Static)
        if result is None:
            steps.display = False
            return
        steps.update(
            "\n".join(
                f"{'✓' if step.ok else '✗'} {step.label}: {step.detail}" for step in result.steps
            )
        )
        steps.display = True

    @on(Switch.Changed, "#notification-switch")
    def _switch_changed(self, event: Switch.Changed) -> None:
        if self._switch_syncing or self.switch is None:
            return
        self.turn_notifications(event.value)

    def turn_notifications(self, on: bool) -> None:
        """Run the full on/off setup in a worker; the settings below reload afterwards."""
        switch = self.switch
        if switch is None:
            return
        self.query_one("#notification-switch", Switch).disabled = True
        self.query_one("#notification-switch-status", Static).update(
            "Setting up…" if on else "Turning off…"
        )

        def done(value: object) -> None:
            self.query_one("#notification-switch", Switch).disabled = False
            result = cast(SwitchResult, value)
            self._show_switch(switch.notifications_on(), result)
            self.call_after_refresh(self.action_refresh)

        self._begin_operation(
            "Turning on notifications" if on else "Turning off notifications",
            lambda cancel, progress: switch.turn_on() if on else switch.turn_off(),
            done,
            timeout_seconds=120.0,
        )

    def _layout_action_rows(self) -> None:
        """Keep complete action labels and rows inside the scrollable pane."""

        columns = max(1, min(4, (self.size.width - 6) // 28))
        for row in self.query(".notification-actions"):
            count = len(row.query(Button))
            row.styles.grid_size_columns = min(columns, max(1, count))
            row.styles.height = 3 * ((count + columns - 1) // columns)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        event.widget.scroll_visible(animate=False, force=True, immediate=True)
        # Tab switches and resize can change the pane bounds after this focus event.
        self.call_after_refresh(self._reveal_focus)

    def on_resize(self, event: Resize) -> None:
        self._layout_action_rows()
        self.call_after_refresh(self._reveal_focus)

    def _reveal_focus(self) -> None:
        if self.focused is not None:
            self.focused.scroll_visible(animate=False, force=True, immediate=True)

    def _confirm(
        self,
        title: str,
        detail: str,
        confirmed: Callable[[], None],
        *,
        confirm_label: str = "Confirm",
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        """Ask for explicit consent without depending on unsupported CSS functions."""

        self.post_message(AreaAction(self.area_name, title, "confirmation-requested"))

        def resolved(value: bool | None) -> None:
            if value:
                confirmed()
            else:
                self.post_message(AreaAction(self.area_name, title, "cancelled"))
                if on_cancel is not None:
                    on_cancel()

        self.app.push_screen(
            NotificationConfirmationScreen(title, detail, confirm_label=confirm_label),
            resolved,
        )

    def _profile(self) -> MonitoringProfile | None:
        return self._profiles.get((self._selected_profile_id or "").casefold())

    def _rule(self) -> NotificationRule | None:
        profile = self._profile()
        if profile is None:
            return None
        return next(
            (item for item in profile.rules if item.rule_id == self._selected_rule_id), None
        )

    def _quiet(self) -> QuietHoursWindow | None:
        profile = self._profile()
        if profile is None:
            return None
        return next(
            (
                item
                for item in profile.quiet_hours.windows
                if item.window_id == self._selected_quiet_id
            ),
            None,
        )

    def _digest(self) -> DigestSchedule | None:
        profile = self._profile()
        if profile is None:
            return None
        return next(
            (
                item
                for item in profile.digest_schedules
                if item.schedule_id == self._selected_digest_id
            ),
            None,
        )

    def _provider_policy(self) -> ProviderPolicy | None:
        profile = self._profile()
        if profile is None:
            return None
        return next(
            (item for item in profile.providers if item.provider_id == self._selected_provider_id),
            None,
        )

    def _webhook(self) -> WebhookConfig | None:
        return self._webhooks.get((self._selected_webhook_name or "").casefold())

    def _change_local_configuration(
        self,
        action: str,
        change: Callable[[], object],
        on_saved: Callable[[object], None] | None = None,
    ) -> None:
        def operation(cancel: Event, progress: ProgressReporter) -> object:
            if cancel.is_set():
                raise PublicOperationError(
                    "The local change was cancelled before starting.", retryable=False
                )
            progress(OperationProgress(action))
            try:
                return change()
            except BaseException:
                raise PublicOperationError(
                    "The local notification change could not be completed. "
                    "Some changes may have completed; refresh the configuration "
                    "before another change.",
                    retryable=False,
                ) from None

        def saved(value: object) -> None:
            if on_saved is not None:
                on_saved(value)
            self.action_refresh()

        self._begin_operation(action, operation, saved, retry_safe=False)

    def _set_configuration_notice(self, message: str) -> None:
        self._configuration_notice = message

    def _read_local_configuration(
        self,
        action: str,
        read: Callable[[], object],
        on_loaded: Callable[[object], None],
    ) -> None:
        def operation(cancel: Event, progress: ProgressReporter) -> object:
            if cancel.is_set():
                raise PublicOperationError("The local read was cancelled.", retryable=False)
            progress(OperationProgress(action))
            try:
                result = read()
            except BaseException as exc:
                raise _safe_public_error(exc) from None
            if cancel.is_set():
                raise PublicOperationError("The local read was cancelled.", retryable=False)
            return result

        self._begin_operation(action, operation, on_loaded, retry_safe=True)

    def action_refresh(self) -> None:
        if self._operation_active:
            return
        requested_id = self._selected_profile_id or self.initial_profile_id
        self._begin_operation(
            "Loading notification settings",
            lambda cancel, progress: self._load_configuration(requested_id, cancel, progress),
            self._configuration_loaded,
            retry_safe=True,
        )

    def _set_view_state(self, state: ViewState, message: str) -> None:
        super()._set_view_state(state, message)
        if state in {ViewState.ERROR, ViewState.OFFLINE, ViewState.CANCELLED}:
            self._pending_configuration_action = None
            self._configuration_notice = None
        loading = self._operation_active
        selectors = self.query("#notification-profile-select")
        if selectors:
            selectors.first(Select).disabled = loading
        if loading and not self._refresh_controls:
            for button in self.query(Button):
                if button.id in {"area-retry", "area-cancel-operation", "notification-refresh"}:
                    continue
                if button.has_class("area-close"):
                    continue
                self._refresh_controls[button] = button.disabled
                button.disabled = True
        elif not loading:
            self._restore_refresh_controls()

    def _restore_refresh_controls(self) -> None:
        for button, disabled in self._refresh_controls.items():
            if button.is_mounted:
                button.disabled = disabled
        self._refresh_controls.clear()

    def _defer_until_loaded(self, action: Callable[[], None]) -> bool:
        if self._operation_active and self._active_action in {
            "Loading notification settings",
        }:
            self._pending_configuration_action = action
            return True
        return False

    def action_cancel_operation(self) -> None:
        self._pending_configuration_action = None
        super().action_cancel_operation()

    def action_close(self) -> None:
        self._pending_configuration_action = None
        super().action_close()

    def _load_configuration(
        self, requested_id: str | None, cancel: Event, progress: ProgressReporter
    ) -> NotificationSettingsSnapshot:
        def checkpoint() -> None:
            if cancel.is_set():
                raise PublicOperationError("Notification settings read cancelled.", retryable=False)

        try:
            checkpoint()
            progress(OperationProgress("Reading local notification metadata"))
            snapshot = self.service.profiles.inspect()
            profiles = snapshot.items
            checkpoint()
            active = self.service.profiles.active_profile()
            checkpoint()
            candidates = (requested_id, active.profile_id if active else None)
            keys = {profile.profile_id.casefold() for profile in profiles}
            selected_id = next(
                (value.casefold() for value in candidates if value and value.casefold() in keys),
                profiles[0].profile_id.casefold() if profiles else None,
            )
            strategy = (
                self.service.profiles.strategy_preference(selected_id) or "automatic"
                if selected_id
                else "automatic"
            )
            checkpoint()
            providers = self.service.providers.inspect()
            checkpoint()
        except BaseException as exc:
            raise _safe_public_error(exc) from None
        webhooks = None
        webhook_error = None
        if self.webhook_registry is not None:
            try:
                webhooks = self.webhook_registry.load()
            except BaseException as exc:
                webhook_error = _safe_public_error(exc)
            checkpoint()
        return NotificationSettingsSnapshot(
            snapshot,
            active.profile_id.casefold() if active else None,
            selected_id,
            strategy,
            providers,
            webhooks,
            webhook_error,
        )

    def _configuration_loaded(self, value: object) -> None:
        if not isinstance(value, NotificationSettingsSnapshot):
            raise PublicOperationError("Notification settings returned an invalid snapshot.")
        self._restore_refresh_controls()
        profiles = value.profiles.items
        self._profiles = {profile.profile_id.casefold(): profile for profile in profiles}
        self._selected_profile_id = value.selected_id
        self._selected_strategy = value.strategy
        self._provider_metadata = {
            item.provider_id.casefold(): item for item in value.providers.items
        }
        selector = self.query_one("#notification-profile-select", Select)
        selector.set_options(
            tuple((profile.name, profile.profile_id.casefold()) for profile in profiles)
        )
        if self._selected_profile_id is not None:
            selector.value = self._selected_profile_id
        self._render_configuration(
            value.active_id,
            value.profiles.diagnostics,
            value.strategy,
            value.providers,
        )
        self._render_webhooks(value.webhooks, value.webhook_error)
        pending = self._pending_configuration_action
        self._pending_configuration_action = None
        notice = self._configuration_notice
        self._configuration_notice = None
        if pending is not None and value.webhook_error is None:
            pending()
        elif notice is not None and value.webhook_error is None:
            self._set_view_state(ViewState.READY, notice)

    def _render_configuration(
        self,
        active_id: str | None,
        profile_diagnostics: tuple[object, ...],
        strategy: str,
        providers: PersistenceSnapshot[ProviderConfigurationMetadata],
    ) -> None:
        profile = self._profile()
        has_profile = profile is not None
        for button_id in (
            "notification-profile-edit",
            "notification-profile-activate",
            "notification-profile-delete",
            "notification-profile-toggle",
        ):
            self.query_one(f"#{button_id}", Button).disabled = not has_profile
        if profile is None:
            self.query_one("#notification-overview", Static).update(
                "No profiles yet. The switch above is all most people need. "
                "Press New profile for rules and quiet hours."
            )
            self._clear_tables()
            self._set_view_state(ViewState.EMPTY, "No profiles yet")
            return
        status = "Active" if active_id == profile.profile_id.casefold() else "Inactive"
        monitoring_state = "Monitoring enabled" if profile.enabled else "Monitoring disabled"
        overview = [
            f"{profile.name} · {status} · {monitoring_state}",
            f"Folders: {', '.join(profile.folders)}",
            f"Detection preference: {strategy.replace('_', ' ')} · "
            f"poll fallback {profile.poll_interval_seconds:g}s",
            f"Rules: {len(profile.rules)} · Providers: {len(profile.providers)} · "
            f"Quiet windows: {len(profile.quiet_hours.windows)} · "
            f"Digests: {len(profile.digest_schedules)}",
            "First-run baseline: "
            + (
                "notify existing items"
                if profile.notify_existing
                else "quiet baseline (recommended)"
            ),
        ]
        if profile_diagnostics or providers.diagnostics:
            diagnostic_count = len(profile_diagnostics) + len(providers.diagnostics)
            overview.append(f"Diagnostics: {diagnostic_count} invalid local row(s) isolated.")
        self.query_one("#notification-overview", Static).update("\n".join(overview))
        self._render_rules(profile)
        self._render_quiet(profile)
        self._render_digests(profile)
        self._render_providers(profile, providers.items)
        self._render_routes(profile)
        self._set_view_state(
            ViewState.READY, f"Loaded local settings for {clean_cell(profile.name)}."
        )

    def _clear_tables(self) -> None:
        for selector in (
            "#notification-rules-table",
            "#notification-quiet-table",
            "#notification-digest-table",
            "#notification-provider-table",
            "#notification-routing-table",
        ):
            self.query_one(selector, DataTable).clear()

    def _select_rendered_row(self, table: DataTable[Any], requested: str | None) -> str | None:
        keys = [str(key.value) for key in table.rows]
        selected = requested if requested in keys else keys[0] if keys else None
        if selected is not None:
            table.move_cursor(row=table.get_row_index(selected), animate=False)
        return selected

    def _render_rules(self, profile: MonitoringProfile) -> None:
        table = self.query_one("#notification-rules-table", DataTable)
        table.clear()
        for rule in profile.rules:
            condition = condition_to_data(rule.condition)
            actions = [action.kind.value for action in rule.actions]
            table.add_row(
                "✓" if rule.enabled else "-",
                str(rule.priority),
                clean_cell(rule.name, max_length=55),
                clean_cell(condition.get("type"), max_length=20),
                clean_cell(", ".join(actions), max_length=80),
                key=rule.rule_id,
            )
        self._selected_rule_id = self._select_rendered_row(table, self._selected_rule_id)
        self._show_rule_detail()

    def _show_rule_detail(self) -> None:
        if self._rule_detail_override is not None:
            self.query_one("#notification-rule-detail", Static).update(self._rule_detail_override)
            return
        rule = self._rule()
        if rule is None:
            self.query_one("#notification-rule-detail", Static).update(
                "No rule selected. New rules are disabled until you explicitly enable them."
            )
            return
        condition = json.dumps(
            condition_to_data(rule.condition), ensure_ascii=False, separators=(", ", ": ")
        )
        actions = ", ".join(
            f"{item.kind.value}{f' → {item.provider_id}' if item.provider_id else ''}"
            for item in rule.actions
        )
        self.query_one("#notification-rule-detail", Static).update(
            f"{clean_cell(rule.name, max_length=160)}\n"
            f"WHEN {clean_cell(condition, max_length=500)}\n"
            f"THEN {clean_cell(actions, max_length=300)}"
        )

    def _render_quiet(self, profile: MonitoringProfile) -> None:
        table = self.query_one("#notification-quiet-table", DataTable)
        table.clear()
        for window in profile.quiet_hours.windows:
            exceptions = (
                len(window.bypass_senders)
                + len(window.bypass_sender_domains)
                + len(window.bypass_rule_ids)
                + len(window.bypass_importance)
            )
            table.add_row(
                "✓" if window.enabled else "-",
                _weekday_text(window.weekdays),
                f"{window.start.isoformat(timespec='minutes')} → "
                f"{window.end.isoformat(timespec='minutes')} {window.timezone}",
                window.behavior.value,
                str(exceptions),
                key=window.window_id,
            )
        self._selected_quiet_id = self._select_rendered_row(table, self._selected_quiet_id)
        self._show_quiet_detail()

    def _show_quiet_detail(self) -> None:
        window = self._quiet()
        if window is None:
            self.query_one("#notification-quiet-detail", Static).update(
                "No quiet-hours window selected. Quiet hours never contact Exchange."
            )
            return
        exceptions = (
            f"importance={','.join(item.value for item in window.bypass_importance) or 'none'}; "
            f"senders={len(window.bypass_senders)}; "
            f"domains={len(window.bypass_sender_domains)}; "
            f"rules={len(window.bypass_rule_ids)}"
        )
        self.query_one("#notification-quiet-detail", Static).update(
            f"{window.window_id} · {window.behavior.value} · "
            f"{'enabled' if window.enabled else 'disabled'}\n"
            f"{_weekday_text(window.weekdays)} · "
            f"{window.start.isoformat(timespec='minutes')} → "
            f"{window.end.isoformat(timespec='minutes')} · {window.timezone}\n"
            f"Bypass exceptions: {exceptions}"
        )

    def _render_digests(self, profile: MonitoringProfile) -> None:
        table = self.query_one("#notification-digest-table", DataTable)
        table.clear()
        for schedule in profile.digest_schedules:
            delivery = (
                f"every {schedule.effective_interval_minutes} min"
                if schedule.effective_interval_minutes
                else (
                    f"{cast(time, schedule.effective_delivery_time).isoformat(timespec='minutes')} "
                    f"{schedule.timezone}"
                )
            )
            table.add_row(
                "✓" if schedule.enabled else "-",
                clean_cell(schedule.name, max_length=60),
                schedule.cadence.value,
                delivery,
                str(schedule.maximum_events),
                key=schedule.schedule_id,
            )
        self._selected_digest_id = self._select_rendered_row(table, self._selected_digest_id)
        self._show_digest_detail()

    def _show_digest_detail(self) -> None:
        schedule = self._digest()
        if schedule is None:
            self.query_one("#notification-digest-detail", Static).update(
                "No digest schedule selected. Digest summaries are generated locally."
            )
            return
        delivery = (
            f"every {schedule.effective_interval_minutes} minutes"
            if schedule.effective_interval_minutes
            else (
                f"at {cast(time, schedule.effective_delivery_time).isoformat(timespec='minutes')} "
                f"in {schedule.timezone}"
            )
        )
        used_by = [
            item.provider_id
            for item in cast(MonitoringProfile, self._profile()).providers
            if item.digest_schedule_id == schedule.schedule_id
        ]
        self.query_one("#notification-digest-detail", Static).update(
            f"{clean_cell(schedule.name, max_length=180)} · "
            f"{'enabled' if schedule.enabled else 'disabled'} · {delivery}\n"
            f"Maximum {schedule.maximum_events} events · used by: "
            f"{clean_cell(', '.join(used_by) or 'no providers', max_length=240)}"
        )

    def _render_providers(
        self, profile: MonitoringProfile, metadata_values: tuple[ProviderConfigurationMetadata, ...]
    ) -> None:
        metadata = {item.provider_id.casefold(): item for item in metadata_values}
        table = self.query_one("#notification-provider-table", DataTable)
        table.clear()
        for policy in profile.providers:
            details = metadata.get(policy.provider_id.casefold())
            table.add_row(
                "✓" if policy.enabled else "-",
                clean_cell(details.name if details else policy.provider_id, max_length=60),
                clean_cell(details.kind if details else "unbound", max_length=24),
                policy.privacy.mode.value,
                str(policy.maximum_notifications_per_burst),
                "Configured" if details and details.secret_ref else "Not configured",
                key=policy.provider_id,
            )
        self._selected_provider_id = self._select_rendered_row(table, self._selected_provider_id)
        self._show_provider_detail(metadata)

    def _show_provider_detail(
        self,
        metadata: dict[str, ProviderConfigurationMetadata] | None = None,
    ) -> None:
        policy = self._provider_policy()
        if policy is None:
            self.query_one("#notification-provider-detail", Static).update(
                "No provider selected. Provider secrets are intentionally hidden."
            )
            return
        if metadata is None:
            metadata = self._provider_metadata
        details = metadata.get(policy.provider_id.casefold())
        permitted = ", ".join(sorted(policy.privacy.permitted_fields)) or "none"
        self.query_one("#notification-provider-detail", Static).update(
            f"{clean_cell(details.name if details else policy.provider_id, max_length=180)} · "
            f"{clean_cell(details.kind if details else 'unbound')} · "
            f"{'enabled' if policy.enabled else 'disabled'}\n"
            f"Privacy: {policy.privacy.mode.value} · permitted fields: "
            f"{clean_cell(permitted, max_length=400)}\n"
            f"Digest: {policy.digest_schedule_id or 'none'} · burst limit: "
            f"{policy.maximum_notifications_per_burst} · secret: "
            f"{'Configured' if details and details.secret_ref else 'Not configured'}"
        )

    def _render_webhooks(
        self,
        loaded: WebhookRegistryLoadResult | None,
        error: PublicOperationError | None,
    ) -> None:
        table = self.query_one("#notification-webhook-table", DataTable)
        table.clear()
        if self.webhook_registry is None:
            self._webhooks = {}
            self.query_one("#notification-webhook-detail", Static).update(
                "Secure webhook setup is unavailable in this embedding."
            )
            return
        if error is not None:
            self._show_error("Load webhook settings", error)
            return
        if loaded is None:
            raise PublicOperationError("Webhook settings returned an invalid snapshot.")
        metadata = self._provider_metadata
        self._webhooks = {item.name.casefold(): item for item in loaded.configurations}
        self._webhook_kinds = {
            item.name.casefold(): metadata.get(
                item.name.casefold(),
                ProviderConfigurationMetadata(item.name, "webhook", item.name),
            ).kind
            for item in loaded.configurations
        }
        for config in loaded.configurations:
            allowlist = config.field_allowlist
            table.add_row(
                "✓" if config.enabled else "-",
                clean_cell(config.name, max_length=80),
                clean_cell(self._webhook_kinds[config.name.casefold()], max_length=24),
                config.method,
                str(len(allowlist)) if allowlist is not None else "all",
                "✓" if config.secret_keyring_name else "-",
                key=config.name,
            )
        self._selected_webhook_name = self._select_rendered_row(
            table, (self._selected_webhook_name or "").casefold() or None
        )
        self._show_webhook_detail(issue_count=len(loaded.issues))

    def _show_webhook_detail(self, *, issue_count: int = 0) -> None:
        config = self._webhook()
        if config is None:
            text = "No secure webhook is configured. New definitions start disabled."
            if issue_count:
                text += f" {issue_count} malformed or legacy definition(s) were isolated."
            self.query_one("#notification-webhook-detail", Static).update(text)
            return
        allowlist = ", ".join(sorted(config.field_allowlist or ())) or "all event fields"
        self.query_one("#notification-webhook-detail", Static).update(
            f"{clean_cell(config.name, max_length=120)} · "
            f"{self._webhook_kinds.get(config.name.casefold(), 'webhook')} · "
            f"{'enabled' if config.enabled else 'paused'}\n"
            "Destination: stored in OS secret storage (value hidden) · "
            f"method {config.method} · retries {config.max_retries}\n"
            f"Payload fields: {clean_cell(allowlist, max_length=400)} · "
            f"HMAC signing: {'configured' if config.secret_keyring_name else 'not configured'}"
        )

    def _render_routes(self, profile: MonitoringProfile) -> None:
        table = self.query_one("#notification-routing-table", DataTable)
        table.clear()
        for rule in profile.rules:
            for provider in profile.providers:
                table.add_row(
                    clean_cell(rule.name, max_length=55),
                    clean_cell(provider.provider_id, max_length=40),
                    profile.routing_matrix.mode_for(rule.rule_id, provider.provider_id).value,
                    key=f"{rule.rule_id}\x1f{provider.provider_id}",
                )
        requested = "\x1f".join(self._selected_route) if self._selected_route else None
        selected = self._select_rendered_row(table, requested)
        if selected is None:
            self._selected_route = None
        else:
            rule_id, provider_id = selected.split("\x1f", 1)
            self._selected_route = (rule_id, provider_id)

    @on(Select.Changed, "#notification-profile-select")
    def _profile_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            selected = event.value.casefold()
            selector = self.query_one("#notification-profile-select", Select)
            # set_options/value during rendering posts delayed Changed messages.
            # Only a new current selection should trigger another database refresh.
            if event.value != selector.value or selected == self._selected_profile_id:
                return
            self._selected_profile_id = selected
            self._selected_rule_id = None
            self._selected_quiet_id = None
            self._selected_digest_id = None
            self._selected_provider_id = None
            self._selected_route = None
            self.action_refresh()

    @on(DataTable.RowHighlighted, "#notification-rules-table")
    def _rule_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        selected = str(event.row_key.value)
        if selected != self._selected_rule_id:
            self._rule_detail_override = None
        self._selected_rule_id = selected
        self._show_rule_detail()

    @on(DataTable.RowHighlighted, "#notification-quiet-table")
    def _quiet_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_quiet_id = str(event.row_key.value)
        self._show_quiet_detail()

    @on(DataTable.RowHighlighted, "#notification-digest-table")
    def _digest_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_digest_id = str(event.row_key.value)
        self._show_digest_detail()

    @on(DataTable.RowHighlighted, "#notification-provider-table")
    def _provider_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_provider_id = str(event.row_key.value)
        self._show_provider_detail()

    @on(DataTable.RowHighlighted, "#notification-webhook-table")
    def _webhook_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._selected_webhook_name = str(event.row_key.value)
        self._show_webhook_detail()

    @on(DataTable.RowHighlighted, "#notification-routing-table")
    def _route_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        parts = str(event.row_key.value).split("\x1f", 1)
        if len(parts) == 2:
            self._selected_route = (parts[0], parts[1])

    def action_new_profile(self) -> None:
        if self._defer_until_loaded(self.action_new_profile):
            return
        self.app.push_screen(ProfileEditorScreen(None), self._profile_editor_resolved)

    def action_edit_profile(self) -> None:
        if self._defer_until_loaded(self.action_edit_profile):
            return
        profile = self._profile()
        if profile is None:
            return
        self.app.push_screen(
            ProfileEditorScreen(profile, strategy=self._selected_strategy),
            self._profile_editor_resolved,
        )

    def _show_rules_tab(self) -> None:
        self.query_one("#notification-tabs", TabbedContent).active = "notification-rules-tab"

    def action_configure_folder_scope(self, scope: str = "add", folder: str | None = None) -> None:
        """Persist a palette-selected watcher folder in the active profile."""
        if self._defer_until_loaded(lambda: self.action_configure_folder_scope(scope, folder)):
            return
        normalized_scope = scope.casefold().strip()
        if normalized_scope not in {"inbox", "add"}:
            self._show_error(
                "Configure monitored folders",
                PublicOperationError("Unknown monitoring-folder operation.", retryable=False),
            )
            return
        selected_folder = "Inbox" if normalized_scope == "inbox" else (folder or "").strip()
        if not selected_folder:
            self._set_view_state(
                ViewState.EMPTY,
                "Choose a folder in the command palette before adding it to monitoring.",
            )
            return
        if len(selected_folder) > 500 or any(
            character in selected_folder for character in ("\x00", "\r", "\n")
        ):
            self._show_error(
                "Configure monitored folders",
                PublicOperationError("The folder path is invalid.", retryable=False),
            )
            return
        fallback_profile = self._profile()

        def change() -> object:
            active = self.service.profiles.active_profile()
            profile = active or fallback_profile
            if profile is None:
                profile = MonitoringProfile(
                    "default",
                    "Default monitoring",
                    (selected_folder,),
                    poll_interval_seconds=60,
                    enabled=False,
                    notify_existing=False,
                )
            elif normalized_scope == "inbox":
                profile = replace(profile, folders=("Inbox",))
            elif selected_folder.casefold() not in {value.casefold() for value in profile.folders}:
                profile = replace(profile, folders=(*profile.folders, selected_folder))
            self.service.profiles.save_profile(profile)
            if active is None:
                self.service.profiles.activate(profile.profile_id)
            return profile

        def saved(value: object) -> None:
            profile = cast(MonitoringProfile, value)
            self._selected_profile_id = profile.profile_id.casefold()
            self._set_configuration_notice(
                f"Monitoring profile now watches: {', '.join(profile.folders)}."
            )

        self._change_local_configuration("Configure monitored folders", change, saved)

    def action_set_desktop_notifications(self, enabled: bool = True) -> None:
        """Turn notifications on or off (the whole setup when the switch is available)."""
        if self._defer_until_loaded(lambda: self.action_set_desktop_notifications(enabled)):
            return
        if self.switch is not None:
            self.turn_notifications(enabled)
            return

        def change() -> object:
            updated = self.service.set_desktop_notifications(enabled=enabled)
            return updated

        def saved(value: object) -> None:
            updated = cast(MonitoringProfile, value)
            self._selected_profile_id = updated.profile_id.casefold()
            state = "enabled" if enabled else "disabled"
            self._set_configuration_notice(
                f"Desktop notifications are {state} for {clean_cell(updated.name)}."
            )

        self._change_local_configuration("Update desktop notifications", change, saved)

    def action_mute_notifications(self) -> None:
        """Mute delivery without disabling monitoring or discarding checkpoints."""
        if self._defer_until_loaded(self.action_mute_notifications):
            return

        def change() -> object:
            updated = self.service.mute_notifications()
            return updated

        def saved(value: object) -> None:
            updated = cast(MonitoringProfile, value)
            self._selected_profile_id = updated.profile_id.casefold()
            self._set_configuration_notice(
                f"All notification destinations are muted for {clean_cell(updated.name)}; "
                "mailbox monitoring remains configured."
            )

        self._change_local_configuration("Mute notifications", change, saved)

    def action_enable_quiet_hours(self) -> None:
        """Enable every configured quiet window or create the safe nightly default."""
        if self._defer_until_loaded(self.action_enable_quiet_hours):
            return

        def change() -> object:
            updated = self.service.enable_quiet_hours()
            return updated

        def saved(value: object) -> None:
            updated = cast(MonitoringProfile, value)
            self._selected_profile_id = updated.profile_id.casefold()
            self._set_configuration_notice(
                f"Quiet hours are enabled for {clean_cell(updated.name)}; queued delivery "
                "preserves suppressed events."
            )

        self._change_local_configuration("Enable quiet hours", change, saved)

    def action_set_notification_privacy(self, mode: str = "private") -> None:
        """Persist one disclosure level across all active-profile destinations."""
        if self._defer_until_loaded(lambda: self.action_set_notification_privacy(mode)):
            return
        try:
            privacy_mode = PrivacyMode(mode)
        except ValueError as exc:
            self._show_error("Update notification privacy", _safe_public_error(exc))
            return

        def change() -> object:
            updated = self.service.set_notification_privacy(privacy_mode)
            return updated

        def saved(value: object) -> None:
            updated = cast(MonitoringProfile, value)
            self._selected_profile_id = updated.profile_id.casefold()
            self._set_configuration_notice(f"Notifications now show: {privacy_mode.label.lower()}.")

        self._change_local_configuration("Update notification privacy", change, saved)

    def action_create_rule(self, preset: str | None = None, value: str | None = None) -> None:
        """Open the persisted rule editor, or profile setup on first use."""

        if self._defer_until_loaded(lambda: self.action_create_rule(preset, value)):
            return
        self._show_rules_tab()
        if self._profile() is None:
            self._set_view_state(
                ViewState.EMPTY,
                "Create a monitoring profile before adding its first notification rule.",
            )
            self.action_new_profile()
            return
        condition: dict[str, Any] | None = None
        names = {
            "sender": "Messages from sender",
            "subject": "Messages matching subject",
            "importance": "Important messages",
            "has_attachments": "Messages with attachments",
        }
        identifiers = {
            "sender": "monitor-sender",
            "subject": "monitor-subject",
            "importance": "monitor-important",
            "has_attachments": "monitor-attachments",
        }
        if preset in {"sender", "subject"}:
            normalized_value = (value or "").strip()
            if not normalized_value:
                self._set_view_state(
                    ViewState.EMPTY,
                    f"Select or enter a {preset} before creating this rule.",
                )
                return
            condition = {
                "type": "text",
                "field": preset,
                "operator": "contains" if preset == "subject" else "exact",
                "values": [normalized_value[:500]],
                "case_sensitive": False,
            }
        elif preset == "importance":
            condition = {
                "type": "choice",
                "field": "importance",
                "values": ["high"],
                "negate": False,
            }
        elif preset == "has_attachments":
            condition = {
                "type": "boolean",
                "field": "has_attachments",
                "expected": True,
            }
        profile = cast(MonitoringProfile, self._profile())
        existing = {rule.rule_id.casefold() for rule in profile.rules}
        base = identifiers.get(preset or "", "notification-rule")
        candidate = base
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        self._open_rule_editor(
            None,
            initial_id=candidate,
            initial_name=names.get(preset or "", "New notification rule"),
            initial_condition=condition,
        )

    def action_edit_rule(self) -> None:
        """Edit the highlighted persisted rule from any command entry point."""

        if self._defer_until_loaded(self.action_edit_rule):
            return

        self._show_rules_tab()
        rule = self._rule()
        if rule is None:
            self._set_view_state(ViewState.EMPTY, "Select a notification rule to edit.")
            return
        self._open_rule_editor(rule)

    def action_delete_rule(self) -> None:
        """Preview and explicitly confirm deletion of the highlighted rule."""

        if self._defer_until_loaded(self.action_delete_rule):
            return

        self._show_rules_tab()
        rule = self._rule()
        if rule is None:
            self._set_view_state(ViewState.EMPTY, "Select a notification rule to delete.")
            return
        self._confirm(
            "Delete this notification rule?",
            f"Remove '{clean_cell(rule.name)}' and its routing cells from this local profile?",
            lambda: self._delete_rule(rule.rule_id),
            confirm_label="Delete rule",
        )

    def _show_webhook_tab(self) -> None:
        self.query_one("#notification-tabs", TabbedContent).active = "notification-webhook-tab"

    def _resolve_webhook(
        self,
        name: str | None = None,
        provider_kind: str | None = None,
    ) -> WebhookConfig | None:
        if self.webhook_registry is None:
            return None
        requested_name = (name or "").strip()
        if requested_name:
            selected = self._webhooks.get(requested_name.casefold())
            if selected is not None:
                self._selected_webhook_name = selected.name.casefold()
                table = self.query_one("#notification-webhook-table", DataTable)
                self._select_rendered_row(table, self._selected_webhook_name)
            return selected
        normalized_kind = (provider_kind or "").casefold().strip()
        if normalized_kind:
            selected = next(
                (
                    config
                    for key, config in self._webhooks.items()
                    if self._webhook_kinds.get(key, "webhook").casefold() == normalized_kind
                ),
                None,
            )
            if selected is not None:
                self._selected_webhook_name = selected.name.casefold()
                table = self.query_one("#notification-webhook-table", DataTable)
                self._select_rendered_row(table, self._selected_webhook_name)
            return selected
        return self._webhook()

    def action_show_webhook_configuration(self, name: str | None = None) -> None:
        """Focus the secure webhook configuration without exposing its destination."""
        if self._defer_until_loaded(lambda: self.action_show_webhook_configuration(name)):
            return

        self._show_webhook_tab()
        selected = self._resolve_webhook(name)
        if name and selected is None:
            self._set_view_state(
                ViewState.EMPTY,
                f"No webhook named {clean_cell(name, max_length=120)} is configured.",
            )
            return
        self._show_webhook_detail()

    def action_configure_webhook(self, provider_kind: str = "webhook") -> None:
        """Open a masked setup form; no provider or network call is made."""
        if self._defer_until_loaded(lambda: self.action_configure_webhook(provider_kind)):
            return

        self._show_webhook_tab()
        if self.webhook_registry is None:
            self._set_view_state(ViewState.EMPTY, "Secure webhook storage is unavailable.")
            return
        try:
            editor = WebhookSetupScreen(provider_kind)
        except ValueError as exc:
            self._show_error("Configure webhook", _safe_public_error(exc))
            return
        self.app.push_screen(editor, self._webhook_setup_resolved)

    def _webhook_setup_resolved(self, value: WebhookSetup | None) -> None:
        if value is None or self.webhook_registry is None:
            return
        display_name = WebhookSetupScreen._LABELS[value.provider_kind]
        profile = self._profile()
        registry = self.webhook_registry

        def change() -> object:
            stored = registry.add(value.config, signing_secret=value.signing_secret)
            self.service.providers.save_provider(
                ProviderConfigurationMetadata(
                    stored.name,
                    value.provider_kind,
                    display_name,
                    False,
                    stored.url,
                    {
                        "method": stored.method,
                        "webhook_name": stored.name,
                        "field_allowlist": tuple(sorted(stored.field_allowlist or ())),
                    },
                )
            )
            if profile is not None and stored.name.casefold() not in {
                item.provider_id.casefold() for item in profile.providers
            }:
                self.service.replace_provider_policies(
                    profile.profile_id, (*profile.providers, ProviderPolicy(stored.name, False))
                )
            return stored

        def saved(value: object) -> None:
            stored = cast(WebhookConfig, value)
            self._selected_webhook_name = stored.name
            self._show_webhook_tab()
            self._set_configuration_notice(
                f"Stored {clean_cell(stored.name)} disabled; no webhook request was sent."
            )

        self._change_local_configuration("Configure webhook", change, saved)

    def _set_profile_provider_enabled(self, provider_id: str, *, enabled: bool) -> None:
        for profile in self.service.profiles.list_profiles():
            if provider_id.casefold() not in {
                item.provider_id.casefold() for item in profile.providers
            }:
                continue
            updated = tuple(
                replace(item, enabled=enabled)
                if item.provider_id.casefold() == provider_id.casefold()
                else item
                for item in profile.providers
            )
            self.service.replace_provider_policies(profile.profile_id, updated)

    def _set_webhook_enabled(self, provider_id: str | None = None, *, enabled: bool) -> None:
        config = self._resolve_webhook(provider_id)
        if config is None or self.webhook_registry is None:
            return
        registry = self.webhook_registry

        def change() -> object:
            registry.set_enabled(config.name, enabled=enabled)
            details = self.service.providers.get_provider(config.name)
            if details is not None:
                self.service.providers.set_enabled(config.name, enabled=enabled)
            self._set_profile_provider_enabled(config.name, enabled=enabled)
            return None

        def saved(value: object) -> None:
            self._show_webhook_tab()
            self._set_configuration_notice(
                f"{clean_cell(config.name)} is {'enabled' if enabled else 'paused'}; "
                "no test event was sent."
            )

        self._change_local_configuration("Update webhook", change, saved)

    def action_pause_webhook(self) -> None:
        if self._defer_until_loaded(self.action_pause_webhook):
            return
        self._show_webhook_tab()
        self._set_webhook_enabled(enabled=False)

    def action_resume_webhook(self) -> None:
        if self._defer_until_loaded(self.action_resume_webhook):
            return
        self._show_webhook_tab()
        config = self._webhook()
        if config is None:
            self._set_view_state(ViewState.EMPTY, "Select a webhook to resume.")
            return
        self._confirm(
            "Enable outbound webhook delivery?",
            f"Future watcher runs may send the permitted fields to '{clean_cell(config.name)}'. "
            "This confirmation sends nothing now.",
            lambda: self._set_webhook_enabled(config.name, enabled=True),
            confirm_label="Enable webhook",
        )

    def _remove_selected_webhook(self, provider_id: str | None = None) -> None:
        config = self._resolve_webhook(provider_id)
        if config is None or self.webhook_registry is None:
            return
        provider_id = config.name
        registry = self.webhook_registry

        def change() -> object:
            registry.set_enabled(provider_id, enabled=False)
            for profile in self.service.profiles.list_profiles():
                if provider_id.casefold() not in {
                    item.provider_id.casefold() for item in profile.providers
                }:
                    continue
                providers = tuple(
                    item
                    for item in profile.providers
                    if item.provider_id.casefold() != provider_id.casefold()
                )
                matrix = RoutingMatrix(
                    tuple(
                        item
                        for item in profile.routing_matrix.provider_ids
                        if item.casefold() != provider_id.casefold()
                    ),
                    tuple(
                        cell
                        for cell in profile.routing_matrix.cells
                        if cell.provider_id.casefold() != provider_id.casefold()
                    ),
                )
                self.service.profiles.save_profile(
                    replace(profile, providers=providers, routing_matrix=matrix)
                )
            self.service.providers.delete_provider(provider_id)
            removed = registry.remove(provider_id)
            if not removed:
                raise ValueError("the selected webhook no longer exists")
            return None

        def saved(value: object) -> None:
            self._selected_webhook_name = None
            self._show_webhook_tab()
            self._set_configuration_notice(
                f"Removed local webhook definition {clean_cell(provider_id)}."
            )

        self._change_local_configuration("Remove webhook", change, saved)

    def action_remove_webhook(self) -> None:
        if self._defer_until_loaded(self.action_remove_webhook):
            return
        self._show_webhook_tab()
        config = self._webhook()
        if config is None:
            self._set_view_state(ViewState.EMPTY, "Select a webhook to remove.")
            return
        self._confirm(
            "Remove this webhook definition?",
            f"Remove '{clean_cell(config.name)}', its provider policy, and routing cells? "
            "Mailbox data and delivery history are not changed.",
            lambda: self._remove_selected_webhook(config.name),
            confirm_label="Remove webhook",
        )

    def _execute_webhook_test(
        self,
        config: WebhookConfig,
        event: NotificationEvent,
        cancel: Event,
        progress: ProgressReporter,
    ) -> DeliveryResult:
        if cancel.is_set():
            raise PublicOperationError("The webhook test was cancelled.", retryable=False)
        if self.webhook_delivery is None:
            raise PublicOperationError(
                "Outbound webhook testing is unavailable in this offline session.",
                retryable=False,
            )
        if self.webhook_registry is not None:
            try:
                current = self.webhook_registry.get(config.name)
            except BaseException:
                raise PublicOperationError(
                    "The webhook configuration could not be checked. "
                    "Refresh before sending a test.",
                    retryable=False,
                ) from None
            if current != config:
                raise PublicOperationError(
                    "The webhook configuration changed after selection. "
                    "Refresh and confirm another test.",
                    retryable=False,
                )
        if cancel.is_set():
            raise PublicOperationError("The webhook test was cancelled.", retryable=False)
        progress(OperationProgress("Sending one synthetic event", completed=0, total=1))
        try:
            result = self.webhook_delivery(config, event)
        except BaseException as exc:
            raise PublicOperationError(
                "The synthetic webhook test failed. The destination remains secret; "
                "open delivery history for the redacted result."
            ) from exc
        progress(OperationProgress("Synthetic event completed", completed=1, total=1))
        return result

    def _webhook_test_completed(self, result: object) -> None:
        if not isinstance(result, DeliveryResult):
            raise PublicOperationError("The webhook provider returned an invalid result.")
        self.action_refresh()
        self._show_webhook_tab()
        detail = sanitize_terminal(result.detail or "No provider detail.", max_length=240)
        if result.state is DeliveryState.DELIVERED:
            self._set_view_state(
                ViewState.READY,
                f"Synthetic webhook test delivered through {clean_cell(result.provider)}. {detail}",
            )
            return
        self._set_view_state(
            ViewState.ERROR,
            f"Synthetic webhook test ended as {result.state.value}: {detail}",
        )

    def _start_webhook_test(self, config: WebhookConfig) -> None:
        now = datetime.now(UTC)
        event = NotificationEvent(
            event_id=f"test-webhook-{now.strftime('%Y%m%dT%H%M%S%fZ')}",
            event_type="test",
            occurred_at=now,
            account_id="synthetic",
            subject="Synthetic webhook test",
            preview="No mailbox data was used.",
        )
        self._begin_operation(
            "Testing webhook",
            lambda cancel, progress: self._execute_webhook_test(
                config,
                event,
                cancel,
                progress,
            ),
            self._webhook_test_completed,
            timeout_seconds=30.0,
        )

    def action_test_webhook(
        self,
        name: str | None = None,
        provider_kind: str | None = None,
        *,
        confirmed: bool = False,
    ) -> None:
        """Send one privacy-safe synthetic event through the exact selected provider."""
        if self._defer_until_loaded(
            lambda: self.action_test_webhook(name, provider_kind, confirmed=confirmed)
        ):
            return

        self._show_webhook_tab()
        config = self._resolve_webhook(name, provider_kind)
        if config is None:
            qualifier = f" {provider_kind}" if provider_kind else ""
            self._set_view_state(
                ViewState.EMPTY,
                f"Configure or select a{qualifier} webhook before testing it.",
            )
            return
        if self.webhook_delivery is None:
            self._set_view_state(
                ViewState.OFFLINE,
                "Outbound webhook testing is unavailable in this offline session.",
            )
            return
        if confirmed:
            self._start_webhook_test(config)
            return
        self._confirm(
            "Send a synthetic webhook test?",
            f"Send one privacy-safe synthetic event through '{clean_cell(config.name)}'? "
            "No mailbox message or body is used.",
            lambda: self._start_webhook_test(config),
            confirm_label="Send test",
        )

    def _profile_editor_resolved(self, value: ProfileEdit | None) -> None:
        if value is None:
            return

        def change() -> object:
            current = self.service.profiles.get_profile(value.profile_id)
            if current is None:
                profile = MonitoringProfile(
                    value.profile_id,
                    value.name,
                    value.folders,
                    poll_interval_seconds=value.poll_interval_seconds,
                    enabled=value.enabled,
                    notify_existing=value.notify_existing,
                )
            else:
                profile = replace(
                    current,
                    name=value.name,
                    folders=value.folders,
                    poll_interval_seconds=value.poll_interval_seconds,
                    enabled=value.enabled,
                    notify_existing=value.notify_existing,
                )
            self.service.profiles.save_profile(profile)
            self.service.profiles.set_strategy_preference(profile.profile_id, value.strategy)
            return profile.profile_id.casefold()

        def saved(value: object) -> None:
            self._selected_profile_id = cast(str | None, value)

        self._change_local_configuration("Save monitoring profile", change, saved)

    def _activate_profile(self) -> None:
        profile = self._profile()
        if profile is None:
            return

        def change() -> object:
            self.service.profiles.activate(profile.profile_id)
            return None

        self._change_local_configuration("Activate monitoring profile", change)

    def _delete_profile(self) -> None:
        profile = self._profile()
        if profile is None:
            return

        def change() -> object:
            self.service.profiles.delete_profile(profile.profile_id)
            return None

        def saved(value: object) -> None:
            self._selected_profile_id = cast(str | None, value)

        self._change_local_configuration("Delete monitoring profile", change, saved)

    def _toggle_profile(self) -> None:
        profile = self._profile()
        if profile is None:
            return

        def change() -> object:
            self.service.profiles.set_enabled(profile.profile_id, enabled=not profile.enabled)
            return None

        self._change_local_configuration("Update monitoring profile", change)

    def _provider_fallback(self) -> str:
        profile = self._profile()
        return profile.providers[0].provider_id if profile and profile.providers else "desktop"

    def _open_rule_editor(
        self,
        rule: NotificationRule | None,
        *,
        initial_id: str = "",
        initial_name: str = "",
        initial_condition: dict[str, Any] | None = None,
    ) -> None:
        profile = self._profile()
        if profile is None:
            return
        self.app.push_screen(
            RuleEditorScreen(
                rule,
                next_order=len(profile.rules),
                provider_id=self._provider_fallback(),
                initial_id=initial_id,
                initial_name=initial_name,
                initial_condition=initial_condition,
            ),
            self._rule_editor_resolved,
        )

    def _rule_editor_resolved(self, rule: NotificationRule | None) -> None:
        profile = self._profile()
        if profile is None or rule is None:
            return

        def change() -> object:
            self.service.rules(profile.profile_id).save_rule(rule)
            return rule.rule_id

        def saved(value: object) -> None:
            self._selected_rule_id = cast(str | None, value)

        self._change_local_configuration("Save notification rule", change, saved)

    def _duplicate_rule(self) -> None:
        profile = self._profile()
        rule = self._rule()
        if profile is None or rule is None:
            return
        base = f"{rule.rule_id}-copy"
        existing = {item.rule_id.casefold() for item in profile.rules}
        candidate = base
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1

        def change() -> object:
            duplicate = self.service.rules(profile.profile_id).duplicate_rule(
                rule.rule_id, new_id=candidate, new_name=f"{rule.name} copy"
            )
            return duplicate.rule_id

        def saved(value: object) -> None:
            self._selected_rule_id = cast(str | None, value)

        self._change_local_configuration("Duplicate notification rule", change, saved)

    def _toggle_rule(self) -> None:
        profile = self._profile()
        rule = self._rule()
        if profile is None or rule is None:
            return

        def change() -> object:
            self.service.rules(profile.profile_id).set_enabled(
                rule.rule_id, enabled=not rule.enabled
            )
            return None

        self._change_local_configuration("Update notification rule", change)

    def _move_rule(self, offset: int) -> None:
        profile = self._profile()
        rule = self._rule()
        if profile is None or rule is None:
            return
        rules = list(profile.rules)
        index = next((i for i, item in enumerate(rules) if item.rule_id == rule.rule_id), -1)
        target = index + offset
        if index < 0 or not 0 <= target < len(rules):
            return
        rules[index], rules[target] = rules[target], rules[index]

        def change() -> object:
            self.service.rules(profile.profile_id).reorder(tuple(item.rule_id for item in rules))
            return None

        self._change_local_configuration("Reorder notification rules", change)

    def _delete_rule(self, rule_id: str | None = None) -> None:
        profile = self._profile()
        selected_rule = rule_id or self._selected_rule_id
        if profile is None or selected_rule is None:
            return

        def change() -> object:
            self.service.rules(profile.profile_id).delete_rule(selected_rule)
            return None

        def saved(value: object) -> None:
            self._selected_rule_id = cast(str | None, value)

        self._change_local_configuration("Delete notification rule", change, saved)

    def _synthetic_event(self) -> NewMailEvent:
        now = datetime.now(UTC)
        sender_text = self.query_one("#notification-sim-sender", Input).value.strip()
        sender = Address(sender_text, "Synthetic sender") if sender_text else None
        return NewMailEvent(
            "settings-synthetic-event",
            now,
            now,
            "settings-synthetic-account",
            folder=self.query_one("#notification-sim-folder", Input).value.strip() or None,
            message_reference="synthetic-local-reference",
            sender=sender,
            subject=self.query_one("#notification-sim-subject", Input).value,
            importance=Importance.HIGH,
            is_read=False,
            has_attachments=False,
        )

    @on(Button.Pressed, "#notification-inbox-rules")
    def _inbox_rules_button(self, event: Button.Pressed) -> None:
        event.stop()
        if self.automatic_replies_available:
            self.post_message(InboxRulesRequested())

    @on(Button.Pressed, "#notification-automatic-replies")
    def _automatic_replies_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_automatic_replies()

    def action_automatic_replies(self) -> None:
        if self._defer_until_loaded(self.action_automatic_replies):
            return
        if self.automatic_replies_available:
            self.post_message(AutomaticRepliesRequested())

    def _simulate_rules(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        events = self.simulation_events or (self._synthetic_event(),)
        rule = self._rule()
        self._render_simulation(
            profile.profile_id,
            events,
            source="Synthetic / supplied sample",
            rule_id=rule.rule_id if rule is not None else None,
        )

    @on(Button.Pressed, "#notification-rule-recent")
    def _simulate_recent(self, event: Button.Pressed) -> None:
        event.stop()
        profile = self._profile()
        source = self.recent_simulation_source
        if profile is None or source is None:
            return
        rule = self._rule()
        rule_id = rule.rule_id if rule is not None else None
        self._begin_operation(
            "Testing on recent mail",
            lambda cancel, progress: source(profile, cancel, progress),
            lambda result: self._recent_sample_loaded(profile.profile_id, result, rule_id=rule_id),
            retry_safe=True,
        )

    def _recent_sample_loaded(
        self,
        profile_id: str,
        result: object,
        *,
        rule_id: str | None = None,
    ) -> None:
        if not isinstance(result, NotificationSample):
            raise PublicOperationError("Recent-mail sampling returned an invalid result.")
        source = (
            f"Recent metadata sample · {len(result.folders)} folders · limit {result.limit} · "
            f"omitted folders: {result.omitted_folder_count}\n"
            "Body, recipient and attachment-name rules require fields absent from this sample."
        )
        self._render_simulation(profile_id, result.events, source=source, rule_id=rule_id)

    def _render_simulation(
        self,
        profile_id: str,
        events: tuple[NewMailEvent, ...],
        *,
        source: str,
        rule_id: str | None = None,
    ) -> None:
        if not events:
            self._rule_detail_override = f"{source}\nNo mail returned. No provider called."
            self.query_one("#notification-rule-detail", Static).update(self._rule_detail_override)
            self._set_view_state(
                ViewState.EMPTY, "No recent mail returned for this sample."
            )
            return

        def read() -> object:
            result = self.service.simulate(profile_id, events, max_examples=20, rule_id=rule_id)
            dry_run = self.service.test_event(profile_id, events[0], rule_id=rule_id)
            fields = sorted(
                {field for values in result.provider_exposed_fields.values() for field in values}
            )
            provider_counts = clean_cell(dict(result.provider_counts), max_length=220)
            exposed_fields = clean_cell(", ".join(fields) or "none", max_length=300)
            immediate = dry_run.routing.immediate_providers or "none"
            digest = dry_run.routing.digest_providers or "none"
            queued = dry_run.routing.queued_providers or "none"
            scope = (
                f"{clean_cell(rule_id)} (evaluated even if disabled)"
                if rule_id is not None
                else "enabled profile rules"
            )
            report = (
                "DRY RUN — no provider called\n"
                f"{source}\nScope: {scope}\n"
                f"Events: {result.total_events} · Matches: {result.matching_events} · "
                f"Planned notification events: {result.notification_events} · "
                f"Suppressed: {result.suppressed_events}\n"
                f"Planned provider events (including digest / queued): {provider_counts}\n"
                f"Exposed fields: {exposed_fields}\n"
                f"First-event routing: immediate {immediate}, digest {digest}, queued {queued}\n"
                "Profile routing preview; monitoring and actual delivery are unchanged.\n"
                "Matching examples (up to 20):"
            )
            for example in result.examples:
                destinations = (
                    f"immediate {clean_cell(example.immediate_providers)}; "
                    f"digest {clean_cell(example.digest_providers)}; "
                    f"queued {clean_cell(example.queued_providers)}"
                )
                report += (
                    f"\n• {clean_cell(example.message_reference, max_length=100)} · "
                    f"{clean_cell(example.folder, max_length=100)} · "
                    f"{clean_cell(example.subject, max_length=160)}\n"
                    f"  Rules: {clean_cell(example.matched_rule_ids, max_length=300)}; "
                    f"suppressed: {example.suppressed}; {destinations}\n"
                    f"  Fields by provider: "
                    f"{clean_cell(dict(example.exposed_fields), max_length=1000)}"
                )
                if example.skipped_providers:
                    report += (
                        f"\n  Skipped: "
                        f"{clean_cell(dict(example.skipped_providers), max_length=600)}"
                    )
            if not result.examples:
                report += "\nNone matched."
            return report

        def loaded(value: object) -> None:
            if not isinstance(value, str):
                raise PublicOperationError("Notification simulation returned an invalid report.")
            self._rule_detail_override = value
            self.query_one("#notification-rule-detail", Static).update(value)
            self._set_view_state(
                ViewState.READY, "Rule simulation completed locally; no notifications were sent."
            )

        self._read_local_configuration("Simulate notification rules", read, loaded)

    def _open_quiet_editor(self, window: QuietHoursWindow | None) -> None:
        if self._profile() is not None:
            self.app.push_screen(QuietHoursEditorScreen(window), self._quiet_editor_resolved)

    def _quiet_editor_resolved(self, window: QuietHoursWindow | None) -> None:
        profile = self._profile()
        if profile is None or window is None:
            return
        values = [
            item for item in profile.quiet_hours.windows if item.window_id != window.window_id
        ]
        values.append(window)

        def change() -> object:
            self.service.replace_quiet_hours(profile.profile_id, QuietHoursSchedule(tuple(values)))
            return window.window_id

        def saved(value: object) -> None:
            self._selected_quiet_id = cast(str | None, value)

        self._change_local_configuration("Save quiet hours", change, saved)

    def _toggle_quiet(self) -> None:
        window = self._quiet()
        if window is not None:
            self._quiet_editor_resolved(replace(window, enabled=not window.enabled))

    def _delete_quiet(self) -> None:
        profile = self._profile()
        window = self._quiet()
        if profile is None or window is None:
            return

        def change() -> object:
            self.service.replace_quiet_hours(
                profile.profile_id,
                QuietHoursSchedule(
                    tuple(
                        item
                        for item in profile.quiet_hours.windows
                        if item.window_id != window.window_id
                    )
                ),
            )
            return None

        def saved(value: object) -> None:
            self._selected_quiet_id = cast(str | None, value)

        self._change_local_configuration("Delete quiet-hours window", change, saved)

    def _open_digest_editor(self, schedule: DigestSchedule | None) -> None:
        if self._profile() is not None:
            self.app.push_screen(DigestEditorScreen(schedule), self._digest_editor_resolved)

    def _digest_editor_resolved(self, schedule: DigestSchedule | None) -> None:
        profile = self._profile()
        if profile is None or schedule is None:
            return
        values = [
            item for item in profile.digest_schedules if item.schedule_id != schedule.schedule_id
        ]
        values.append(schedule)

        def change() -> object:
            self.service.replace_digest_schedules(profile.profile_id, tuple(values))
            return schedule.schedule_id

        def saved(value: object) -> None:
            self._selected_digest_id = cast(str | None, value)

        self._change_local_configuration("Save digest schedule", change, saved)

    def _toggle_digest(self) -> None:
        schedule = self._digest()
        if schedule is not None:
            self._digest_editor_resolved(replace(schedule, enabled=not schedule.enabled))

    def _delete_digest(self) -> None:
        profile = self._profile()
        schedule = self._digest()
        if profile is None or schedule is None:
            return
        schedules = tuple(
            item for item in profile.digest_schedules if item.schedule_id != schedule.schedule_id
        )
        providers = tuple(
            replace(item, digest_schedule_id=None)
            if item.digest_schedule_id
            and item.digest_schedule_id.casefold() == schedule.schedule_id.casefold()
            else item
            for item in profile.providers
        )

        def change() -> object:
            self.service.profiles.update_profile(
                profile.profile_id,
                lambda current: replace(current, digest_schedules=schedules, providers=providers),
            )
            return None

        def saved(value: object) -> None:
            self._selected_digest_id = cast(str | None, value)

        self._change_local_configuration("Delete digest schedule", change, saved)

    def _open_provider_editor(self, policy: ProviderPolicy | None) -> None:
        profile = self._profile()
        if profile is None:
            return
        metadata = self._provider_metadata.get(policy.provider_id.casefold()) if policy else None
        self.app.push_screen(
            ProviderEditorScreen(
                metadata,
                policy,
                digest_ids=tuple(item.schedule_id for item in profile.digest_schedules),
            ),
            self._provider_editor_resolved,
        )

    def _provider_editor_resolved(self, value: ProviderEdit | None) -> None:
        profile = self._profile()
        if profile is None or value is None:
            return
        policies = [
            item for item in profile.providers if item.provider_id != value.policy.provider_id
        ]
        policies.append(value.policy)
        provider_ids = tuple(
            dict.fromkeys((*profile.routing_matrix.provider_ids, value.policy.provider_id))
        )

        def change() -> object:
            self.service.providers.save_provider(value.metadata)
            self.service.profiles.update_profile(
                profile.profile_id,
                lambda current: replace(
                    current,
                    providers=tuple(policies),
                    routing_matrix=RoutingMatrix(provider_ids, current.routing_matrix.cells),
                ),
            )
            return value.policy.provider_id

        def saved(value: object) -> None:
            self._selected_provider_id = cast(str | None, value)

        self._change_local_configuration("Save notification provider", change, saved)

    def _toggle_provider(self) -> None:
        profile = self._profile()
        policy = self._provider_policy()
        if profile is None or policy is None:
            return
        policies = tuple(
            replace(item, enabled=not item.enabled)
            if item.provider_id == policy.provider_id
            else item
            for item in profile.providers
        )

        def change() -> object:
            self.service.replace_provider_policies(profile.profile_id, policies)
            metadata = self.service.providers.get_provider(policy.provider_id)
            if metadata is not None:
                self.service.providers.set_enabled(policy.provider_id, enabled=not policy.enabled)
            return None

        self._change_local_configuration("Update notification provider", change)

    def _request_provider_toggle(self) -> None:
        """Enable external-capable providers only after an explicit local review."""
        policy = self._provider_policy()
        if policy is None:
            return
        if policy.enabled:
            self._toggle_provider()
            return

        def loaded(value: object) -> None:
            metadata = cast(ProviderConfigurationMetadata | None, value)
            external = metadata is not None and metadata.kind in {
                "command",
                "discord",
                "slack",
                "teams",
                "telegram",
                "webhook",
            }
            if not external:
                self._toggle_provider()
                return
            assert metadata is not None
            self._confirm(
                "Enable this external notification provider?",
                f"Enabling {clean_cell(metadata.name)} allows future watcher runs to send "
                "the fields permitted by its privacy policy outside this process. "
                "No notification is sent now.",
                self._toggle_provider,
                confirm_label="Enable provider",
            )

        self._read_local_configuration(
            "Review notification provider",
            lambda: self.service.providers.get_provider(policy.provider_id),
            loaded,
        )

    def _remove_provider(self) -> None:
        profile = self._profile()
        policy = self._provider_policy()
        if profile is None or policy is None:
            return
        providers = tuple(
            item for item in profile.providers if item.provider_id != policy.provider_id
        )
        provider_ids = tuple(
            item
            for item in profile.routing_matrix.provider_ids
            if item.casefold() != policy.provider_id.casefold()
        )
        cells = tuple(
            item
            for item in profile.routing_matrix.cells
            if item.provider_id.casefold() != policy.provider_id.casefold()
        )

        def change() -> object:
            self.service.profiles.update_profile(
                profile.profile_id,
                lambda current: replace(
                    current, providers=providers, routing_matrix=RoutingMatrix(provider_ids, cells)
                ),
            )
            return None

        def saved(value: object) -> None:
            self._selected_provider_id = cast(str | None, value)

        self._change_local_configuration("Remove provider from profile", change, saved)

    def _set_route(self, mode: RouteMode) -> None:
        profile = self._profile()
        route = self._selected_route
        if profile is None or route is None:
            return

        def change() -> object:
            matrix = profile.routing_matrix.with_cell(
                route[0], route[1], mode, enabled=mode is not RouteMode.OFF
            )
            self.service.replace_routing_matrix(profile.profile_id, matrix)
            return None

        self._change_local_configuration("Update notification route", change)

    def _cycle_route(self) -> None:
        profile = self._profile()
        route = self._selected_route
        if profile is None or route is None:
            return
        current = profile.routing_matrix.mode_for(*route)
        next_mode = {
            RouteMode.OFF: RouteMode.IMMEDIATE,
            RouteMode.IMMEDIATE: RouteMode.DIGEST,
            RouteMode.DIGEST: RouteMode.OFF,
        }[current]
        self._set_route(next_mode)

    def _export(self) -> None:
        profile = self._profile()
        kind = _selected(self.query_one("#notification-transfer-kind", Select), default="profiles")
        fingerprint = self._transfer_fingerprint()

        def read() -> object:
            if kind == "profiles":
                payload = self.service.profiles.export_document()
            elif kind == "rules":
                if profile is None:
                    raise ValueError("choose a profile before exporting rules")
                payload = self.service.rules(profile.profile_id).export_document()
            else:
                payload = self.service.providers.export_document(include_secret_references=False)
            return payload

        def loaded(value: object) -> None:
            if self._transfer_fingerprint() != fingerprint:
                raise PublicOperationError(
                    "The transfer document changed during export. Export again to replace it.",
                    retryable=False,
                )
            if not isinstance(value, str):
                raise PublicOperationError("Notification export returned an invalid document.")
            self.query_one("#notification-transfer-payload", TextArea).load_text(value)
            self.query_one("#notification-transfer-detail", Static).update(
                "Redacted export generated locally. Provider secret references and "
                "secret values are omitted."
            )
            self._import_preview_valid = False
            self._import_preview_fingerprint = None
            self.query_one("#notification-import-apply", Button).disabled = True

        self._read_local_configuration("Export notification settings", read, loaded)

    def _transfer_fingerprint(self) -> str:
        kind = _selected(
            self.query_one("#notification-transfer-kind", Select),
            default="profiles",
        )
        payload = self.query_one("#notification-transfer-payload", TextArea).text
        replace_existing = self.query_one("#notification-transfer-replace", Checkbox).value
        material = f"{kind}\0{int(replace_existing)}\0{payload}".encode()
        return hashlib.sha256(material).hexdigest()

    def _preview_import(self) -> None:
        profile = self._profile()
        kind = _selected(self.query_one("#notification-transfer-kind", Select), default="profiles")
        payload = self.query_one("#notification-transfer-payload", TextArea).text
        try:
            if kind == "profiles":
                items: tuple[object, ...] = import_profiles(payload)
            elif kind == "rules":
                if profile is None:
                    raise ValueError("choose a profile before importing rules")
                items = import_rules(payload)
            else:
                items = preview_provider_metadata_document(payload)
        except BaseException as exc:
            self._import_preview_valid = False
            self._import_preview_fingerprint = None
            self.query_one("#notification-import-apply", Button).disabled = True
            self._show_error("Preview notification import", _safe_public_error(exc))
            return
        replace_existing = self.query_one("#notification-transfer-replace", Checkbox).value
        self._import_preview_valid = True
        self._import_preview_fingerprint = self._transfer_fingerprint()
        self.query_one("#notification-import-apply", Button).disabled = False
        self.query_one("#notification-transfer-detail", Static).update(
            f"Valid {kind} document · {len(items)} item(s) · "
            + ("replace-existing requested" if replace_existing else "conflicts will be rejected")
            + ". Nothing has been applied."
        )
        self._set_view_state(
            ViewState.READY, "Import preview is valid; confirmation is required to apply it."
        )

    def _apply_import(self) -> None:
        if not self._import_preview_valid:
            return
        if self._import_preview_fingerprint != self._transfer_fingerprint():
            self._import_preview_valid = False
            self._import_preview_fingerprint = None
            self.query_one("#notification-import-apply", Button).disabled = True
            self._show_error(
                "Apply notification import",
                PublicOperationError(
                    "The import document or replace option changed after preview. "
                    "Preview it again before applying.",
                    retryable=False,
                ),
            )
            return
        profile = self._profile()
        kind = _selected(self.query_one("#notification-transfer-kind", Select), default="profiles")
        payload = self.query_one("#notification-transfer-payload", TextArea).text
        replace_existing = self.query_one("#notification-transfer-replace", Checkbox).value

        def change() -> object:
            if kind == "profiles":
                self.service.profiles.import_document(payload, replace_existing=replace_existing)
            elif kind == "rules":
                if profile is None:
                    raise ValueError("choose a profile before importing rules")
                self.service.rules(profile.profile_id).import_document(
                    payload, replace_existing=replace_existing
                )
            else:
                preview_provider_metadata_document(payload)
                self.service.providers.import_document(payload, replace_existing=replace_existing)
            return None

        def saved(value: object) -> None:
            self._import_preview_valid = False
            self._import_preview_fingerprint = None
            self.query_one("#notification-import-apply", Button).disabled = True
            self.query_one("#notification-transfer-detail", Static).update(
                "Import applied atomically to local state. Provider secrets were not imported."
            )

        self._change_local_configuration("Apply notification import", change, saved)

    def _run_diagnostics(self) -> None:
        if self._operation_active:
            return
        self._begin_operation(
            "Loading notification settings",
            self._diagnostics_report,
            self._diagnostics_loaded,
            retry_safe=True,
        )

    def _diagnostics_report(self, cancel: Event, progress: ProgressReporter) -> str:
        def checkpoint() -> None:
            if cancel.is_set():
                raise PublicOperationError(
                    "Local notification diagnostics cancelled.", retryable=False
                )

        checkpoint()
        progress(OperationProgress("Inspecting local notification configuration"))
        profile_snapshot = self.service.profiles.inspect()
        checkpoint()
        provider_snapshot = self.service.providers.inspect()
        checkpoint()
        active = self.service.profiles.active_profile()
        checkpoint()
        lines = [
            "LOCAL-ONLY DIAGNOSTICS — no authentication, mailbox, provider, or network access",
            f"Profiles: {len(profile_snapshot.items)} valid · "
            f"{len(profile_snapshot.diagnostics)} isolated invalid rows",
            f"Provider metadata: {len(provider_snapshot.items)} valid · "
            f"{len(provider_snapshot.diagnostics)} isolated invalid rows",
            f"Active profile: {active.name if active else 'None'}",
        ]
        for profile in profile_snapshot.items:
            checkpoint()
            rule_snapshot = self.service.rules(profile.profile_id).inspect()
            checkpoint()
            strategy = self.service.profiles.strategy_preference(profile.profile_id) or "automatic"
            secret_count = sum(
                metadata.secret_ref is not None
                for metadata in provider_snapshot.items
                if metadata.provider_id.casefold()
                in {item.provider_id.casefold() for item in profile.providers}
            )
            state = "enabled" if profile.enabled else "disabled"
            lines.append(
                f"{profile.name}: {state}, strategy={strategy}, "
                f"folders={len(profile.folders)}, rules={len(rule_snapshot.items)}, "
                f"rule-errors={len(rule_snapshot.diagnostics)}, "
                f"providers={len(profile.providers)}, secret-bindings={secret_count}, "
                f"routes={len(profile.routing_matrix.cells)}"
            )
        diagnostics = (*profile_snapshot.diagnostics, *provider_snapshot.diagnostics)
        lines.extend(
            f"Isolated {clean_cell(getattr(item, 'component', 'row'))}: "
            f"{clean_cell(getattr(item, 'message', 'invalid local data'), max_length=240)}"
            for item in diagnostics[:20]
        )
        checkpoint()
        return sanitize_terminal("\n".join(lines), max_length=32_000)

    def _diagnostics_loaded(self, value: object) -> None:
        if not isinstance(value, str):
            raise PublicOperationError("Local diagnostics returned an invalid report.")
        self.query_one("#notification-diagnostics", Static).update(value)
        self._set_view_state(
            ViewState.READY, "Local notification diagnostics completed without exposing secrets."
        )
        pending = self._pending_configuration_action
        self._pending_configuration_action = None
        if pending is not None:
            pending()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        profile = self._profile()
        if button_id == "notification-refresh":
            self.action_refresh()
        elif button_id == "notification-desktop-setup" and self.desktop_setup is not None:
            self.desktop_setup()
        elif button_id == "notification-profile-new":
            self.action_new_profile()
        elif button_id == "notification-profile-edit":
            self.action_edit_profile()
        elif button_id == "notification-profile-activate" and profile is not None:
            self._confirm(
                "Make this the active monitoring profile?",
                f"Future watcher launches will use {clean_cell(profile.name)}. "
                "This does not start monitoring now.",
                self._activate_profile,
                confirm_label="Make active",
            )
        elif button_id == "notification-profile-delete" and profile is not None:
            self._confirm(
                "Delete this monitoring profile?",
                f"This permanently removes the local profile '{clean_cell(profile.name)}', "
                "its rules, quiet hours, digests, and routing matrix. Export first if "
                "needed. Mailbox data is not touched.",
                self._delete_profile,
                confirm_label="Delete profile",
            )
        elif button_id == "notification-profile-toggle":
            self._toggle_profile()
        elif button_id == "notification-rule-new":
            self.action_create_rule()
        elif button_id == "notification-rule-edit":
            self.action_edit_rule()
        elif button_id == "notification-rule-duplicate":
            self._duplicate_rule()
        elif button_id == "notification-rule-toggle":
            self._toggle_rule()
        elif button_id == "notification-rule-up":
            self._move_rule(-1)
        elif button_id == "notification-rule-down":
            self._move_rule(1)
        elif button_id == "notification-rule-simulate":
            self._simulate_rules()
        elif button_id == "notification-rule-delete":
            self.action_delete_rule()
        elif button_id == "notification-quiet-new":
            self._open_quiet_editor(None)
        elif button_id == "notification-quiet-edit":
            self._open_quiet_editor(self._quiet())
        elif button_id == "notification-quiet-toggle":
            self._toggle_quiet()
        elif button_id == "notification-quiet-delete" and self._quiet() is not None:
            self._confirm(
                "Delete this quiet-hours window?",
                "Notifications will no longer be suppressed, queued, or digested by "
                "this local schedule.",
                self._delete_quiet,
                confirm_label="Delete window",
            )
        elif button_id == "notification-digest-new":
            self._open_digest_editor(None)
        elif button_id == "notification-digest-edit":
            self._open_digest_editor(self._digest())
        elif button_id == "notification-digest-toggle":
            self._toggle_digest()
        elif button_id == "notification-digest-delete" and self._digest() is not None:
            self._confirm(
                "Delete this digest schedule?",
                "Provider references to this schedule will be cleared atomically. "
                "Queued runtime batches are not silently rewritten.",
                self._delete_digest,
                confirm_label="Delete digest",
            )
        elif button_id == "notification-provider-new":
            self._open_provider_editor(None)
        elif button_id == "notification-provider-edit":
            self._open_provider_editor(self._provider_policy())
        elif button_id == "notification-provider-toggle":
            self._request_provider_toggle()
        elif button_id == "notification-provider-remove" and self._provider_policy() is not None:
            self._confirm(
                "Remove provider from this profile?",
                "This removes its routing cells and privacy policy from the selected "
                "profile. Shared non-secret provider metadata is retained.",
                self._remove_provider,
                confirm_label="Remove provider",
            )
        elif button_id == "notification-webhook-add":
            self.action_configure_webhook("webhook")
        elif button_id == "notification-webhook-add-discord":
            self.action_configure_webhook("discord")
        elif button_id == "notification-webhook-add-slack":
            self.action_configure_webhook("slack")
        elif button_id == "notification-webhook-add-teams":
            self.action_configure_webhook("teams")
        elif button_id == "notification-webhook-test":
            self.action_test_webhook()
        elif button_id == "notification-webhook-pause":
            self.action_pause_webhook()
        elif button_id == "notification-webhook-resume":
            self.action_resume_webhook()
        elif button_id == "notification-webhook-remove":
            self.action_remove_webhook()
        elif button_id == "notification-route-cycle":
            self._cycle_route()
        elif button_id == "notification-route-off":
            self._set_route(RouteMode.OFF)
        elif button_id == "notification-export":
            self._export()
        elif button_id == "notification-import-preview":
            self._preview_import()
        elif button_id == "notification-import-apply" and self._import_preview_valid:
            detail = "Apply the validated document atomically to local notification configuration?"
            if self.query_one("#notification-transfer-replace", Checkbox).value:
                detail += (
                    " Replace-existing may discard conflicting local configuration; "
                    "export a backup first."
                )
            self._confirm(
                "Apply notification import?",
                detail,
                self._apply_import,
                confirm_label="Apply import",
            )
        elif button_id == "notification-diagnostics-run":
            try:
                self._run_diagnostics()
            except BaseException as exc:
                self._show_error("Run local notification diagnostics", _safe_public_error(exc))


__all__ = [
    "DigestEditorScreen",
    "NotificationConfirmationScreen",
    "NotificationSettingsScreen",
    "ProfileEdit",
    "ProfileEditorScreen",
    "ProviderEdit",
    "ProviderEditorScreen",
    "QuietHoursEditorScreen",
    "RuleEditorScreen",
    "WebhookSetup",
    "WebhookSetupScreen",
]
