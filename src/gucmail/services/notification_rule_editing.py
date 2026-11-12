"""Transactional edits of the rule engine's validated, bounded condition tree."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from gucmail.notification_rules import condition_from_data, condition_to_data

ConditionPath = tuple[int, ...]


def condition_at(root: dict[str, Any], path: ConditionPath) -> dict[str, Any]:
    node = root
    for index in path:
        if type(index) is not int or index < 0:
            raise ValueError("The selected condition no longer exists.")
        if node["type"] == "not" and index == 0:
            node = node["child"]
        elif node["type"] in {"and", "or"}:
            if index >= len(node["children"]):
                raise ValueError("The selected condition no longer exists.")
            node = node["children"][index]
        else:
            raise ValueError("The selected condition no longer exists.")
    return node


def edit_condition_tree(
    root: dict[str, Any],
    path: ConditionPath,
    operation: str,
    value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a validated copy; failed edits leave the caller's tree unchanged."""
    candidate = deepcopy(root)
    node = condition_at(candidate, path)
    if operation == "replace":
        if value is None:
            raise ValueError("A replacement condition is required.")
        replacement = deepcopy(value)
    elif operation in {"and", "or"}:
        replacement = {"type": operation, "children": [deepcopy(node)]}
    elif operation == "not":
        replacement = (
            deepcopy(node["child"])
            if node["type"] == "not"
            else {"type": "not", "child": deepcopy(node)}
        )
    elif operation == "add":
        if value is None:
            raise ValueError("A new condition is required.")
        if node["type"] in {"and", "or"}:
            node["children"].append(deepcopy(value))
            replacement = node
        else:
            replacement = {"type": "and", "children": [deepcopy(node), deepcopy(value)]}
    elif operation in {"remove", "duplicate", "up", "down"}:
        if not path:
            raise ValueError("Select a child condition for this action.")
        parent = condition_at(candidate, path[:-1])
        if parent["type"] not in {"and", "or"}:
            raise ValueError("Select a condition in an AND or OR group.")
        children = parent["children"]
        index = path[-1]
        if operation == "remove":
            if len(children) == 1:
                raise ValueError("Keep at least one condition in this group.")
            del children[index]
        elif operation == "duplicate":
            children.insert(index + 1, deepcopy(node))
        else:
            destination = index + (-1 if operation == "up" else 1)
            if not 0 <= destination < len(children):
                raise ValueError("The condition is already at the edge of its group.")
            children[index], children[destination] = children[destination], children[index]
        return condition_to_data(condition_from_data(candidate))
    else:
        raise ValueError("Unknown condition edit.")
    if not path:
        candidate = replacement
    else:
        parent = condition_at(candidate, path[:-1])
        if parent["type"] == "not":
            parent["child"] = replacement
        else:
            parent["children"][path[-1]] = replacement
    return condition_to_data(condition_from_data(candidate))
