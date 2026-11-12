"""Export real mounted continuation screens using only synthetic data and local services.

SVGs record Textual's rendered terminal cells, not desktop/native-window validation.
No credentials, EWS connection, notification delivery or OS tasks are used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Event
from uuid import uuid4

from textual.app import App
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.errors import ErrorContext
from gucmail.models import Address, CalendarEvent, DraftData, MessageSummary
from gucmail.services.automatic_replies import AutomaticReplyService, AutomaticReplySettings
from gucmail.services.contact_groups import ContactGroupService, LocalContactGroups
from gucmail.services.inbox_rules import (
    InboxRule,
    InboxRuleService,
    InboxRuleVerificationError,
)
from gucmail.services.message_columns import columns_value
from gucmail.settings import SettingsService
from gucmail.state import StateStore
from gucmail.tui.app import ComposeScreen, ConfirmationScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen as AreaConfirmationScreen
from gucmail.tui.area_common import ViewState
from gucmail.tui.automatic_replies_screen import AutomaticRepliesScreen
from gucmail.tui.contact_groups_screen import ContactGroupsScreen
from gucmail.tui.inbox_rules_screen import InboxRulesScreen
from gucmail.tui.layout_presets_screen import LayoutPresetsScreen
from gucmail.tui.message_columns_preview import MessageColumnsPreviewScreen
from gucmail.tui.message_columns_screen import MessageColumnsScreen
from gucmail.tui.notification_settings_screen import RuleEditorScreen
from gucmail.tui.pim_screens import CalendarScopeScreen
from gucmail.tui.settings_screen import SettingsScreen
from gucmail.tui.write_coordinator import app_write_coordinator


class SyntheticPolicy:
    def list_inbox_rules(self) -> tuple[InboxRule, ...]:
        return (
            InboxRule(
                "synthetic-rule-1",
                "Synthetic course notices",
                0,
                False,
                False,
                False,
                {"contains_subject_strings": ["Lecture", "Tutorial"]},
                {},
                {"assign_categories": ["Synthetic course"]},
                "synthetic-preview-1",
            ),
        )

    def set_inbox_rule_enabled(self, rule: InboxRule, enabled: bool) -> InboxRule:
        raise InboxRuleVerificationError(
            "Synthetic accepted-write readback failure. Refresh the policy before another change.",
            context=ErrorContext(retry_safe=False, mutation_uncertain=True),
        )

    def get_automatic_replies(self) -> AutomaticReplySettings:
        return AutomaticReplySettings(internal_reply="Synthetic reply for demonstration only.")


class CaptureApp(App[None]):
    TITLE = "GUC Mail CLI · synthetic continuation demo"

    def __init__(self, screen: Screen[object]) -> None:
        super().__init__()
        self.initial = screen

    def on_mount(self) -> None:
        self.push_screen(self.initial)


class SyntheticWriteGateway:
    """Hold exactly one synthetic flag request for a cross-screen demonstration."""

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def flag(self, reference: str, *, flagged: bool) -> None:
        if reference != "synthetic-pending" or not flagged:
            raise RuntimeError("Unexpected synthetic mutation scope.")
        self.started.set()
        if not self.release.wait(60):
            raise RuntimeError("Synthetic held request was not released.")


@lru_cache(maxsize=1)
def capture_source_fingerprint() -> str:
    """Sample shipped source at the first capture; capture runs freeze product edits."""
    root = Path(__file__).resolve().parents[1] / "src/gucmail"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def save_capture(
    app: App[None], output: Path, name: str, width: int, height: int, rows: list[dict[str, object]]
) -> None:
    filename = f"{name}-{width}x{height}.svg"
    path = Path(app.save_screenshot(filename=filename, path=str(output)))
    value = path.read_text(encoding="utf-8")
    # Rich's optional online font declarations must not enter offline artifacts.
    value = re.sub(r"@font-face\s*\{.*?\}", "", value, flags=re.DOTALL)
    path.write_text(value, encoding="utf-8")
    rows.append(
        {
            "file": filename,
            "size": [width, height],
            "state": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "method": "Mounted Textual run_test terminal-cell SVG export",
            "data": "synthetic only",
            "native_display_verified": False,
            "mount_host": type(app).__name__,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "source_fingerprint_at_first_capture": capture_source_fingerprint(),
        }
    )


def synthetic_context(root: Path) -> AppContext:
    paths = AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config/config.toml",
        state_database=root / "data/state.sqlite",
        recovery_dir=root / "data/recovery",
        index_database=root / "data/index.sqlite",
        backup_dir=root / "data/backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="synthetic@example.test")
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database, account_key="synthetic-demo"),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


async def capture(output: Path, state_path: Path) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=True)
    # ConfigStore.save preserves existing Settings. A new isolated scenario root
    # keeps a rerun from treating a previous demonstration as an existing preset.
    scenario_root = state_path.parent / ("capture-session-" + uuid4().hex)
    state = StateStore(state_path, account_key="synthetic-demo")
    groups = ContactGroupService(LocalContactGroups(state))
    if not groups.list():
        groups.apply(
            groups.plan(
                "Synthetic study group",
                (
                    Address("synthetic.one@example.test", "Synthetic One"),
                    Address("synthetic.two@example.test", "Synthetic Two"),
                ),
            ),
            confirmed=True,
        )
    now = datetime(2026, 10, 2, 9, tzinfo=UTC)
    occurrence = CalendarEvent(
        "synthetic-occurrence",
        "Synthetic weekly tutorial",
        now,
        now + timedelta(hours=1),
        "UTC",
        item_type="Occurrence",
        recurrence={"pattern": "weekly"},
    )
    policy = SyntheticPolicy()
    rows: list[dict[str, object]] = []
    for width, height in ((120, 35), (80, 24), (70, 20)):
        for name, screen in (
            ("calendar-scope", CalendarScopeScreen(occurrence, operation="Edit event")),
            (
                "notification-builder",
                RuleEditorScreen(
                    None, next_order=0, provider_id="desktop", initial_name="Synthetic alert rule"
                ),
            ),
            ("automatic-replies", AutomaticRepliesScreen(AutomaticReplyService(policy))),
            ("inbox-rules", InboxRulesScreen(InboxRuleService(policy))),
            ("contact-groups", ContactGroupsScreen(groups)),
            ("message-columns", MessageColumnsScreen(columns_value())),
        ):
            app = CaptureApp(screen)
            async with app.run_test(size=(width, height)) as pilot:
                for _ in range(50):
                    await pilot.pause(0.05)
                    if not getattr(screen, "_operation_active", False):
                        break
                await pilot.pause()

                def save(
                    suffix: str = "",
                    *,
                    name: str = name,
                    width: int = width,
                    height: int = height,
                    app: CaptureApp = app,
                ) -> None:
                    save_capture(app, output, name + suffix, width, height, rows)

                save()
                if name == "notification-builder":
                    screen.query_one("#rule-builder").scroll_visible(animate=False)
                    await pilot.pause()
                    save("-guided")
                if name == "contact-groups":
                    screen.query_one("#group-create", Button).press()
                    await pilot.pause()
                    app.screen.query_one("#group-name", Input).value = "Synthetic new group"
                    app.screen.query_one("#group-members", TextArea).load_text(
                        "synthetic.one@example.test\nsynthetic.two@example.test"
                    )
                    save("-editor")
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    save("-confirmation")
                    await pilot.press("escape")
                elif name == "inbox-rules":
                    screen.action_toggle_rule()
                    await pilot.pause()
                    save("-confirmation")
                    await pilot.press("y")
                    for _ in range(50):
                        await pilot.pause(0.05)
                        if not screen._operation_active:
                            break
                    save("-uncertain-write")
        context = synthetic_context(scenario_root / f"columns-demo-{width}x{height}")
        mail = GUCmailApp(context, offline=True)
        mail.title = "GUC Mail CLI · synthetic column preview"
        async with mail.run_test(size=(width, height)) as pilot:
            mail._render_message_rows(
                (
                    MessageSummary(
                        "synthetic-first",
                        "Lecture notes · literal [red] label",
                        Address("synthetic.one@example.test", "Synthetic One"),
                        now,
                        has_attachments=True,
                    ),
                    MessageSummary(
                        "synthetic-second",
                        "Synthetic tutorial schedule",
                        Address("synthetic.two@example.test", "Synthetic Two"),
                        now,
                    ),
                )
            )
            value = columns_value()
            value[2]["width"] = 36
            value[5]["visible"] = True
            value[5]["compact"] = True
            revert = mail._preview_message_columns(value)
            await mail.push_screen(
                MessageColumnsPreviewScreen(
                    mail.query_one("#message-table", DataTable), "synthetic-second"
                )
            )
            await pilot.pause()
            save_capture(mail, output, "message-column-preview", width, height, rows)
            revert()
        context = synthetic_context(scenario_root / f"header-demo-{width}x{height}")
        session = SettingsService(context.config_store).begin()
        session.set("layout.header_compactness", "compact" if width < 85 else "detailed")
        session.set("layout.status_bar_contents", "activity,connection,folder,monitoring")
        session.apply()
        mail = GUCmailApp(context, offline=True)
        mail.title = "GUC Mail CLI · synthetic header preferences"
        async with mail.run_test(size=(width, height)) as pilot:
            mail._render_messages(
                (MessageSummary("synthetic-header", "Synthetic selected row", None, None),)
            )
            mail._render_status(
                {"exchange_version": "Synthetic", "inbox_count": 1},
                quota_label="Send threshold 92 MiB (Known GUC Fallback) · usage unknown",
            )
            mail._set_status("Synthetic display preview")
            await pilot.pause()
            save_capture(mail, output, "header-preferences", width, height, rows)
        context = synthetic_context(scenario_root / f"layouts-demo-{width}x{height}")
        mail = GUCmailApp(context, offline=True)
        mail.title = "GUC Mail CLI · synthetic saved layouts"
        async with mail.run_test(size=(width, height)) as pilot:
            mail._sidebar_ratio = 0.30
            mail._preview_visible = False
            mail.action_layout_presets()
            for _ in range(100):
                await pilot.pause(0.02)
                if isinstance(mail.screen, LayoutPresetsScreen) and mail.screen.catalog is not None:
                    break
            if not isinstance(mail.screen, LayoutPresetsScreen) or mail.screen.catalog is None:
                raise RuntimeError("Synthetic layouts did not finish loading.")
            screen = mail.screen
            screen.query_one("#layout-presets-name", Input).value = "Synthetic Reading"
            screen.query_one("#layout-presets-save", Button).focus()
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if (
                    screen.catalog is not None
                    and "Synthetic Reading" in screen.catalog.presets
                    and not screen._operation_active
                ):
                    break
            if screen.catalog is None or "Synthetic Reading" not in screen.catalog.presets:
                raise RuntimeError("Synthetic layout save failed.")
            await pilot.pause()
            save_capture(mail, output, "layout-presets-saved", width, height, rows)
            save_button = screen.query_one("#layout-presets-save", Button)
            for _ in range(100):
                if not save_button.has_class("-active"):
                    break
                await pilot.pause(0.02)
            save_button.focus()
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if isinstance(mail.screen, AreaConfirmationScreen) and mail.screen.is_mounted:
                    break
            if not isinstance(mail.screen, AreaConfirmationScreen):
                raise RuntimeError("Synthetic layout replacement review did not open.")
            await pilot.pause()
            save_capture(mail, output, "layout-presets-replace-confirmation", width, height, rows)
            mail.screen.dismiss(False)
            await pilot.pause()
        context = synthetic_context(scenario_root / f"sort-demo-{width}x{height}")
        mail = GUCmailApp(context, offline=True)
        mail.title = "GUC Mail CLI · synthetic saved message order"
        async with mail.run_test(size=(width, height)) as pilot:
            mail._open_area("settings")
            for _ in range(100):
                await pilot.pause(0.02)
                if isinstance(mail.screen, SettingsScreen) and mail.screen._catalog is not None:
                    break
            if not isinstance(mail.screen, SettingsScreen) or mail.screen._catalog is None:
                raise RuntimeError("Synthetic sort Settings did not load.")
            screen = mail.screen
            for key, value in (
                ("mail.sort_field", "subject"),
                ("mail.sort_direction", "ascending"),
            ):
                if not screen.action_select_setting(key):
                    raise RuntimeError("Synthetic sort setting is unavailable.")
                await pilot.pause()
                screen.query_one("#settings-choice", Select).value = value
                screen.query_one("#settings-stage", Button).press()
                for _ in range(500):
                    await pilot.pause(0.02)
                    if (
                        key in screen._staged
                        and not screen._operation_active
                        and screen.view_state is ViewState.READY
                    ):
                        break
                if (
                    key not in screen._staged
                    or screen._operation_active
                    or screen.view_state is not ViewState.READY
                ):
                    raise RuntimeError("Synthetic sort setting failed to stage.")
            screen.action_apply()
            for _ in range(500):
                await pilot.pause(0.02)
                if (
                    mail._mail_sort_order == "subject"
                    and not screen._operation_active
                    and screen.view_state is ViewState.READY
                ):
                    break
            if mail._mail_sort_order != "subject":
                state_note = screen.query_one("#area-state").render()
                raise RuntimeError(f"Synthetic sort preference failed to apply: {state_note}")
            await pilot.pause()
            save_capture(mail, output, "mail-sort-settings-applied", width, height, rows)
            screen.query_one("#settings-apply", Button).scroll_visible(
                animate=False, immediate=True
            )
            await pilot.pause()
            save_capture(mail, output, "mail-sort-settings-actions", width, height, rows)
        context = synthetic_context(scenario_root / f"composer-demo-{width}x{height}")
        session = SettingsService(context.config_store).begin()
        for setting, value in {
            "composer.default_body_format": "html",
            "composer.default_importance": "high",
            "composer.request_read_receipt": True,
            "composer.signature_enabled": True,
            "composer.signature": "Synthetic signature <safe>",
            "composer.empty_subject_policy": "warn",
            "composer.recipient_warning_threshold": 2,
        }.items():
            session.set(setting, value)
        session.apply()
        mail = GUCmailApp(context, offline=True)
        mail.title = "GUC Mail CLI · synthetic composer defaults"
        async with mail.run_test(size=(width, height)) as pilot:
            mail._open_compose(
                DraftData(
                    "preview:synthetic-composer",
                    to=[Address("synthetic.one@example.test")],
                    bcc=[Address("synthetic.two@example.test")],
                    body_text="Please find attached the synthetic schedule.\n\n"
                    "--- Original message ---\nSynthetic quoted history.",
                )
            )
            await pilot.pause()
            mail.screen.query_one("#compose-options").scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            save_capture(mail, output, "composer-defaults", width, height, rows)
            mail.screen.query_one("#compose-send", Button).press()
            for _ in range(100):
                if isinstance(mail.screen, ConfirmationScreen):
                    break
                await pilot.pause(0.02)
            if not isinstance(mail.screen, ConfirmationScreen):
                raise RuntimeError("Synthetic composer failed to reach its real send review.")
            await pilot.pause()
            save_capture(mail, output, "composer-send-confirmation", width, height, rows)
            mail.screen.query_one("#confirm", Button).scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            save_capture(mail, output, "composer-send-confirmation-actions", width, height, rows)
            mail.screen.dismiss(False)
            await pilot.pause()
        context = synthetic_context(scenario_root / f"shared-write-demo-{width}x{height}")
        gateway = SyntheticWriteGateway()
        mail = GUCmailApp(context, gateway=gateway, offline=True)
        mail.title = "GUC Mail CLI · synthetic shared write guard"
        async with mail.run_test(size=(width, height)) as pilot:
            try:
                mail._set_message_flag("synthetic-pending", flagged=True)
                for _ in range(500):
                    if gateway.started.is_set():
                        break
                    await asyncio.sleep(0.02)
                if not gateway.started.is_set():
                    raise RuntimeError("Synthetic held request failed to start.")
                rules = InboxRulesScreen(InboxRuleService(policy))
                await mail.push_screen(rules)
                for _ in range(500):
                    if rules.view_state is ViewState.READY and rules.selected:
                        break
                    await pilot.pause(0.02)
                if not rules.selected or rules.view_state is not ViewState.READY:
                    raise RuntimeError("Synthetic rule read failed to finish.")
                rules.action_toggle_rule()
                await pilot.pause()
                if mail.screen is not rules or "still running" not in str(
                    rules.query_one("#area-state", Static).content
                ):
                    raise RuntimeError("Shared write guard failed to reject the rule change.")
                save_capture(mail, output, "shared-write-rule-blocked", width, height, rows)
                await mail.pop_screen()
                composer = ComposeScreen(
                    DraftData(
                        "preview:synthetic-pending-composer",
                        to=[Address("synthetic.one@example.test")],
                        subject="Synthetic unsent draft",
                        body_text="Keep this synthetic composed text open.",
                    )
                )
                await mail.push_screen(composer)
                composer.query_one("#compose-send", Button).press()
                await pilot.pause()
                note = composer.query_one("#compose-note", Static)
                if mail.screen is not composer or "composer remains open" not in str(note.content):
                    raise RuntimeError("Shared write guard failed to preserve the composer.")
                note.scroll_visible(animate=False, immediate=True)
                await pilot.pause()
                save_capture(mail, output, "shared-write-composer-retained", width, height, rows)
            finally:
                gateway.release.set()
            for _ in range(500):
                if not app_write_coordinator(mail).pending:
                    break
                await asyncio.sleep(0.02)
            if app_write_coordinator(mail).pending:
                raise RuntimeError("Synthetic held write did not release its app guard.")
            await mail.workers.wait_for_complete()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("final-demo/screenshots"))
    parser.add_argument(
        "--state", type=Path, default=Path(".tmp-continuation-20261001/demo-state.sqlite")
    )
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(capture(args.output.resolve(), args.state.resolve()))
    (args.output / "manifest.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(rows)} synthetic mounted-screen SVGs.")


if __name__ == "__main__":
    main()
