"""Structured settings registry, staged editing, import/export, and undo.

This module intentionally contains no TUI code.  It is the shared settings model
for the TUI, command palette, CLI, and Help index.  Operational state (watcher
leases, scheduled jobs, sync checkpoints, and similar correctness data) does not
belong here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from rapidfuzz import fuzz

from gucmail.config import (
    AppConfig,
    ConfigDocument,
    ConfigStore,
    _parse_bool,
    _reject_plaintext_secrets,
)
from gucmail.errors import ConfigurationError
from gucmail.services._base import validate_reference
from gucmail.services.attachment_indexing import normalize_formats, normalize_language
from gucmail.services.attachments import CollisionPolicy
from gucmail.services.calendar_working_time import (
    WORKING_TIME_SETTINGS,
    CalendarWorkingTime,
    normalize_workday_time,
    normalize_working_days,
)
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
    normalize_binding_overrides,
    validate_binding_configuration,
)
from gucmail.services.layout_presets import normalize_presets
from gucmail.services.mail_preferences import (
    SORT_DIRECTIONS,
    SORT_FIELDS,
    DeleteBehavior,
    ReplyMode,
)
from gucmail.services.message_columns import columns_value, parse_columns
from gucmail.services.pim_preferences import OPTIONAL_INTEGER_SETTINGS, optional_whole_number
from gucmail.services.shell_preferences import HEADER_DEFAULT, STATUS_DEFAULT, parse_shell_fields
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS, normalize_shortcut, validate_shortcuts
from gucmail.services.task_recurrence import TASK_REPEAT_SETTINGS, TaskRepeatPreferences

SETTINGS_EXPORT_SCHEMA_VERSION = 1


class SettingType(StrEnum):
    STRING = "string"
    OPTIONAL_STRING = "optional_string"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    OPTIONAL_NUMBER = "optional_number"
    STRING_LIST = "string_list"
    CHOICE = "choice"
    STRUCTURED = "structured"


class SettingScope(StrEnum):
    GLOBAL = "global"
    ACCOUNT = "account"
    MACHINE = "machine"
    PROFILE = "profile"


class SettingLevel(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"


class ApplySemantics(StrEnum):
    IMMEDIATE = "immediate"
    STAGED = "staged"
    RESTART = "restart"
    EXTERNAL = "external"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"  # noqa: S105 - classification label, never a credential value


class ValueSource(StrEnum):
    DEFAULT = "default"
    CONFIGURATION = "configuration"
    ENVIRONMENT = "environment"
    ACCOUNT = "account"
    MACHINE = "machine"
    PROFILE = "profile"
    COMMAND_LINE = "command_line"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class SettingDependency:
    setting_id: str
    required_value: Any = True
    explanation: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyLock:
    """An optional centrally supplied lock, never persisted as a user preference."""

    reason: str
    enforced_value: Any = None
    enforce_value: bool = False
    source: str = "policy"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    setting_id: str
    label: str
    description: str
    category: str
    value_type: SettingType
    default: Any
    aliases: tuple[str, ...]
    scope: SettingScope
    apply_semantics: ApplySemantics
    help_id: str
    attr_name: str | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    exportable: bool = True
    level: SettingLevel = SettingLevel.BASIC
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    external_effect: bool = False
    keywords: tuple[str, ...] = ()
    dependencies: tuple[SettingDependency, ...] = ()

    @property
    def restart_required(self) -> bool:
        return self.apply_semantics is ApplySemantics.RESTART

    @property
    def sensitive(self) -> bool:
        return self.sensitivity is not Sensitivity.PUBLIC

    @property
    def environment_variable(self) -> str:
        if self.attr_name:
            return "GUCMAIL_" + self.attr_name.upper()
        normalized = self.setting_id.upper().replace("-", "_").replace(".", "__")
        return "GUCMAIL_SETTING_" + normalized

    def parse(self, raw: Any) -> Any:
        try:
            value = self._parse_unvalidated(raw)
            if self.setting_id in {"layout.header_contents", "layout.status_bar_contents"}:
                value = parse_shell_fields(
                    value, status=self.setting_id == "layout.status_bar_contents"
                )
            if self.setting_id == "mail.delete_folder" and value is not None:
                value = (
                    validate_reference(value, label="Delete destination folder")
                    if value.strip()
                    else None
                )
            if self.setting_id == "search.attachment_formats":
                value = normalize_formats(value)
            if self.setting_id == "search.attachment_ocr_language":
                value = normalize_language(value)
            if self.setting_id in OPTIONAL_INTEGER_SETTINGS:
                value = optional_whole_number(value)
            if self.setting_id == "calendar.working_days":
                value = normalize_working_days(value)
            if self.setting_id in {"calendar.workday_start", "calendar.workday_end"}:
                value = normalize_workday_time(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid value for {self.setting_id}: {exc}") from exc
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ConfigurationError(
                    f"{self.label} must be at least {_friendly_number(self.minimum)}."
                )
            if self.maximum is not None and value > self.maximum:
                raise ConfigurationError(
                    f"{self.label} must be at most {_friendly_number(self.maximum)}."
                )
        return value

    def _parse_unvalidated(self, raw: Any) -> Any:
        if self.value_type is SettingType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return _parse_bool(raw)
            raise TypeError("expected a boolean")
        if self.value_type is SettingType.INTEGER:
            if isinstance(raw, bool):
                raise TypeError("expected an integer")
            if isinstance(raw, int):
                return raw
            if isinstance(raw, str):
                return int(raw.strip())
            raise TypeError("expected an integer")
        if self.value_type in {SettingType.NUMBER, SettingType.OPTIONAL_NUMBER}:
            if raw is None and self.value_type is SettingType.OPTIONAL_NUMBER:
                return None
            if isinstance(raw, bool):
                raise TypeError("expected a number")
            if isinstance(raw, (int, float)):
                numeric_value = float(raw)
                if not math.isfinite(numeric_value):
                    raise ValueError("expected a finite number")
                return numeric_value
            if isinstance(raw, str):
                normalized = raw.strip().casefold()
                if self.value_type is SettingType.OPTIONAL_NUMBER and normalized in {
                    "",
                    "none",
                    "unknown",
                    "null",
                }:
                    return None
                numeric_value = float(normalized)
                if not math.isfinite(numeric_value):
                    raise ValueError("expected a finite number")
                return numeric_value
            raise TypeError("expected a number")
        if self.value_type is SettingType.STRING_LIST:
            if isinstance(raw, str):
                stripped = raw.strip()
                if not stripped:
                    return ()
                if stripped.startswith("["):
                    decoded = json.loads(stripped)
                    if not isinstance(decoded, list) or not all(
                        isinstance(item, str) for item in decoded
                    ):
                        raise ValueError("expected a JSON list of strings")
                    return tuple(decoded)
                return tuple(part.strip() for part in stripped.split(",") if part.strip())
            if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
                if not all(isinstance(item, str) for item in raw):
                    raise TypeError("expected a list of strings")
                return tuple(raw)
            raise TypeError("expected a list of strings")
        if self.value_type is SettingType.STRUCTURED:
            if isinstance(raw, str):
                raw = json.loads(raw)
            if not isinstance(raw, (dict, list, tuple)):
                raise TypeError("expected a JSON object or array")
            value = copy.deepcopy(raw)
            _validate_structured_value(value, setting_id=self.setting_id)
            if isinstance(value, tuple):
                value = list(value)
            if self.setting_id == "layout.message_columns":
                value = columns_value(parse_columns(value))
            elif self.setting_id == "layout.presets":
                value = normalize_presets(
                    value, lambda key, raw: DEFAULT_SETTINGS.get(key).parse(raw)
                )
            elif self.setting_id == KEYBOARD_SETTING:
                value = normalize_binding_overrides(value)
            return value
        if self.value_type is SettingType.CHOICE:
            if not isinstance(raw, str):
                raise TypeError("expected text")
            for choice in self.choices:
                if raw.strip().casefold() == choice.casefold():
                    return choice
            if self.setting_id == "appearance.branding":
                # The branding browser exposes the entire bundled catalog, not
                # only the small list of convenient Settings choices.
                from gucmail.tui.branding import BrandingCatalog, BrandingValidationError

                requested = raw.strip().casefold()
                try:
                    return BrandingCatalog().get(requested).id
                except BrandingValidationError as exc:
                    raise ValueError("choose a valid branding preset") from exc
            raise ValueError("choose one of: " + ", ".join(self.choices))
        if self.value_type is SettingType.OPTIONAL_STRING:
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise TypeError("expected text or no value")
            return raw
        if not isinstance(raw, str):
            raise TypeError("expected text")
        if self.setting_id in SHORTCUT_DEFAULTS:
            return normalize_shortcut(raw)
        return raw


@dataclass(frozen=True, slots=True)
class SettingSearchResult:
    spec: SettingSpec
    score: float
    matched_text: str


@dataclass(frozen=True, slots=True)
class SettingsFilter:
    categories: tuple[str, ...] = ()
    scopes: tuple[SettingScope, ...] = ()
    value_types: tuple[SettingType, ...] = ()
    levels: tuple[SettingLevel, ...] = ()
    apply_semantics: tuple[ApplySemantics, ...] = ()
    sensitivities: tuple[Sensitivity, ...] = ()
    restart_required: bool | None = None

    def matches(self, spec: SettingSpec) -> bool:
        categories = {value.casefold() for value in self.categories}
        return not (
            (categories and spec.category.casefold() not in categories)
            or (self.scopes and spec.scope not in self.scopes)
            or (self.value_types and spec.value_type not in self.value_types)
            or (self.levels and spec.level not in self.levels)
            or (self.apply_semantics and spec.apply_semantics not in self.apply_semantics)
            or (self.sensitivities and spec.sensitivity not in self.sensitivities)
            or (
                self.restart_required is not None
                and spec.restart_required is not self.restart_required
            )
        )


class SettingRegistry:
    def __init__(self, specs: Iterable[SettingSpec]) -> None:
        ordered = tuple(specs)
        by_id: dict[str, SettingSpec] = {}
        by_alias: dict[str, SettingSpec] = {}
        attrs: dict[str, str] = {}
        for spec in ordered:
            _validate_setting_spec_metadata(spec)
            normalized_id = spec.setting_id.casefold()
            if normalized_id in by_id:
                raise ValueError(f"Duplicate setting ID: {spec.setting_id}")
            by_id[normalized_id] = spec
            if not spec.help_id:
                raise ValueError(f"Setting {spec.setting_id} has no Help target.")
            if spec.sensitivity is Sensitivity.SECRET and spec.exportable:
                raise ValueError(f"Secret setting {spec.setting_id} may not be exportable.")
            if spec.attr_name:
                if spec.attr_name in attrs:
                    raise ValueError(
                        f"AppConfig field {spec.attr_name} is registered by both "
                        f"{attrs[spec.attr_name]} and {spec.setting_id}."
                    )
                attrs[spec.attr_name] = spec.setting_id
            for name in (spec.setting_id, *spec.aliases):
                normalized = _normalize_search(name)
                existing = by_alias.get(normalized)
                if existing is not None and existing.setting_id != spec.setting_id:
                    raise ValueError(
                        f"Setting lookup alias {name!r} belongs to both "
                        f"{existing.setting_id} and {spec.setting_id}."
                    )
                by_alias[normalized] = spec
        _validate_setting_dependencies(ordered, by_alias)
        self._specs = ordered
        self._by_id = by_id
        self._by_alias = by_alias
        self._attr_ids = attrs

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, setting_id_or_alias: str) -> SettingSpec:
        exact = self._by_id.get(setting_id_or_alias.casefold())
        if exact is not None:
            return exact
        normalized = _normalize_search(setting_id_or_alias)
        alias = self._by_alias.get(normalized)
        if alias is not None:
            return alias
        raise ConfigurationError(f"Unknown setting: {setting_id_or_alias}")

    def for_app_config_field(self, field_name: str) -> SettingSpec:
        setting_id = self._attr_ids.get(field_name)
        if setting_id is None:
            raise ConfigurationError(f"No setting metadata exists for AppConfig.{field_name}.")
        return self.get(setting_id)

    def by_category(self, category: str) -> tuple[SettingSpec, ...]:
        normalized = category.casefold()
        return tuple(spec for spec in self._specs if spec.category.casefold() == normalized)

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(spec.category for spec in self._specs))

    def filter(self, settings_filter: SettingsFilter | None = None) -> tuple[SettingSpec, ...]:
        active_filter = settings_filter or SettingsFilter()
        return tuple(spec for spec in self._specs if active_filter.matches(spec))

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        maximum_level: SettingLevel = SettingLevel.EXPERT,
        settings_filter: SettingsFilter | None = None,
    ) -> tuple[SettingSearchResult, ...]:
        if limit < 1:
            return ()
        normalized_query = _normalize_search(_natural_settings_query(query))
        eligible = tuple(
            spec
            for spec in self._specs
            if _level_rank(spec.level) <= _level_rank(maximum_level)
            and (settings_filter is None or settings_filter.matches(spec))
        )
        if not normalized_query:
            return tuple(SettingSearchResult(spec, 0.0, spec.label) for spec in eligible[:limit])
        results: list[SettingSearchResult] = []
        for spec in eligible:
            candidates = (
                (spec.setting_id, 100.0),
                (spec.label, 100.0),
                *((alias, 100.0) for alias in spec.aliases),
                *((keyword, 100.0) for keyword in spec.keywords),
                (spec.description, 90.0),
                (spec.category, 82.0),
            )
            best_score = 0.0
            best_text = spec.label
            query_tokens = set(normalized_query.split())
            for candidate, weight in candidates:
                normalized_candidate = _normalize_search(candidate)
                candidate_tokens = set(normalized_candidate.split())
                if normalized_query == normalized_candidate:
                    score = weight
                elif query_tokens and query_tokens <= candidate_tokens:
                    score = min(weight - 1.0, 98.0)
                elif normalized_query in normalized_candidate:
                    score = min(weight - 2.0, 96.0 - len(normalized_candidate) / 30.0)
                else:
                    score = float(fuzz.WRatio(normalized_query, normalized_candidate)) * (
                        weight / 100.0
                    )
                if score > best_score:
                    best_score = score
                    best_text = candidate
            if best_score >= 42.0:
                results.append(SettingSearchResult(spec, best_score, best_text))
        results.sort(key=lambda result: (-result.score, result.spec.label.casefold()))
        return tuple(results[:limit])


@dataclass(frozen=True, slots=True)
class SettingsContext:
    account_id: str | None = None
    machine_id: str | None = None
    profile: str = "default"
    scoped_app_config: bool = False


@dataclass(frozen=True, slots=True)
class EffectiveSetting:
    spec: SettingSpec
    configured_value: Any
    effective_value: Any
    source: ValueSource
    overridden: bool
    environment_variable: str | None = None
    policy_locked: bool = False
    policy_reason: str | None = None
    dependencies_satisfied: bool = True
    dependency_reason: str | None = None
    secret_configured: bool = False


@dataclass(frozen=True, slots=True)
class SettingsDiff:
    setting_id: str
    before: Any
    after: Any
    reset: bool
    external_effect: bool


@dataclass(frozen=True, slots=True)
class SettingsImportPlan:
    changes: tuple[SettingsDiff, ...]
    unsupported: tuple[str, ...]
    excluded_secrets: tuple[str, ...]
    source_schema_version: int
    blocked: tuple[str, ...] = ()
    excluded_private: tuple[str, ...] = ()
    expected_revision: str | None = None
    guard_revision: bool = False

    @property
    def requires_external_confirmation(self) -> bool:
        return any(change.external_effect for change in self.changes)


@dataclass(frozen=True, slots=True)
class SettingsRevision:
    revision_id: str
    created_at: str
    reason: str
    changes: tuple[SettingsDiff, ...]
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    before_unset: tuple[str, ...] = ()


class RevisionStore(Protocol):
    def record(self, revision: SettingsRevision) -> None: ...

    def list(self) -> tuple[SettingsRevision, ...]: ...

    def get(self, revision_id: str) -> SettingsRevision | None: ...


class InMemoryRevisionStore:
    def __init__(self, *, limit: int = 20) -> None:
        self.limit = max(1, limit)
        self._revisions: list[SettingsRevision] = []

    def record(self, revision: SettingsRevision) -> None:
        _reject_plaintext_secrets(_revision_to_dict(revision))
        self._revisions.insert(0, revision)
        del self._revisions[self.limit :]

    def list(self) -> tuple[SettingsRevision, ...]:
        return tuple(self._revisions)

    def get(self, revision_id: str) -> SettingsRevision | None:
        return next(
            (revision for revision in self._revisions if revision.revision_id == revision_id),
            None,
        )


class FileRevisionStore:
    """Small JSON revision history for non-secret preferences."""

    def __init__(self, directory: Path, *, limit: int = 20) -> None:
        self.directory = directory
        self.limit = max(1, limit)

    def record(self, revision: SettingsRevision) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = _revision_to_dict(revision)
        _reject_plaintext_secrets(payload)
        target = (
            self.directory / f"{revision.created_at.replace(':', '')}-{revision.revision_id}.json"
        )
        _atomic_json_write(target, payload)
        self._prune()

    def list(self) -> tuple[SettingsRevision, ...]:
        if not self.directory.exists():
            return ()
        revisions: list[SettingsRevision] = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                revisions.append(_revision_from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, json.JSONDecodeError, ConfigurationError):
                continue
        return tuple(revisions[: self.limit])

    def get(self, revision_id: str) -> SettingsRevision | None:
        if not re.fullmatch(r"[0-9a-f]{32}", revision_id):
            return None
        return next(
            (revision for revision in self.list() if revision.revision_id == revision_id),
            None,
        )

    def _prune(self) -> None:
        paths = sorted(self.directory.glob("*.json"), reverse=True)
        for stale in paths[self.limit :]:
            try:
                stale.unlink()
            except OSError:
                continue


@dataclass(frozen=True, slots=True)
class _Change:
    spec: SettingSpec
    value: Any
    reset: bool


@dataclass(frozen=True, slots=True)
class SettingsValuesSnapshot:
    revision: str | None
    values: tuple[EffectiveSetting, ...]


class SettingsSession:
    """A staged edit transaction with exact preview/apply/cancel semantics."""

    def __init__(self, service: SettingsService, document: ConfigDocument) -> None:
        self._service = service
        self._document = document
        self._changes: dict[str, _Change] = {}
        self._closed = False

    @property
    def dirty(self) -> bool:
        return bool(self._changes)

    @property
    def base_revision(self) -> str | None:
        """Revision observed when staging began, including ``None`` for an absent file."""

        return self._document.revision

    def set(self, setting_id: str, raw_value: Any) -> SettingsSession:
        self._ensure_open()
        spec = self._service.registry.get(setting_id)
        if spec.setting_id in UNAVAILABLE_SETTINGS:
            raise ConfigurationError(UNAVAILABLE_SETTINGS[spec.setting_id])
        self._service._ensure_editable(spec, self._document)
        if spec.setting_id in MANAGED_SETTINGS:
            raise ConfigurationError(MANAGED_SETTINGS[spec.setting_id][1])
        if spec.sensitivity is Sensitivity.SECRET:
            raise ConfigurationError(
                f"{spec.label} must be changed through secure secret storage, not Settings."
            )
        self._changes[spec.setting_id] = _Change(spec, spec.parse(raw_value), False)
        return self

    def reset(self, setting_id: str) -> SettingsSession:
        self._ensure_open()
        spec = self._service.registry.get(setting_id)
        self._service._ensure_editable(spec, self._document)
        self._changes[spec.setting_id] = _Change(spec, spec.default, True)
        return self

    def reset_category(self, category: str) -> SettingsSession:
        self._ensure_open()
        specs = self._service.registry.by_category(category)
        if not specs:
            raise ConfigurationError(f"Unknown settings category: {category}")
        for spec in specs:
            if spec.sensitivity is Sensitivity.SECRET:
                continue
            try:
                self.reset(spec.setting_id)
            except ConfigurationError as exc:
                if "overridden" not in str(exc) and "locked by policy" not in str(exc):
                    raise
        return self

    def reset_all(self) -> SettingsSession:
        """Reset preferences only; operational state, secrets, and mailbox data are untouched."""

        self._ensure_open()
        for spec in self._service.registry:
            if spec.sensitivity is Sensitivity.SECRET:
                continue
            try:
                self.reset(spec.setting_id)
            except ConfigurationError as exc:
                if "overridden" not in str(exc) and "locked by policy" not in str(exc):
                    raise
        return self

    def preview(self) -> tuple[SettingsDiff, ...]:
        self._ensure_open()
        return self._service._preview(self._document, self._changes)

    def apply(
        self,
        *,
        reason: str = "Settings updated",
        confirm_external: bool = False,
    ) -> SettingsRevision | None:
        self._ensure_open()
        revision = self._service._apply(
            self._document,
            self._changes,
            reason=reason,
            confirm_external=confirm_external,
        )
        self._closed = True
        self._changes.clear()
        return revision

    def cancel(self) -> None:
        self._changes.clear()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigurationError("This settings edit session is already closed.")


UNAVAILABLE_SETTINGS: dict[str, str] = {
    "advanced.raw_soap_enabled": (
        "Arbitrary raw SOAP execution is not implemented. Registered typed EWS operations "
        "and ews request-preview classification remain available; this preference cannot "
        "authorize or execute XML."
    ),
    "privacy.remote_content": (
        "Remote email resource rendering is not implemented. HTML stays sanitized text; "
        "explicit browser links remain available."
    ),
    "privacy.cache_message_bodies": (
        "Automatic message-body caching is not implemented. Use explicit backups or indexing "
        "for offline content."
    ),
    "storage.cache_limit_mb": (
        "Automatic disk-cache quota enforcement is not implemented. Storage shows actual usage "
        "and offers confirmed cleanup."
    ),
    "advanced.feature_flags": (
        "No generic experimental flags are currently defined. Named feature settings have "
        "their own controls."
    ),
    "integrations.command_hooks_enabled": (
        "Hooks are enabled only per watch invocation with --command-hook and explicit "
        "confirmation. A global toggle cannot authorize a command."
    ),
}


MANAGED_SETTINGS: dict[str, tuple[str, str]] = {
    "monitoring.start_at_login": (
        "startup",
        "Manage sign-in startup in Windows Startup. The legacy preference is not OS status. "
        "CLI: gucmail startup enable --apply or startup disable --apply.",
    ),
    "integrations.webhook_secret": (
        "notifications",
        "Manage each provider secret through secure Notifications & Monitoring setup; "
        "no shared webhook secret is used.",
    ),
}


class SettingsService:
    def __init__(
        self,
        config_store: ConfigStore,
        *,
        registry: SettingRegistry | None = None,
        context: SettingsContext | None = None,
        revisions: RevisionStore | None = None,
        policy_locks: Mapping[str, PolicyLock] | None = None,
        command_line_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self.config_store = config_store
        self.registry = registry or DEFAULT_SETTINGS
        self.context = context or SettingsContext()
        self.revisions = revisions or FileRevisionStore(
            config_store.revisions_directory / "settings", limit=20
        )
        self.policy_locks: dict[str, PolicyLock] = {}
        for setting_id, policy in (policy_locks or {}).items():
            spec = self.registry.get(setting_id)
            if policy.enforce_value:
                spec.parse(policy.enforced_value)
            self.policy_locks[spec.setting_id] = policy
        self.command_line_overrides: dict[str, Any] = {}
        for setting_id, raw_value in (command_line_overrides or {}).items():
            spec = self.registry.get(setting_id)
            if spec.sensitivity is Sensitivity.SECRET:
                self.command_line_overrides[spec.setting_id] = True
            else:
                self.command_line_overrides[spec.setting_id] = spec.parse(raw_value)

    def begin(self) -> SettingsSession:
        return SettingsSession(self, self.config_store.load_document())

    def value(self, setting_id: str) -> EffectiveSetting:
        document = self.config_store.load_document()
        return self._value_from_document(self.registry.get(setting_id), document)

    def snapshot(
        self, setting_ids: Iterable[str], *, recover_invalid_keyboard: bool = False
    ) -> SettingsValuesSnapshot:
        """Return selected values and the revision from the same configuration read."""
        specs = tuple(self.registry.get(identifier) for identifier in setting_ids)
        document = self.config_store.load_document()
        return SettingsValuesSnapshot(
            document.revision,
            tuple(
                self._value_from_document(
                    spec, document, recover_invalid_keyboard=recover_invalid_keyboard
                )
                for spec in specs
            ),
        )

    def values(self, *, category: str | None = None) -> tuple[EffectiveSetting, ...]:
        document = self.config_store.load_document()
        specs = self.registry.by_category(category) if category else tuple(self.registry)
        return tuple(self._value_from_document(spec, document) for spec in specs)

    def filtered_values(
        self, settings_filter: SettingsFilter | None = None
    ) -> tuple[EffectiveSetting, ...]:
        document = self.config_store.load_document()
        return tuple(
            self._value_from_document(spec, document)
            for spec in self.registry.filter(settings_filter)
        )

    def materialize_config(self) -> AppConfig:
        """Apply scoped, environment, command-line, and policy values to AppConfig."""

        document = self.config_store.load_document()
        replacements = {
            spec.attr_name: self._value_from_document(spec, document).effective_value
            for spec in self.registry
            if spec.attr_name and spec.sensitivity is not Sensitivity.SECRET
        }
        return replace(document.stored_config, **replacements).validate()

    def set(
        self,
        setting_id: str,
        raw_value: Any,
        *,
        reason: str = "Setting updated",
        confirm_external: bool = False,
    ) -> SettingsRevision | None:
        session = self.begin().set(setting_id, raw_value)
        return session.apply(reason=reason, confirm_external=confirm_external)

    def reset(self, setting_id: str, *, reason: str = "Setting reset") -> SettingsRevision | None:
        return self.begin().reset(setting_id).apply(reason=reason)

    def reset_category(
        self, category: str, *, reason: str | None = None
    ) -> SettingsRevision | None:
        return (
            self.begin()
            .reset_category(category)
            .apply(reason=reason or f"Reset {category} settings")
        )

    def reset_all(self, *, reason: str = "Reset all preferences") -> SettingsRevision | None:
        return self.begin().reset_all().apply(reason=reason)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        maximum_level: SettingLevel = SettingLevel.EXPERT,
        settings_filter: SettingsFilter | None = None,
    ) -> tuple[SettingSearchResult, ...]:
        return self.registry.search(
            query,
            limit=limit,
            maximum_level=maximum_level,
            settings_filter=settings_filter,
        )

    def export(
        self,
        *,
        categories: Iterable[str] | None = None,
        setting_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        document = self.config_store.load_document()
        category_set = {category.casefold() for category in categories or ()}
        known_categories = {category.casefold() for category in self.registry.categories}
        unknown_categories = sorted(category_set - known_categories)
        if unknown_categories:
            raise ConfigurationError(
                "Unknown settings categories: " + ", ".join(unknown_categories)
            )
        id_set = {self.registry.get(setting_id).setting_id for setting_id in setting_ids or ()}
        selected: dict[str, Any] = {}
        excluded_secrets: list[str] = []
        excluded_private: list[str] = []
        for spec in self.registry:
            if category_set and spec.category.casefold() not in category_set:
                continue
            if id_set and spec.setting_id not in id_set:
                continue
            if spec.sensitivity is Sensitivity.SECRET:
                excluded_secrets.append(spec.setting_id)
                continue
            if not spec.exportable:
                excluded_private.append(spec.setting_id)
                continue
            selected[spec.setting_id] = self._value_from_document(spec, document).configured_value
        payload = {
            "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
            "application": "gucmail",
            "exported_at": datetime.now(UTC).isoformat(),
            "settings": selected,
            "excluded_secrets": sorted(excluded_secrets),
            "excluded_private": sorted(excluded_private),
        }
        _reject_plaintext_secrets(payload)
        return payload

    def export_to_path(
        self,
        path: Path,
        *,
        categories: Iterable[str] | None = None,
        setting_ids: Iterable[str] | None = None,
        overwrite: bool = False,
    ) -> Path:
        if path.exists() and not overwrite:
            raise ConfigurationError(f"Settings export already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(
            path,
            self.export(categories=categories, setting_ids=setting_ids),
        )
        return path

    def plan_import_file(
        self, path: Path, *, maximum_bytes: int = 4 * 1024 * 1024
    ) -> SettingsImportPlan:
        try:
            if path.stat().st_size > maximum_bytes:
                raise ConfigurationError("Settings import is larger than the 4 MiB safety limit.")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Could not read settings import: {type(exc).__name__}."
            ) from exc
        if not isinstance(payload, Mapping):
            raise ConfigurationError("Settings import must be a JSON object.")
        return self.plan_import(payload)

    def plan_import(self, payload: Mapping[str, Any]) -> SettingsImportPlan:
        schema = payload.get("schema_version", 0)
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise ConfigurationError("Settings import schema_version must be a positive integer.")
        if schema > SETTINGS_EXPORT_SCHEMA_VERSION:
            raise ConfigurationError(
                "This settings export was written by a newer GUC Mail version. Upgrade before "
                "importing it."
            )
        application = payload.get("application")
        if application is not None and application != "gucmail":
            raise ConfigurationError("This settings export belongs to a different application.")
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ConfigurationError("Settings import must contain a settings object.")
        unsupported: list[str] = []
        excluded_secrets: list[str] = []
        excluded_private: list[str] = []
        blocked: list[str] = []
        changes: list[SettingsDiff] = []
        seen: set[str] = set()
        document = self.config_store.load_document()
        for setting_id, raw_value in raw_settings.items():
            if not isinstance(setting_id, str):
                unsupported.append(str(setting_id))
                continue
            try:
                spec = self.registry.get(setting_id)
            except ConfigurationError:
                if _looks_like_secret_setting_id(setting_id):
                    excluded_secrets.append(setting_id)
                else:
                    unsupported.append(setting_id)
                continue
            if spec.setting_id in seen:
                raise ConfigurationError(
                    f"Settings import contains {spec.setting_id} more than once through aliases."
                )
            seen.add(spec.setting_id)
            if spec.sensitivity is Sensitivity.SECRET:
                excluded_secrets.append(spec.setting_id)
                continue
            if not spec.exportable:
                excluded_private.append(spec.setting_id)
                continue
            try:
                self._ensure_editable(spec, document)
            except ConfigurationError:
                blocked.append(spec.setting_id)
                continue
            parsed = spec.parse(raw_value)
            before = self._value_from_document(spec, document).configured_value
            if before != parsed:
                changes.append(
                    SettingsDiff(
                        setting_id=spec.setting_id,
                        before=before,
                        after=parsed,
                        reset=False,
                        external_effect=spec.external_effect,
                    )
                )
        staged_changes = {
            change.setting_id: _Change(
                self.registry.get(change.setting_id), change.after, change.reset
            )
            for change in changes
        }
        validated_changes = self._preview(document, staged_changes)
        return SettingsImportPlan(
            changes=validated_changes,
            unsupported=tuple(sorted(set(unsupported))),
            excluded_secrets=tuple(sorted(set(excluded_secrets))),
            source_schema_version=schema,
            blocked=tuple(sorted(set(blocked))),
            excluded_private=tuple(sorted(set(excluded_private))),
            expected_revision=document.revision,
            guard_revision=True,
        )

    def apply_import(
        self,
        plan: SettingsImportPlan,
        *,
        confirm_external: bool = False,
    ) -> SettingsRevision | None:
        session = self.begin()
        if plan.guard_revision and session.base_revision != plan.expected_revision:
            session.cancel()
            raise ConfigurationError(
                "Settings changed after this import preview. Generate a fresh preview before "
                "applying it."
            )
        for change in plan.changes:
            session.set(change.setting_id, change.after)
        return session.apply(reason="Settings imported", confirm_external=confirm_external)

    def undo(
        self,
        revision_id: str | None = None,
        *,
        confirm_external: bool = False,
    ) -> SettingsRevision | None:
        if revision_id is None:
            revisions = self.revisions.list()
            revision = revisions[0] if revisions else None
        else:
            revision = self.revisions.get(revision_id)
        if revision is None:
            raise ConfigurationError("No settings revision is available to restore.")
        session = self.begin()
        for setting_id, value in revision.before.items():
            spec = self.registry.get(setting_id)
            if spec.sensitivity is Sensitivity.SECRET:
                continue
            try:
                if spec.setting_id in revision.before_unset:
                    session.reset(setting_id)
                else:
                    session.set(setting_id, value)
            except ConfigurationError as exc:
                if "overridden by" not in str(exc):
                    raise
        return session.apply(
            reason=f"Restored settings revision {revision.revision_id}",
            confirm_external=confirm_external,
        )

    def _ensure_editable(self, spec: SettingSpec, document: ConfigDocument) -> None:
        policy = self.policy_locks.get(spec.setting_id)
        if policy is not None:
            raise ConfigurationError(f"{spec.label} is locked by policy: {policy.reason}")
        if spec.setting_id in self.command_line_overrides:
            raise ConfigurationError(
                f"{spec.label} is overridden for this invocation by a command-line option."
            )
        if spec.attr_name and document.value_sources.get(spec.attr_name) == "environment":
            raise ConfigurationError(
                f"{spec.label} is overridden by {spec.environment_variable}; "
                "remove that environment override before editing it."
            )
        if not spec.attr_name and spec.environment_variable in os.environ:
            raise ConfigurationError(
                f"{spec.label} is overridden by {spec.environment_variable}; "
                "remove that environment override before editing it."
            )

    def _value_from_document(
        self, spec: SettingSpec, document: ConfigDocument, *, recover_invalid_keyboard: bool = False
    ) -> EffectiveSetting:
        policy = self.policy_locks.get(spec.setting_id)
        dependency_ok, dependency_reason = self._dependency_status(spec, document)

        if spec.sensitivity is Sensitivity.SECRET:
            source = ValueSource.DEFAULT
            configured = False
            if spec.environment_variable in os.environ:
                source = ValueSource.ENVIRONMENT
                configured = True
            if spec.setting_id in self.command_line_overrides:
                source = ValueSource.COMMAND_LINE
                configured = True
            if policy is not None and policy.enforce_value:
                source = ValueSource.POLICY
                configured = bool(policy.enforced_value)
            return EffectiveSetting(
                spec=spec,
                configured_value=None,
                effective_value=None,
                source=source,
                overridden=configured or policy is not None,
                environment_variable=(
                    spec.environment_variable if spec.environment_variable in os.environ else None
                ),
                policy_locked=policy is not None,
                policy_reason=policy.reason if policy is not None else None,
                dependencies_satisfied=dependency_ok,
                dependency_reason=dependency_reason,
                secret_configured=configured,
            )

        if spec.attr_name:
            configured = getattr(document.stored_config, spec.attr_name)
            source = ValueSource.CONFIGURATION
            storage_key = self._storage_key(spec)
            if self._scoped_app_config_enabled(spec) and storage_key in document.settings:
                configured = spec.parse(document.settings[storage_key])
                source = _source_for_scope(spec.scope)
            effective = configured
            source_text = document.value_sources.get(spec.attr_name, "configuration")
            if source_text == "environment":
                effective = getattr(document.effective_config, spec.attr_name)
                source = ValueSource.ENVIRONMENT
        else:
            storage_key = self._storage_key(spec)
            fallback_key = spec.setting_id
            raw: Any
            source = ValueSource.DEFAULT
            if storage_key in document.settings:
                raw = document.settings[storage_key]
                source = _source_for_scope(spec.scope)
            elif fallback_key in document.settings:
                raw = document.settings[fallback_key]
                source = ValueSource.CONFIGURATION
            else:
                raw = spec.default
            try:
                configured = spec.parse(raw)
            except ConfigurationError:
                if spec.setting_id != KEYBOARD_SETTING or not recover_invalid_keyboard:
                    raise
                # Preserve the exact invalid value for an explicit repair diff. Only
                # the guided recovery reader may request this; normal reads stay strict.
                configured = copy.deepcopy(raw)
                effective = {}
            else:
                effective = configured
            if spec.environment_variable in os.environ:
                effective = spec.parse(os.environ[spec.environment_variable])
                source = ValueSource.ENVIRONMENT

        environment_variable = (
            spec.environment_variable if source is ValueSource.ENVIRONMENT else None
        )
        if spec.setting_id in self.command_line_overrides:
            effective = self.command_line_overrides[spec.setting_id]
            source = ValueSource.COMMAND_LINE
            environment_variable = None
        if policy is not None and policy.enforce_value:
            effective = spec.parse(policy.enforced_value)
            source = ValueSource.POLICY
            environment_variable = None
        return EffectiveSetting(
            spec=spec,
            configured_value=configured,
            effective_value=effective,
            source=source,
            overridden=source
            in {
                ValueSource.ENVIRONMENT,
                ValueSource.COMMAND_LINE,
                ValueSource.POLICY,
            },
            environment_variable=environment_variable,
            policy_locked=policy is not None,
            policy_reason=policy.reason if policy is not None else None,
            dependencies_satisfied=dependency_ok,
            dependency_reason=dependency_reason,
        )

    def _dependency_status(
        self, spec: SettingSpec, document: ConfigDocument
    ) -> tuple[bool, str | None]:
        for dependency in spec.dependencies:
            dependency_spec = self.registry.get(dependency.setting_id)
            # Dependency specs must not themselves form cycles; the registry checks this.
            value = self._base_effective_value(dependency_spec, document)
            if value != dependency.required_value:
                return (
                    False,
                    dependency.explanation
                    or f"Requires {dependency_spec.label} = {dependency.required_value!r}.",
                )
        return True, None

    def _base_effective_value(self, spec: SettingSpec, document: ConfigDocument) -> Any:
        """Resolve precedence without recursively evaluating dependencies."""

        if spec.sensitivity is Sensitivity.SECRET:
            return None
        if spec.attr_name:
            value = getattr(document.stored_config, spec.attr_name)
            storage_key = self._storage_key(spec)
            if self._scoped_app_config_enabled(spec) and storage_key in document.settings:
                value = spec.parse(document.settings[storage_key])
            if document.value_sources.get(spec.attr_name) == "environment":
                value = getattr(document.effective_config, spec.attr_name)
        else:
            storage_key = self._storage_key(spec)
            raw = document.settings.get(
                storage_key,
                document.settings.get(spec.setting_id, spec.default),
            )
            value = spec.parse(raw)
            if spec.environment_variable in os.environ:
                value = spec.parse(os.environ[spec.environment_variable])
        if spec.setting_id in self.command_line_overrides:
            value = self.command_line_overrides[spec.setting_id]
        policy = self.policy_locks.get(spec.setting_id)
        if policy is not None and policy.enforce_value:
            value = spec.parse(policy.enforced_value)
        return value

    def _scoped_app_config_enabled(self, spec: SettingSpec) -> bool:
        if not spec.attr_name or spec.scope is SettingScope.GLOBAL:
            return False
        # These two fields identify the primary account used to locate all other
        # account-scoped values. Storing them beneath their own hash would make a
        # changed identity unreachable on the next process start.
        if spec.attr_name in {"email", "username"}:
            return False
        if self.context.scoped_app_config:
            return True
        if spec.scope is SettingScope.ACCOUNT:
            return self.context.account_id is not None
        if spec.scope is SettingScope.MACHINE:
            return self.context.machine_id is not None
        return self.context.profile != "default"

    def _storage_key(self, spec: SettingSpec) -> str:
        if spec.scope is SettingScope.GLOBAL:
            return spec.setting_id
        if spec.scope is SettingScope.ACCOUNT and self.context.account_id:
            return f"account.{_scope_hash(self.context.account_id)}.{spec.setting_id}"
        if spec.scope is SettingScope.MACHINE and self.context.machine_id:
            return f"machine.{_scope_hash(self.context.machine_id)}.{spec.setting_id}"
        if spec.scope is SettingScope.PROFILE:
            return f"profile.{_scope_slug(self.context.profile)}.{spec.setting_id}"
        return spec.setting_id

    def _preview(
        self, document: ConfigDocument, changes: Mapping[str, _Change]
    ) -> tuple[SettingsDiff, ...]:
        differences: list[SettingsDiff] = []
        for change in changes.values():
            before = self._value_from_document(
                change.spec, document, recover_invalid_keyboard=True
            ).configured_value
            after = self._reset_value(change.spec, document) if change.reset else change.value
            explicit = self._is_explicitly_configured(change.spec, document)
            # An explicit default can override a profile or inherited value. Reset
            # must also remove that override even when its displayed value is equal.
            storage_change = (
                not change.spec.attr_name or self._scoped_app_config_enabled(change.spec)
            ) and (explicit if change.reset else not explicit)
            if before != after or storage_change:
                differences.append(
                    SettingsDiff(
                        setting_id=change.spec.setting_id,
                        before=before,
                        after=after,
                        reset=change.reset,
                        external_effect=change.spec.external_effect,
                    )
                )
        differences.sort(key=lambda difference: difference.setting_id)
        self._validate_preview(document, changes, differences)
        return tuple(differences)

    def _reset_value(self, spec: SettingSpec, document: ConfigDocument) -> Any:
        """Return the value revealed by removing this context's explicit override."""

        storage_key = self._storage_key(spec)
        if spec.attr_name:
            if self._scoped_app_config_enabled(spec):
                return getattr(document.stored_config, spec.attr_name)
            return spec.default
        if storage_key != spec.setting_id and spec.setting_id in document.settings:
            return spec.parse(document.settings[spec.setting_id])
        return spec.default

    def _is_explicitly_configured(self, spec: SettingSpec, document: ConfigDocument) -> bool:
        if spec.attr_name and not self._scoped_app_config_enabled(spec):
            return True
        return self._storage_key(spec) in document.settings

    def _candidate_document(
        self,
        document: ConfigDocument,
        changes: Mapping[str, _Change],
    ) -> ConfigDocument:
        config = document.stored_config
        stored_settings = dict(document.settings)
        for change in changes.values():
            spec = change.spec
            value = self._reset_value(spec, document) if change.reset else change.value
            if spec.attr_name and not self._scoped_app_config_enabled(spec):
                config = replace(config, **{spec.attr_name: value})
                continue
            storage_key = self._storage_key(spec)
            if change.reset:
                stored_settings.pop(storage_key, None)
            else:
                stored_settings[storage_key] = value
        return ConfigDocument(
            schema_version=document.schema_version,
            stored_config=config,
            effective_config=config,
            settings=stored_settings,
            value_sources={},
            revision=document.revision,
        )

    def _validate_preview(
        self,
        document: ConfigDocument,
        changes: Mapping[str, _Change],
        differences: Sequence[SettingsDiff],
    ) -> None:
        if not differences:
            return
        candidate = self._candidate_document(document, changes)
        replacements = {
            spec.attr_name: self._value_from_document(spec, candidate).configured_value
            for spec in self.registry
            if spec.attr_name and spec.sensitivity is not Sensitivity.SECRET
        }
        replace(candidate.stored_config, **replacements).validate()
        changed_ids = {difference.setting_id for difference in differences}
        if changed_ids.intersection(TASK_REPEAT_SETTINGS):
            for value_kind in ("configured_value", "effective_value"):
                repeat_values = [
                    getattr(
                        self._value_from_document(self.registry.get(identifier), candidate),
                        value_kind,
                    )
                    for identifier in TASK_REPEAT_SETTINGS
                ]
                TaskRepeatPreferences(*repeat_values)
        if changed_ids.intersection(WORKING_TIME_SETTINGS):
            for value_kind in ("configured_value", "effective_value"):
                working_values = [
                    getattr(
                        self._value_from_document(self.registry.get(identifier), candidate),
                        value_kind,
                    )
                    for identifier in WORKING_TIME_SETTINGS
                ]
                CalendarWorkingTime(*working_values)
        if changed_ids & (SHORTCUT_DEFAULTS.keys() | {KEYBOARD_SETTING}):
            for value_kind in ("configured_value", "effective_value"):
                global_values = {
                    identifier: getattr(
                        self._value_from_document(self.registry.get(identifier), candidate),
                        value_kind,
                    )
                    for identifier in SHORTCUT_DEFAULTS
                }
                validate_shortcuts(global_values)
                validate_binding_configuration(
                    getattr(
                        self._value_from_document(self.registry.get(KEYBOARD_SETTING), candidate),
                        value_kind,
                    ),
                    global_values,
                )
        if "appearance.theme" in changed_ids:
            theme_id = str(
                self._value_from_document(
                    self.registry.get("appearance.theme"), candidate
                ).configured_value
            )
            normalized_theme_id = theme_id.strip().casefold()
            if normalized_theme_id.startswith("builtin."):
                from gucmail.tui.themes import ThemeRegistry, ThemeValidationError

                try:
                    ThemeRegistry().get(normalized_theme_id)
                except ThemeValidationError as exc:
                    raise ConfigurationError(f"Unknown theme: {theme_id}") from exc
            elif not re.fullmatch(r"custom\.[a-z0-9][a-z0-9._-]{0,95}", normalized_theme_id):
                raise ConfigurationError(f"Unknown theme: {theme_id}")
        if "appearance.branding" in changed_ids:
            branding_id = str(
                self._value_from_document(
                    self.registry.get("appearance.branding"), candidate
                ).configured_value
            )
            from gucmail.tui.branding import BrandingCatalog, BrandingValidationError

            if branding_id not in {"custom", "none"}:
                try:
                    BrandingCatalog().get(branding_id)
                except BrandingValidationError as exc:
                    raise ConfigurationError(
                        f"Unknown ASCII branding preset: {branding_id}"
                    ) from exc
        try:
            issue_warning_spec = self.registry.get("storage.issue_warning_mb")
            prohibit_send_spec = self.registry.get("storage.prohibit_send_mb")
            prohibit_send_receive_spec = self.registry.get("storage.prohibit_send_receive_mb")
        except ConfigurationError:
            return
        issue_warning = self._value_from_document(issue_warning_spec, candidate).configured_value
        prohibit_send = self._value_from_document(prohibit_send_spec, candidate).configured_value
        prohibit_send_receive = self._value_from_document(
            prohibit_send_receive_spec, candidate
        ).configured_value
        if (
            issue_warning is not None
            and prohibit_send is not None
            and issue_warning > prohibit_send
        ):
            raise ConfigurationError(
                "Issue Warning threshold must not exceed Prohibit Send threshold."
            )
        if (
            prohibit_send is not None
            and prohibit_send_receive is not None
            and prohibit_send_receive < prohibit_send
        ):
            raise ConfigurationError(
                "Prohibit Send & Receive threshold must be at least Prohibit Send threshold."
            )

    def _apply(
        self,
        document: ConfigDocument,
        changes: Mapping[str, _Change],
        *,
        reason: str,
        confirm_external: bool,
    ) -> SettingsRevision | None:
        differences = self._preview(document, changes)
        if not differences:
            return None
        external = [change.setting_id for change in differences if change.external_effect]
        if external and not confirm_external:
            raise ConfigurationError(
                "These settings affect external systems and require explicit confirmation: "
                + ", ".join(external)
            )
        config = document.stored_config
        stored_settings = dict(document.settings)
        before_values: dict[str, Any] = {}
        after_values: dict[str, Any] = {}
        before_unset: list[str] = []
        for difference in differences:
            change = changes[difference.setting_id]
            spec = change.spec
            before_values[spec.setting_id] = difference.before
            after_values[spec.setting_id] = difference.after
            if not self._is_explicitly_configured(spec, document):
                before_unset.append(spec.setting_id)
            if spec.attr_name and not self._scoped_app_config_enabled(spec):
                config = replace(config, **{spec.attr_name: difference.after})
                continue
            storage_key = self._storage_key(spec)
            if change.reset:
                stored_settings.pop(storage_key, None)
            else:
                stored_settings[storage_key] = difference.after
        config.validate()
        self.config_store.save_document(
            config,
            stored_settings,
            expected_revision=document.revision,
        )
        revision = SettingsRevision(
            revision_id=uuid4().hex,
            created_at=datetime.now(UTC).isoformat(),
            reason=reason,
            changes=differences,
            before=before_values,
            after=after_values,
            before_unset=tuple(sorted(before_unset)),
        )
        self.revisions.record(revision)
        return revision


def _scope_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _scope_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-")
    return normalized or "default"


def _source_for_scope(scope: SettingScope) -> ValueSource:
    return {
        SettingScope.GLOBAL: ValueSource.CONFIGURATION,
        SettingScope.ACCOUNT: ValueSource.ACCOUNT,
        SettingScope.MACHINE: ValueSource.MACHINE,
        SettingScope.PROFILE: ValueSource.PROFILE,
    }[scope]


def _normalize_search(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).split())


def _natural_settings_query(value: str) -> str:
    """Reduce common Help-style questions to useful local search terms."""

    normalized = _normalize_search(value)
    replacements = {
        "dark mode": "theme",
        "light mode": "theme",
        "mail check frequency": "poll interval",
        "check mail frequency": "poll interval",
        "where downloads go": "download directory",
        "start with windows": "windows startup",
        "run when windows starts": "windows startup",
        "command launcher": "command palette",
    }
    for phrase, replacement in replacements.items():
        normalized = normalized.replace(phrase, replacement)
    stop_words = {
        "a",
        "an",
        "are",
        "can",
        "change",
        "configure",
        "do",
        "does",
        "how",
        "i",
        "is",
        "make",
        "me",
        "my",
        "off",
        "on",
        "please",
        "set",
        "setting",
        "settings",
        "the",
        "to",
        "turn",
        "where",
    }
    useful = tuple(token for token in normalized.split() if token not in stop_words)
    return " ".join(useful) or normalized


def _looks_like_secret_setting_id(setting_id: str) -> bool:
    normalized = _normalize_search(setting_id)
    tokens = set(normalized.split())
    intrinsically_secret = {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "hmac",
        "password",
        "secret",
        "token",
    }
    return (
        bool(tokens & intrinsically_secret)
        or ("webhook" in tokens and bool(tokens & {"destination", "url"}))
        or ({"signing", "key"} <= tokens)
    ) and not normalized.endswith(" secret reference")


def _validate_structured_value(value: Any, *, setting_id: str) -> None:
    """Validate a bounded JSON-compatible preference without accepting objects or secrets."""

    nodes = 0

    def visit(candidate: Any, *, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 10_000:
            raise ValueError(f"{setting_id} contains too many structured values")
        if depth > 12:
            raise ValueError(f"{setting_id} is nested too deeply")
        if candidate is None or isinstance(candidate, (bool, int, float)):
            return
        if isinstance(candidate, str):
            if len(candidate) > 16_384:
                raise ValueError(f"{setting_id} contains an oversized text value")
            return
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise ValueError(f"{setting_id} contains an invalid object key")
                if _looks_like_secret_setting_id(key) and not (
                    isinstance(nested, str) and nested.startswith("keyring://")
                ):
                    raise ValueError(
                        f"{setting_id} must store a keyring reference, not a plaintext secret"
                    )
                visit(nested, depth=depth + 1)
            return
        if isinstance(candidate, (list, tuple)):
            for nested in candidate:
                visit(nested, depth=depth + 1)
            return
        raise TypeError(f"{setting_id} contains a value that is not JSON-compatible")

    visit(value, depth=0)
    try:
        _reject_plaintext_secrets({"settings": {setting_id: value}})
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def _validate_setting_spec_metadata(spec: SettingSpec) -> None:
    """Fail fast when registry metadata cannot safely drive parsing and presentation."""

    if not re.fullmatch(r"[a-z][a-z0-9_.-]*", spec.setting_id):
        raise ValueError(f"Invalid stable setting ID: {spec.setting_id!r}")
    for field_name, value in (
        ("label", spec.label),
        ("description", spec.description),
        ("category", spec.category),
        ("help target", spec.help_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Setting {spec.setting_id} has no {field_name}.")
    for metadata_name, values in (("alias", spec.aliases), ("keyword", spec.keywords)):
        normalized: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not _normalize_search(value):
                raise ValueError(f"Setting {spec.setting_id} has an invalid {metadata_name}.")
            candidate = _normalize_search(value)
            if candidate in normalized:
                raise ValueError(f"Setting {spec.setting_id} repeats {metadata_name} {value!r}.")
            normalized.add(candidate)
    for enum_name, value, enum_type in (
        ("type", spec.value_type, SettingType),
        ("scope", spec.scope, SettingScope),
        ("apply semantics", spec.apply_semantics, ApplySemantics),
        ("sensitivity", spec.sensitivity, Sensitivity),
        ("level", spec.level, SettingLevel),
    ):
        if not isinstance(value, enum_type):
            raise ValueError(f"Setting {spec.setting_id} has invalid {enum_name} metadata.")
    numeric_types = {
        SettingType.INTEGER,
        SettingType.NUMBER,
        SettingType.OPTIONAL_NUMBER,
    }
    if (
        spec.minimum is not None or spec.maximum is not None
    ) and spec.value_type not in numeric_types:
        raise ValueError(f"Setting {spec.setting_id} has a range but is not numeric.")
    if spec.minimum is not None and not math.isfinite(float(spec.minimum)):
        raise ValueError(f"Setting {spec.setting_id} has a non-finite minimum.")
    if spec.maximum is not None and not math.isfinite(float(spec.maximum)):
        raise ValueError(f"Setting {spec.setting_id} has a non-finite maximum.")
    if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
        raise ValueError(f"Setting {spec.setting_id} has an inverted valid range.")
    if spec.value_type is SettingType.CHOICE:
        normalized_choices = [choice.casefold() for choice in spec.choices]
        if not spec.choices or any(not choice.strip() for choice in spec.choices):
            raise ValueError(f"Choice setting {spec.setting_id} has no valid options.")
        if len(set(normalized_choices)) != len(normalized_choices):
            raise ValueError(f"Choice setting {spec.setting_id} repeats an option.")
    elif spec.choices:
        raise ValueError(f"Non-choice setting {spec.setting_id} declares choice options.")
    if spec.attr_name is not None:
        app_fields = {item.name for item in fields(AppConfig)}
        if spec.attr_name not in app_fields:
            raise ValueError(
                f"Setting {spec.setting_id} refers to unknown AppConfig.{spec.attr_name}."
            )
        if spec.sensitivity is Sensitivity.SECRET:
            raise ValueError(
                f"Secret setting {spec.setting_id} cannot use ordinary AppConfig storage."
            )
    try:
        spec.parse(spec.default)
    except ConfigurationError as exc:
        raise ValueError(f"Setting {spec.setting_id} has an invalid default: {exc}") from exc


def _validate_setting_dependencies(
    specs: Sequence[SettingSpec], by_alias: Mapping[str, SettingSpec]
) -> None:
    graph: dict[str, set[str]] = {spec.setting_id: set() for spec in specs}
    for spec in specs:
        for dependency in spec.dependencies:
            if not isinstance(dependency, SettingDependency):
                raise ValueError(f"Setting {spec.setting_id} has invalid dependency metadata.")
            target = by_alias.get(_normalize_search(dependency.setting_id))
            if target is None:
                raise ValueError(
                    f"Setting {spec.setting_id} depends on unknown setting {dependency.setting_id}."
                )
            if target.sensitivity is Sensitivity.SECRET:
                raise ValueError(f"Setting {spec.setting_id} may not depend on a secret value.")
            try:
                target.parse(dependency.required_value)
            except ConfigurationError as exc:
                raise ValueError(
                    f"Setting {spec.setting_id} has an invalid required value for "
                    f"{target.setting_id}."
                ) from exc
            graph[spec.setting_id].add(target.setting_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(setting_id: str) -> None:
        if setting_id in visiting:
            raise ValueError(f"Settings dependency cycle includes {setting_id}.")
        if setting_id in visited:
            return
        visiting.add(setting_id)
        for dependency_id in graph[setting_id]:
            visit(dependency_id)
        visiting.remove(setting_id)
        visited.add(setting_id)

    for setting_id in graph:
        visit(setting_id)


def _level_rank(level: SettingLevel) -> int:
    return {
        SettingLevel.BASIC: 0,
        SettingLevel.ADVANCED: 1,
        SettingLevel.EXPERT: 2,
    }[level]


def _friendly_number(value: float | int) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _revision_to_dict(revision: SettingsRevision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "created_at": revision.created_at,
        "reason": revision.reason,
        "changes": [asdict(change) for change in revision.changes],
        "before": dict(revision.before),
        "after": dict(revision.after),
        "before_unset": list(revision.before_unset),
    }


def _revision_from_dict(payload: Any) -> SettingsRevision:
    if not isinstance(payload, dict):
        raise ConfigurationError("Settings revision must be an object.")
    _reject_plaintext_secrets(payload)
    try:
        revision_id = payload["revision_id"]
        created_at = payload["created_at"]
        reason = payload["reason"]
        raw_changes = payload["changes"]
        before = payload["before"]
        after = payload["after"]
        before_unset = payload.get("before_unset", [])
    except KeyError as exc:
        raise ConfigurationError("Settings revision is incomplete.") from exc
    if not isinstance(revision_id, str) or not re.fullmatch(r"[0-9a-f]{32}", revision_id):
        raise ConfigurationError("Settings revision ID is invalid.")
    if not isinstance(created_at, str):
        raise ConfigurationError("Settings revision timestamp is invalid.")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ConfigurationError("Settings revision timestamp is invalid.") from exc
    if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
        raise ConfigurationError("Settings revision timestamp must include a UTC offset.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1_024:
        raise ConfigurationError("Settings revision reason is invalid.")
    if (
        not isinstance(raw_changes, list)
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(before_unset, list)
        or not all(isinstance(setting_id, str) for setting_id in before_unset)
    ):
        raise ConfigurationError("Settings revision data is malformed.")
    if len(set(before_unset)) != len(before_unset) or not set(before_unset) <= set(before):
        raise ConfigurationError("Settings revision unset-state metadata is malformed.")
    changes: list[SettingsDiff] = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ConfigurationError("Settings revision change is malformed.")
        setting_id = raw.get("setting_id")
        reset = raw.get("reset", False)
        external_effect = raw.get("external_effect", False)
        if not isinstance(setting_id, str) or not setting_id:
            raise ConfigurationError("Settings revision change has an invalid setting ID.")
        if not isinstance(reset, bool) or not isinstance(external_effect, bool):
            raise ConfigurationError("Settings revision change flags are malformed.")
        changes.append(
            SettingsDiff(
                setting_id=setting_id,
                before=raw.get("before"),
                after=raw.get("after"),
                reset=reset,
                external_effect=external_effect,
            )
        )
    for snapshot_name, snapshot in (("before", before), ("after", after)):
        if not all(isinstance(key, str) and key for key in snapshot):
            raise ConfigurationError(f"Settings revision {snapshot_name} keys are malformed.")
        try:
            _validate_structured_value(snapshot, setting_id=f"revision.{snapshot_name}")
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"Settings revision {snapshot_name} values are malformed."
            ) from exc
    return SettingsRevision(
        revision_id=revision_id,
        created_at=created_at,
        reason=reason,
        changes=tuple(changes),
        before=dict(before),
        after=dict(after),
        before_unset=tuple(before_unset),
    )


def _atomic_json_write(target: Path, payload: Mapping[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _spec(
    setting_id: str,
    label: str,
    description: str,
    category: str,
    value_type: SettingType,
    default: Any,
    aliases: Sequence[str],
    *,
    scope: SettingScope = SettingScope.GLOBAL,
    apply: ApplySemantics = ApplySemantics.IMMEDIATE,
    help_id: str | None = None,
    attr: str | None = None,
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
    exportable: bool = True,
    level: SettingLevel = SettingLevel.BASIC,
    choices: Sequence[str] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    external_effect: bool = False,
    keywords: Sequence[str] = (),
    dependencies: Sequence[SettingDependency] = (),
) -> SettingSpec:
    return SettingSpec(
        setting_id=setting_id,
        label=label,
        description=description,
        category=category,
        value_type=value_type,
        default=default,
        aliases=tuple(aliases),
        scope=scope,
        apply_semantics=apply,
        help_id=help_id or _default_help_id(category, setting_id),
        attr_name=attr,
        sensitivity=sensitivity,
        exportable=exportable,
        level=level,
        choices=tuple(choices),
        minimum=minimum,
        maximum=maximum,
        external_effect=external_effect,
        keywords=tuple(keywords),
        dependencies=tuple(dependencies),
    )


def _default_help_id(category: str, setting_id: str) -> str:
    if setting_id == "appearance.theme" or "theme" in setting_id:
        return "appearance.themes"
    if "branding" in setting_id or "banner" in setting_id:
        return "appearance.branding"
    return {
        "Connection": "troubleshooting.connection",
        "Mail": "mail.reading",
        "Notifications & Monitoring": "monitoring.overview",
        "Appearance": "appearance.themes",
        "Accessibility": "appearance.themes",
        "Calendar": "calendar.overview",
        "Composer & Drafts": "mail.compose",
        "Privacy & Security": "privacy.local-data",
        "Storage": "storage.quota",
        "Search & Indexing": "search.overview",
        "Integrations": "integrations.webhooks",
        "Advanced": "advanced.ews",
        "Help": "settings.overview",
        "Layout": "appearance.layout",
        "Contacts": "contacts.overview",
        "Tasks": "tasks.overview",
        "Command Palette": "palette.overview",
        "Windows Startup": "startup.windows",
        "Backup": "backup.overview",
        "Keyboard": "reference.shortcuts",
    }.get(category, "settings.overview")


def _app_config_specs() -> tuple[SettingSpec, ...]:
    # Every AppConfig field appears exactly once.  Keep IDs stable even if labels change.
    specs = (
        _spec(
            "connection.email",
            "Account email",
            "Exchange mailbox address.",
            "Connection",
            SettingType.OPTIONAL_STRING,
            None,
            ("mail account address",),
            scope=SettingScope.ACCOUNT,
            attr="email",
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "connection.username",
            "Login username",
            "Username used for Exchange authentication.",
            "Connection",
            SettingType.OPTIONAL_STRING,
            None,
            ("exchange login name",),
            scope=SettingScope.ACCOUNT,
            attr="username",
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "connection.endpoint",
            "EWS endpoint",
            "HTTPS endpoint for the GUC Exchange service.",
            "Connection",
            SettingType.STRING,
            AppConfig().endpoint,
            ("exchange server url",),
            scope=SettingScope.ACCOUNT,
            apply=ApplySemantics.RESTART,
            attr="endpoint",
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "mail.default_folder",
            "Default startup folder",
            "Starting folder when no previous folder is saved. "
            "A remembered folder takes precedence.",
            "Mail",
            SettingType.STRING,
            AppConfig().default_folder,
            ("startup inbox",),
            scope=SettingScope.ACCOUNT,
            attr="default_folder",
        ),
        _spec(
            "mail.page_size",
            "Message page size",
            "Preferred number of message headers per page; the TUI may load more to fill its view.",
            "Mail",
            SettingType.INTEGER,
            AppConfig().page_size,
            ("messages per page",),
            scope=SettingScope.ACCOUNT,
            attr="page_size",
            minimum=1,
            maximum=200,
        ),
        _spec(
            "monitoring.poll_interval_seconds",
            "Polling interval",
            "Minimum time between conservative mailbox polling checks.",
            "Notifications & Monitoring",
            SettingType.INTEGER,
            AppConfig().poll_interval_seconds,
            ("watcher frequency", "check mail interval"),
            scope=SettingScope.PROFILE,
            attr="poll_interval_seconds",
            minimum=30,
            maximum=86400,
        ),
        _spec(
            "appearance.theme",
            "Theme",
            "Built-in or custom visual theme.",
            "Appearance",
            SettingType.STRING,
            AppConfig().theme,
            ("colour scheme",),
            attr="theme",
            keywords=("dark", "light", "color scheme", "mode"),
        ),
        _spec(
            "accessibility.monochrome",
            "Monochrome mode",
            "Avoid color-dependent presentation.",
            "Accessibility",
            SettingType.BOOLEAN,
            AppConfig().no_color,
            ("no color mode",),
            attr="no_color",
        ),
        _spec(
            "mail.datetime_format",
            "Date and time format",
            "Message list and reader date format. Structured timestamps remain ISO 8601.",
            "Mail",
            SettingType.STRING,
            AppConfig().datetime_format,
            ("timestamp format",),
            attr="datetime_format",
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "calendar.timezone",
            "Display time zone",
            "Time zone used for mail, calendar, and schedules.",
            "Calendar",
            SettingType.STRING,
            AppConfig().timezone,
            ("local timezone",),
            scope=SettingScope.ACCOUNT,
            attr="timezone",
        ),
        _spec(
            "composer.external_editor",
            "External editor",
            "Program used when composing outside the TUI.",
            "Composer & Drafts",
            SettingType.OPTIONAL_STRING,
            None,
            ("edit mail program",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            attr="external_editor",
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "mail.download_directory",
            "Attachment download folder",
            "Default local destination for saved attachments.",
            "Mail",
            SettingType.OPTIONAL_STRING,
            None,
            ("where downloads are stored",),
            scope=SettingScope.MACHINE,
            attr="download_directory",
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "composer.signature",
            "Default signature",
            "Text appended to new messages when enabled.",
            "Composer & Drafts",
            SettingType.STRING,
            "",
            ("email sign off",),
            scope=SettingScope.ACCOUNT,
            apply=ApplySemantics.STAGED,
            attr="signature",
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "privacy.remote_content",
            "Remote images",
            "Allow remote email resources; off protects against tracking.",
            "Privacy & Security",
            SettingType.BOOLEAN,
            AppConfig().allow_remote_content,
            ("tracking pixels", "load images in mail"),
            scope=SettingScope.ACCOUNT,
            attr="allow_remote_content",
        ),
        _spec(
            "storage.cache_enabled",
            "Local cache",
            "Store bounded mailbox metadata for responsiveness.",
            "Storage",
            SettingType.BOOLEAN,
            AppConfig().cache_enabled,
            ("mail cache",),
            scope=SettingScope.MACHINE,
            attr="cache_enabled",
        ),
        _spec(
            "privacy.cache_message_bodies",
            "Cache message bodies",
            "Keep private message text in the local cache.",
            "Privacy & Security",
            SettingType.BOOLEAN,
            AppConfig().cache_message_bodies,
            ("offline body storage",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            attr="cache_message_bodies",
            sensitivity=Sensitivity.PRIVATE,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "search.index_attachments",
            "Attachment text indexing",
            "Extract supported attachment text into the local index.",
            "Search & Indexing",
            SettingType.BOOLEAN,
            AppConfig().index_attachments,
            ("search inside attachments",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            attr="index_attachments",
            sensitivity=Sensitivity.PRIVATE,
            level=SettingLevel.ADVANCED,
            keywords=("attachment index", "index attachment"),
        ),
        _spec(
            "search.semantic_enabled",
            "Semantic search",
            "Build an optional local semantic index.",
            "Search & Indexing",
            SettingType.BOOLEAN,
            AppConfig().semantic_search_enabled,
            ("meaning search",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            attr="semantic_search_enabled",
            sensitivity=Sensitivity.PRIVATE,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "monitoring.enabled",
            "Mailbox monitoring",
            "Watch selected folders for new mail.",
            "Notifications & Monitoring",
            SettingType.BOOLEAN,
            AppConfig().monitoring_enabled,
            ("watch mail",),
            scope=SettingScope.ACCOUNT,
            apply=ApplySemantics.STAGED,
            attr="monitoring_enabled",
        ),
        _spec(
            "integrations.command_hooks_enabled",
            "Local command hooks",
            "Permit explicitly configured local notification commands.",
            "Integrations",
            SettingType.BOOLEAN,
            AppConfig().command_hooks_enabled,
            ("notification scripts",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.EXTERNAL,
            attr="command_hooks_enabled",
            level=SettingLevel.EXPERT,
            external_effect=True,
        ),
        _spec(
            "connection.request_timeout_seconds",
            "Request timeout",
            "Maximum duration of an individual Exchange request.",
            "Connection",
            SettingType.INTEGER,
            AppConfig().request_timeout_seconds,
            ("ews timeout",),
            scope=SettingScope.ACCOUNT,
            attr="request_timeout_seconds",
            minimum=1,
            maximum=600,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "connection.retry_attempts",
            "Retry attempts",
            "Conservative retry count for definitely safe failures.",
            "Connection",
            SettingType.INTEGER,
            AppConfig().retry_attempts,
            ("connection retries",),
            scope=SettingScope.ACCOUNT,
            attr="retry_attempts",
            minimum=0,
            maximum=10,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "connection.retry_base_seconds",
            "Initial retry delay",
            "Starting delay for exponential backoff.",
            "Connection",
            SettingType.NUMBER,
            AppConfig().retry_base_seconds,
            ("backoff start",),
            scope=SettingScope.ACCOUNT,
            attr="retry_base_seconds",
            minimum=0.1,
            maximum=600,
            level=SettingLevel.EXPERT,
        ),
        _spec(
            "connection.retry_max_seconds",
            "Maximum retry delay",
            "Upper bound for exponential backoff.",
            "Connection",
            SettingType.NUMBER,
            AppConfig().retry_max_seconds,
            ("backoff ceiling",),
            scope=SettingScope.ACCOUNT,
            attr="retry_max_seconds",
            minimum=1,
            maximum=3600,
            level=SettingLevel.EXPERT,
        ),
        _spec(
            "mail.max_attachment_bytes",
            "Maximum attachment download",
            "Safety limit for a single attachment download.",
            "Mail",
            SettingType.INTEGER,
            AppConfig().max_attachment_bytes,
            ("attachment size limit",),
            scope=SettingScope.MACHINE,
            attr="max_attachment_bytes",
            minimum=1,
        ),
        _spec(
            "composer.autosave_seconds",
            "Draft autosave interval",
            "Seconds between non-blocking local draft recovery saves.",
            "Composer & Drafts",
            SettingType.INTEGER,
            AppConfig().autosave_seconds,
            ("save draft frequency",),
            scope=SettingScope.ACCOUNT,
            attr="autosave_seconds",
            minimum=5,
            maximum=3600,
            keywords=("autosave", "draft timer"),
        ),
        _spec(
            "composer.undo_send_seconds",
            "Undo Send duration",
            "Local delay before a message is transmitted.",
            "Composer & Drafts",
            SettingType.INTEGER,
            AppConfig().undo_send_seconds,
            ("delay sending",),
            scope=SettingScope.ACCOUNT,
            attr="undo_send_seconds",
            minimum=0,
            maximum=600,
        ),
        _spec(
            "privacy.logging_level",
            "Logging level",
            "Amount of sanitized diagnostic detail recorded locally.",
            "Privacy & Security",
            SettingType.CHOICE,
            AppConfig().logging_level,
            ("debug logs",),
            scope=SettingScope.MACHINE,
            attr="logging_level",
            choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "privacy.redact_email_addresses",
            "Redact email addresses",
            "Hide addresses in logs and diagnostic bundles.",
            "Privacy & Security",
            SettingType.BOOLEAN,
            AppConfig().redact_email_addresses,
            ("hide addresses in logs",),
            scope=SettingScope.MACHINE,
            attr="redact_email_addresses",
        ),
        _spec(
            "advanced.mode",
            "Settings complexity",
            "Choose Basic, Advanced, or Expert disclosure.",
            "Advanced",
            SettingType.CHOICE,
            AppConfig().mode,
            ("expert settings",),
            attr="mode",
            choices=("basic", "advanced", "expert"),
        ),
        _spec(
            "help.onboarding_complete",
            "Introductory setup acknowledged",
            "Records the setup acknowledgement; guided lesson progress is saved separately.",
            "Help",
            SettingType.BOOLEAN,
            AppConfig().onboarding_complete,
            ("setup finished",),
            attr="onboarding_complete",
        ),
        _spec(
            "advanced.raw_soap_enabled",
            "Raw SOAP execution (unavailable)",
            "Arbitrary XML execution is unavailable; typed operations have separate controls.",
            "Advanced",
            SettingType.BOOLEAN,
            AppConfig().expert_raw_soap_enabled,
            ("raw ews",),
            scope=SettingScope.ACCOUNT,
            apply=ApplySemantics.STAGED,
            attr="expert_raw_soap_enabled",
            level=SettingLevel.EXPERT,
        ),
    )
    registered = {spec.attr_name for spec in specs}
    expected = {item.name for item in fields(AppConfig)}
    if registered != expected:
        missing = sorted(expected - registered)
        extra = sorted(name for name in registered - expected if name is not None)
        raise RuntimeError(f"AppConfig setting metadata mismatch; missing={missing}, extra={extra}")
    return specs


def _extended_specs() -> tuple[SettingSpec, ...]:
    # UI/state preferences remain extensible and do not inflate the core runtime dataclass.
    return (
        _spec(
            "mail.fill_viewport",
            "Fill the message list",
            "Load enough message headers to fill a tall inbox, up to 200. "
            "Disable for a fixed page size.",
            "Mail",
            SettingType.BOOLEAN,
            True,
            ("fill inbox", "automatic page size"),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "search.attachment_formats",
            "Attachment index file types",
            "Allowed formats for deliberately requested mailbox index builds. Image "
            "formats also require separate OCR consent.",
            "Search & Indexing",
            SettingType.STRING_LIST,
            ("csv", "html", "json", "txt", "xml"),
            ("indexed file extensions",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "search.attachment_max_bytes",
            "Maximum indexed attachment bytes",
            "Skip files above this size before downloading when metadata provides it. "
            "Streaming also enforces the limit.",
            "Search & Indexing",
            SettingType.INTEGER,
            5 * 1024 * 1024,
            ("attachment extraction size",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=10 * 1024 * 1024,
        ),
        _spec(
            "search.attachment_timeout",
            "Attachment processing timeout",
            "Seconds per isolated parser or image recognition request. Exchange uses the "
            "connection request timeout separately.",
            "Search & Indexing",
            SettingType.NUMBER,
            8.0,
            ("attachment extraction deadline",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=15,
        ),
        _spec(
            "search.attachment_files_per_message",
            "Indexed files per message",
            "Bound the selected attachment metadata and downloads in each message. "
            "Additional files receive a limit outcome.",
            "Search & Indexing",
            SettingType.INTEGER,
            5,
            ("per message indexing limit",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=20,
        ),
        _spec(
            "search.attachment_files_per_run",
            "Downloaded files per index run",
            "Maximum attachment downloads across the entire selected mailbox page.",
            "Search & Indexing",
            SettingType.INTEGER,
            25,
            ("index download count",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=100,
        ),
        _spec(
            "search.attachment_total_bytes",
            "Attachment download budget per run",
            "A total byte ceiling shared by all selected files, including size-probe "
            "bytes. It is not a process-memory quota.",
            "Search & Indexing",
            SettingType.INTEGER,
            20 * 1024 * 1024,
            ("index attachment byte budget",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=100 * 1024 * 1024,
        ),
        _spec(
            "search.attachment_output_chars",
            "Indexed attachment text per message",
            "Maximum extracted characters retained for each message; labelled per-file "
            "outcomes record truncation.",
            "Search & Indexing",
            SettingType.INTEGER,
            50_000,
            ("extracted text ceiling",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            minimum=1,
            maximum=100_000,
        ),
        _spec(
            "search.attachment_ocr_enabled",
            "Image OCR in attachment indexes",
            "Explicitly allow local recognition of permitted PNG/JPEG attachments during "
            "a confirmed index build. OCR text may be inaccurate; disabled by default.",
            "Search & Indexing",
            SettingType.BOOLEAN,
            False,
            ("index image text",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            sensitivity=Sensitivity.PRIVATE,
            dependencies=(SettingDependency("search.index_attachments"),),
        ),
        _spec(
            "search.attachment_ocr_language",
            "Attachment index OCR language",
            "One installed recognizer language, such as en-US or ar-SA. Missing engines "
            "or languages produce a labelled outcome without installing anything.",
            "Search & Indexing",
            SettingType.STRING,
            "en-US",
            ("image indexing language",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
            level=SettingLevel.ADVANCED,
            dependencies=(SettingDependency("search.attachment_ocr_enabled"),),
        ),
        _spec(
            KEYBOARD_SETTING,
            "Keyboard action assignments",
            "Remap application action keys with the guided shortcut editor. Navigation and "
            "text-editing controls stay fixed; empty assignments disable one action key.",
            "Keyboard",
            SettingType.STRUCTURED,
            {},
            ("general shortcut editor", "custom keys", "keybindings", "keyboard shortcuts"),
        ),
        _spec(
            "appearance.theme_favorites",
            "Favorite themes",
            "Themes pinned to the top of the browser.",
            "Appearance",
            SettingType.STRING_LIST,
            (),
            ("starred themes",),
        ),
        _spec(
            "appearance.recent_themes",
            "Recently used themes",
            "Bounded theme history.",
            "Appearance",
            SettingType.STRING_LIST,
            (),
            ("theme history list",),
        ),
        _spec(
            "appearance.previous_theme_shortcut",
            "Previous-theme shortcut",
            "Preview the previous theme from anywhere in the mail shell.",
            "Appearance",
            SettingType.STRING,
            "ctrl+alt+left",
            ("previous theme hotkey", "cycle theme backward"),
        ),
        _spec(
            "appearance.next_theme_shortcut",
            "Next-theme shortcut",
            "Preview the next theme from anywhere in the mail shell.",
            "Appearance",
            SettingType.STRING,
            "ctrl+alt+right",
            ("next theme hotkey", "cycle theme forward"),
        ),
        _spec(
            "appearance.branding",
            "ASCII branding",
            "Select the GUC logo or GUC Mail banner preset.",
            "Appearance",
            SettingType.CHOICE,
            "builtin.guc-mail-minimal",
            ("guc logo preset", "ascii banner"),
            choices=(
                "builtin.guc-original-mark",
                "builtin.guc-ribbon",
                "builtin.guc-ribbon-outline",
                "builtin.guc-block",
                "builtin.guc-slant",
                "builtin.guc-circuit",
                "builtin.guc-mail-standard",
                "builtin.guc-mail-shadow",
                "builtin.guc-mail-boxed",
                "builtin.guc-mail-campus",
                "builtin.guc-mail-terminal",
                "builtin.guc-mail-minimal",
                "builtin.guc-tricolor",
                "custom",
                "none",
            ),
        ),
        _spec(
            "appearance.custom_branding",
            "Custom ASCII branding",
            "User-defined local banner text.",
            "Appearance",
            SettingType.OPTIONAL_STRING,
            None,
            ("custom guc art",),
            apply=ApplySemantics.STAGED,
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "appearance.banner_visible",
            "Show ASCII banner",
            "Display branding when terminal space permits.",
            "Appearance",
            SettingType.BOOLEAN,
            True,
            ("hide logo",),
        ),
        _spec(
            "appearance.banner_alignment",
            "Banner alignment",
            "Align the compact banner in the title surface.",
            "Appearance",
            SettingType.CHOICE,
            "left",
            ("logo position",),
            choices=("left", "center", "right"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "appearance.branding_size",
            "ASCII branding size",
            "Choose automatic, full, compact, or minimal branding.",
            "Appearance",
            SettingType.CHOICE,
            "auto",
            ("logo size", "compact banner"),
            choices=("auto", "full", "compact", "minimal"),
        ),
        _spec(
            "appearance.branding_color_behavior",
            "ASCII branding colors",
            "Use theme colors, German-flag colors, or monochrome output.",
            "Appearance",
            SettingType.CHOICE,
            "theme",
            ("logo colors", "german flag colors"),
            choices=("theme", "german_flag", "monochrome"),
        ),
        _spec(
            "appearance.branding_width",
            "ASCII branding width",
            "Optional target width for safely resized branding.",
            "Appearance",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("logo width",),
            minimum=1,
            maximum=200,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "appearance.branding_height",
            "ASCII branding height",
            "Optional target height for safely resized branding.",
            "Appearance",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("logo height",),
            minimum=1,
            maximum=32,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "appearance.density",
            "Interface density",
            "Choose compact, comfortable, or spacious rows.",
            "Appearance",
            SettingType.CHOICE,
            "theme",
            ("row spacing",),
            choices=("theme", "compact", "comfortable", "spacious"),
        ),
        _spec(
            "appearance.border_style",
            "Border style",
            "Visual treatment for pane boundaries.",
            "Appearance",
            SettingType.CHOICE,
            "theme",
            ("panel borders",),
            choices=("theme", "none", "subtle", "solid", "double"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "accessibility.high_contrast",
            "High contrast",
            "Increase contrast for text and focus indicators.",
            "Accessibility",
            SettingType.BOOLEAN,
            False,
            ("strong contrast",),
        ),
        _spec(
            "accessibility.reduced_motion",
            "Reduced motion",
            "Avoid nonessential animation and transitions.",
            "Accessibility",
            SettingType.BOOLEAN,
            False,
            ("disable animations",),
        ),
        _spec(
            "accessibility.unicode_fallback",
            "ASCII fallback",
            "Use ASCII glyphs when Unicode rendering is unreliable.",
            "Accessibility",
            SettingType.BOOLEAN,
            False,
            ("plain characters",),
        ),
        _spec(
            "layout.sidebar_visible",
            "Show folder sidebar",
            "Show or collapse account and folder navigation.",
            "Layout",
            SettingType.BOOLEAN,
            True,
            ("hide folders",),
        ),
        _spec(
            "layout.sidebar_ratio",
            "Folder sidebar width",
            "Adaptive proportion assigned to the folder pane.",
            "Layout",
            SettingType.NUMBER,
            0.22,
            ("make sidebar wider",),
            apply=ApplySemantics.STAGED,
            minimum=0.12,
            maximum=0.45,
        ),
        _spec(
            "layout.message_list_ratio",
            "Message list width",
            "Adaptive proportion assigned to the message list.",
            "Layout",
            SettingType.NUMBER,
            0.36,
            ("make mail list wider",),
            apply=ApplySemantics.STAGED,
            minimum=0.20,
            maximum=0.70,
        ),
        _spec(
            "layout.preview_ratio",
            "Preview width",
            "Adaptive proportion assigned to the reading pane.",
            "Layout",
            SettingType.NUMBER,
            0.42,
            ("make the preview wider", "reading pane width"),
            apply=ApplySemantics.STAGED,
            minimum=0.20,
            maximum=0.75,
            keywords=("preview width", "reading pane wider"),
        ),
        _spec(
            "layout.split_orientation",
            "Preview split",
            "Place preview beside or below the message list.",
            "Layout",
            SettingType.CHOICE,
            "vertical",
            ("horizontal preview",),
            apply=ApplySemantics.STAGED,
            choices=("vertical", "horizontal"),
        ),
        _spec(
            "layout.preview_visible",
            "Show preview pane",
            "Show the selected message alongside the list.",
            "Layout",
            SettingType.BOOLEAN,
            True,
            ("list only mode",),
        ),
        _spec(
            "layout.reader_mode",
            "Open-message behavior",
            "Choose preview, full screen, floating, external, or ask.",
            "Layout",
            SettingType.CHOICE,
            "preview",
            ("detached reader", "floating email"),
            choices=("preview", "fullscreen", "floating", "external", "ask"),
        ),
        _spec(
            "layout.status_bar_visible",
            "Show status bar",
            "Show connection, monitoring, and activity status.",
            "Layout",
            SettingType.BOOLEAN,
            True,
            ("hide footer",),
        ),
        _spec(
            "layout.message_columns",
            "Message columns",
            "Choose column order, visibility, compact visibility and automatic or fixed widths.",
            "Layout",
            SettingType.STRUCTURED,
            columns_value(),
            ("message list columns", "sender width", "table fields"),
        ),
        _spec(
            "layout.header_contents",
            "Header contents",
            "Ordered fields: account, storage, monitoring, folder. Empty hides status text.",
            "Layout",
            SettingType.STRING_LIST,
            HEADER_DEFAULT,
            ("header fields", "quota indicator", "monitoring indicator"),
        ),
        _spec(
            "layout.header_compactness",
            "Header compactness",
            "Keep a one-row header: adaptive drops fields to fit, "
            "compact shortens storage, detailed uses ellipsis.",
            "Layout",
            SettingType.CHOICE,
            "adaptive",
            ("compact header",),
            choices=("adaptive", "compact", "detailed"),
        ),
        _spec(
            "layout.status_bar_contents",
            "Status-bar contents",
            "Ordered fields: activity first, then optional connection, folder, monitoring.",
            "Layout",
            SettingType.STRING_LIST,
            STATUS_DEFAULT,
            ("footer fields", "activity status"),
        ),
        _spec(
            "layout.last_preset",
            "Last stable layout",
            "Layout preset restored after restart.",
            "Layout",
            SettingType.STRING,
            "default",
            ("saved layout",),
        ),
        _spec(
            "layout.presets",
            "Saved layout presets",
            "Up to 32 named layouts. Use Save layout preset or Load layout preset "
            "in the command palette.",
            "Layout",
            SettingType.STRUCTURED,
            {},
            ("custom layout presets", "saved pane arrangements"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "mail.last_folder",
            "Last selected folder",
            "Non-dangerous mailbox location restored on launch.",
            "Mail",
            SettingType.STRING,
            "inbox",
            ("restore last inbox",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.folder_expansion",
            "Expanded folders",
            "Folder-tree branches restored between sessions.",
            "Mail",
            SettingType.STRING_LIST,
            (),
            ("folder tree state",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.sort_field",
            "Message sort field",
            "Order folder pages on the server. Explicit search ranking is kept separately.",
            "Mail",
            SettingType.CHOICE,
            "received",
            ("sort messages by",),
            scope=SettingScope.ACCOUNT,
            choices=SORT_FIELDS,
        ),
        _spec(
            "mail.sort_direction",
            "Message sort direction",
            "Ascending or descending folder order. Applies on the next folder refresh.",
            "Mail",
            SettingType.CHOICE,
            "descending",
            ("oldest first", "newest first"),
            scope=SettingScope.ACCOUNT,
            choices=SORT_DIRECTIONS,
        ),
        _spec(
            "mail.attachment_collision",
            "Attachment collision behavior",
            "Default for CLI downloads and the attachment workspace. Overwriting existing files "
            "still requires exact-path confirmation; each download can override this choice.",
            "Mail",
            SettingType.CHOICE,
            "rename",
            ("attachment overwrite policy", "duplicate download names"),
            scope=SettingScope.ACCOUNT,
            choices=tuple(policy.value for policy in CollisionPolicy),
        ),
        _spec(
            "mail.group_conversations",
            "Group conversations within each page",
            "TUI only: place matching exact Exchange conversation IDs together in the loaded "
            "page. Every message remains selectable; counts are page-local. "
            "No extra mail is fetched.",
            "Mail",
            SettingType.BOOLEAN,
            False,
            ("thread grouping", "conversation grouping preference"),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.delete_behavior",
            "Ordinary Delete destination",
            "Move to Deleted Items, or an explicit folder. "
            "Every Delete still requires confirmation. "
            "Permanent deletion is an explicit separate action and is never a saved default.",
            "Mail",
            SettingType.CHOICE,
            "deleted_items",
            ("safe deletion preferences", "archive on delete"),
            scope=SettingScope.ACCOUNT,
            choices=tuple(behavior.value for behavior in DeleteBehavior),
        ),
        _spec(
            "mail.delete_folder",
            "Delete destination folder",
            "A folder selector, path or ID for move_to_folder. Choose an existing folder; "
            "availability is checked only when the confirmed move runs. No folder is auto-created.",
            "Mail",
            SettingType.OPTIONAL_STRING,
            None,
            ("delete to archive folder",),
            scope=SettingScope.ACCOUNT,
            dependencies=(SettingDependency("mail.delete_behavior", "move_to_folder"),),
        ),
        _spec(
            "mail.default_reply_mode",
            "Default reply recipients",
            "Reply starts with the sender, or all visible recipients. Reply all always uses all; "
            "your own address and Bcc are excluded. Review recipients before sending.",
            "Mail",
            SettingType.CHOICE,
            "sender",
            ("default reply behavior", "reply to everyone"),
            scope=SettingScope.ACCOUNT,
            choices=tuple(mode.value for mode in ReplyMode),
        ),
        _spec(
            "mail.reply_include_history",
            "Quote original text in replies",
            "Start sender replies with bounded, inert quoted text. Saved drafts are unchanged.",
            "Mail",
            SettingType.BOOLEAN,
            True,
            ("reply history",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.reply_all_include_history",
            "Quote original text in reply all",
            "Start reply-all compositions with bounded quoted text; recipients still need review.",
            "Mail",
            SettingType.BOOLEAN,
            True,
            ("reply-all behavior",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.forward_include_history",
            "Include original text in forwards",
            "Include bounded original text in a new forward. This does not remove inherited "
            "server attachments; those remain disclosed separately before sending.",
            "Mail",
            SettingType.BOOLEAN,
            True,
            ("forward behavior", "forward quoted text"),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.save_sent_copy",
            "Save Sent Items copies by default",
            "Request a Sent Items copy for new compositions. Saved drafts keep their choice; "
            "turning this off requires review and makes uncertain delivery harder to reconcile. "
            "An imported server-only draft starts with this safety choice enabled.",
            "Mail",
            SettingType.BOOLEAN,
            True,
            ("default sent items behavior",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.mark_read_on_open",
            "Mark read on open",
            "Whether opening a message changes read state.",
            "Mail",
            SettingType.BOOLEAN,
            False,
            ("read behavior",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "mail.mark_read_delay_seconds",
            "Mark read after seconds",
            "Wait while the opened message remains selected and visible. Zero means after display.",
            "Mail",
            SettingType.NUMBER,
            0.0,
            ("mark-as-read timing", "read delay"),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=300,
            dependencies=(SettingDependency("mail.mark_read_on_open", True),),
        ),
        _spec(
            "composer.default_body_format",
            "Default body format",
            "Start new compositions as plain text or HTML. Saved drafts keep their format.",
            "Composer & Drafts",
            SettingType.CHOICE,
            "plain",
            ("compose as html",),
            scope=SettingScope.ACCOUNT,
            choices=("plain", "html"),
        ),
        _spec(
            "composer.default_importance",
            "Default message importance",
            "Importance for new compositions; per-message choices take precedence.",
            "Composer & Drafts",
            SettingType.CHOICE,
            "normal",
            ("compose priority",),
            scope=SettingScope.ACCOUNT,
            choices=("normal", "high", "low"),
        ),
        _spec(
            "composer.request_read_receipt",
            "Request read receipts by default",
            "New messages request a read receipt. Recipients can decline or ignore it.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            False,
            ("default read receipt",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "composer.request_delivery_receipt",
            "Request delivery receipts by default",
            "New messages request a delivery receipt when the receiving service supports it.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            False,
            ("default delivery receipt",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "composer.signature_enabled",
            "Insert signature into new compositions",
            "Insert the saved signature once when starting a new message, reply or forward.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            False,
            ("automatic signature",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "composer.warn_missing_attachment",
            "Warn about a missing attachment",
            "Warn when new text mentions an attachment but no file is attached.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            True,
            ("forgotten file warning",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "composer.recipient_warning_threshold",
            "Recipient-count warning",
            "Warn at this many recipients. Zero disables the warning; the safety limit remains.",
            "Composer & Drafts",
            SettingType.INTEGER,
            20,
            ("many recipients",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=500,
        ),
        _spec(
            "composer.empty_subject_policy",
            "Empty subject behavior",
            "Require a subject, or allow an empty subject with a warning and final send review.",
            "Composer & Drafts",
            SettingType.CHOICE,
            "require",
            ("empty subject warning",),
            scope=SettingScope.ACCOUNT,
            choices=("require", "warn"),
        ),
        _spec(
            "composer.local_recovery",
            "Local draft recovery",
            "Automatically save recovery copies. Explicit close-and-recover remains available.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            True,
            ("recover unsaved draft",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "composer.server_draft_sync",
            "Server draft synchronization",
            "Synchronize compatible drafts with Exchange.",
            "Composer & Drafts",
            SettingType.BOOLEAN,
            True,
            ("save drafts online",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "calendar.default_view",
            "Default calendar view",
            "Open agenda, day, work week, week or month. Applies when opening Calendar.",
            "Calendar",
            SettingType.CHOICE,
            "agenda",
            ("calendar layout",),
            scope=SettingScope.ACCOUNT,
            choices=("agenda", "day", "work_week", "week", "month"),
        ),
        _spec(
            "calendar.week_start",
            "Week starts on",
            "First day in week and complete month grids. Query boundaries update on refresh.",
            "Calendar",
            SettingType.CHOICE,
            "saturday",
            ("first weekday",),
            scope=SettingScope.ACCOUNT,
            choices=("saturday", "sunday", "monday"),
        ),
        _spec(
            "calendar.working_days",
            "Working days",
            "Full weekday names, separated by commas. "
            "Work-week views and free-time suggestions use these days.",
            "Calendar",
            SettingType.STRING_LIST,
            ("monday", "tuesday", "wednesday", "thursday", "friday"),
            ("work week weekdays",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "calendar.workday_start",
            "Working-hour start",
            "Local 24-hour HH:MM. Free-time suggestions must fit within "
            "the working window; event views retain all hours.",
            "Calendar",
            SettingType.STRING,
            "09:00",
            ("work hours begin",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "calendar.workday_end",
            "Working-hour end",
            "Local 24-hour HH:MM, later than start on the same day. "
            "Stage both endpoints together when changing the window.",
            "Calendar",
            SettingType.STRING,
            "17:00",
            ("work hours finish",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "calendar.default_duration_minutes",
            "New event duration",
            "Minutes for a new timed event when no end is supplied; "
            "existing events keep their times.",
            "Calendar",
            SettingType.INTEGER,
            60,
            ("default event length",),
            scope=SettingScope.ACCOUNT,
            minimum=1,
            maximum=10080,
        ),
        _spec(
            "calendar.default_reminder_minutes",
            "New event reminder",
            "Whole minutes before a new event starts; blank disables the reminder.",
            "Calendar",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("default calendar reminder",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=525600,
        ),
        _spec(
            "calendar.default_availability",
            "New event availability",
            "Initial availability for a fresh event; "
            "explicit values and existing events take precedence.",
            "Calendar",
            SettingType.CHOICE,
            "busy",
            ("default busy state",),
            scope=SettingScope.ACCOUNT,
            choices=("free", "working_elsewhere", "tentative", "busy", "out_of_office"),
        ),
        _spec(
            "calendar.invitation_response",
            "Initial invitation response",
            "Initial choice for an ordinary response form or CLI response "
            "with no response argument. "
            "Explicit choices take precedence; sending still requires review and confirmation.",
            "Calendar",
            SettingType.CHOICE,
            "accept",
            ("default meeting reply",),
            scope=SettingScope.ACCOUNT,
            choices=("accept", "tentative", "decline"),
        ),
        _spec(
            "calendar.color_behavior",
            "Calendar event colors",
            "Color event subjects by availability or their first category, or use no event colors. "
            "Presentation only; saved event fields and exports do not change.",
            "Calendar",
            SettingType.CHOICE,
            "none",
            ("appointment color policy",),
            scope=SettingScope.ACCOUNT,
            choices=("none", "availability", "category"),
        ),
        _spec(
            "calendar.notifications",
            "Calendar change notifications",
            "Allow Calendar changes through configured watcher providers, quiet-hour queues and "
            "digests. Off suppresses delivery, including queued changes. This does not start "
            "monitoring, select a provider or schedule occurrence reminders.",
            "Calendar",
            SettingType.BOOLEAN,
            True,
            ("appointment change alerts",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "calendar.show_related_emails",
            "Related-mail searches",
            "Offer explicit bounded subject searches from Calendar. Off blocks queries and late "
            "results. Subject matches are suggestions, not proof that mail belongs to a meeting.",
            "Calendar",
            SettingType.BOOLEAN,
            True,
            ("meeting correspondence lookup",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.sort_order",
            "Contact sorting",
            "Sort each bounded CLI and TUI contact page by display name, family name or company.",
            "Contacts",
            SettingType.CHOICE,
            "display_name",
            ("people sorting",),
            scope=SettingScope.ACCOUNT,
            choices=("display_name", "family_name", "company"),
        ),
        _spec(
            "contacts.display_name_format",
            "Contact display-name format",
            "Show the stored name, given names then family name, or family name first. "
            "Contact records and exports keep their original names.",
            "Contacts",
            SettingType.CHOICE,
            "stored",
            ("people name format",),
            scope=SettingScope.ACCOUNT,
            choices=("stored", "given_family", "family_given"),
        ),
        _spec(
            "contacts.duplicate_threshold",
            "Duplicate threshold",
            "Minimum explainable score for possible duplicates in a bounded contact page. "
            "A higher score selects fewer suggestions; merging still requires review.",
            "Contacts",
            SettingType.INTEGER,
            70,
            ("duplicate detection sensitivity",),
            scope=SettingScope.ACCOUNT,
            minimum=1,
            maximum=100,
        ),
        _spec(
            "contacts.autocomplete",
            "Recipient autocomplete",
            "Complete each To/Cc/Bcc address from bounded local recent recipients. "
            "Accept with Right at the end of the field; no directory lookup or mailbox scan.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("email suggestions",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.recent_recipients",
            "Use and remember recent recipients",
            "Use and record up to 20 local recipients after accepted immediate CLI/TUI sends. "
            "Off stops both recording and suggestions; retained history can be cleared in "
            "Recently contacted people. Queued sends are not recorded here.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("recipient history privacy", "recent-recipient behavior"),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.remember_bcc",
            "Remember Bcc recipients",
            "Include Bcc addresses in new local recipient history after an accepted send. "
            "Off excludes new Bcc-only addresses; older history has no recipient-role metadata "
            "and must be cleared separately. History stays local and is excluded from "
            "settings exports.",
            "Contacts",
            SettingType.BOOLEAN,
            False,
            ("Bcc history privacy",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.frequent_correspondents",
            "Frequent correspondent reports",
            "Allow explicit CLI/TUI frequency reports from bounded Inbox/Sent metadata. "
            "Off blocks these reports before mailbox access; recent reports stay separate.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("frequent-correspondent behavior",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.directory_lookup",
            "Exchange directory lookup",
            "Allow explicit partial-name lookups and address resolution. "
            "Off blocks requests before connection; On still requires server permission. "
            "Composer autocomplete uses local history independently.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("directory privacy", "ResolveNames preference"),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.duplicate_detection",
            "Contact duplicate suggestions",
            "Show explainable duplicate suggestions for an explicit bounded contact page. "
            "Off disables discovery; import duplicate protection and reviewed manual merges "
            "remain available.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("duplicate detection",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "contacts.correspondence_history",
            "Contact mailbox-history access",
            "Allow explicit recent/frequent reports, contact correspondence and Open mail. "
            "Off blocks these mailbox queries and new history indexing. "
            "Existing local history remains until cleared or expired.",
            "Contacts",
            SettingType.BOOLEAN,
            True,
            ("contact privacy", "history access"),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.history_indexing",
            "Index observed contact history",
            "Opt in to retaining address, timestamp, direction and local message reference "
            "from explicit metadata reports. Up to 10,000 observations; no bodies or names. "
            "Off stops both recording and local-history display; it does not erase old entries. "
            "This is an incomplete observation index, not automatic mailbox enumeration.",
            "Contacts",
            SettingType.BOOLEAN,
            False,
            ("contact-history indexing", "local correspondence privacy"),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "contacts.history_retention_days",
            "Contact history retention",
            "Retain observed correspondence for this many days from its message timestamp. "
            "Expired entries are pruned on index reads/writes. Clear local history explicitly "
            "to remove all observations now.",
            "Contacts",
            SettingType.INTEGER,
            30,
            ("contact history expiry",),
            scope=SettingScope.ACCOUNT,
            minimum=1,
            maximum=365,
        ),
        _spec(
            "tasks.default_view",
            "Default task view",
            "Initial view for task lists and new Tasks workspaces; current selections override it.",
            "Tasks",
            SettingType.CHOICE,
            "open",
            ("task filter",),
            scope=SettingScope.ACCOUNT,
            choices=("open", "due", "all", "today", "this_week", "overdue", "completed"),
        ),
        _spec(
            "tasks.show_completed",
            "Show completed tasks",
            "Include completed tasks in All, Today and This week; "
            "Open, Due and Overdue stay incomplete, while Completed shows only completed tasks.",
            "Tasks",
            SettingType.BOOLEAN,
            False,
            ("completed items",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "tasks.sort_order",
            "Task sorting",
            "Sort each bounded page by due date, subject, importance (high first) or status. "
            "Undated tasks follow dated tasks in due-date order.",
            "Tasks",
            SettingType.CHOICE,
            "due_date",
            ("task order", "task priority order"),
            scope=SettingScope.ACCOUNT,
            choices=("due_date", "subject", "importance", "status"),
        ),
        _spec(
            "tasks.default_repeat",
            "New task repeat pattern",
            "Repeat fresh tasks on fixed dates or after completion. Exchange manages "
            "regeneration; existing tasks and open forms keep their rules. No local scheduler.",
            "Tasks",
            SettingType.CHOICE,
            "none",
            ("recurring task defaults", "task recurrence"),
            scope=SettingScope.ACCOUNT,
            choices=(
                "none",
                "daily",
                "weekly",
                "monthly",
                "yearly",
                "after_completion_daily",
                "after_completion_weekly",
                "after_completion_monthly",
                "after_completion_yearly",
            ),
        ),
        _spec(
            "tasks.default_repeat_interval",
            "New task repeat interval",
            "Number of periods between repeats. Fixed-date yearly needs 1; fixed-date "
            "weekly/monthly allow 99. After-completion repeats allow 1-999. "
            "Stage pattern and interval together.",
            "Tasks",
            SettingType.INTEGER,
            1,
            ("repeat every",),
            scope=SettingScope.ACCOUNT,
            minimum=1,
            maximum=999,
        ),
        _spec(
            "tasks.default_repeat_count",
            "New task occurrence limit",
            "Stop after this many occurrences, including the first; blank repeats "
            "without an end. Ignored when the repeat pattern is None.",
            "Tasks",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("repeat count", "recurrence end"),
            scope=SettingScope.ACCOUNT,
            minimum=1,
            maximum=999,
        ),
        _spec(
            "tasks.default_due_days",
            "New task due date",
            "Whole days after its start date, or local creation date without a start; "
            "blank leaves no due date.",
            "Tasks",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("default task deadline",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=3650,
        ),
        _spec(
            "tasks.default_reminder_minutes",
            "New task reminder",
            "Whole minutes after creation for a new task reminder; "
            "blank disables it. Independent of the due date.",
            "Tasks",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("default task reminder",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=525600,
        ),
        _spec(
            "tasks.default_importance",
            "New task importance",
            "Initial priority for fresh tasks; explicit values and existing tasks take precedence.",
            "Tasks",
            SettingType.CHOICE,
            "normal",
            ("default task priority",),
            scope=SettingScope.ACCOUNT,
            choices=("low", "normal", "high"),
        ),
        _spec(
            "search.default_scope",
            "Default search scope",
            "Choose current folder, mailbox, local index, backup, or all.",
            "Search & Indexing",
            SettingType.CHOICE,
            "mailbox",
            ("where search looks",),
            scope=SettingScope.ACCOUNT,
            choices=("current_folder", "mailbox", "local_index", "backups", "all"),
        ),
        _spec(
            "search.fuzzy_enabled",
            "Fuzzy search",
            "Find close matches for misspellings.",
            "Search & Indexing",
            SettingType.BOOLEAN,
            True,
            ("typo tolerant search",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "search.fuzzy_threshold",
            "Fuzzy threshold",
            "Minimum similarity score for approximate results.",
            "Search & Indexing",
            SettingType.INTEGER,
            82,
            ("match sensitivity",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
            maximum=100,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "search.history_enabled",
            "Search history",
            "Keep a bounded local list of prior searches.",
            "Search & Indexing",
            SettingType.BOOLEAN,
            True,
            ("remember searches",),
            scope=SettingScope.MACHINE,
            sensitivity=Sensitivity.PRIVATE,
        ),
        _spec(
            "palette.primary_shortcut",
            "Primary palette shortcut",
            "Main shortcut for the universal command palette.",
            "Command Palette",
            SettingType.STRING,
            "ctrl+k",
            ("command hotkey",),
            keywords=("ctrl k", "control k"),
        ),
        _spec(
            "palette.secondary_shortcut",
            "Compatibility palette shortcut",
            "Secondary shortcut opening the same palette.",
            "Command Palette",
            SettingType.STRING,
            "ctrl+p",
            ("alternate command hotkey",),
        ),
        _spec(
            "palette.launcher_style",
            "On-screen palette launcher",
            "Hidden, circle, hamburger, search, text, or custom.",
            "Command Palette",
            SettingType.CHOICE,
            "hidden",
            ("palette button",),
            choices=("hidden", "circle", "hamburger", "search", "text", "custom"),
        ),
        _spec(
            "palette.launcher_symbol",
            "Custom palette symbol",
            "One short user-selected symbol used by the custom on-screen launcher.",
            "Command Palette",
            SettingType.STRING,
            "⌘",
            ("custom command icon", "custom launcher text"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "palette.launcher_position",
            "Palette launcher position",
            "Optional on-screen launcher location.",
            "Command Palette",
            SettingType.CHOICE,
            "top_left",
            ("command button location",),
            choices=("top_left", "top_right", "bottom_left", "bottom_right"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "palette.fuzzy_threshold",
            "Command match threshold",
            "Minimum fuzzy score for palette suggestions.",
            "Command Palette",
            SettingType.INTEGER,
            70,
            ("command typo sensitivity",),
            minimum=0,
            maximum=100,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "palette.natural_language",
            "Natural-language commands",
            "Interpret common deterministic command phrases.",
            "Command Palette",
            SettingType.BOOLEAN,
            True,
            ("plain english commands",),
        ),
        _spec(
            "palette.suggestion_count",
            "Visible command suggestions",
            "Maximum command results shown at once.",
            "Command Palette",
            SettingType.INTEGER,
            12,
            ("palette result count",),
            minimum=3,
            maximum=50,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "palette.show_descriptions",
            "Show command descriptions",
            "Display a short explanation beside palette actions.",
            "Command Palette",
            SettingType.BOOLEAN,
            True,
            ("explain palette commands",),
        ),
        _spec(
            "palette.command_history",
            "Command history",
            "Bounded recent-command history.",
            "Command Palette",
            SettingType.STRING_LIST,
            (),
            ("recent palette commands",),
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "palette.recent_folders",
            "Recent folders",
            "Bounded account-local folder navigation history used by the command palette.",
            "Command Palette",
            SettingType.STRING_LIST,
            (),
            ("recent mailbox folders",),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "palette.recent_people",
            "Recently contacted people",
            "Bounded account-local recipient history used by the command palette.",
            "Command Palette",
            SettingType.STRING_LIST,
            (),
            ("recent recipients", "recent contacts"),
            scope=SettingScope.ACCOUNT,
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "help.favorites",
            "Favorite Help pages",
            "Help topics pinned for quick access.",
            "Help",
            SettingType.STRING_LIST,
            (),
            ("bookmarked docs",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "help.recent_pages",
            "Recent Help pages",
            "Bounded local Help navigation history.",
            "Help",
            SettingType.STRING_LIST,
            (),
            ("documentation history",),
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "help.contextual_suggestions",
            "Contextual Help suggestions",
            "Offer relevant offline Help for the active view.",
            "Help",
            SettingType.BOOLEAN,
            True,
            ("help tips",),
        ),
        _spec(
            "help.skill_level",
            "Help detail level",
            "Prefer beginner, intermediate, or advanced explanations.",
            "Help",
            SettingType.CHOICE,
            "beginner",
            ("documentation level",),
            choices=("beginner", "intermediate", "advanced"),
        ),
        _spec(
            "monitoring.profile",
            "Monitoring profile",
            "Named rule and delivery profile used by the watcher.",
            "Notifications & Monitoring",
            SettingType.STRING,
            "default",
            ("watcher profile",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "monitoring.watched_folders",
            "Monitored folders",
            "Folder paths observed by the watcher.",
            "Notifications & Monitoring",
            SettingType.STRING_LIST,
            ("inbox",),
            ("watch these folders",),
            scope=SettingScope.PROFILE,
        ),
        _spec(
            "monitoring.strategy",
            "Detection method",
            "Automatic, streaming, pull, incremental sync, or polling.",
            "Notifications & Monitoring",
            SettingType.CHOICE,
            "automatic",
            ("watcher method",),
            scope=SettingScope.PROFILE,
            choices=("automatic", "streaming", "pull", "incremental", "polling"),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "monitoring.desktop_notifications",
            "Windows desktop notifications",
            "Show local new-mail notifications.",
            "Notifications & Monitoring",
            SettingType.BOOLEAN,
            False,
            ("popup notifications",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
        ),
        _spec(
            "monitoring.quiet_hours_enabled",
            "Quiet hours",
            "Suppress or queue notifications during configured hours.",
            "Notifications & Monitoring",
            SettingType.BOOLEAN,
            False,
            ("do not disturb",),
            scope=SettingScope.PROFILE,
        ),
        _spec(
            "monitoring.digest_enabled",
            "Notification digest",
            "Group selected events into a scheduled summary.",
            "Notifications & Monitoring",
            SettingType.BOOLEAN,
            False,
            ("daily summary",),
            scope=SettingScope.PROFILE,
            keywords=("schedule", "scheduled notifications"),
        ),
        _spec(
            "monitoring.start_at_login",
            "Start monitoring with Windows",
            "Manage watcher startup through the external integration.",
            "Windows Startup",
            SettingType.BOOLEAN,
            False,
            ("start guc mail with windows",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.EXTERNAL,
            external_effect=True,
            keywords=("startup", "windows login"),
        ),
        _spec(
            "integrations.notification_privacy",
            "Notification privacy",
            "Choose full, sender-only, or private notification fields.",
            "Integrations",
            SettingType.CHOICE,
            "private",
            ("hide subject in notifications",),
            scope=SettingScope.PROFILE,
            choices=("full", "sender_only", "private"),
        ),
        _spec(
            "integrations.webhook_secret",
            "Webhook secret",
            "Stored only in the operating-system keyring.",
            "Integrations",
            SettingType.OPTIONAL_STRING,
            None,
            ("discord token",),
            scope=SettingScope.PROFILE,
            apply=ApplySemantics.EXTERNAL,
            sensitivity=Sensitivity.SECRET,
            exportable=False,
            level=SettingLevel.ADVANCED,
            external_effect=True,
        ),
        _spec(
            "storage.quota_auto_detect",
            "Automatic quota detection",
            "Prefer safely detected Exchange quota evidence.",
            "Storage",
            SettingType.BOOLEAN,
            True,
            ("detect mailbox limit",),
            scope=SettingScope.ACCOUNT,
        ),
        _spec(
            "storage.prefer_server_quota",
            "Prefer server quota",
            "Use server-reported thresholds ahead of fallbacks and overrides.",
            "Storage",
            SettingType.BOOLEAN,
            True,
            ("exchange quota priority",),
            scope=SettingScope.ACCOUNT,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "storage.issue_warning_mb",
            "Issue Warning threshold",
            "Mailbox usage where Exchange begins warning; Unknown is allowed.",
            "Storage",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("mailbox warning limit",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
        ),
        _spec(
            "storage.issue_warning_source",
            "Issue Warning source",
            "Evidence source for the warning threshold.",
            "Storage",
            SettingType.CHOICE,
            "unknown",
            ("warning quota evidence",),
            scope=SettingScope.ACCOUNT,
            choices=(
                "server_reported",
                "server_notification",
                "automatic",
                "known_guc_fallback",
                "user_configured",
                "unknown",
            ),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "storage.prohibit_send_mb",
            "Prohibit Send threshold",
            "Mailbox usage where Exchange can block outgoing mail.",
            "Storage",
            SettingType.OPTIONAL_NUMBER,
            92.0,
            ("change send limit", "mailbox send quota"),
            scope=SettingScope.ACCOUNT,
            minimum=0,
        ),
        _spec(
            "storage.prohibit_send_source",
            "Prohibit Send source",
            "Evidence source for the send-block threshold.",
            "Storage",
            SettingType.CHOICE,
            "known_guc_fallback",
            ("send quota evidence",),
            scope=SettingScope.ACCOUNT,
            choices=(
                "server_reported",
                "server_notification",
                "automatic",
                "known_guc_fallback",
                "user_configured",
                "unknown",
            ),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "storage.prohibit_send_receive_mb",
            "Prohibit Send & Receive threshold",
            (
                "Mailbox usage where Exchange can block incoming and outgoing mail; "
                "Unknown is allowed."
            ),
            "Storage",
            SettingType.OPTIONAL_NUMBER,
            None,
            ("receive mailbox limit",),
            scope=SettingScope.ACCOUNT,
            minimum=0,
        ),
        _spec(
            "storage.prohibit_send_receive_source",
            "Prohibit Send & Receive source",
            "Evidence source for the send/receive-block threshold.",
            "Storage",
            SettingType.CHOICE,
            "unknown",
            ("receive quota evidence",),
            scope=SettingScope.ACCOUNT,
            choices=(
                "server_reported",
                "server_notification",
                "automatic",
                "known_guc_fallback",
                "user_configured",
                "unknown",
            ),
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "storage.cache_limit_mb",
            "Local cache limit",
            "Maximum target size for expendable local cache data.",
            "Storage",
            SettingType.INTEGER,
            512,
            ("cache size",),
            scope=SettingScope.MACHINE,
            minimum=0,
        ),
        _spec(
            "backup.default_destination",
            "Default backup folder",
            "Local destination for self-describing mailbox backups.",
            "Backup",
            SettingType.OPTIONAL_STRING,
            None,
            ("where are backups stored",),
            scope=SettingScope.MACHINE,
            sensitivity=Sensitivity.PRIVATE,
            exportable=False,
        ),
        _spec(
            "backup.default_kind",
            "Default backup type",
            "Choose whether the Backup Center starts in full or incremental mode.",
            "Backup",
            SettingType.CHOICE,
            "full",
            ("full or incremental backup", "backup mode"),
            scope=SettingScope.MACHINE,
            choices=("full", "incremental"),
        ),
        _spec(
            "backup.encryption_enabled",
            "Encrypt new backups",
            "Use established encryption for newly created backups.",
            "Backup",
            SettingType.BOOLEAN,
            False,
            ("secure backups",),
            scope=SettingScope.MACHINE,
            apply=ApplySemantics.STAGED,
        ),
        _spec(
            "backup.verify_after_create",
            "Verify after backup",
            "Check manifests and hashes after successful creation.",
            "Backup",
            SettingType.BOOLEAN,
            True,
            ("backup integrity check",),
            scope=SettingScope.MACHINE,
        ),
        _spec(
            "backup.retention_count",
            "Backup retention",
            "Number of completed backups retained by the selected profile.",
            "Backup",
            SettingType.INTEGER,
            5,
            ("keep backups",),
            scope=SettingScope.MACHINE,
            minimum=1,
            maximum=100,
            level=SettingLevel.ADVANCED,
        ),
        _spec(
            "advanced.feature_flags",
            "Feature flags",
            "Deliberately user-configurable expert feature switches.",
            "Advanced",
            SettingType.STRING_LIST,
            (),
            ("experimental features",),
            apply=ApplySemantics.RESTART,
            level=SettingLevel.EXPERT,
        ),
    )


DEFAULT_SETTINGS = SettingRegistry((*_app_config_specs(), *_extended_specs()))
