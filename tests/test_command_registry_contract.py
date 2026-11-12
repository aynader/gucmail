from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace

from gucmail.actions import DEFAULT_ACTION_COMMANDS, DEFAULT_ACTIONS, LEGACY_ACTION_ALIASES
from gucmail.cli import app
from gucmail.command_catalog import (
    CORE_COMMANDS,
    MINIMUM_COMMAND_ACTIONS,
    minimum_action_command_id,
)
from gucmail.commands import (
    CommandContext,
    CommandInvocation,
    CommandRegistry,
    CommandSpec,
    ParameterType,
    discover_cli_paths,
    discover_typer_registry,
    project_cli_invocation,
)
from gucmail.help import HelpRepository
from gucmail.models import SafetyClass
from gucmail.nlp import FolderEntity, PersonEntity
from gucmail.palette import PaletteEngine
from gucmail.settings import DEFAULT_SETTINGS


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def test_minimum_product_inventory_is_canonical_and_complete() -> None:
    assert sum(len(names) for names in MINIMUM_COMMAND_ACTIONS.values()) == 421
    action_ids = {
        minimum_action_command_id(category, name)
        for category, names in MINIMUM_COMMAND_ACTIONS.items()
        for name in names
    }
    assert len(action_ids) == 421
    assert {"layout.save_layout_preset", "layout.load_layout_preset"} <= action_ids
    assert action_ids <= {command.command_id for command in CORE_COMMANDS.all()}
    assert len(CORE_COMMANDS) >= 421

    for category, names in MINIMUM_COMMAND_ACTIONS.items():
        for name in names:
            command = CORE_COMMANDS.get(minimum_action_command_id(category, name))
            assert command.category == category
            documented_phrases = {_words(command.name), *map(_words, command.aliases)}
            if command.command_id == "drafts.open_drafts":
                # W03: the original inventory reused "Open drafts" for two destinations.
                # Preserve that phrase on the Exchange folder and name the manager clearly.
                assert "open draft manager" in documented_phrases
                folder = CORE_COMMANDS.get("mail.open_drafts")
                assert _words(name) in {_words(folder.name), *map(_words, folder.aliases)}
                assert _words(name) not in documented_phrases
            else:
                assert _words(name) in documented_phrases


def test_registry_cross_links_resolve_against_installed_cli_and_help() -> None:
    cli_paths = discover_cli_paths(app)
    help_topics = {page.page_id for page in HelpRepository.load_default().pages}
    CORE_COMMANDS.assert_valid(
        cli_paths=cli_paths,
        help_topics=help_topics,
        setting_ids={spec.setting_id for spec in DEFAULT_SETTINGS},
    )

    cli_registry = discover_typer_registry(app)
    linked_paths = [command.cli_path for command in cli_registry.all()]
    assert len(linked_paths) == len(set(linked_paths))
    assert len(cli_registry) >= 200


def test_every_canonical_command_exposes_full_discovery_metadata() -> None:
    for command in CORE_COMMANDS.all():
        assert command.subcategory
        assert command.long_description
        assert command.misspellings
        assert command.examples
        assert command.related_commands
        assert command.follow_up_commands
        assert command.related_settings

    exact_cli = CORE_COMMANDS.search("messages mark-read", limit=5)
    assert exact_cli[0].command_id == "mail.mark_read"

    attachment_results = CORE_COMMANDS.search("atta", limit=20)
    attachment_ids = {command.command_id for command in attachment_results}
    assert {
        "attachments.open",
        "attachments.show_largest_attachments",
        "attachments.search",
        "attachments.download",
    } <= attachment_ids
    search_attachments = CORE_COMMANDS.get("attachments.search")
    assert "search attachment content" in search_attachments.aliases

    backup_results = CORE_COMMANDS.search("bkp", limit=3)
    assert {command.command_id for command in backup_results} == {
        "backup.create",
        "backup.open",
        "backup.verify",
    }


def test_secondpass_short_discovery_queries_return_the_complete_command_families() -> None:
    engine = PaletteEngine(CORE_COMMANDS)
    runtime = CommandContext.from_mapping({"current_area": "calendar"}, online=True)
    expected = {
        "atta": {
            "attachments.open",
            "attachments.show_largest_attachments",
            "attachments.search",
            "attachments.download",
        },
        "bkp": {"backup.create", "backup.open", "backup.verify"},
        "notif": {
            "notifications.settings",
            "notifications.enable_desktop_notifications",
            "notifications.test",
            "notifications.history",
        },
        "cal tom": {
            "calendar.tomorrow",
            "calendar.create_tomorrow",
            "calendar.show_tomorrow_events",
        },
    }

    for query, command_ids in expected.items():
        suggestions = engine.suggest(query, context=runtime, limit=len(command_ids))
        assert {suggestion.command.command_id for suggestion in suggestions} == command_ids
        assert all("command-family abbreviation" in suggestion.reason for suggestion in suggestions)

    # Prefixes and common imperfect spellings enter the same inspectable family path rather than
    # being mistaken for a person's name.
    assert {
        suggestion.command.command_id
        for suggestion in engine.suggest("attac", context=runtime, limit=4)
    } == expected["atta"]
    assert {
        suggestion.command.command_id
        for suggestion in engine.suggest("noti", context=runtime, limit=4)
    } == expected["notif"]
    assert {
        suggestion.command.command_id
        for suggestion in engine.suggest("bkup", context=runtime, limit=3)
    } == expected["bkp"]


def test_secondpass_selected_object_queries_return_contextual_action_families() -> None:
    engine = PaletteEngine(CORE_COMMANDS)
    cases = (
        (
            "reply",
            {"current_area": "mail", "selected_message": "message-1"},
            ("mail.reply", "mail.reply_all", "compose.reply"),
        ),
        (
            "send later",
            {"current_area": "drafts", "current_draft": "draft-1"},
            ("scheduled.schedule_current", "compose.send_after_delay"),
        ),
        (
            "accept",
            {"current_area": "calendar", "selected_event": "event-1"},
            ("calendar.accept_invitation",),
        ),
        (
            "email",
            {"current_area": "contacts", "selected_contact": "person@example.test"},
            ("contacts.compose_to_contact",),
        ),
        (
            "search",
            {"current_area": "backup", "current_backup": "backup-1"},
            ("search.search_backups",),
        ),
    )

    for query, values, expected in cases:
        context = CommandContext.from_mapping(values, online=True)
        suggestions = engine.suggest(query, context=context, limit=max(8, len(expected)))
        ordered = tuple(suggestion.command.command_id for suggestion in suggestions)
        assert ordered[: len(expected)] == expected
        assert all(
            "Selected-object command family" in suggestion.reason
            for suggestion in suggestions[: len(expected)]
        )
        result = engine.interpret(query, context=context)
        assert result.invocation is not None
        assert result.invocation.command_id == expected[0]

    contact_result = engine.interpret(
        "email",
        context=CommandContext.from_mapping(
            {"current_area": "contacts", "selected_contact": "person@example.test"},
            online=True,
        ),
    )
    assert isinstance(contact_result.invocation.as_dict()["person"], PersonEntity)

    backup_result = engine.interpret(
        "search",
        context=CommandContext.from_mapping(
            {"current_area": "backup", "current_backup": "backup-1"}, online=True
        ),
    )
    assert backup_result.invocation.as_dict() == {}

    # Context is a ranking signal for compatible short verbs; it never filters an unrelated
    # explicit command out of the palette.
    unrelated = engine.suggest(
        "open storage",
        context=CommandContext.from_mapping(
            {"current_area": "contacts", "selected_contact": "person@example.test"},
            online=True,
        ),
        limit=8,
    )
    assert unrelated[0].command.command_id == "storage.open"


def test_palette_ranking_combines_lexical_context_recency_and_frequency_signals() -> None:
    registry = CommandRegistry(
        (
            CommandSpec(
                "alpha.open",
                "Open Alpha Workspace",
                "Open the alpha workspace.",
                "alpha",
                ("alpha", "workspace"),
                ("alpha work",),
                long_description="Open the alpha workspace.",
                subcategory="Navigation",
                misspellings=("opne alfa workspace",),
            ),
            CommandSpec(
                "beta.open",
                "Open Beta Workspace",
                "Open the beta workspace.",
                "beta",
                ("beta", "workspace"),
                ("beta work",),
                long_description="Open the beta workspace.",
                subcategory="Navigation",
                misspellings=("opne btea workspace",),
            ),
        )
    )
    neutral = CommandContext(online=True)
    engine = PaletteEngine(registry)

    assert (
        engine.suggest("Open Alpha Workspace", context=neutral, limit=1)[0].command.command_id
        == "alpha.open"
    )
    assert (
        engine.suggest("open alp", context=neutral, limit=1)[0].command.command_id == "alpha.open"
    )
    assert (
        engine.suggest("alpha workspace", context=neutral, limit=1)[0].command.command_id
        == "alpha.open"
    )
    assert (
        engine.suggest("workspace alpha", context=neutral, limit=1)[0].command.command_id
        == "alpha.open"
    )
    assert (
        engine.suggest("opne alfa workspace", context=neutral, limit=1)[0].command.command_id
        == "alpha.open"
    )

    contextual = CommandContext.from_mapping({"current_area": "beta"}, online=True)
    assert engine.suggest("open", context=contextual, limit=1)[0].command.command_id == "beta.open"
    assert (
        engine.suggest("open", context=neutral, recent_command_ids=("beta.open",), limit=1)[
            0
        ].command.command_id
        == "beta.open"
    )
    frequent = PaletteEngine(
        registry,
        usage_score=lambda command_id: 1.0 if command_id == "beta.open" else 0.0,
    )
    assert frequent.suggest("open", context=neutral, limit=1)[0].command.command_id == "beta.open"
    # Usage is a bounded tiebreaker and cannot dislodge an exact command phrase.
    assert (
        frequent.suggest("Open Alpha Workspace", context=neutral, limit=1)[0].command.command_id
        == "alpha.open"
    )


def test_empty_palette_suggestions_are_local_deterministic_and_context_complete() -> None:
    engine = PaletteEngine(CORE_COMMANDS)
    context = CommandContext.from_mapping(
        {
            "current_area": "mail",
            "selected_message": "m-synthetic",
            "selected_message_has_attachments": "true",
            "selected_sender": "sender@example.test",
            "mailbox_near_limit": "true",
        },
        online=True,
    )

    first = engine.suggest("", context=context, limit=8)
    second = engine.suggest("", context=context, limit=8)
    assert first == second
    assert [suggestion.command.command_id for suggestion in first] == [
        "mail.reply",
        "attachments.download",
        "contacts.open_sender_contact",
        "search.similar_message_search",
        "storage.largest_messages",
        "storage.analyze_reclaimable_space",
        "folders.empty_folder",
        "backup.create",
    ]
    assert all(suggestion.group == "Suggested actions" for suggestion in first)
    assert [suggestion.display_name for suggestion in first] == [
        "Reply",
        "Download attachments",
        "Open sender contact",
        "Search related messages",
        "Show largest messages",
        "Analyze reclaimable space",
        "Empty Deleted Items",
        "Create backup",
    ]
    empty_deleted = first[6].invocation
    assert empty_deleted is not None
    assert empty_deleted.get("folder") == "trash"


def test_registry_has_no_generic_unimplemented_availability_placeholders() -> None:
    for command in CORE_COMMANDS.all():
        assert not any(rule == "not_implemented" for rule in command.availability_rules)
        assert not any(
            rule.startswith("unavailable:") and "no bound in-process handler" in rule
            for rule in command.availability_rules
        )

        if command.handler_id and command.handler_id.startswith("catalog."):
            assert command.handler_id == f"catalog.{command.category}"


def test_handler_presets_share_behavior_without_cloning_handlers() -> None:
    presets = [
        command
        for command in CORE_COMMANDS.all()
        if command.handler_id is not None and command.handler_id != command.command_id
    ]
    assert presets
    for preset in presets:
        if preset.handler_id and preset.handler_id.startswith("catalog."):
            continue
        target = CORE_COMMANDS.get(preset.handler_id or "")
        assert preset.safety is target.safety
        assert preset.changes_server is target.changes_server
        assert preset.changes_local is target.changes_local
        assert preset.parameters == target.parameters
        assert preset.cli_path == target.cli_path


def test_legacy_actions_are_views_over_canonical_commands() -> None:
    assert set(LEGACY_ACTION_ALIASES) == {action.name for action in DEFAULT_ACTIONS.all()}
    for action in DEFAULT_ACTIONS.all():
        canonical = CORE_COMMANDS.get(action.canonical_id)
        projected = DEFAULT_ACTION_COMMANDS.get(action.name)
        assert action.safety is canonical.safety
        assert action.confirmation is canonical.confirmation_required
        assert action.shortcut == canonical.shortcut
        assert action.help_topic == canonical.help_topic
        assert projected.handler_id == canonical.handler_id
        assert projected.safety is canonical.safety
        assert projected.changes_server is canonical.changes_server
        assert projected.changes_local is canonical.changes_local


def test_linked_cli_parameter_adapters_produce_valid_vectors() -> None:
    cli_registry = discover_typer_registry(app)
    start = SimpleNamespace(
        utc=datetime(2027, 1, 1, tzinfo=UTC),
        local=datetime(2027, 1, 1, tzinfo=UTC),
    )
    end = SimpleNamespace(
        utc=datetime(2027, 1, 1, 1, tzinfo=UTC),
        local=datetime(2027, 1, 1, 1, tzinfo=UTC),
    )
    cases: dict[str, dict[str, object]] = {
        "mail.move": {
            "selection": ("@1", "@2"),
            "folder": FolderEntity("Archive", "archive"),
        },
        "folders.create": {"folder": FolderEntity("Projects", "projects")},
        "attachments.open": {"reference": "@1"},
        "calendar.create": {
            "query": "Synthetic event",
            "when": start,
            "end": end,
            "person": PersonEntity("Example", "person@example.test"),
        },
        "contacts.create": {"person": PersonEntity("Example", "person@example.test")},
        "tasks.create": {"query": "Synthetic task", "when": start},
        "backup.verify": {"reference": "backup-1"},
        "integrations.webhook_test": {"name": "synthetic"},
        "ews.request_preview": {"query": "GetItem"},
    }
    for command_id, values in cases.items():
        projection = project_cli_invocation(
            CommandInvocation.from_mapping(command_id, values),
            registry=CORE_COMMANDS,
            cli_registry=cli_registry,
        )
        assert projection.available, f"{command_id}: {projection.reason}"
        assert projection.argv is not None
        assert tuple(projection.argv[: len(CORE_COMMANDS.get(command_id).cli_path or ())]) == (
            CORE_COMMANDS.get(command_id).cli_path
        )

    task = project_cli_invocation(
        CommandInvocation.from_mapping("tasks.create", cases["tasks.create"]),
        registry=CORE_COMMANDS,
        cli_registry=cli_registry,
    )
    assert task.argv is not None
    assert "2027-01-01" in task.argv


def test_discovered_cli_safety_policy_matches_high_risk_leaves() -> None:
    cli = {command.cli_path: command for command in discover_typer_registry(app).all()}
    assert cli[("reply-all",)].confirmation_required
    assert cli[("scheduled", "cancel")].safety is SafetyClass.DESTRUCTIVE
    assert cli[("folders", "empty")].confirmation_required
    assert cli[("version",)].safety is SafetyClass.READ_ONLY
    assert all(
        parameter.type is not ParameterType.ENUM or parameter.choices
        for command in cli.values()
        for parameter in command.parameters
    )


_OFFLINE_CONTEXT = CommandContext(offline_mode=True)
