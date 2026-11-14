from __future__ import annotations

import json

import pytest
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

import gucmail.cli as cli_module
from gucmail.cli import app
from gucmail.models import FolderInfo, TaskInfo

GROUPS = (
    "folders",
    "messages",
    "drafts",
    "attachments",
    "saved-searches",
    "notifications",
    "webhooks",
    "config",
    "cache",
    "quota",
    "ews",
    "calendar",
    "contacts",
    "automatic-replies",
    "inbox-rules",
    "contact-groups",
    "directory",
    "recipients",
    "tasks",
    "scheduled",
    "scheduler",
    "recurring",
    "backup",
    "index",
    "semantic",
    "clusters",
    "insights",
    "cleanup",
)


def _environment(tmp_path) -> dict[str, str]:
    return {
        "GUCMAIL_CONFIG_DIR": str(tmp_path / "config"),
        "GUCMAIL_DATA_DIR": str(tmp_path / "data"),
        "GUCMAIL_CACHE_DIR": str(tmp_path / "cache"),
        "GUCMAIL_LOG_DIR": str(tmp_path / "logs"),
        "NO_COLOR": "1",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["messages", "delete", "synthetic-ref"],
        ["messages", "delete", "synthetic-ref", "--permanent"],
        ["messages", "move", "synthetic-ref", "--to", "Synthetic archive"],
        ["messages", "copy", "synthetic-ref", "--to", "Synthetic archive"],
    ],
    ids=["recoverable-delete", "permanent-delete", "move", "copy"],
)
def test_message_mutation_dry_run_requires_no_gateway_or_credentials(
    tmp_path, monkeypatch, arguments
):
    import gucmail.cli as cli

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A local mutation preview must not resolve a gateway or credentials")

    monkeypatch.setattr(cli.core, "gateway", forbidden)
    result = CliRunner().invoke(
        app, ["--json", "--non-interactive", *arguments, "--dry-run"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)["data"]
    assert value["dry_run"] and not value["executed"]
    assert value["plan"]["targets"] == ["synthetic-ref"]


@pytest.mark.parametrize("permanent", [False, True])
def test_unconfirmed_message_delete_rejects_before_gateway_resolution(
    tmp_path, monkeypatch, permanent
):
    import gucmail.cli as cli

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Confirmation must precede credential resolution")

    monkeypatch.setattr(cli.core, "gateway", forbidden)
    arguments = ["--json", "--non-interactive", "messages", "delete", "synthetic-ref"]
    if permanent:
        arguments.append("--permanent")
    result = CliRunner().invoke(app, arguments, env=_environment(tmp_path))
    assert result.exit_code != 0
    assert "--yes" in json.loads(result.output)["data"]["message"]


def test_confirmed_message_delete_resolves_once_and_executes_exact_previewed_target(
    tmp_path, monkeypatch
):
    import gucmail.cli as cli

    calls = []

    class SyntheticGateway:
        def delete_message(self, reference, permanent=False):
            calls.append((reference, permanent))
            return "synthetic-deleted"

    def resolve(_context):
        calls.append("resolve")
        return SyntheticGateway()

    monkeypatch.setattr(cli.core, "gateway", resolve)
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "--non-interactive",
            "--yes",
            "messages",
            "delete",
            "synthetic-ref",
            "--permanent",
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert calls == ["resolve", ("synthetic-ref", True)]


@pytest.mark.parametrize("group", GROUPS)
def test_every_command_group_help_renders_offline(group, tmp_path) -> None:
    result = CliRunner().invoke(app, [group, "--help"], env=_environment(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_json_version_status_and_local_index_are_stable_offline(tmp_path) -> None:
    runner = CliRunner()
    environment = _environment(tmp_path)
    for command, expected in (
        (["--json", "version"], "version"),
        (["--json", "status"], "status"),
        (["--json", "index", "status"], "index status"),
        (["--json", "backup", "list"], "backup list"),
        (["--json", "quota", "local"], "quota local"),
        (["--json", "semantic", "status"], "semantic status"),
    ):
        result = runner.invoke(app, command, env=environment)
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["schema"] == "gucmail.output/v1"
        assert envelope["command"] == expected


def test_eager_version_flag_is_offline(tmp_path) -> None:
    result = CliRunner().invoke(app, ["--version"], env=_environment(tmp_path))
    assert result.exit_code == 0, result.output
    assert result.output.strip()


def test_local_status_redacts_platform_paths(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        ["--json", "status"],
        env=_environment(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert str(tmp_path) not in result.output
    assert json.loads(result.output)["data"]["config_path"] == "<configured>"


def test_invalid_iso_date_fails_before_credentials_or_network(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        ["calendar", "day", "not-a-date"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


def test_task_reminder_cli_plans_set_and_clear_without_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TaskInfo("task-reminder", "Reminder task", "not_started")
    gateway = type(
        "TaskReminderGateway",
        (),
        {"get_task": lambda _self, _reference: task},
    )()
    monkeypatch.setattr(cli_module.core, "gateway", lambda _ctx: gateway)
    runner = CliRunner()
    environment = _environment(tmp_path)

    create = runner.invoke(
        app,
        [
            "--json",
            "tasks",
            "create",
            "--subject",
            "Reminder task",
            "--reminder-at",
            "2026-08-24T09:00:00+02:00",
            "--dry-run",
        ],
        env=environment,
    )
    clear = runner.invoke(
        app,
        [
            "--json",
            "tasks",
            "edit",
            "task-reminder",
            "--clear-reminder",
            "--dry-run",
        ],
        env=environment,
    )

    assert create.exit_code == 0, create.output
    assert json.loads(create.output)["data"]["task"]["reminder_at"] == ("2026-08-24T07:00:00+00:00")
    assert clear.exit_code == 0, clear.output
    assert json.loads(clear.output)["data"]["changes"] == {"reminder_at": None}


def test_conflicting_structured_output_flags_are_usage_error(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        ["--json", "--ndjson", "version"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 2


def test_ndjson_expands_tuple_results_and_reports_the_right_count(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        ["--ndjson", "notifications", "history"],
        env=_environment(tmp_path),
    )

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert records == [
        {
            "schema": "gucmail.output/v1",
            "type": "summary",
            "command": "notifications history",
            "count": 0,
            "warnings": [],
        }
    ]


def test_every_nested_help_page_renders_offline(tmp_path) -> None:
    root = get_command(app)
    environment = _environment(tmp_path)
    runner = CliRunner()
    pending: list[tuple[tuple[str, ...], object]] = [((), root)]
    visited: list[tuple[str, ...]] = []
    while pending:
        path, command = pending.pop()
        if path:
            result = runner.invoke(app, [*path, "--help"], env=environment)
            assert result.exit_code == 0, f"{' '.join(path)}:\n{result.output}"
            assert "Usage:" in result.output
            visited.append(path)
        if isinstance(command, TyperGroup):
            pending.extend(((*path, name), child) for name, child in command.commands.items())
    assert len(visited) >= 200


def test_folders_move_supports_offline_preview_and_confirmed_execution(
    tmp_path, monkeypatch
) -> None:
    class FolderGatewayFake:
        def __init__(self) -> None:
            self.folders = {
                "folder-source": FolderInfo(
                    "folder-source", "Projects", "/Inbox/Projects", "folder-inbox"
                ),
                "folder-destination": FolderInfo("folder-destination", "Archive", "/Archive", None),
            }
            self.moves: list[tuple[str, str]] = []

        def get_folder(self, reference: str) -> FolderInfo:
            return self.folders[reference]

        def move_folder(self, reference: str, destination_reference: str) -> FolderInfo:
            self.moves.append((reference, destination_reference))
            return FolderInfo(
                reference,
                self.folders[reference].name,
                "/Archive/Projects",
                destination_reference,
            )

    gateway = FolderGatewayFake()
    monkeypatch.setattr(cli_module.core, "gateway", lambda _ctx: gateway)
    runner = CliRunner()
    environment = _environment(tmp_path)

    preview = runner.invoke(
        app,
        [
            "--json",
            "folders",
            "move",
            "id:folder-source",
            "id:folder-destination",
            "--dry-run",
        ],
        env=environment,
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["executed"] is False
    assert gateway.moves == []

    moved = runner.invoke(
        app,
        [
            "--json",
            "--yes",
            "folders",
            "move",
            "id:folder-source",
            "id:folder-destination",
        ],
        env=environment,
    )
    assert moved.exit_code == 0, moved.output
    assert json.loads(moved.output)["data"]["executed"] is True
    assert gateway.moves == [("folder-source", "folder-destination")]


def test_webhook_preview_and_delivery_confirmation_are_network_free(tmp_path, monkeypatch) -> None:
    environment = _environment(tmp_path)
    runner = CliRunner()
    from gucmail.secret_store import InMemorySecretBackend, SecretStore

    webhook_secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    monkeypatch.setattr(cli_module.notifications, "SecretStore", lambda _namespace: webhook_secrets)
    added = runner.invoke(
        app,
        ["webhooks", "add", "example", "--url-stdin"],
        input="https://hooks.example.test/mail\n",
        env=environment,
    )
    assert added.exit_code == 0, added.output

    class UnexpectedProvider:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("default preview must not construct a network provider")

    monkeypatch.setattr(cli_module.notifications, "WebhookNotificationProvider", UnexpectedProvider)
    preview = runner.invoke(
        app,
        ["--json", "webhooks", "test", "example"],
        env=environment,
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["sent"] is False

    delivery = runner.invoke(
        app,
        ["--non-interactive", "webhooks", "test", "example", "--deliver"],
        env=environment,
    )
    assert delivery.exit_code == 9
    assert "Confirmation required" in delivery.output
    assert "hooks.example.test" not in delivery.output
