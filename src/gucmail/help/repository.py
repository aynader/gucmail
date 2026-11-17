"""Package-resource loader and capability-aware Help repository."""

from __future__ import annotations

import tomllib
from collections.abc import Collection, Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from gucmail.help.cli_syntax import CliCommandSyntax
from gucmail.help.models import (
    HelpAvailability,
    HelpCorpusError,
    HelpFeatureKind,
    HelpHeading,
    HelpHomeSections,
    HelpPage,
    HelpRuntimeContext,
    HelpSearchResult,
    HelpSkillLevel,
    HelpTocNode,
    HelpUserState,
    HelpValidationReport,
    RenderedHelpPage,
)
from gucmail.help.search import HelpSearchIndex
from gucmail.help.structure import build_help_toc, markdown_outline
from gucmail.help.validation import is_safe_help_source_path, validate_help_pages

_CATALOG_RESOURCE = "catalog.toml"
_CATALOG_KEYS = frozenset({"schema_version", "home_page", "pages"})
_PAGE_KEYS = frozenset(
    {
        "id",
        "title",
        "category",
        "summary",
        "path",
        "legacy_ids",
        "keywords",
        "aliases",
        "commands",
        "settings",
        "related",
        "level",
        "platforms",
        "contexts",
        "capabilities",
        "kind",
        "errors",
        "nlp_examples",
    }
)


class HelpRepository:
    """Immutable, offline repository for discovery, rendering, and validation."""

    def __init__(
        self,
        pages: Sequence[HelpPage],
        *,
        home_page_id: str,
        schema_version: int,
    ) -> None:
        self._pages = tuple(pages)
        pages_by_id: dict[str, HelpPage] = {}
        for page in self._pages:
            for identifier in (page.page_id, *page.legacy_ids):
                normalized = identifier.casefold().strip()
                if normalized in pages_by_id:
                    raise HelpCorpusError(f"Duplicate Help page or legacy ID: {identifier}")
                pages_by_id[normalized] = page
        self._by_id: Mapping[str, HelpPage] = MappingProxyType(pages_by_id)
        self.home_page_id = home_page_id
        self.schema_version = schema_version
        self._search = HelpSearchIndex(self._pages)

    @classmethod
    def load_default(cls, *, validate: bool = True) -> HelpRepository:
        return _load_default_repository(validate)

    @property
    def pages(self) -> tuple[HelpPage, ...]:
        return self._pages

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(page.category for page in self._pages))

    def get(self, page_id: str) -> HelpPage:
        try:
            return self._by_id[page_id.casefold().strip()]
        except KeyError as exc:
            raise KeyError(f"Unknown Help page: {page_id}") from exc

    def pages_in_category(self, category: str) -> tuple[HelpPage, ...]:
        normalized = category.casefold().strip()
        return tuple(page for page in self._pages if page.category.casefold() == normalized)

    def pages_for_context(
        self, active_view: str, *, platform: str | None = None
    ) -> tuple[HelpPage, ...]:
        normalized = active_view.casefold().strip()
        matches = tuple(page for page in self._pages if normalized in page.contexts)
        if platform is None:
            return matches
        return tuple(page for page in matches if page.supports_platform(platform))

    def table_of_contents(
        self,
        *,
        platform: str | None = None,
        include_headings: bool = True,
    ) -> HelpTocNode:
        """Return Help → category → page → heading navigation data."""

        return build_help_toc(
            self._pages,
            platform=platform,
            include_headings=include_headings,
        )

    def outline(self, page_id: str) -> tuple[HelpHeading, ...]:
        """Return jump targets for one article, including its level-one title."""

        return markdown_outline(self.get(page_id).body)

    def home_sections(
        self,
        *,
        context: HelpRuntimeContext | None = None,
        state: HelpUserState | None = None,
    ) -> HelpHomeSections:
        """Build a concise, contextual Help home without dumping the manual."""

        platform = context.platform if context is not None else None
        contextual = (
            self.pages_for_context(context.active_view, platform=platform)
            if context is not None and context.active_view
            else ()
        )
        state = state or HelpUserState()
        return HelpHomeSections(
            start_here=self.pages_in_category("Start Here")[:6],
            contextual=contextual[:6],
            popular=self._known_pages(
                (
                    "mail.scheduled",
                    "notifications.windows",
                    "startup.windows",
                    "backup.overview",
                    "integrations.discord",
                    "storage.quota",
                ),
                platform=platform,
            ),
            troubleshooting=self.pages_in_category("Troubleshooting")[:8],
            recent=self._known_pages(state.recent_page_ids[:8], platform=platform),
            favorites=self._known_pages(tuple(sorted(state.favorite_page_ids)), platform=platform),
        )

    def _known_pages(
        self,
        page_ids: Sequence[str],
        *,
        platform: str | None,
    ) -> tuple[HelpPage, ...]:
        pages: list[HelpPage] = []
        for page_id in page_ids:
            try:
                page = self.get(page_id)
            except KeyError:
                continue
            if platform is None or page.supports_platform(platform):
                pages.append(page)
        return tuple(pages)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        context: HelpRuntimeContext | None = None,
    ) -> tuple[HelpSearchResult, ...]:
        return self._search.search(query, limit=limit, context=context)

    def for_error(
        self,
        error: BaseException | str,
        *,
        context: HelpRuntimeContext | None = None,
        limit: int = 3,
    ) -> tuple[HelpSearchResult, ...]:
        """Resolve a sanitized error description directly to recovery guidance."""

        label = str(error) if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return self._search.search_error(label, limit=limit, context=context)

    def render(
        self, page_id: str, *, context: HelpRuntimeContext | None = None
    ) -> RenderedHelpPage:
        page = self.get(page_id)
        availability, detail = _availability(page, context)
        # Say nothing when the feature simply works. Flag only the exceptions.
        status = (
            ""
            if availability is HelpAvailability.AVAILABLE
            else f"> **Availability:** {detail}\n\n"
        )
        related = ""
        if page.related_pages:
            links = "\n".join(
                f"- [{_markdown_label(self.get(identifier).title)}](help:{identifier})"
                for identifier in page.related_pages
            )
            related = f"\n\n## Related topics\n\n{links}"
        actions: list[str] = []
        actions.extend(
            f"- [Command `{_markdown_label(identifier)}`](command:{identifier})"
            for identifier in page.related_commands
        )
        actions.extend(
            f"- [Setting `{_markdown_label(identifier)}`](setting:{identifier})"
            for identifier in page.related_settings
        )
        if actions:
            related += "\n\n## Related actions and settings\n\n" + "\n".join(actions)
        generated = _generated_reference(page.page_id, context=context)
        return RenderedHelpPage(
            page,
            availability,
            detail,
            status + page.body + generated + related,
        )

    def validate(
        self,
        *,
        known_command_ids: Collection[str] | None = None,
        known_setting_ids: Collection[str] | None = None,
        known_shortcut_ids: Collection[str] | None = None,
        known_nlp_examples: Collection[str] | None = None,
        known_cli_paths: Collection[str] | None = None,
        known_cli_options: Mapping[str, Collection[str]] | None = None,
        known_cli_syntax: CliCommandSyntax | None = None,
    ) -> HelpValidationReport:
        return validate_help_pages(
            self._pages,
            home_page_id=self.home_page_id,
            known_command_ids=known_command_ids,
            known_setting_ids=known_setting_ids,
            known_shortcut_ids=known_shortcut_ids,
            known_nlp_examples=known_nlp_examples,
            known_cli_paths=known_cli_paths,
            known_cli_options=known_cli_options,
            known_cli_syntax=known_cli_syntax,
        )


def _generated_reference(
    page_id: str,
    *,
    context: HelpRuntimeContext | None = None,
) -> str:
    """Generate drift-free command and settings reference sections at render time."""

    if page_id == "reference.commands":
        from gucmail.help.reference import command_reference_markdown
        from gucmail.palette import universal_palette_registry

        commands = universal_palette_registry().all()
        return "\n\n## Generated canonical command catalog\n\n" + command_reference_markdown(
            commands
        )
    if page_id == "palette.overview":
        from gucmail.help.reference import natural_language_reference_markdown
        from gucmail.palette import universal_palette_registry

        return (
            "\n\n## Tested natural-language example library\n\n"
            "Every example below comes from the same deterministic corpus exercised by the "
            "parser regression suite. Review the preview before running a mutating command.\n\n"
            + natural_language_reference_markdown(universal_palette_registry().all())
        )
    if page_id == "settings.overview":
        from gucmail.help.reference import settings_reference_markdown

        return "\n\n## Generated settings reference\n\n" + settings_reference_markdown(context)
    if page_id == "reference.shortcuts":
        from gucmail.help.reference import shortcuts_reference_markdown

        return "\n\n## Current shortcut catalog\n\n" + shortcuts_reference_markdown(context)
    if page_id == "storage.quota":
        facts = context.facts if context is not None else {}
        used = facts.get("quota.used.bytes")
        used_text = _help_bytes(used) if isinstance(used, int) else "not locally recorded"
        source = str(facts.get("quota.used.source") or "unavailable")
        observed = str(facts.get("quota.used.observed_at") or "not recorded")
        capacity = facts.get("quota.total_capacity.bytes")
        if isinstance(capacity, int):
            capacity_text = _help_bytes(capacity)
            capacity_source = str(
                facts.get("quota.total_capacity.source") or "recorded observation"
            )
        else:
            capacity_text = "Unknown"
            capacity_source = "not reported"
        return (
            "\n\n## Current local quota snapshot\n\n"
            f"- Current usage: **{used_text}** · source: `{source}` · observed: `{observed}`\n"
            f"- Effective capacity: **{capacity_text}** · source: "
            f"`{capacity_source}`\n"
            "- Open Storage for a fresh bounded connected estimate and the battery indicator.\n\n"
            "This Help snapshot reads only durable local evidence and never contacts Exchange."
        )
    return ""


def _help_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


@lru_cache(maxsize=2)
def _load_default_repository(validate: bool) -> HelpRepository:
    root = files("gucmail.help")
    try:
        catalog = tomllib.loads(root.joinpath(_CATALOG_RESOURCE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HelpCorpusError("Packaged Help catalog could not be loaded.") from exc
    unknown_catalog_keys = set(catalog) - _CATALOG_KEYS
    if unknown_catalog_keys:
        names = ", ".join(sorted(unknown_catalog_keys))
        raise HelpCorpusError(f"Unknown Help catalog metadata: {names}")
    schema_version = catalog.get("schema_version")
    home_page_id = catalog.get("home_page")
    raw_pages = catalog.get("pages")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise HelpCorpusError("Help catalog schema_version must be a positive integer.")
    if not isinstance(home_page_id, str) or not home_page_id.strip():
        raise HelpCorpusError("Help catalog home_page is missing.")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise HelpCorpusError("Help catalog contains no pages.")

    pages: list[HelpPage] = []
    declared_paths: list[str] = []
    for raw in raw_pages:
        if not isinstance(raw, dict):
            raise HelpCorpusError("Every Help catalog page must be a table.")
        unknown = set(raw) - _PAGE_KEYS
        if unknown:
            raise HelpCorpusError(f"Unknown Help page metadata: {', '.join(sorted(unknown))}")
        path = _required_string(raw, "path")
        if not is_safe_help_source_path(path):
            raise HelpCorpusError(f"Unsafe Help page resource path: {path}")
        source = PurePosixPath(path)
        try:
            body = root.joinpath("pages", *source.parts).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise HelpCorpusError(f"Packaged Help page is missing: {path}") from exc
        declared_paths.append(path)
        try:
            skill_level = HelpSkillLevel(raw.get("level", "beginner"))
            feature_kind = HelpFeatureKind(raw.get("kind", "reference"))
        except (TypeError, ValueError) as exc:
            raise HelpCorpusError(f"Help page has invalid enum metadata: {path}") from exc
        pages.append(
            HelpPage(
                page_id=_required_string(raw, "id").casefold(),
                title=_required_string(raw, "title"),
                category=_required_string(raw, "category"),
                summary=_required_string(raw, "summary"),
                body=body,
                source_path=path,
                legacy_ids=tuple(value.casefold() for value in _string_tuple(raw, "legacy_ids")),
                keywords=_string_tuple(raw, "keywords"),
                aliases=_string_tuple(raw, "aliases"),
                related_commands=_string_tuple(raw, "commands"),
                related_settings=_string_tuple(raw, "settings"),
                related_pages=_string_tuple(raw, "related"),
                skill_level=skill_level,
                platforms=_string_tuple(raw, "platforms") or ("all",),
                contexts=tuple(value.casefold() for value in _string_tuple(raw, "contexts")),
                required_capabilities=_string_tuple(raw, "capabilities"),
                feature_kind=feature_kind,
                error_patterns=_string_tuple(raw, "errors"),
                nlp_examples=_string_tuple(raw, "nlp_examples"),
            )
        )

    normalized_paths = [path.casefold() for path in declared_paths]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise HelpCorpusError("Help catalog contains duplicate Markdown source paths.")
    try:
        packaged_paths = set(_markdown_resource_paths(root.joinpath("pages")))
    except OSError as exc:
        raise HelpCorpusError("Packaged Help page directory could not be inspected.") from exc
    orphan_paths = packaged_paths - set(declared_paths)
    if orphan_paths:
        names = ", ".join(sorted(orphan_paths))
        raise HelpCorpusError(f"Packaged Help pages are missing from the catalog: {names}")

    repository = HelpRepository(
        pages, home_page_id=home_page_id.casefold().strip(), schema_version=schema_version
    )
    if validate:
        repository.validate().raise_for_errors()
    return repository


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HelpCorpusError(f"Help page metadata {key!r} must be a non-empty string.")
    return value.strip()


def _string_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, ())
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise HelpCorpusError(f"Help page metadata {key!r} must be an array of strings.")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _markdown_resource_paths(
    directory: Traversable,
    prefix: PurePosixPath | None = None,
) -> tuple[str, ...]:
    prefix = prefix or PurePosixPath()
    paths: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        relative = prefix.joinpath(child.name)
        if child.is_dir():
            paths.extend(_markdown_resource_paths(child, relative))
        elif child.is_file() and PurePosixPath(child.name).suffix.casefold() == ".md":
            paths.append(relative.as_posix())
    return tuple(paths)


def _markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _availability(
    page: HelpPage, context: HelpRuntimeContext | None
) -> tuple[HelpAvailability, str]:
    if context is not None and not page.supports_platform(context.platform):
        return (
            HelpAvailability.NOT_APPLICABLE,
            f"For {', '.join(page.platforms)} only.",
        )
    if page.feature_kind in {HelpFeatureKind.LOCAL, HelpFeatureKind.REFERENCE}:
        return HelpAvailability.AVAILABLE, "Works offline."
    if not page.required_capabilities:
        return HelpAvailability.AVAILABLE, "Needs a connection to the GUC server."
    if context is None:
        return HelpAvailability.UNKNOWN, "Not checked on the GUC server yet."
    values = [context.capabilities.get(name) for name in page.required_capabilities]
    if any(value is False for value in values):
        return (
            HelpAvailability.UNAVAILABLE,
            "The GUC server doesn't allow this.",
        )
    if all(value is True for value in values):
        return HelpAvailability.AVAILABLE, "Supported by the GUC server."
    return HelpAvailability.UNKNOWN, "Not checked on the GUC server yet."
