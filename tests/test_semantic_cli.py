from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import gucmail.cli as module
from gucmail.config import AppConfig
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.semantic import SemanticDocument, SemanticStore, account_identity
from gucmail.services.semantic_model import LocalTopicModel
from test_semantic_search import CHUNKS, FIXTURE


def environment(path):
    return {
        "GUCMAIL_CONFIG_DIR": str(path / "config"),
        "GUCMAIL_DATA_DIR": str(path / "data"),
        "GUCMAIL_CACHE_DIR": str(path / "cache"),
        "GUCMAIL_LOG_DIR": str(path / "logs"),
        "NO_COLOR": "1",
    }


def invoke(path, args):
    return CliRunner().invoke(
        module.app, ["--json", "--non-interactive", *args], env=environment(path)
    )


@pytest.mark.parametrize(
    "command", ["status", "build", "enable", "disable", "search", "clear", "update"]
)
def test_semantic_help_and_argument_validation_are_available_without_optional_dependency(
    tmp_path, command
):
    result = CliRunner().invoke(
        module.app, ["semantic", command, "--help"], env=environment(tmp_path)
    )
    assert result.exit_code == 0 and "Usage" in result.output


def test_cli_status_preview_and_unconfirmed_actions_never_resolve_gateway(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Semantic command tried to authenticate or access Exchange")

    monkeypatch.setattr(module, "_gateway", forbidden)
    status = invoke(tmp_path, ["semantic", "status"])
    assert status.exit_code == 0, status.output
    value = json.loads(status.output)["data"]
    assert not value["enabled"] and not value["built"] and not value["remote_transmission"]
    assert not value["query_requires_numpy"]
    preview = invoke(tmp_path, ["semantic", "build"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["apply"] is False
    for command in (
        ["semantic", "enable"],
        ["semantic", "clear"],
        ["semantic", "build", "--apply"],
    ):
        result = invoke(tmp_path, command)
        assert result.exit_code == 9 and "--yes" in result.output
    assert not (tmp_path / "data/semantic.sqlite3").exists()


def test_cli_opt_in_actual_query_update_clear_and_disable_are_local(tmp_path, monkeypatch):
    from gucmail.services import semantic as semantic_module

    monkeypatch.setattr(
        semantic_module,
        "fit_in_worker",
        lambda *args, **kwargs: LocalTopicModel.from_bytes(FIXTURE.read_bytes()),
    )
    monkeypatch.setattr(
        module, "_gateway", lambda *args: pytest.fail("Semantic operation resolved Exchange")
    )
    # The standard application path is discovered from config path output.
    paths = invoke(tmp_path, ["config", "path"])
    from pathlib import Path

    index = SearchIndex(Path(json.loads(paths.output)["data"]["index"]))
    index.upsert_many(IndexDocument(f"m-{i}", subject=text) for i, text in enumerate(CHUNKS))
    result = invoke(tmp_path, ["--yes", "semantic", "enable"])
    assert result.exit_code == 0, result.output
    build = invoke(tmp_path, ["--yes", "semantic", "build", "--apply", "--dimensions", "2"])
    assert build.exit_code == 0, build.output
    assert json.loads(build.output)["data"]["documents"] == 8
    query = invoke(tmp_path, ["semantic", "search", "car"])
    assert query.exit_code == 0, query.output
    assert any(
        row["reference"] == "m-2" and row["lexical_score"] == 0
        for row in json.loads(query.output)["data"]
    )
    index.upsert_many([IndexDocument("m-2", subject=CHUNKS[2] + " road")])
    stale = invoke(tmp_path, ["semantic", "search", "car"])
    assert stale.exit_code != 0 and "changed" in stale.output
    update = invoke(tmp_path, ["--yes", "semantic", "update", "--apply"])
    assert update.exit_code == 0, update.output
    assert json.loads(update.output)["data"]["updated_documents"] == 1
    clear = invoke(tmp_path, ["--yes", "semantic", "clear"])
    assert clear.exit_code == 0 and json.loads(clear.output)["data"]["cleared"] == 8
    assert index.stats().documents == 8
    disable = invoke(tmp_path, ["semantic", "disable"])
    assert disable.exit_code == 0, disable.output
    disabled = invoke(tmp_path, ["semantic", "search", "car"])
    assert disabled.exit_code == 8


def test_cli_query_preserves_no_color_human_output_and_rejects_wrong_account(tmp_path, monkeypatch):
    paths = invoke(tmp_path, ["config", "path"])
    from pathlib import Path

    data_dir = Path(json.loads(paths.output)["data"]["state"]).parent
    semantic = SemanticStore(
        data_dir / "semantic.sqlite3",
        identity=account_identity("foreign@example.test", AppConfig().endpoint),
        enabled=True,
    )
    semantic.build(
        [SemanticDocument("a", subject=CHUNKS[0]), SemanticDocument("b", subject=CHUNKS[4])],
        confirmed=True,
        dimensions=2,
        fit=lambda *args, **kwargs: LocalTopicModel.from_bytes(FIXTURE.read_bytes()),
    )
    result = CliRunner().invoke(
        module.app, ["--no-color", "semantic", "status"], env=environment(tmp_path)
    )
    assert result.exit_code != 0 and "another account" in result.output
    assert "\x1b" not in result.output
