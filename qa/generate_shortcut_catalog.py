"""Regenerate packaged fixed-action metadata; never instantiate services or screens."""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path
from typing import Any

from textual.screen import Screen

import gucmail.tui
from gucmail.tui.app import GUCmailApp
from gucmail.tui.shortcuts_screen import binding_scope
from gucmail.tui.unanswered_screen import UnansweredScreen

FIXED_ACTIONS = {
    "accept",
    "reject",
    "confirm",
    "quit",
    "close",
    "cancel",
    "dismiss",
    "navigate_back",
    "back",
    "screen.copy_text",
    "focus_next",
    "focus_previous",
    "focus_next_pane",
    "focus_previous_pane",
    "palette",
    "app.palette",
    "previous_theme",
    "next_theme",
}
FIXED_KEYS = {
    "escape",
    "backspace",
    "tab",
    "shift+tab",
    "enter",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pagedown",
    "ctrl+c",
    "ctrl+x",
    "ctrl+v",
    "ctrl+z",
    "ctrl+a",
    "f6",
    "f7",
}
MAIL_ACTIONS = {
    "compose",
    "reply",
    "reply_all",
    "forward",
    "delete",
    "search",
    "toggle_read",
    "toggle_message_selection",
    "toggle_sidebar",
    "preview_narrower",
    "preview_wider",
    "reset_layout",
    "toggle_preview",
    "detach_reader",
}


def catalog_payload() -> dict[str, Any]:
    # Most screens load on first use. Import them all so every binding is listed.
    for module in pkgutil.iter_modules(gucmail.tui.__path__):
        importlib.import_module(f"gucmail.tui.{module.name}")
    owners: set[type[Any]] = {GUCmailApp, UnansweredScreen}
    pending = list(Screen.__subclasses__())
    visited: set[type[Any]] = set()
    while pending:
        owner = pending.pop()
        if owner in visited:
            continue
        visited.add(owner)
        pending.extend(owner.__subclasses__())
        if owner.__module__.startswith("gucmail.tui."):
            owners.add(owner)
    definitions = []
    fixed = []
    for owner in sorted(owners, key=lambda cls: (cls.__module__, cls.__name__)):
        if "BINDINGS" not in owner.__dict__ or owner._merged_bindings is None:
            continue
        context = f"{owner.__module__}.{owner.__name__}"
        for key, bindings in sorted(owner._merged_bindings.key_to_bindings.items()):
            for index, binding in enumerate(bindings):
                if (
                    key in FIXED_KEYS
                    or binding.action in FIXED_ACTIONS
                    or owner.__name__ == "KeyboardEditorScreen"
                ):
                    fixed.append({"context": context, "key": key, "action": binding.action})
                    continue
                scope = (
                    ("Mail" if binding.action in MAIL_ACTIONS else "Global")
                    if owner is GUCmailApp
                    else binding_scope(owner)
                )
                definitions.append(
                    {
                        "id": f"{context}:{binding.action}:{key}:{index}",
                        "context": context,
                        "scope": scope,
                        "default_key": key,
                        "action": binding.action,
                        "description": binding.description,
                        "priority": binding.priority,
                        "show": binding.show,
                    }
                )
    if len({row["id"] for row in definitions}) != len(definitions):
        raise ValueError("Duplicate binding IDs")
    return {"schema": 1, "definitions": definitions, "fixed_bindings": fixed}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = catalog_payload()
    target = root / "src/gucmail/data/shortcut_catalog.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(
        f"{len(payload['definitions'])} action keys; "
        f"{len(payload['fixed_bindings'])} fixed bindings"
    )


if __name__ == "__main__":
    main()
