"""A cancelled read must never publish its late data or error over a newer view."""

from threading import Event

import pytest

from gucmail.commands import CommandInvocation
from gucmail.errors import NetworkError
from gucmail.models import SearchQuery
from gucmail.tui.app import GUCmailApp
from test_mail_search_heading import SyntheticMailboxGateway
from test_mail_search_prompt import wait
from test_ui_session import context_for


class HeldMailbox(SyntheticMailboxGateway):
    def __init__(self, old_kind, old_fails):
        super().__init__()
        self.old_kind = old_kind
        self.old_fails = old_fails
        self.entered = Event()
        self.release = Event()
        self.claimed = False

    def hold(self, kind):
        if kind != self.old_kind or self.claimed:
            return
        self.claimed = True
        self.entered.set()
        if not self.release.wait(15):
            raise AssertionError("Owned delayed read was not released")
        if self.old_fails:
            raise NetworkError("Owned obsolete synthetic read failed")

    def search_messages_planned(self, query, plan, *, limit, offset):
        self.hold("literal")
        return super().search_messages_planned(query, plan, limit=limit, offset=offset)

    def search_messages_expression_planned(self, query, plan, *, limit, offset):
        self.hold("expression")
        return super().search_messages_expression_planned(query, plan, limit=limit, offset=offset)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_kind", ["literal", "expression"])
@pytest.mark.parametrize("old_fails", [False, True])
@pytest.mark.parametrize("new_view", ["search", "refresh"])
async def test_obsolete_search_cannot_replace_newer_result_scope_or_status(
    tmp_path, monkeypatch, old_kind, old_fails, new_view
):
    context = context_for(tmp_path)
    gateway = HeldMailbox(old_kind, old_fails)
    app = GUCmailApp(context, gateway=gateway)
    statuses = []
    set_status = app._set_status

    def status(value):
        statuses.append(value)
        set_status(value)

    monkeypatch.setattr(app, "_set_status", status)
    finished = Event()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            if old_kind == "literal":
                app._remember_mail_search_scope("mailbox")
                worker = app._search_mail(
                    SearchQuery(text="Missing owned obsolete term", mailbox_scope=True),
                    "Obsolete search",
                )
            else:
                worker = app._search_expression(
                    'scope:mailbox AND subject:"Missing owned obsolete term"',
                    "Obsolete search",
                    payload={"query": 'scope:mailbox AND subject:"Missing owned obsolete term"'},
                    source="palette",
                    name=None,
                )
            # Test-only worker completion signal stays valid after Textual cancels its task.
            # Cancelling a task does not stop the already-running Python thread.
            original_work = worker._work

            def recorded_work():
                try:
                    return original_work()
                finally:
                    finished.set()

            worker._work = recorded_work
            await wait(pilot, gateway.entered.is_set)
            if new_view == "search":
                app._palette_search(
                    CommandInvocation.from_mapping(
                        "search.mail",
                        {
                            "query": "synthetic",
                            "scope": "current_folder",
                            "_preserve_query": True,
                            "_label": "inbox search",
                        },
                    ),
                    app._palette_context(),
                )
                await wait(
                    pilot,
                    lambda: (
                        len(gateway.searches) == 1
                        and len(app._messages) == 1
                        and "inbox search" in str(app.query_one("#message-heading").render())
                    ),
                )
            else:
                app.refresh_mailbox()
                await wait(pilot, lambda: app.ui.busy is None and statuses[-1] == "Ready")
            latest_rows = app._messages
            latest_scope = app._last_mail_search_scope
            latest_status = statuses[-1]
            gateway.release.set()
            await wait(pilot, finished.is_set)
            await pilot.pause()
            assert app._messages == latest_rows
            assert app._last_mail_search_scope == latest_scope
            assert statuses[-1] == latest_status
            assert len(app._messages) == 1 and app._message_detail is None
    finally:
        gateway.release.set()
        context.close()


class HeldRefreshMailbox(SyntheticMailboxGateway):
    def __init__(self, fails):
        super().__init__()
        self.fails = fails
        self.lists = 0
        self.entered = Event()
        self.release = Event()

    def list_messages(self, *, folder, limit, offset=0, order_by="-datetime_received"):
        self.lists += 1
        if self.lists == 2:
            self.entered.set()
            if not self.release.wait(15):
                raise AssertionError("Owned refresh was not released")
            if self.fails:
                raise NetworkError("Owned obsolete refresh failed")
        return super().list_messages(folder=folder, limit=limit, offset=offset, order_by=order_by)


def signal_worker_completion(worker, finished):
    original = worker._work

    def recorded():
        try:
            return original()
        finally:
            finished.set()

    worker._work = recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("old_fails", [False, True])
async def test_obsolete_folder_refresh_cannot_replace_newer_all_mail_search(
    tmp_path, monkeypatch, old_fails
):
    context = context_for(tmp_path)
    gateway = HeldRefreshMailbox(old_fails)
    app = GUCmailApp(context, gateway=gateway)
    statuses = []
    original_status = app._set_status

    def status(value):
        statuses.append(value)
        original_status(value)

    monkeypatch.setattr(app, "_set_status", status)
    finished = Event()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            start = app._refresh_mailbox_worker

            def capture_worker(*args):
                worker = start(*args)
                signal_worker_completion(worker, finished)
                return worker

            monkeypatch.setattr(app, "_refresh_mailbox_worker", capture_worker)
            app.refresh_mailbox()
            await wait(pilot, gateway.entered.is_set)
            app._palette_search(
                CommandInvocation.from_mapping(
                    "search.mail",
                    {
                        "query": "synthetic",
                        "scope": "mailbox",
                        "_preserve_query": True,
                        "_label": "All mail search",
                    },
                ),
                app._palette_context(),
            )
            await wait(pilot, lambda: len(app._messages) == 3)
            latest = app._messages
            latest_status = statuses[-1]
            gateway.release.set()
            await wait(pilot, finished.is_set)
            await pilot.pause()
            assert app._messages == latest
            assert statuses[-1] == latest_status
            assert app.ui.online and app.ui.busy is None
            assert app._last_mail_search_scope == "mailbox"
    finally:
        gateway.release.set()
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_kind", ["literal", "expression"])
@pytest.mark.parametrize("cancel", ["escape", "timeout"])
async def test_mail_search_exposes_loading_and_ignores_results_after_cancel_or_timeout(
    tmp_path, old_kind, cancel
):
    context = context_for(tmp_path)
    gateway = HeldMailbox(old_kind, False)
    app = GUCmailApp(context, gateway=gateway)
    finished = Event()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            previous = app._messages
            app._load_timeout_seconds = 0.2 if cancel == "timeout" else 60
            if old_kind == "literal":
                worker = app._search_mail(
                    SearchQuery(text="Missing owned obsolete term", mailbox_scope=True),
                    "Delayed owned search",
                )
            else:
                worker = app._search_expression(
                    'scope:mailbox AND subject:"Missing owned obsolete term"',
                    "Delayed owned search",
                    payload={"query": 'scope:mailbox AND subject:"Missing owned obsolete term"'},
                    source="palette",
                    name=None,
                )
            signal_worker_completion(worker, finished)
            await wait(pilot, gateway.entered.is_set)
            assert app.ui.busy == "mailbox"
            if cancel == "escape":
                await pilot.press("escape")
            await wait(pilot, lambda: app.ui.busy is None)
            expected_status = str(app.query_one("#status-bar").render())
            assert (
                "cancelled" if cancel == "escape" else "taking too long"
            ) in expected_status.casefold()
            gateway.release.set()
            await wait(pilot, finished.is_set)
            await pilot.pause()
            assert app._messages == previous
            assert str(app.query_one("#status-bar").render()) == expected_status
            assert app._last_mail_search_scope is None
            assert app._message_detail is None
    finally:
        gateway.release.set()
        context.close()
