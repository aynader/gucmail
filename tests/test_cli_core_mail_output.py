"""Regression tests from the core-mail CLI beta QA pass (synthetic data only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

import gucmail.cli as cli_module
from gucmail.cli import app
from gucmail.errors import ConfigurationError
from gucmail.models import Address, FolderInfo, MessageDetail, MessageSummary
from gucmail.services.folders import FolderService


def _environment(tmp_path) -> dict[str, str]:
    return {
        "GUCMAIL_CONFIG_DIR": str(tmp_path / "config"),
        "GUCMAIL_DATA_DIR": str(tmp_path / "data"),
        "GUCMAIL_CACHE_DIR": str(tmp_path / "cache"),
        "GUCMAIL_LOG_DIR": str(tmp_path / "logs"),
        "NO_COLOR": "1",
    }


def _summary(index: int, subject: str) -> MessageSummary:
    return MessageSummary(
        f"msg_{index:04}",
        subject,
        Address(f"person{index}@example.test", f"Person {index}"),
        datetime(2026, 11, 10, 9, tzinfo=UTC),
        folder_path="/Inbox",
        categories=("Course-1",),
        size=1000 + index,
    )


class FakeGateway:
    def __init__(self) -> None:
        self.messages = (_summary(0, "Plain subject"), _summary(1, "[/b] hostile [bold]x"))
        self.queries: list = []

    def list_messages(self, folder="inbox", limit=25, offset=0, order_by=None):
        return self.messages[offset : offset + limit]

    def search_messages(self, query, limit=25, offset=0):
        self.queries.append(query)
        return ()

    def show_message(self, reference, mark_read=False):
        return MessageDetail(
            summary=self.messages[0], to=(Address("student@example.test"),), body_text="Hello"
        )


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(cli_module.core, "gateway", lambda _ctx, **_kw: fake)
    return fake


def test_ndjson_page_streams_one_record_per_message(tmp_path, gateway) -> None:
    result = CliRunner().invoke(
        app, ["--ndjson", "messages", "list", "--limit", "2"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    items = [record for record in records if record["type"] == "item"]
    assert [item["data"]["reference"] for item in items] == ["msg_0000", "msg_0001"]
    summary = records[-1]
    assert summary["type"] == "summary" and summary["count"] == 2
    assert summary["page"]["has_more"] is True and summary["page"]["next_offset"] == 2


def test_human_message_list_is_a_compact_table_and_markup_stays_literal(tmp_path, gateway) -> None:
    result = CliRunner().invoke(
        app, ["messages", "list", "--limit", "2"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert '{"reference"' not in result.output
    assert "Setting" not in result.output
    assert "Reference" in result.output and "Subject" in result.output
    assert "[/b]" in result.output  # literal, not consumed as Rich markup
    assert "Person 0" in result.output
    assert "more field(s) per item are available with --json" in result.output
    assert "rerun with --offset 2" in result.output


def test_human_errors_keep_hostile_markup_literal(tmp_path, monkeypatch) -> None:
    def missing(_ctx, **_kw):
        raise ConfigurationError("Folder not found: [/b]x")

    monkeypatch.setattr(cli_module.core, "gateway", missing)
    result = CliRunner().invoke(app, ["messages", "list"], env=_environment(tmp_path))
    assert result.exit_code == 3
    assert "Folder not found: [/b]x" in result.output


def test_human_message_show_flattens_the_summary(tmp_path, gateway) -> None:
    result = CliRunner().invoke(app, ["messages", "show", "msg_0000"], env=_environment(tmp_path))
    assert result.exit_code == 0, result.output
    assert '{"reference"' not in result.output
    assert "Plain subject" in result.output
    assert "student@example.test" in result.output


def test_search_accepts_documented_date_only_bounds(tmp_path, gateway) -> None:
    result = CliRunner().invoke(
        app,
        ["--json", "search", "--sender", "teacher@example.test", "--after", "2026-01-01"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    after = gateway.queries[-1].after
    assert after.isoformat() == "2026-01-01T00:00:00+02:00"  # Africa/Cairo default


def test_debug_shows_redacted_detail_in_human_mode(tmp_path, monkeypatch) -> None:
    def explode(_ctx, **_kw):
        raise RuntimeError("boom password=Synthetic-Secret-1")

    monkeypatch.setattr(cli_module.core, "gateway", explode)
    runner = CliRunner()
    plain = runner.invoke(app, ["messages", "list"], env=_environment(tmp_path))
    debug = runner.invoke(app, ["--debug", "messages", "list"], env=_environment(tmp_path))
    assert plain.exit_code == debug.exit_code == 12
    assert "Debug" not in plain.output
    assert "Debug (RuntimeError)" in debug.output
    assert "Synthetic-Secret-1" not in debug.output


class FolderGateway:
    def get_folder(self, reference):
        return FolderInfo(reference, "Inbox", "/Inbox", None, distinguished_role="inbox")


def test_standard_folders_cannot_be_deleted_or_moved() -> None:
    service = FolderService(FolderGateway())
    with pytest.raises(ConfigurationError, match="standard Exchange folder"):
        service.plan_delete("id:fld-inbox")
    with pytest.raises(ConfigurationError, match="standard Exchange folder"):
        service.plan_move("id:fld-inbox", "/")


def test_redirected_legacy_code_page_output_never_raises_unicode_errors(monkeypatch) -> None:
    import io
    import sys

    from gucmail import cli

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    cli.main._tolerate_narrow_console_encoding()
    print("▌ Help — ok", file=stream)
    stream.flush()
    assert b"Help" in buffer.getvalue()


def test_draft_delete_also_moves_its_server_copy_to_deleted_items(tmp_path, monkeypatch) -> None:
    """A draft saved with `drafts save-server` must not be orphaned in Exchange Drafts."""

    from types import SimpleNamespace

    calls: list[tuple] = []

    class Drafts:
        def get(self, reference):
            return SimpleNamespace(server_id="drf_server")

        def delete(self, reference):
            calls.append(("local-delete", reference))
            return True

    class Gateway:
        def delete_message(self, reference, permanent=False):
            calls.append(("server-delete", reference, permanent))

    monkeypatch.setattr(cli_module.core, "draft_service", lambda _ctx: Drafts())
    monkeypatch.setattr(cli_module.core, "gateway", lambda _ctx, **_kw: Gateway())
    result = CliRunner().invoke(
        app, ["--yes", "--json", "drafts", "delete", "dft-1"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert calls == [("server-delete", "drf_server", False), ("local-delete", "dft-1")]
    assert json.loads(result.output)["data"]["server_copy_moved_to_deleted_items"] is True

    calls.clear()
    result = CliRunner().invoke(
        app,
        ["--yes", "drafts", "delete", "dft-1", "--local-only"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert calls == [("local-delete", "dft-1")]


def test_html_text_keeps_inline_markup_on_one_line() -> None:
    """Real GUC mail: '6<sup>th</sup> Semester' was split over three lines."""

    from gucmail.security import html_to_safe_text

    html = (
        "<p>Business Informatics 6<sup>th</sup> Semester</p>"
        "<p>Dear <b>GUC</b> Students,<br>Congratulations</p>"
        "<ul><li>One</li><li>Two</li></ul>"
        "<table><tr><td>a</td><td>b</td></tr></table><script>alert(1)</script>"
    )
    assert html_to_safe_text(html) == (
        "Business Informatics 6th Semester\n\nDear GUC Students,\nCongratulations"
        "\n\nOne\nTwo\n\na  b"
    )
