"""One-key delete, archive and flag act at once and Ctrl+Z reverses them."""

import pytest

from gucmail.quotes import split_quoted
from gucmail.tui.app import GUCmailApp
from test_compose_preferences import wait_for
from test_mail_deletion_grouping_preferences import MailGateway, configure
from test_settings_secondpass import _context


class MovingGateway(MailGateway):
    def move_message(self, reference, destination):
        self.mutations.append(("move", reference, destination))
        return f"{reference}-moved"

    def flag(self, reference, flagged=True):
        self.mutations.append(("flag", reference, flagged))
        return reference


def _app(tmp_path, monkeypatch, gateway):
    context = _context(tmp_path)
    configure(context).set("mail.delete_behavior", "deleted_items")
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    monkeypatch.setattr(app, "refresh_mailbox", lambda: None)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,destination,label", [("d", "deleteditems", "Deleted"), ("e", "archive", "Archived")]
)
async def test_quick_move_hides_row_and_undo_moves_it_back(
    tmp_path, monkeypatch, key, destination, label
):
    gateway = MovingGateway()
    app = _app(tmp_path, monkeypatch, gateway)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.ui.selected_reference = "other"
        app.query_one("#message-table").focus()
        await pilot.press(key)
        await wait_for(pilot, lambda: gateway.mutations)
        assert "other" not in {m.reference for m in app._messages}
        await wait_for(pilot, lambda: "Ctrl+Z" in app._status_activity)
        assert gateway.mutations == [("move", "other", destination)]
        assert app._status_activity.startswith(label)
        await pilot.press("ctrl+z")
        await wait_for(pilot, lambda: len(gateway.mutations) == 2)
        assert gateway.mutations[1] == ("move", "other-moved", "inbox")
        await wait_for(pilot, lambda: app._status_activity == "Moved back")
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert len(gateway.mutations) == 2
        assert app._status_activity == "Nothing to undo"


@pytest.mark.asyncio
async def test_flag_key_toggles_and_undo_restores(tmp_path, monkeypatch):
    gateway = MovingGateway()
    app = _app(tmp_path, monkeypatch, gateway)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.ui.selected_reference = "first"
        app.query_one("#message-table").focus()
        await pilot.press("exclamation_mark")
        await wait_for(pilot, lambda: gateway.mutations)
        assert gateway.mutations == [("flag", "first", True)]
        await wait_for(pilot, lambda: app._status_activity.startswith("Flagged"))
        await pilot.press("ctrl+z")
        await wait_for(pilot, lambda: len(gateway.mutations) == 2)
        assert gateway.mutations[1] == ("flag", "first", False)
        await wait_for(pilot, lambda: app._status_activity == "Flags restored")



@pytest.mark.asyncio
async def test_ctrl_a_selects_all_and_d_moves_every_selected_message(tmp_path, monkeypatch):
    gateway = MovingGateway()
    app = _app(tmp_path, monkeypatch, gateway)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.ui.selected_reference = "first"
        app.query_one("#message-table").focus()
        await pilot.press("ctrl+a")
        await pilot.pause()
        total = len(gateway.rows)
        assert len(app._selected_message_references) == total
        assert app._status_activity.startswith(f"{total} selected · d delete")
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert not app._selected_message_references
        assert app._status_activity == "Nothing selected"
        await pilot.press("ctrl+a", "d")
        await wait_for(pilot, lambda: len(gateway.mutations) == total)
        assert {m[2] for m in gateway.mutations} == {"deleteditems"}


@pytest.mark.asyncio
async def test_flag_key_flags_every_selected_message_and_undo_restores_each(
    tmp_path, monkeypatch
):
    gateway = MovingGateway()
    app = _app(tmp_path, monkeypatch, gateway)
    async with app.run_test() as pilot:
        app._render_messages(gateway.rows)
        app.ui.selected_reference = "first"
        app.query_one("#message-table").focus()
        await pilot.press("ctrl+a", "exclamation_mark")
        await wait_for(pilot, lambda: len(gateway.mutations) == len(gateway.rows))
        flagged = {ref for kind, ref, state in gateway.mutations if state is True}
        assert flagged == {item.reference for item in gateway.rows}
        await wait_for(pilot, lambda: app._status_activity.endswith("Flagged · Ctrl+Z undo"))
        before = len(gateway.mutations)
        await pilot.press("ctrl+z")
        await wait_for(pilot, lambda: len(gateway.mutations) == before + len(gateway.rows))
        restored = {ref: state for kind, ref, state in gateway.mutations[before:]}
        assert restored == {item.reference: item.flagged for item in gateway.rows}

def test_reply_history_is_split_off_but_forwards_are_kept():
    reply = "Thanks, see you then.\n\nOn Mon, 16 Nov 2026 Dr. Farouk wrote:\n> Lab at 10."
    assert split_quoted(reply) == (
        "Thanks, see you then.",
        "On Mon, 16 Nov 2026 Dr. Farouk wrote:\n> Lab at 10.",
    )
    outlook = "Sure.\n________________\nFrom: Registrar\nSent: Monday\nSubject: Forms"
    assert split_quoted(outlook)[0] == "Sure."
    forward = "FYI\n---------- Forwarded message ---------\nFrom: IT\nDate: Mon"
    assert split_quoted(forward) == (forward, "")
    assert split_quoted("> only quotes\n> here") == ("> only quotes\n> here", "")
    assert split_quoted("No history at all.") == ("No history at all.", "")


def test_signature_is_hidden_with_the_quoted_history():
    body = (
        "Thanks, see you Monday.\n\n-- \nMona Adel\nTeaching assistant\n\n"
        "On Sun, Mona wrote:\n> hi"
    )
    fresh, hidden = split_quoted(body)
    assert fresh == "Thanks, see you Monday."
    assert hidden.startswith("-- \nMona Adel")
    assert hidden.endswith("> hi")


def test_a_double_dash_inside_long_text_is_not_a_signature():
    body = "Plan\n--\n" + "\n".join(f"line {n}" for n in range(30))
    assert split_quoted(body) == (body, "")
