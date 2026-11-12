"""Canonical, non-conflicting keys for the four customizable global actions."""

from __future__ import annotations

import re
from collections.abc import Mapping

from gucmail.errors import ConfigurationError

SHORTCUT_DEFAULTS = {
    "palette.primary_shortcut": "ctrl+k",
    "palette.secondary_shortcut": "ctrl+p",
    "appearance.previous_theme_shortcut": "ctrl+alt+left",
    "appearance.next_theme_shortcut": "ctrl+alt+right",
}
_MODIFIERS = ("ctrl", "alt", "shift")
_NAMED_KEYS = {
    "space",
    "enter",
    "backspace",
    "delete",
    "left",
    "right",
    "up",
    "down",
    "home",
    "end",
    "pageup",
    "pagedown",
    "tab",
    "escape",
    "slash",
    "backslash",
    "minus",
    "equals",
}
# Navigation, normal mail/layout actions and editing controls must not be silently
# repurposed by a global palette/theme assignment, especially inside the composer.
RESERVED_SHORTCUTS = frozenset(
    {
        "f5",
        "f1",
        "f6",
        "f7",
        "ctrl+q",
        "ctrl+c",
        "ctrl+s",
        "ctrl+enter",
        "ctrl+z",
        "ctrl+f",
        "ctrl+d",
        "ctrl+a",
        "ctrl+x",
        "ctrl+v",
        "ctrl+b",
        "ctrl+backslash",
        "ctrl+f5",
        "ctrl+home",
        "ctrl+e",
        "ctrl+shift+k",
        "ctrl+pageup",
        "ctrl+pagedown",
        "ctrl+end",
        "ctrl+left",
        "ctrl+right",
        "ctrl+up",
        "ctrl+down",
        "ctrl+n",
        "ctrl+r",
        "ctrl+w",
        "ctrl+y",
        "ctrl+u",
        "ctrl+h",
        "ctrl+j",
        "ctrl+m",
        "ctrl+tab",
        "ctrl+shift+tab",
        "alt+left",
        "alt+right",
        "alt+up",
        "alt+down",
        "alt+t",
        "alt+0",
        "shift+enter",
        "alt+f4",
        "alt+tab",
        "alt+space",
        "ctrl+alt+delete",
    }
)


def normalize_shortcut(value: object) -> str:
    """Accept one portable modified key or function key, never a key list/chord."""

    if not isinstance(value, str) or len(value) > 40:
        raise ConfigurationError("Enter one keyboard shortcut, such as Ctrl+Space or F8.")
    if not value.strip():
        return ""
    parts = value.strip().casefold().replace("control+", "ctrl+").split("+")
    if not parts or not parts[-1] or any(part not in _MODIFIERS for part in parts[:-1]):
        raise ConfigurationError("Use Ctrl/Alt/Shift plus one key, or a function key F1-F24.")
    modifiers, key = parts[:-1], parts[-1]
    if len(set(modifiers)) != len(modifiers):
        raise ConfigurationError("A shortcut modifier may appear only once.")
    function_key = bool(re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key))
    if key not in _NAMED_KEYS and not function_key and not re.fullmatch(r"[a-z0-9]", key):
        raise ConfigurationError("Choose a supported letter, digit, named key or F1-F24.")
    if not function_key and not ({"ctrl", "alt"} & set(modifiers)):
        raise ConfigurationError("Global shortcuts need Ctrl or Alt to preserve typing/navigation.")
    return "+".join([*(modifier for modifier in _MODIFIERS if modifier in modifiers), key])


def validate_shortcuts(values: Mapping[str, object]) -> dict[str, str]:
    """Validate an entire staged/imported configuration, allowing palette aliases."""

    normalized = {
        identifier: normalize_shortcut(values.get(identifier, default))
        for identifier, default in SHORTCUT_DEFAULTS.items()
    }
    owners: dict[str, str] = {}
    for identifier, key in normalized.items():
        if not key:
            continue
        if (
            key in RESERVED_SHORTCUTS
            or key.replace("shift+", "") in RESERVED_SHORTCUTS
            or (key == "ctrl+p" and not identifier.startswith("palette."))
        ):
            raise ConfigurationError(
                f"{key} is reserved for navigation, editing or an existing action. "
                "Choose another key."
            )
        action = "palette" if identifier.startswith("palette.") else identifier
        if key in owners and owners[key] != action:
            raise ConfigurationError(
                f"Shortcut {key} is assigned to different actions. "
                "Resolve the conflict before Apply."
            )
        owners[key] = action
    return normalized
