"""Startup stays fast. The app shows the saved inbox list before the server answers."""

import subprocess
import sys
from datetime import UTC, datetime
from threading import Event

import pytest

from gucmail import __version__
from gucmail.models import Address, Importance, ItemSource, MessageSummary
from gucmail.services.mail_list_cache import (
    load_message_list,
    save_message_list,
    summary_from_dict,
)
from gucmail.tui.app import GUCmailApp
from test_mail_search_heading import SyntheticMailboxGateway
from test_mail_search_prompt import wait
from test_ui_session import context_for


def _loaded_modules(statement: str) -> set[str]:
    # Count only what the import adds. Test guards may preload modules in child processes.
    code = (
        "import sys\nbefore = set(sys.modules)\n"
        f"{statement}\nprint('\\n'.join(set(sys.modules) - before))"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    return set(result.stdout.split())


def test_version_check_does_not_load_the_cli_or_tui():
    loaded = _loaded_modules("import gucmail.entry")
    assert not {"typer", "textual", "exchangelib", "gucmail.cli"} & loaded


def test_version_command_prints_one_line():
    result = subprocess.run(
        [sys.executable, "-m", "gucmail", "version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == f"gucmail {__version__}"


def test_app_import_leaves_heavy_screens_for_later():
    loaded = _loaded_modules("import gucmail.tui.app")
    heavy = {
        "exchangelib",
        "markdown_it",
        "gucmail.tui.help_screen",
        "gucmail.tui.compose_screen",
        "gucmail.tui.palette_screen",
        "gucmail.tui.pim_screens",
        "gucmail.tui.settings_screen",
    }
    assert not heavy & loaded


def test_saved_list_round_trips_without_previews(tmp_path):
    context = context_for(tmp_path)
    summary = MessageSummary(
        reference="m1",
        subject="Exam timetable",
        sender=Address("registrar@example.invalid", "Registrar"),
        received_at=datetime(2026, 11, 2, 9, 30, tzinfo=UTC),
        is_read=True,
        has_attachments=True,
        importance=Importance.HIGH,
        categories=("Uni",),
        size=2048,
        preview="Private preview text",
    )
    save_message_list(context.state, "inbox", [summary])
    [restored] = load_message_list(context.state, "inbox")
    assert restored.subject == "Exam timetable"
    assert restored.sender == Address("registrar@example.invalid", "Registrar")
    assert restored.received_at == summary.received_at
    assert restored.importance is Importance.HIGH
    assert restored.source is ItemSource.CACHE
    assert restored.preview is None
    assert summary_from_dict({"subject": "no reference"}) is None
    assert context.state.clear_disposable_cache()["message_list_cache"] == 1
    assert load_message_list(context.state, "inbox") == []


class SlowMailbox(SyntheticMailboxGateway):
    def __init__(self):
        super().__init__()
        self.release = Event()

    def list_messages(self, **kwargs):
        if not self.release.wait(15):
            raise AssertionError("Mailbox read was not released")
        return super().list_messages(**kwargs)


@pytest.mark.asyncio
async def test_saved_inbox_shows_before_the_server_answers(tmp_path):
    context = context_for(tmp_path)
    first = GUCmailApp(context, gateway=SyntheticMailboxGateway())
    async with first.run_test(size=(100, 30)) as pilot:
        await wait(pilot, lambda: first.ui.online and first.ui.busy is None)
        live = [m.subject for m in first._messages]
    assert live
    assert [m.subject for m in load_message_list(context.state, "inbox")] == live

    gateway = SlowMailbox()
    second = GUCmailApp(context, gateway=gateway)
    try:
        async with second.run_test(size=(100, 30)) as pilot:
            await wait(pilot, lambda: bool(second._messages))
            assert [m.subject for m in second._messages] == live
            assert all(m.source is ItemSource.CACHE for m in second._messages)
            assert second.ui.busy == "mailbox"
            gateway.release.set()
            await wait(pilot, lambda: second.ui.online and second.ui.busy is None)
            assert all(m.source is not ItemSource.CACHE for m in second._messages)
    finally:
        gateway.release.set()
