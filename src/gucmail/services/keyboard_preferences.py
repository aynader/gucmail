"""Keyboard-scoped editing and transfer through the canonical Settings service."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gucmail.errors import ConfigurationError
from gucmail.file_completion import complete_temporary_file
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
    KEYBOARD_SETTING_IDS,
    normalize_binding_overrides,
    validate_binding_configuration,
)
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS
from gucmail.settings import SettingsImportPlan, SettingsService, SettingsSession


@dataclass(frozen=True, slots=True)
class KeyboardSnapshot:
    revision: str | None
    configured: dict[str, Any]
    effective: dict[str, Any]
    blocked: dict[str, str]
    warning: str = ""


class KeyboardPreferencesService:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings

    def snapshot(self) -> KeyboardSnapshot:
        snapshot = self.settings.snapshot(KEYBOARD_SETTING_IDS, recover_invalid_keyboard=True)
        result = KeyboardSnapshot(
            snapshot.revision,
            {
                value.spec.setting_id: copy.deepcopy(value.configured_value)
                for value in snapshot.values
            },
            {
                value.spec.setting_id: copy.deepcopy(value.effective_value)
                for value in snapshot.values
            },
            {
                value.spec.setting_id: value.policy_reason
                or value.dependency_reason
                or "Overridden; remove the override first."
                for value in snapshot.values
                if value.overridden or value.policy_locked or not value.dependencies_satisfied
            },
        )
        try:
            normalize_binding_overrides(result.configured[KEYBOARD_SETTING])
        except ConfigurationError:
            result.configured[KEYBOARD_SETTING] = {}
            warning = "Saved action assignments are invalid. Reset all then Apply to repair them."
        else:
            warning = ""
        try:
            validate_binding_configuration(
                result.configured[KEYBOARD_SETTING],
                {identifier: result.configured[identifier] for identifier in SHORTCUT_DEFAULTS},
            )
            validate_binding_configuration(
                result.effective[KEYBOARD_SETTING],
                {identifier: result.effective[identifier] for identifier in SHORTCUT_DEFAULTS},
            )
        except ConfigurationError:
            warning = "Saved or overridden assignments conflict. Resolve them before Apply."
        if warning:
            result.effective[KEYBOARD_SETTING] = {}
            result.effective.update(SHORTCUT_DEFAULTS)
        return KeyboardSnapshot(
            result.revision, result.configured, result.effective, result.blocked, warning
        )

    def parse(self, setting_id: str, value: object) -> Any:
        if setting_id not in KEYBOARD_SETTING_IDS:
            raise ConfigurationError("Only keyboard assignments can be edited here.")
        return self.settings.registry.get(setting_id).parse(value)

    def _session(self, changes: Mapping[str, object], revision: str | None) -> SettingsSession:
        if set(changes) - set(KEYBOARD_SETTING_IDS):
            raise ConfigurationError("Only keyboard assignments can be edited here.")
        session = self.settings.begin()
        if session.base_revision != revision:
            session.cancel()
            raise ConfigurationError(
                "Settings changed elsewhere. Refresh before applying shortcuts."
            )
        for identifier, value in changes.items():
            session.set(identifier, value)
        return session

    def apply(self, changes: Mapping[str, object], *, revision: str | None) -> tuple[str, ...]:
        session = self._session(changes, revision)
        changed = tuple(difference.setting_id for difference in session.preview())
        session.apply(reason="Keyboard assignments updated")
        return changed

    def export(self, path: Path, *, overwrite: bool = False) -> Path:
        payload = self.settings.export(setting_ids=KEYBOARD_SETTING_IDS)
        values = payload["settings"]
        validate_binding_configuration(
            values[KEYBOARD_SETTING],
            {identifier: values[identifier] for identifier in SHORTCUT_DEFAULTS},
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".keyboard-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            complete_temporary_file(temporary, path, overwrite=overwrite)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def preview_import(self, path: Path) -> SettingsImportPlan:
        try:
            with path.open("rb") as handle:
                content = handle.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ConfigurationError("Keyboard import exceeds the 4 MiB limit.")
            payload = json.loads(content.decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ConfigurationError(
                f"Could not read keyboard import: {type(exc).__name__}."
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("settings"), Mapping):
            raise ConfigurationError("Use a complete keyboard export containing a settings object.")
        if set(payload["settings"]) != set(KEYBOARD_SETTING_IDS):
            raise ConfigurationError(
                "Keyboard import must contain all action assignments and the four global keys, "
                "and no other settings. Export them from the shortcut editor first."
            )
        plan = self.settings.plan_import(payload)
        if plan.blocked or plan.unsupported or plan.excluded_private or plan.excluded_secrets:
            raise ConfigurationError(
                "Some keyboard assignments are locked or unavailable; import stopped."
            )
        return plan

    def apply_import(self, plan: SettingsImportPlan) -> tuple[str, ...]:
        if any(change.setting_id not in KEYBOARD_SETTING_IDS for change in plan.changes):
            raise ConfigurationError("This import contains non-keyboard settings.")
        self.settings.apply_import(plan)
        return tuple(change.setting_id for change in plan.changes)

    def preview_import_state(self, path: Path) -> tuple[KeyboardSnapshot, SettingsImportPlan]:
        plan = self.preview_import(path)
        snapshot = self.snapshot()
        if snapshot.revision != plan.expected_revision:
            raise ConfigurationError("Settings changed during preview. Generate a fresh preview.")
        return snapshot, plan
