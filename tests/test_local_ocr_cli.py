from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import gucmail.cli as module
from test_local_ocr import setup_image


def invoke(path, args):
    env = {
        f"GUCMAIL_{kind.upper()}_DIR": str(path / kind)
        for kind in ("config", "data", "cache", "log")
    }
    return CliRunner().invoke(module.app, ["--json", "--non-interactive", *args], env=env)


@pytest.mark.parametrize("command", ["status", "extract"])
def test_optional_ocr_cli_help_is_available_without_an_engine(tmp_path, command):
    result = invoke(tmp_path, ["ocr", command, "--help"])
    assert result.exit_code == 0 and "Usage" in result.output


def test_status_preview_and_unconfirmed_apply_do_not_recognize_or_resolve_exchange(
    tmp_path, monkeypatch
):
    source, backend, service = setup_image(tmp_path)
    monkeypatch.setattr(module, "OcrService", lambda: service)
    monkeypatch.setattr(module, "_gateway", lambda *a: pytest.fail("Local OCR resolved Exchange"))
    result = invoke(tmp_path, ["ocr", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["enabled_by_default"] is False
    args = ["ocr", "extract", str(source), "--language", "en-US"]
    preview = invoke(tmp_path, args)
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["apply"] is False
    unconfirmed = invoke(tmp_path, [*args, "--apply"])
    assert unconfirmed.exit_code == 9 and "--yes" in unconfirmed.output
    assert all(call["mode"] == "status" for call in backend.calls)
    confirmed = invoke(tmp_path, ["--yes", *args, "--apply"])
    assert confirmed.exit_code == 0, confirmed.output
    data = json.loads(confirmed.output)["data"]
    assert data["label"] == "ocr_attachment_text" and data["status"] == "success"
    assert backend.path is not None and not backend.path.exists()
    assert not (tmp_path / "data/search.sqlite3").exists()


@pytest.mark.parametrize("language", ["zz-ZZ", "en-US"])
def test_unavailable_language_or_failed_worker_have_nonzero_secret_safe_exit(
    tmp_path, monkeypatch, language
):
    source, backend, service = setup_image(tmp_path)
    backend.result = {"status": "failed", "error": "secret-placeholder"}
    monkeypatch.setattr(module, "OcrService", lambda: service)
    result = invoke(
        tmp_path, ["--yes", "ocr", "extract", str(source), "--language", language, "--apply"]
    )
    assert result.exit_code != 0 and "ocr status" in result.output
    assert "secret-placeholder" not in result.output


def test_ocr_apply_requires_a_language_and_bounded_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OcrService", lambda: pytest.fail("Invalid CLI args started OCR"))
    for args in (
        ["ocr", "extract", "file.png"],
        ["ocr", "extract", "file.png", "--language", "en-US", "--timeout", "16"],
        ["ocr", "extract", "file.png", "--language", "en-US", "--max-chars", "100001"],
    ):
        result = invoke(tmp_path, args)
        assert result.exit_code == 2
