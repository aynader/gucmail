from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gucmail.cli import app


def _environment(root: Path) -> dict[str, str]:
    return {
        "GUCMAIL_CONFIG_DIR": str(root / "config"),
        "GUCMAIL_DATA_DIR": str(root / "data"),
        "GUCMAIL_CACHE_DIR": str(root / "cache"),
        "GUCMAIL_LOG_DIR": str(root / "logs"),
        "NO_COLOR": "1",
    }


def _json_result(
    runner: CliRunner,
    arguments: list[str],
    *,
    environment: dict[str, str],
) -> dict[str, object]:
    result = runner.invoke(app, ["--json", *arguments], env=environment)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)  # type: ignore[no-any-return]


def test_notification_profile_and_rule_cli_lifecycle_is_offline(tmp_path: Path) -> None:
    runner = CliRunner()
    environment = _environment(tmp_path)

    created = _json_result(
        runner,
        [
            "notifications",
            "profiles",
            "create",
            "study",
            "--name",
            "Study mailbox",
            "--folder",
            "Inbox/Courses",
            "--enabled",
            "--activate",
        ],
        environment=environment,
    )
    assert created["data"]["profile_id"] == "study"  # type: ignore[index]
    assert created["data"]["enabled"] is True  # type: ignore[index]

    rule = _json_result(
        runner,
        [
            "notifications",
            "rules",
            "create",
            "attachments",
            "--profile",
            "study",
            "--name",
            "Messages with attachments",
            "--field",
            "has_attachments",
            "--value",
            "true",
            "--action",
            "immediate",
            "--provider",
            "console",
            "--enabled",
        ],
        environment=environment,
    )
    assert rule["data"]["rule_id"] == "attachments"  # type: ignore[index]
    assert rule["data"]["enabled"] is True  # type: ignore[index]

    simulation = _json_result(
        runner,
        [
            "notifications",
            "rules",
            "test",
            "--profile",
            "study",
            "--has-attachments",
        ],
        environment=environment,
    )
    decision = simulation["data"]["decision"]  # type: ignore[index]
    assert decision["matched_rule_ids"] == ["attachments"]
    assert decision["immediate_providers"] == ["console"]

    duplicated = _json_result(
        runner,
        [
            "notifications",
            "rules",
            "duplicate",
            "attachments",
            "--profile",
            "study",
            "--new-id",
            "attachments-copy",
            "--new-name",
            "Attachments copy",
        ],
        environment=environment,
    )
    assert duplicated["data"]["enabled"] is False  # type: ignore[index]

    disabled = _json_result(
        runner,
        ["notifications", "profiles", "disable", "study"],
        environment=environment,
    )
    assert disabled["data"]["enabled"] is False  # type: ignore[index]

    deleted = _json_result(
        runner,
        ["--yes", "notifications", "profiles", "delete", "study"],
        environment=environment,
    )
    assert deleted["data"]["deleted"] is True  # type: ignore[index]


def test_provider_test_is_configuration_preview_by_default(tmp_path: Path) -> None:
    result = _json_result(
        CliRunner(),
        ["notifications", "providers", "test", "--provider", "console"],
        environment=_environment(tmp_path),
    )

    assert result["data"] == {
        "configured": True,
        "delivered": False,
        "note": "Use --deliver with explicit confirmation to invoke the provider.",
        "provider": "console",
    }
