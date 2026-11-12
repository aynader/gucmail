"""Corpus integrity and synthetic-data validation shared by tests and runtime."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePosixPath

from gucmail.help.cli_syntax import CliCommandSyntax
from gucmail.help.links import extract_markdown_links, parse_help_link
from gucmail.help.models import (
    HelpIssueSeverity,
    HelpPage,
    HelpValidationIssue,
    HelpValidationReport,
)
from gucmail.help.structure import markdown_outline

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_LEGACY_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
_DISCORD_SECRET = re.compile(
    r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w.-]+"
)
_BEARER_SECRET = re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}", re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_PROMPT_LANGUAGE = re.compile(
    r"\b(?:AGENTS\.md|Secondpass\.md|original prompt|developer prompt)\b", re.I
)
_GUCMAIL_INLINE = re.compile(r"`gucmail\s+([^`]+)`", re.IGNORECASE)
_GUCMAIL_LINE = re.compile(r"(?m)^[ \t]*(?:[-*>][ \t]+)?gucmail[ \t]+([^\r\n`]+)", re.IGNORECASE)
_CLI_OPTION = re.compile(r"(?<!\S)(--[a-z0-9][a-z0-9-]*)(?:[=\s]|$)", re.IGNORECASE)
_ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.edu",
    "example.invalid",
    "example.test",
)
_PLATFORMS = frozenset({"all", "windows", "linux", "macos"})


def validate_help_pages(
    pages: Sequence[HelpPage],
    *,
    home_page_id: str,
    known_help_ids: Collection[str] | None = None,
    known_command_ids: Collection[str] | None = None,
    known_setting_ids: Collection[str] | None = None,
    known_shortcut_ids: Collection[str] | None = None,
    known_nlp_examples: Collection[str] | None = None,
    known_cli_paths: Collection[str] | None = None,
    known_cli_options: Mapping[str, Collection[str]] | None = None,
    known_cli_syntax: CliCommandSyntax | None = None,
) -> HelpValidationReport:
    issues: list[HelpValidationIssue] = []
    ids = [page.page_id for page in pages]
    id_set = set(ids)
    normalized_ids = [identifier.casefold().strip() for identifier in ids]
    if len(normalized_ids) != len(set(normalized_ids)):
        issues.append(_error("duplicate_page_id", "Help page IDs must be unique."))
    if home_page_id not in id_set:
        issues.append(_error("missing_home_page", f"Unknown Help home page: {home_page_id}"))

    all_ids = [identifier for page in pages for identifier in (page.page_id, *page.legacy_ids)]
    normalized_all_ids = [identifier.casefold().strip() for identifier in all_ids]
    if len(normalized_all_ids) != len(set(normalized_all_ids)):
        issues.append(
            _error("duplicate_page_or_legacy_id", "Help page and legacy IDs must be unique.")
        )

    source_paths = [page.source_path.casefold() for page in pages]
    if len(source_paths) != len(set(source_paths)):
        issues.append(
            _error("duplicate_source_path", "Each Help page must use a distinct Markdown source.")
        )

    resolvable_ids = set(normalized_all_ids)
    if known_help_ids is not None:
        resolvable_ids.update(identifier.casefold().strip() for identifier in known_help_ids)
    command_ids = (
        {identifier.casefold().strip() for identifier in known_command_ids}
        if known_command_ids is not None
        else None
    )
    setting_ids = (
        {identifier.casefold().strip() for identifier in known_setting_ids}
        if known_setting_ids is not None
        else None
    )
    nlp_examples = (
        {" ".join(example.casefold().split()) for example in known_nlp_examples}
        if known_nlp_examples is not None
        else None
    )
    cli_paths = (
        {" ".join(path.casefold().split()) for path in known_cli_paths}
        if known_cli_paths is not None
        else None
    )
    cli_options = (
        {
            " ".join(path.casefold().split()): {option.casefold().strip() for option in options}
            for path, options in known_cli_options.items()
        }
        if known_cli_options is not None
        else None
    )
    if known_shortcut_ids is not None:
        normalized_shortcuts = [identifier.casefold().strip() for identifier in known_shortcut_ids]
        if len(normalized_shortcuts) != len(set(normalized_shortcuts)):
            issues.append(_error("duplicate_shortcut_id", "Shortcut IDs must be unique."))
    for page in pages:
        issues.extend(
            _validate_page(
                page,
                id_set,
                resolvable_ids,
                command_ids,
                setting_ids,
                nlp_examples,
                cli_paths,
                cli_options,
                known_cli_syntax,
            )
        )
    return HelpValidationReport(tuple(issues))


def _validate_page(
    page: HelpPage,
    page_ids: set[str],
    resolvable_ids: set[str],
    command_ids: set[str] | None,
    setting_ids: set[str] | None,
    known_nlp_examples: set[str] | None,
    known_cli_paths: set[str] | None,
    known_cli_options: Mapping[str, set[str]] | None,
    known_cli_syntax: CliCommandSyntax | None,
) -> list[HelpValidationIssue]:
    issues: list[HelpValidationIssue] = []
    page_id = page.page_id
    if not _IDENTIFIER.fullmatch(page_id):
        issues.append(_error("invalid_page_id", "Page ID uses an unsafe format.", page_id))
    for legacy_id in page.legacy_ids:
        if not _LEGACY_IDENTIFIER.fullmatch(legacy_id):
            issues.append(
                _error("invalid_legacy_id", "Legacy page ID uses an unsafe format.", page_id)
            )
    if not page.title.strip() or not page.category.strip() or not page.summary.strip():
        issues.append(
            _error("missing_metadata", "Title, category, and summary are required.", page_id)
        )
    if not is_safe_help_source_path(page.source_path):
        issues.append(_error("unsafe_source_path", "Page source path is unsafe.", page_id))

    headings = markdown_outline(page.body)
    h1_titles = tuple(heading.title for heading in headings if heading.level == 1)
    if h1_titles != (page.title,):
        issues.append(
            _error(
                "article_title_mismatch",
                "Article must contain exactly one level-one heading matching its title.",
                page_id,
            )
        )
    if len(page.body.split()) < 35 or not any(heading.level >= 2 for heading in headings):
        issues.append(
            _error(
                "article_too_shallow",
                "Article needs useful prose and section headings.",
                page_id,
            )
        )
    if not page.keywords or not page.contexts:
        issues.append(
            _error(
                "missing_discovery_metadata",
                "Keywords and contexts are required.",
                page_id,
            )
        )
    if not page.platforms or set(page.platforms) - _PLATFORMS:
        issues.append(_error("invalid_platform", "Page has an unknown platform value.", page_id))
    if len(page.related_pages) != len(set(page.related_pages)):
        issues.append(_error("duplicate_related_page", "Related page IDs must be unique.", page_id))
    for related in page.related_pages:
        if related == page_id:
            issues.append(_error("self_related_page", "A page cannot relate to itself.", page_id))
        if related not in page_ids:
            issues.append(
                _error("unknown_related_page", f"Related page does not exist: {related}", page_id)
            )
    normalized_page_examples = [
        " ".join(example.casefold().split()) for example in page.nlp_examples
    ]
    if len(normalized_page_examples) != len(set(normalized_page_examples)):
        issues.append(
            _error("duplicate_nlp_example", "Natural-language examples must be unique.", page_id)
        )
    for original, normalized in zip(page.nlp_examples, normalized_page_examples, strict=True):
        if original not in page.body:
            issues.append(
                _error(
                    "undocumented_nlp_example",
                    f"Natural-language example is metadata-only: {original}",
                    page_id,
                )
            )
        if known_nlp_examples is not None and normalized not in known_nlp_examples:
            issues.append(
                _error(
                    "missing_nlp_fixture",
                    f"Natural-language example has no parser fixture: {original}",
                    page_id,
                )
            )
    if command_ids is not None:
        for command in page.related_commands:
            if command not in command_ids:
                issues.append(
                    _error("unknown_command", f"Related command does not exist: {command}", page_id)
                )
    if setting_ids is not None:
        for setting in page.related_settings:
            if setting not in setting_ids:
                issues.append(
                    _error("unknown_setting", f"Related setting does not exist: {setting}", page_id)
                )

    heading_anchors = {heading.anchor for heading in headings}
    if page.related_pages:
        heading_anchors.add("related-topics")
    for target in extract_markdown_links(page.body):
        try:
            link = parse_help_link(target)
        except ValueError as exc:
            issues.append(_error("unsafe_link", str(exc), page_id))
            continue
        if link.scheme == "help" and link.identifier not in resolvable_ids:
            issues.append(
                _error(
                    "unknown_help_link",
                    f"Help target does not exist: {link.identifier}",
                    page_id,
                )
            )
        elif link.scheme == "command" and command_ids is not None:
            if link.identifier not in command_ids:
                issues.append(
                    _error(
                        "unknown_command_link",
                        f"Command link does not exist: {link.identifier}",
                        page_id,
                    )
                )
        elif link.scheme == "setting" and setting_ids is not None:
            if link.identifier not in setting_ids:
                issues.append(
                    _error(
                        "unknown_setting_link",
                        f"Setting link does not exist: {link.identifier}",
                        page_id,
                    )
                )
        elif link.scheme == "anchor" and link.identifier not in heading_anchors:
            issues.append(
                _error(
                    "unknown_anchor",
                    f"Local heading anchor does not exist: {link.identifier}",
                    page_id,
                )
            )

    text = f"{page.title}\n{page.summary}\n{page.body}"
    for domain in _EMAIL.findall(text):
        if domain.casefold() not in _ALLOWED_EMAIL_DOMAINS:
            issues.append(
                _error("non_synthetic_email", f"Non-synthetic email domain: {domain}", page_id)
            )
    for code, pattern in (
        ("discord_webhook_secret", _DISCORD_SECRET),
        ("bearer_secret", _BEARER_SECRET),
        ("private_key", _PRIVATE_KEY),
        ("development_prompt_language", _PROMPT_LANGUAGE),
    ):
        if pattern.search(text):
            issues.append(_error(code, "Sensitive or internal-only text is not allowed.", page_id))
    if known_cli_syntax is not None:
        for example in _documented_cli_examples(page.body):
            # These two reference templates intentionally name no actual command.
            if re.fullmatch(r"GROUP(?: COMMAND)? --help", example):
                continue
            problem = known_cli_syntax.validate(example)
            if problem is not None:
                issues.append(_error("invalid_cli_syntax", problem, page_id))
    elif known_cli_paths is not None:
        for example in _documented_cli_examples(page.body):
            normalized = " ".join(example.casefold().split())
            if example in {"--help", "GROUP --help", "GROUP COMMAND --help"}:
                continue
            matching_paths = tuple(
                path
                for path in known_cli_paths
                if normalized == path
                or normalized.startswith(path + " ")
                or normalized.startswith(path + " --")
            )
            if not matching_paths:
                issues.append(
                    _error(
                        "unknown_cli_example",
                        f"CLI example does not start with an installed command: gucmail {example}",
                        page_id,
                    )
                )
                continue
            command_path = max(matching_paths, key=len)
            remainder = normalized[len(command_path) :].strip()
            group_path = any(path.startswith(command_path + " ") for path in known_cli_paths)
            if group_path and remainder and not remainder.startswith("-"):
                issues.append(
                    _error(
                        "unknown_cli_example",
                        f"CLI example names an unknown subcommand: gucmail {example}",
                        page_id,
                    )
                )
                continue
            if known_cli_options is not None:
                permitted = known_cli_options.get(command_path, set()) | {"--help"}
                for option in _CLI_OPTION.findall(normalized[len(command_path) :]):
                    if option not in permitted:
                        issues.append(
                            _error(
                                "unknown_cli_option",
                                f"CLI example uses an unknown option for {command_path}: {option}",
                                page_id,
                            )
                        )
    return issues


def _documented_cli_examples(markdown: str) -> tuple[str, ...]:
    """Return unique inline and fenced/shell-line examples in source order."""

    matches: list[tuple[int, str]] = []
    matches.extend(
        (match.start(), match.group(1).strip()) for match in _GUCMAIL_INLINE.finditer(markdown)
    )
    matches.extend(
        (match.start(), match.group(1).strip()) for match in _GUCMAIL_LINE.finditer(markdown)
    )
    seen: set[str] = set()
    examples: list[str] = []
    for _, example in sorted(matches):
        if example and example not in seen:
            seen.add(example)
            examples.append(example)
    return tuple(examples)


def is_safe_help_source_path(value: str) -> bool:
    """Return whether a catalog path is normalized, relative, and portable."""

    if not value or value != value.strip():
        return False
    if "\\" in value or ":" in value or any(ord(character) < 32 for character in value):
        return False
    source = PurePosixPath(value)
    return (
        not source.is_absolute()
        and ".." not in source.parts
        and source.suffix.casefold() == ".md"
        and source.as_posix() == value
    )


def _error(code: str, detail: str, page_id: str | None = None) -> HelpValidationIssue:
    return HelpValidationIssue(HelpIssueSeverity.ERROR, code, detail, page_id)
