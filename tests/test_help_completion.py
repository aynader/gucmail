from __future__ import annotations

import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from gucmail.cli import _installed_cli_help_references, _installed_cli_help_syntax, app
from gucmail.help import (
    DOCUMENTED_NLP_EXAMPLES,
    HelpRepository,
    HelpRuntimeContext,
    HelpTocKind,
    HelpUserState,
)
from gucmail.help.reference import (
    command_reference_markdown,
    natural_language_reference_markdown,
    settings_reference_markdown,
    shortcut_conflicts,
    shortcut_records,
    shortcuts_reference_markdown,
)
from gucmail.help.validation import validate_help_pages
from gucmail.nlp import NaturalLanguageParser, load_command_corpus
from gucmail.palette import universal_palette_registry
from gucmail.settings import DEFAULT_SETTINGS


def _environment(tmp_path: object) -> dict[str, str]:
    root = str(tmp_path)
    return {
        "GUCMAIL_CONFIG_DIR": root + "/config",
        "GUCMAIL_DATA_DIR": root + "/data",
        "GUCMAIL_CACHE_DIR": root + "/cache",
        "GUCMAIL_LOG_DIR": root + "/logs",
        "NO_COLOR": "1",
    }


def test_help_toc_is_hierarchical_and_every_heading_is_a_jump_target() -> None:
    repository = HelpRepository.load_default()
    toc = repository.table_of_contents(platform="windows")

    assert toc.kind is HelpTocKind.ROOT
    assert tuple(node.title for node in toc.children) == repository.categories
    toc_pages = tuple(page for category in toc.children for page in category.children)
    assert {page.page_id for page in toc_pages} == {page.page_id for page in repository.pages}
    assert all(page.kind is HelpTocKind.PAGE for page in toc_pages)
    assert all(page.children for page in toc_pages)
    assert all(
        heading.kind is HelpTocKind.HEADING and heading.page_id == page.page_id
        for page in toc_pages
        for heading in page.children
    )


def test_help_home_sections_combine_context_popular_recent_and_favorites() -> None:
    repository = HelpRepository.load_default()
    sections = repository.home_sections(
        context=HelpRuntimeContext(active_view="calendar", platform="windows"),
        state=HelpUserState(
            recent_page_ids=("mail.reading", "missing.old-page"),
            favorite_page_ids=frozenset({"backup.overview", "missing.favorite"}),
        ),
    )

    assert sections.start_here
    assert sections.contextual[0].page_id == "calendar.overview"
    assert {page.page_id for page in sections.popular} >= {
        "mail.scheduled",
        "notifications.windows",
        "backup.overview",
    }
    assert tuple(page.page_id for page in sections.recent) == ("mail.reading",)
    assert tuple(page.page_id for page in sections.favorites) == ("backup.overview",)
    assert all(page.category == "Troubleshooting" for page in sections.troubleshooting)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("notifcations", "notifications.windows"),
        ("webhok", "integrations.webhooks"),
        ("scheduel email", "mail.scheduled"),
        ("backuo", "backup.overview"),
        ("calender", "calendar.overview"),
        ("mail not loadng", "troubleshooting.connection"),
        ("how do i schedule an email for tomorrow", "mail.scheduled"),
        ("how do i get notifications when windows starts", "startup.windows"),
        ("how do i send student affairs mail to discord", "integrations.discord"),
        ("why did my mail stop loading", "troubleshooting.connection"),
        ("how do i back up everything", "backup.overview"),
        ("how do i free mailbox space", "cleanup.overview"),
        ("what does the purple dot mean", "mail.reading"),
        ("how do i make the preview bigger", "appearance.layout"),
        ("how do i restore a deleted email", "mail.deleted-items"),
        ("how do i search emails from Ahmed with PDFs", "search.local"),
    ],
)
def test_help_search_ranks_typos_and_natural_questions(query: str, expected: str) -> None:
    result = HelpRepository.load_default().search(query, limit=3)
    assert result[0].page.page_id == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Connection timed out.", "troubleshooting.connection"),
        ("Discord webhook delivery failed: HTTP 401", "integrations.discord"),
        ("sqlite3.IntegrityError: UNIQUE constraint failed", "troubleshooting.database"),
        ("InvalidRecipient: recipient rejected", "troubleshooting.sending"),
    ],
)
def test_error_aware_help_prefers_explicit_error_signatures(error: str, expected: str) -> None:
    result = HelpRepository.load_default().for_error(error)
    assert result[0].page.page_id == expected
    assert any("error signature" in reason for reason in result[0].reasons)


def test_generated_references_cover_canonical_commands_settings_and_shortcuts() -> None:
    commands = universal_palette_registry().all()
    command_markdown = command_reference_markdown(commands)
    nlp_markdown = natural_language_reference_markdown(commands)
    settings_markdown = settings_reference_markdown(
        HelpRuntimeContext(
            settings={
                "palette.primary_shortcut": "ctrl+space",
                "connection.email": "private.person@example.invalid",
            }
        )
    )
    shortcut_markdown = shortcuts_reference_markdown(
        HelpRuntimeContext(settings={"palette.primary_shortcut": "ctrl+space"})
    )

    assert all(f"`{command.command_id}`" in command_markdown for command in commands)
    corpus = load_command_corpus()
    assert all(f"`{group.phrases[0]}`" in nlp_markdown for group in corpus.groups)
    assert all(f"`{spec.setting_id}`" in settings_markdown for spec in DEFAULT_SETTINGS)
    assert "private.person@example.invalid" not in settings_markdown
    assert "`ctrl+space`" in shortcut_markdown
    assert (
        "Current shortcut catalog"
        in HelpRepository.load_default()
        .render(
            "reference.shortcuts",
            context=HelpRuntimeContext(settings={"palette.primary_shortcut": "ctrl+space"}),
        )
        .markdown
    )


def test_shortcut_registry_uses_current_settings_and_allows_same_action_aliases() -> None:
    records = shortcut_records(
        HelpRuntimeContext(
            settings={
                "palette.primary_shortcut": "ctrl+space",
                "palette.secondary_shortcut": "ctrl+space",
            }
        )
    )
    by_id = {record.shortcut_id: record for record in records}
    assert by_id["global.palette.primary"].key == "ctrl+space"
    assert by_id["global.palette.secondary"].key == "ctrl+space"
    assert not shortcut_conflicts(records)


def test_static_documented_nlp_examples_are_real_parser_fixtures() -> None:
    parser = NaturalLanguageParser()
    for fixture in DOCUMENTED_NLP_EXAMPLES:
        result = parser.interpret(fixture.text)
        assert result.kind.value == fixture.interpretation_kind, fixture.text
        command_id = result.invocation.command_id if result.invocation else None
        assert command_id == fixture.command_id, fixture.text


def test_full_documentation_validation_uses_real_application_registries() -> None:
    repository = HelpRepository.load_default()
    registry = universal_palette_registry()
    cli_command_ids, cli_paths, cli_options = _installed_cli_help_references()
    setting_ids = {spec.setting_id for spec in DEFAULT_SETTINGS}
    setting_ids.update(spec.attr_name for spec in DEFAULT_SETTINGS if spec.attr_name)
    corpus = load_command_corpus()
    examples = {phrase for group in corpus.groups for phrase in group.phrases}
    examples.update(fixture.text for fixture in DOCUMENTED_NLP_EXAMPLES)

    report = repository.validate(
        known_command_ids={command.command_id for command in registry.all()} | cli_command_ids,
        known_setting_ids=setting_ids,
        known_shortcut_ids={record.shortcut_id for record in shortcut_records()},
        known_nlp_examples=examples,
        known_cli_paths=cli_paths,
        known_cli_options=cli_options,
        known_cli_syntax=_installed_cli_help_syntax(),
    )
    assert report.ok, report.errors


def test_validation_rejects_static_nlp_example_without_fixture() -> None:
    repository = HelpRepository.load_default()
    original = repository.get("palette.overview")
    broken = replace(
        original,
        body=original.body + "\n\n## Unverified example\n\nTry synthetic phrase now.",
        nlp_examples=(*original.nlp_examples, "synthetic phrase now"),
    )
    pages = tuple(broken if page.page_id == broken.page_id else page for page in repository.pages)

    report = validate_help_pages(
        pages,
        home_page_id=repository.home_page_id,
        known_nlp_examples={fixture.text for fixture in DOCUMENTED_NLP_EXAMPLES},
    )
    assert "missing_nlp_fixture" in {issue.code for issue in report.errors}


def test_help_cli_surfaces_are_offline_structured_and_validated(tmp_path: object) -> None:
    runner = CliRunner()
    environment = _environment(tmp_path)
    for command, expected_command in (
        (["--json", "help", "toc"], "help toc"),
        (["--json", "help", "error", "connection timed out"], "help error"),
        (["--json", "help", "commands"], "help commands"),
        (["--json", "help", "shortcuts"], "help shortcuts"),
        (["--json", "help", "glossary"], "help glossary"),
        (["--json", "help", "validate"], "help validate"),
    ):
        result = runner.invoke(app, command, env=environment)
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["command"] == expected_command
        assert envelope["source"] == "packaged_offline_help"
    validated = runner.invoke(app, ["--json", "help", "validate"], env=environment)
    assert json.loads(validated.output)["data"]["valid"] is True


def test_help_registry_includes_only_groups_with_default_invocation_and_retains_children():
    identifiers, paths, options = _installed_cli_help_references()
    assert {"calendar", "contacts", "help", "quota"} <= paths
    assert not {"messages", "folders", "tasks"} & paths
    assert {"calendar.agenda", "calendar.month", "messages.show"} <= identifiers
    assert {"calendar agenda", "calendar month", "messages show"} <= paths
    assert "--days" in options["calendar agenda"] and "calendar" in options


@pytest.mark.parametrize(
    "example,valid",
    [
        ("gucmail calendar", True),
        ("gucmail calendar --help", True),
        ("gucmail calendar agenda --days 14", True),
        ("gucmail calendar unknown-subcommand", False),
        ("gucmail messages", False),
        ("gucmail tasks", False),
        ("gucmail calendar --unknown-option", False),
    ],
)
def test_help_examples_accept_default_group_without_accepting_unknown_children(example, valid):
    repository = HelpRepository.load_default()
    original = repository.get("calendar.overview")
    changed = replace(original, body=original.body + f"\n\n## Route validation\n\nRun `{example}`.")
    pages = tuple(changed if page.page_id == changed.page_id else page for page in repository.pages)
    _, paths, options = _installed_cli_help_references()
    report = validate_help_pages(
        pages,
        home_page_id=repository.home_page_id,
        known_cli_paths=paths,
        known_cli_options=options,
        known_cli_syntax=_installed_cli_help_syntax(),
    )
    assert report.ok is valid, report.errors
    if not valid:
        assert any(
            issue.code in {"unknown_cli_example", "unknown_cli_option", "invalid_cli_syntax"}
            for issue in report.errors
        )
