"""Bounded fixed-action shortcut definitions, independent of presentation code."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from gucmail.errors import ConfigurationError
from gucmail.services.shortcuts import (
    RESERVED_SHORTCUTS,
    SHORTCUT_DEFAULTS,
    normalize_shortcut,
    validate_shortcuts,
)

KEYBOARD_SETTING = "keyboard.bindings"
KEYBOARD_SETTING_IDS = (
    KEYBOARD_SETTING,
    *(
        "palette.primary_shortcut",
        "palette.secondary_shortcut",
        "appearance.previous_theme_shortcut",
        "appearance.next_theme_shortcut",
    ),
)


@dataclass(frozen=True, slots=True)
class KeyboardBinding:
    binding_id: str
    context: str
    scope: str
    default_key: str
    action: str
    description: str
    priority: bool
    show: bool


@dataclass(frozen=True, slots=True)
class FixedBinding:
    context: str
    key: str
    action: str


@lru_cache(maxsize=1)
def keyboard_catalog() -> tuple[tuple[KeyboardBinding, ...], tuple[FixedBinding, ...]]:
    try:
        payload = json.loads(files("gucmail").joinpath("data/shortcut_catalog.json").read_text())
        if payload["schema"] != 1:
            raise ValueError("Unsupported keyboard catalog schema")
        definitions = tuple(
            KeyboardBinding(
                binding_id=row["id"],
                context=row["context"],
                scope=row["scope"],
                default_key=row["default_key"],
                action=row["action"],
                description=row["description"],
                priority=row["priority"],
                show=row["show"],
            )
            for row in payload["definitions"]
        )
        fixed = tuple(FixedBinding(**row) for row in payload["fixed_bindings"])
        if len({row.binding_id for row in definitions}) != len(definitions):
            raise ValueError("Duplicate keyboard binding ID")
        if not definitions or len(definitions) > 1000 or len(fixed) > 2000:
            raise ValueError("Keyboard catalog exceeds limits")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigurationError(
            "The installed keyboard catalog is invalid or unavailable."
        ) from exc
    return definitions, fixed


# In 1.2 these screens moved out of gucmail.tui.app; shortcuts saved before keep working.
_MOVED_SCREENS = {
    "AreaScreen": "shell_widgets",
    "ComposeScreen": "compose_screen",
    "ConfirmationScreen": "dialogs",
    "FolderDetailsScreen": "dialogs",
    "FolderMoveScreen": "dialogs",
    "FolderNameScreen": "dialogs",
    "MessageExportPathScreen": "dialogs",
    "HelpScreen": "help_screen",
    "PaletteParameterReviewScreen": "palette_screen",
    "PaletteScreen": "palette_screen",
    "PaletteSequencePreviewScreen": "palette_screen",
    "ReaderModeScreen": "reader",
    "ReaderScreen": "reader",
}


def _current_binding_id(identifier: str) -> str:
    prefix = "gucmail.tui.app."
    if identifier.startswith(prefix):
        screen = identifier[len(prefix) :].split(":", 1)[0]
        if screen in _MOVED_SCREENS:
            return f"gucmail.tui.{_MOVED_SCREENS[screen]}.{identifier[len(prefix) :]}"
    return identifier


def normalize_binding_overrides(value: object) -> dict[str, str]:
    """Accept only known IDs and single keys; an empty key disables one binding."""
    if not isinstance(value, Mapping) or len(value) > 1000:
        raise ConfigurationError(
            "Keyboard assignments must be a bounded mapping of actions to keys."
        )
    known = {row.binding_id: row for row in keyboard_catalog()[0]}
    result: dict[str, str] = {}
    for identifier, raw in value.items():
        if isinstance(identifier, str):
            identifier = _current_binding_id(identifier)
        if not isinstance(identifier, str) or identifier not in known:
            raise ConfigurationError("Unknown keyboard action; refresh the shortcut catalog.")
        # Existing defaults may be bare keys or use protected editing combinations.
        # Restoring exactly that original action is safe and needs no stored override.
        if (
            isinstance(raw, str)
            and len(raw) <= 40
            and raw.strip().casefold().replace("control+", "ctrl+") == known[identifier].default_key
        ):
            continue
        key = normalize_shortcut(raw)
        if key and (key in RESERVED_SHORTCUTS or key.replace("shift+", "") in RESERVED_SHORTCUTS):
            raise ConfigurationError(f"{key} is reserved for existing editing/navigation controls.")
        result[identifier] = key
    return result


def validate_binding_configuration(
    overrides: object, global_values: Mapping[str, object]
) -> dict[str, str]:
    """Reject conflicts atomically, allowing same-action aliases and separate contexts."""
    normalized = normalize_binding_overrides(overrides)
    global_keys = set(validate_shortcuts(global_values).values()) - {""}
    definitions, fixed = keyboard_catalog()
    protected = {(row.context, row.key): row.action for row in fixed}
    seen: dict[tuple[str, str], KeyboardBinding] = {}
    for definition in definitions:
        key = normalized.get(definition.binding_id, definition.default_key)
        if not key:
            continue
        changed = definition.binding_id in normalized
        if changed and (key in global_keys or (definition.context, key) in protected):
            raise ConfigurationError(
                f"{key} conflicts with a global shortcut or fixed navigation/control binding."
            )
        pair = (definition.context, key)
        earlier = seen.get(pair)
        if (
            earlier is not None
            and earlier.action != definition.action
            and (changed or earlier.binding_id in normalized)
        ):
            raise ConfigurationError(
                f"{key} is assigned to different actions in the same screen. Resolve the conflict."
            )
        seen[pair] = definition
    # Preserve existing contextual/default reuse, e.g. Ctrl+P; newly chosen priority
    # global keys must not steal any screen action. Explicit new contextual assignments
    # to those default global keys have already been rejected above.
    new_global_keys = global_keys - set(SHORTCUT_DEFAULTS.values())
    for definition in definitions:
        key = normalized.get(definition.binding_id, definition.default_key)
        if key and key in new_global_keys:
            raise ConfigurationError(f"{key} conflicts with an existing screen action.")
    return normalized
