"""Named layout snapshots using canonical validation and atomic Settings revisions."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gucmail.errors import ConfigurationError, ConflictError
from gucmail.security import sanitize_terminal

if TYPE_CHECKING:
    from gucmail.settings import SettingsService, SettingsSession

PRESET_FIELDS = (
    "layout.sidebar_visible",
    "layout.sidebar_ratio",
    "layout.message_list_ratio",
    "layout.preview_ratio",
    "layout.split_orientation",
    "layout.preview_visible",
    "layout.reader_mode",
    "layout.status_bar_visible",
    "layout.header_contents",
    "layout.header_compactness",
    "layout.status_bar_contents",
    "layout.message_columns",
)
MAX_PRESETS = 32


def validate_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or len(name) > 40
        or sanitize_terminal(name) != name
        or any(ord(char) < 32 for char in name)
    ):
        raise ConfigurationError("Choose a printable layout name of 1-40 characters.")
    return name


def normalize_presets(
    value: object, validate: Callable[[str, object], Any]
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or len(value) > MAX_PRESETS:
        raise ConfigurationError(
            f"Layout presets must be an object with at most {MAX_PRESETS} names."
        )
    result = {}
    seen = set()
    for raw_name, values in value.items():
        name = validate_name(raw_name)
        if name.casefold() in seen:
            raise ConfigurationError("Layout preset names must be unique ignoring letter case.")
        seen.add(name.casefold())
        if not isinstance(values, Mapping) or set(values) != set(PRESET_FIELDS):
            raise ConfigurationError(
                "A layout preset must contain exactly the supported layout fields."
            )
        result[name] = {key: validate(key, values[key]) for key in PRESET_FIELDS}
    return result


@dataclass(frozen=True, slots=True)
class LayoutPresetCatalog:
    revision: str | None
    presets: dict[str, dict[str, Any]]
    current: dict[str, Any]


class LayoutPresetService:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings

    def _validate(self, key: str, value: object) -> Any:
        return self.settings.registry.get(key).parse(value)

    def catalog(self) -> LayoutPresetCatalog:
        revision = self.settings.begin().base_revision
        values = {
            v.spec.setting_id: v.effective_value for v in self.settings.values(category="Layout")
        }
        if self.settings.begin().base_revision != revision:
            raise ConflictError(
                "Settings changed while loading layout presets. Refresh and review again."
            )
        return LayoutPresetCatalog(
            revision,
            normalize_presets(values["layout.presets"], self._validate),
            copy.deepcopy({key: values[key] for key in PRESET_FIELDS}),
        )

    def _session(self, revision: str | None) -> SettingsSession:
        session = self.settings.begin()
        if session.base_revision != revision:
            raise ConflictError(
                "Settings changed after the layout preview. Refresh and review again."
            )
        return session

    @staticmethod
    def _name(catalog: LayoutPresetCatalog, name: str) -> str | None:
        return next((key for key in catalog.presets if key.casefold() == name.casefold()), None)

    def save(
        self,
        name: str,
        values: Mapping[str, Any],
        *,
        catalog: LayoutPresetCatalog,
        replace_existing: bool = False,
    ) -> None:
        name = validate_name(name)
        parsed = normalize_presets({name: values}, self._validate)[name]
        existing = self._name(catalog, name)
        if existing and not replace_existing:
            raise ConflictError(
                "This layout name already exists. Confirm replacement or choose another name."
            )
        presets = copy.deepcopy(catalog.presets)
        if existing:
            presets.pop(existing)
        presets[name] = parsed
        session = self._session(catalog.revision)
        session.set("layout.presets", presets)
        session.apply(reason="Layout preset saved")

    def load(self, name: str, *, catalog: LayoutPresetCatalog) -> tuple[str, ...]:
        selected = self._name(catalog, validate_name(name))
        if selected is None:
            raise ConfigurationError("Choose an existing layout preset.")
        values = normalize_presets({selected: catalog.presets[selected]}, self._validate)[selected]
        session = self._session(catalog.revision)
        for key, value in values.items():
            session.set(key, value)
        session.set("layout.last_preset", "saved:" + selected)
        session.apply(reason="Named layout preset loaded")
        return (*PRESET_FIELDS, "layout.last_preset")

    def preview(self, name: str, *, catalog: LayoutPresetCatalog) -> dict[str, Any]:
        selected = self._name(catalog, validate_name(name))
        if selected is None:
            raise ConfigurationError("Choose an existing layout preset.")
        return {
            "name": selected,
            "settings": normalize_presets({selected: catalog.presets[selected]}, self._validate)[
                selected
            ],
        }

    def delete(self, name: str, *, catalog: LayoutPresetCatalog) -> None:
        selected = self._name(catalog, validate_name(name))
        if selected is None:
            raise ConfigurationError("Choose an existing layout preset.")
        presets = copy.deepcopy(catalog.presets)
        presets.pop(selected)
        session = self._session(catalog.revision)
        session.set("layout.presets", presets)
        # Deleting the library entry does not change the active arrangement.
        session.apply(reason="Layout preset removed")
