"""The scriptable search uses the same explicit mailbox scope as the TUI."""

import json

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.models import SearchQuery, to_primitive
from gucmail.services.search import build_search_plan
from test_cli import _environment
from test_mailbox_search_scope import mailbox


@pytest.mark.parametrize("command", [["search"], ["messages", "search"]])
def test_cli_all_folders_uses_shared_search_and_reports_bounds(tmp_path, monkeypatch, command):
    gateway, _account = mailbox()
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: gateway)
    result = CliRunner().invoke(
        cli.app,
        [
            "--json",
            "--non-interactive",
            *command,
            "--all-folders",
            "--subject",
            "Synthetic",
            "--limit",
            "2",
        ],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["plan"]["mailbox_scope"] is True
    assert [row["folder_path"] for row in data["items"]] == [
        "/Mailbox/Posteingang/Onderwijs\\/2026",
        "/Mailbox/Gesendet",
    ]
    assert data["offset"] == 0 and data["limit"] == 2


@pytest.mark.parametrize("conflict", [["--folder", "inbox"], ["--local"], ["--fuzzy"]])
def test_cli_scope_conflicts_are_rejected_without_gateway(tmp_path, monkeypatch, conflict):
    monkeypatch.setattr(
        cli.core, "gateway", lambda _ctx: pytest.fail("Invalid search resolved credentials")
    )
    result = CliRunner().invoke(
        cli.app, ["search", "Synthetic", "--all-folders", *conflict], env=_environment(tmp_path)
    )
    assert result.exit_code == 2
    assert "--all-folders cannot be combined" in result.output


def test_cli_help_discloses_bounded_mailbox_scope(tmp_path):
    result = CliRunner().invoke(cli.app, ["search", "--help"], env=_environment(tmp_path))
    assert result.exit_code == 0
    assert "--all-folders" in result.output and "50" in result.output and "200" in result.output


def test_cli_default_folder_search_json_serializes_the_real_service_plan(tmp_path, monkeypatch):
    gateway, _account = mailbox()
    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: gateway)
    result = CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "search", "--subject", "Synthetic"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["plan"]["required_fields"] == []
    assert data["plan"]["mailbox_scope"] is False
    assert len(data["items"]) == 1 and data["items"][0]["folder_path"] == "/Posteingang"


def test_json_search_plan_serializes_nonempty_required_field_sets_deterministically():
    plan = build_search_plan(SearchQuery(mailbox_scope=True, text="Synthetic"))
    value = to_primitive(plan)
    assert value["required_fields"] == sorted(plan.required_fields)
    assert value["required_fields"] and "body" in value["required_fields"]
    assert json.loads(json.dumps(value)) == value
