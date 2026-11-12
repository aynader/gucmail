from __future__ import annotations

import json
import re
import tomllib
from dataclasses import fields
from pathlib import Path

from typer.main import get_command

from gucmail.cli import app
from gucmail.config import AppConfig
from gucmail.ews.capabilities import DEFAULT_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def test_internal_documentation_links_resolve() -> None:
    missing: list[tuple[Path, str]] = []
    for path in (ROOT / "README.md", *DOCS.rglob("*.md")):
        for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (path.parent / relative).resolve().exists():
                missing.append((path.relative_to(ROOT), target))
    assert not missing


def test_command_reference_covers_every_group_command() -> None:
    content = (DOCS / "command-reference" / "index.md").read_text(encoding="utf-8")
    root = get_command(app)
    for group_name, group in root.commands.items():
        commands = getattr(group, "commands", None)
        if not commands:
            continue
        row = next(
            (line for line in content.splitlines() if line.startswith(f"| `{group_name}` |")),
            None,
        )
        assert row is not None, group_name
        for command_name in commands:
            assert f"`{command_name}`" in row, f"{group_name} {command_name}"


def test_support_document_matches_registry_summary() -> None:
    content = (DOCS / "features" / "support-status.md").read_text(encoding="utf-8")
    capabilities = DEFAULT_REGISTRY.all()
    assert f"contains {len(capabilities)} classified capabilities" in content
    for family in {capability.family for capability in capabilities}:
        label = family.replace("_", " ")
        assert label.casefold() in content.casefold(), family


def test_configuration_reference_covers_every_core_and_path_environment_variable() -> None:
    content = (DOCS / "configuration" / "index.md").read_text(encoding="utf-8")
    for field_info in fields(AppConfig):
        assert f"`{field_info.name}`" in content
        assert f"`GUCMAIL_{field_info.name.upper()}`" in content
    for variable in (
        "GUCMAIL_CONFIG_DIR",
        "GUCMAIL_DATA_DIR",
        "GUCMAIL_CACHE_DIR",
        "GUCMAIL_LOG_DIR",
        "GUCMAIL_CONFIG",
    ):
        assert f"`{variable}`" in content


def test_feature_by_feature_operation_matrix_matches_registry() -> None:
    content = (DOCS / "ews" / "operation-matrix.md").read_text(encoding="utf-8")
    rows = [line for line in content.splitlines() if line.startswith("| `")]
    capabilities = DEFAULT_REGISTRY.all()
    assert len(rows) == len(capabilities)
    for capability in capabilities:
        row = next(
            (line for line in rows if line.startswith(f"| `{capability.operation}` |")),
            None,
        )
        assert row is not None, capability.operation
        for value in (
            capability.family.replace("_", " "),
            capability.implementation_path.value,
            capability.implementation_status.value,
            capability.safety.value,
            capability.server_support.value,
            capability.account_access.value,
            capability.verification.value,
        ):
            assert value in row, (capability.operation, value)


def test_synthetic_examples_parse_and_use_example_domains() -> None:
    example_dir = DOCS / "examples"
    json.loads((example_dir / "webhook-event.example.json").read_text(encoding="utf-8"))
    for line in (
        (example_dir / "ndjson-events.example.ndjson").read_text(encoding="utf-8").splitlines()
    ):
        if line.strip():
            json.loads(line)
    tomllib.loads((example_dir / "config.example.toml").read_text(encoding="utf-8"))

    emails: set[str] = set()
    for path in (ROOT / "README.md", *DOCS.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        emails.update(re.findall(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})", text))
    assert emails
    assert all(domain.casefold().startswith("example.") for domain in emails)
