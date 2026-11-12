from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import gucmail.help as help_package
import gucmail.help.repository as repository_module
from gucmail.help import (
    HelpAvailability,
    HelpCorpusError,
    HelpPage,
    HelpRepository,
    HelpRuntimeContext,
    extract_markdown_links,
    parse_help_link,
)
from gucmail.help.validation import validate_help_pages


def _replace_pages(repository: HelpRepository, *replacements: HelpPage) -> tuple[HelpPage, ...]:
    by_id = {page.page_id: page for page in replacements}
    return tuple(by_id.get(page.page_id, page) for page in repository.pages)


def test_packaged_catalog_and_markdown_sources_are_one_to_one_and_titled() -> None:
    repository = HelpRepository.load_default()
    package_root = Path(help_package.__file__).parent
    packaged_sources = {
        path.relative_to(package_root / "pages").as_posix()
        for path in (package_root / "pages").rglob("*.md")
    }
    catalog_sources = {page.source_path for page in repository.pages}

    assert len(catalog_sources) == len(repository.pages)
    assert catalog_sources == packaged_sources
    for page in repository.pages:
        assert page.body.splitlines()[0] == f"# {page.title}"
        assert all(repository.get(identifier) for identifier in page.related_pages)


def test_repository_rejects_an_unlisted_packaged_article(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "help-package"
    pages_root = package_root / "pages"
    pages_root.mkdir(parents=True)
    (package_root / "catalog.toml").write_text(
        """
schema_version = 1
home_page = "start"

[[pages]]
id = "start"
title = "Start"
category = "Start"
summary = "A complete synthetic starting article."
path = "start.md"
keywords = ["start"]
contexts = ["home"]
""".strip(),
        encoding="utf-8",
    )
    article = "# Start\n\n" + "useful article words " * 40 + "\n\n## Next\n\nDone."
    (pages_root / "start.md").write_text(article, encoding="utf-8")
    (pages_root / "orphan.md").write_text(article, encoding="utf-8")
    monkeypatch.setattr(repository_module, "files", lambda package: package_root)
    repository_module._load_default_repository.cache_clear()
    try:
        with pytest.raises(HelpCorpusError, match=r"missing from the catalog: orphan\.md"):
            repository_module._load_default_repository(False)
    finally:
        repository_module._load_default_repository.cache_clear()


def test_rendered_related_topics_are_live_internal_links() -> None:
    repository = HelpRepository.load_default()
    page = repository.get("start.overview")
    markdown = repository.render(page.page_id).markdown

    assert "## Related topics" in markdown
    for identifier in page.related_pages:
        target = repository.get(identifier)
        assert f"[{target.title}](help:{target.page_id})" in markdown


def test_validation_accepts_legacy_help_links_and_rejects_dead_typed_links() -> None:
    repository = HelpRepository.load_default()
    original = repository.get("start.overview")
    linked = replace(
        original,
        body=original.body
        + """

## Link contract

[Legacy topic](help:user-guide/reading-and-organizing-mail),
[known heading](#LINK-CONTRACT), [missing heading](#not-present),
[known command](command:status), [missing command](command:not.installed),
[known setting](setting:mode), and [missing setting](setting:not_installed).
""",
    )
    pages = _replace_pages(repository, linked)
    command_ids = {identifier for page in repository.pages for identifier in page.related_commands}
    setting_ids = {identifier for page in repository.pages for identifier in page.related_settings}

    report = validate_help_pages(
        pages,
        home_page_id=repository.home_page_id,
        known_command_ids=command_ids,
        known_setting_ids=setting_ids,
    )
    codes = {issue.code for issue in report.errors}

    assert "unknown_help_link" not in codes
    assert codes >= {"unknown_anchor", "unknown_command_link", "unknown_setting_link"}


def test_reference_definitions_and_autolinks_cannot_bypass_link_validation() -> None:
    markdown = """
[Start][home]

[home]: help:start.overview
<https://example.invalid/help>
[Inline](<https://example.invalid/inline>)
"""
    assert extract_markdown_links(markdown) == (
        "help:start.overview",
        "https://example.invalid/help",
        "https://example.invalid/inline",
    )

    repository = HelpRepository.load_default()
    original = repository.get("start.overview")
    unsafe = replace(
        original,
        body=original.body
        + """

## Reference link

[Do not open this][unsafe-reference].

[unsafe-reference]: javascript:alert(1)
""",
    )
    report = validate_help_pages(
        _replace_pages(repository, unsafe),
        home_page_id=repository.home_page_id,
    )
    assert "unsafe_link" in {issue.code for issue in report.errors}


def test_validation_detects_unsafe_duplicate_sources_and_title_drift() -> None:
    repository = HelpRepository.load_default()
    duplicate = replace(repository.pages[1], source_path=repository.pages[0].source_path)
    unsafe = replace(repository.pages[2], source_path=r"..\outside.md")
    title_drift = replace(repository.pages[3], title="A title not present in the article")
    report = validate_help_pages(
        _replace_pages(repository, duplicate, unsafe, title_drift),
        home_page_id=repository.home_page_id,
    )

    assert {issue.code for issue in report.errors} >= {
        "article_title_mismatch",
        "duplicate_source_path",
        "unsafe_source_path",
    }


@pytest.mark.parametrize(
    "target",
    [
        "https://example.invalid:99999/help",
        "https://example.invalid\\@attacker.invalid/help",
        "https://exa mple.invalid/help",
    ],
)
def test_external_help_links_reject_dead_or_ambiguous_urls(target: str) -> None:
    with pytest.raises(ValueError):
        parse_help_link(target)


def test_anchor_identifiers_are_canonical_and_local_pages_respect_platforms() -> None:
    assert parse_help_link("#LINK-CONTRACT").identifier == "link-contract"

    rendered = HelpRepository.load_default().render(
        "startup.windows",
        context=HelpRuntimeContext(platform="linux"),
    )
    assert rendered.availability is HelpAvailability.NOT_APPLICABLE
