from __future__ import annotations

from dataclasses import fields, replace

import pytest
from typer.main import get_command

from gucmail.actions import DEFAULT_ACTIONS
from gucmail.cli import app
from gucmail.config import AppConfig
from gucmail.help import (
    HelpAvailability,
    HelpNavigator,
    HelpPage,
    HelpRepository,
    HelpRuntimeContext,
    HelpSkillLevel,
    MemoryHelpStateStore,
    parse_help_link,
)
from gucmail.help.validation import validate_help_pages


def _command_ids() -> set[str]:
    root = get_command(app)
    command_ids = set(root.commands)
    for group_name, group in root.commands.items():
        children = getattr(group, "commands", None)
        if children:
            command_ids.update(f"{group_name}.{name}" for name in children)
    return command_ids


def test_help_corpus_is_substantive_structured_and_registry_consistent() -> None:
    repository = HelpRepository.load_default()
    required_categories = {
        "Start Here",
        "Mail",
        "Compose",
        "Drafts and Scheduled Mail",
        "Search",
        "Command Palette",
        "Calendar",
        "Contacts",
        "Tasks",
        "Files and Attachments",
        "Storage",
        "Cleanup",
        "Backup and Offline Mode",
        "Notifications and Monitoring",
        "Start with Windows",
        "Integrations",
        "Running GUC Mail 24/7",
        "Appearance and Personalization",
        "Settings",
        "Privacy and Security",
        "Troubleshooting",
        "Diagnostics",
        "Advanced",
        "Reference",
    }
    assert len(repository.pages) >= 45
    assert required_categories <= set(repository.categories)
    assert len({page.page_id for page in repository.pages}) == len(repository.pages)
    assert all(len(page.body.split()) >= 35 for page in repository.pages)
    assert all(page.keywords and page.contexts for page in repository.pages)

    report = repository.validate(
        known_command_ids=_command_ids(),
        known_setting_ids={field.name for field in fields(AppConfig)},
    )
    assert report.ok, report.errors


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("webhhok discord", "integrations.discord"),
        ("does my computer need to stay awake", "faq.pc-on"),
        ("mailbox full 100 mb", "storage.quota"),
        ("lost draft after crash", "mail.drafts"),
        ("mailbox unavailable integrityerror", "troubleshooting.database"),
        ("serch unread emals", "search.overview"),
    ],
)
def test_help_search_handles_phrases_typos_and_aliases(query: str, expected: str) -> None:
    results = HelpRepository.load_default().search(query, limit=5)
    assert results
    assert results[0].page.page_id == expected
    assert results[0].score > 0
    assert results[0].reasons


def test_contextual_search_and_capability_aware_rendering() -> None:
    repository = HelpRepository.load_default()
    context = HelpRuntimeContext(
        platform="windows",
        active_view="calendar",
        capabilities={"ews.calendar": False},
    )
    results = repository.search("events this week", context=context)
    assert results[0].page.page_id == "calendar.overview"

    unavailable = repository.render("calendar.overview", context=context)
    assert unavailable.availability is HelpAvailability.UNAVAILABLE
    assert unavailable.markdown.startswith("> **Availability:**")

    available = repository.render(
        "calendar.overview",
        context=HelpRuntimeContext(capabilities={"ews.calendar": True}),
    )
    assert available.availability is HelpAvailability.AVAILABLE
    unknown = repository.render("calendar.overview")
    assert unknown.availability is HelpAvailability.UNKNOWN


def test_help_skill_setting_materially_changes_local_result_ranking() -> None:
    pages = tuple(
        HelpPage(
            page_id=f"synthetic.{level.value}",
            title="Synthetic topic",
            category="Synthetic",
            summary="The same synthetic explanation for deterministic ranking.",
            body="Synthetic topic details are deliberately identical for this ranking test.",
            source_path=f"synthetic/{level.value}.md",
            keywords=("synthetic", "topic"),
            contexts=("mail",),
            skill_level=level,
        )
        for level in (HelpSkillLevel.BEGINNER, HelpSkillLevel.ADVANCED)
    )
    repository = HelpRepository(pages, home_page_id=pages[0].page_id, schema_version=1)

    beginner = repository.search(
        "synthetic topic",
        context=HelpRuntimeContext(settings={"help.skill_level": "beginner"}),
    )
    advanced = repository.search(
        "synthetic topic",
        context=HelpRuntimeContext(settings={"help.skill_level": "advanced"}),
    )

    assert beginner[0].page.skill_level is HelpSkillLevel.BEGINNER
    assert advanced[0].page.skill_level is HelpSkillLevel.ADVANCED


def test_help_navigation_back_forward_favorites_and_persistent_recents() -> None:
    repository = HelpRepository.load_default()
    store = MemoryHelpStateStore()
    navigator = HelpNavigator(repository, store=store, recent_limit=3, query_limit=2)

    assert navigator.home().page_id == "start.overview"
    navigator.open("mail.reading")
    navigator.open("attachments.safety")
    assert navigator.breadcrumbs() == (
        "Help",
        "Files and Attachments",
        "View, download, and inspect attachments safely",
    )
    assert navigator.back().page_id == "mail.reading"  # type: ignore[union-attr]
    assert navigator.forward().page_id == "attachments.safety"  # type: ignore[union-attr]
    assert navigator.toggle_favorite("mail.reading") is True
    assert navigator.toggle_favorite("mail.reading") is False
    assert navigator.toggle_favorite("backup.overview") is True
    navigator.search("backup encryption")
    navigator.search("discord privacy")

    restored = HelpNavigator(repository, store=store)
    assert restored.state.recent_page_ids[0] == "attachments.safety"
    assert restored.state.recent_queries == ("discord privacy", "backup encryption")
    assert [page.page_id for page in restored.favorite_pages()] == ["backup.overview"]


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "data:text/plain,secret",
        "file:///tmp/private",
        "http://example.invalid/help",
        "../outside.md",
        "help:../../outside",
        "https://user:secret@example.invalid/help",
    ],
)
def test_help_links_reject_executable_insecure_or_filesystem_targets(target: str) -> None:
    with pytest.raises(ValueError):
        parse_help_link(target)


def test_help_validation_detects_broken_internal_links_and_private_data() -> None:
    repository = HelpRepository.load_default()
    original = repository.get("start.overview")
    broken = replace(
        original,
        body=original.body
        + "\n\n## Unsafe example\n\nOpen [missing](help:no.such.page) or contact "
        + "private@real-domain.edu for a credential.",
    )
    pages = tuple(broken if page.page_id == broken.page_id else page for page in repository.pages)
    report = validate_help_pages(pages, home_page_id=repository.home_page_id)
    assert {issue.code for issue in report.errors} >= {
        "unknown_help_link",
        "non_synthetic_email",
    }


def test_help_platform_filter_and_context_index() -> None:
    repository = HelpRepository.load_default()
    windows = repository.pages_for_context("startup", platform="windows")
    linux = repository.pages_for_context("startup", platform="linux")
    assert [page.page_id for page in windows] == ["startup.windows"]
    assert linux == ()


def test_first_pass_action_help_topics_resolve_through_legacy_ids() -> None:
    repository = HelpRepository.load_default()
    references = {
        action.help_topic for action in DEFAULT_ACTIONS.all() if action.help_topic is not None
    }
    assert references
    assert {repository.get(page_id).page_id for page_id in references} == {
        "advanced.ews",
        "backup.overview",
        "calendar.overview",
        "cleanup.overview",
        "contacts.overview",
        "mail.compose",
        "mail.drafts",
        "mail.reading",
        "mail.scheduled",
        "privacy.credentials",
        "search.overview",
        "tasks.overview",
    }
