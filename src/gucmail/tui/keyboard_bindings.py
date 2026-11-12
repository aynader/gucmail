"""Apply configured action bindings to instances without altering class defaults."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from textual.dom import DOMNode

from gucmail.services.keyboard_bindings import keyboard_catalog


def apply_keyboard_bindings(node: DOMNode, overrides: Mapping[str, str]) -> None:
    context = f"{type(node).__module__}.{type(node).__name__}"
    definitions = tuple(row for row in keyboard_catalog()[0] if row.context == context)
    if not definitions:
        return
    owned_actions = {row.action for row in definitions}
    default_map = type(node)._merged_bindings
    if default_map is None:
        return
    # Replace only catalog-owned actions. Dynamic palette/theme keys and every fixed
    # editing/navigation binding stay untouched, including unrelated bindings on one key.
    for key, entries in tuple(node._bindings.key_to_bindings.items()):
        retained = [entry for entry in entries if entry.action not in owned_actions]
        if retained:
            node._bindings.key_to_bindings[key] = retained
        else:
            node._bindings.key_to_bindings.pop(key, None)
    for definition in definitions:
        key = overrides.get(definition.binding_id, definition.default_key)
        if not key:
            continue
        original = next(
            (
                binding
                for binding in default_map.key_to_bindings.get(definition.default_key, ())
                if binding.action == definition.action
            ),
            None,
        )
        if original is None:
            continue
        node._bindings.key_to_bindings.setdefault(key, []).append(replace(original, key=key))
