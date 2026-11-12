from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from gucmail.bootstrap import AppContext
from gucmail.commands import CommandInvocation
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.errors import ConflictError
from gucmail.models import Address, MessageSummary
from gucmail.services.index import SearchIndex
from gucmail.state import StateStore
from gucmail.tui.app import AreaScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.diagnostics_screen import DiagnosticsScreen, LocalDiagnosticsBackend
from gucmail.tui.palette_routes import RouteKind, catalog_route


def _context(tmp_path: Path) -> AppContext:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.sqlite3",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.sqlite3",
        backup_dir=tmp_path / "data" / "backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="private-person@example.test")
    store.save(config)
    paths.ensure()
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


class MetadataGateway:
    def __init__(self) -> None:
        self.reset_count = 0

    def status(self) -> dict[str, object]:
        return {"exchange_version": "Synthetic", "inbox_count": 2}

    def reset_session(self) -> None:
        self.reset_count += 1

    def get_quota(self) -> None:
        return None

    def list_messages(
        self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
    ) -> tuple[MessageSummary, ...]:
        return (
            MessageSummary(
                reference=f"{folder}-one",
                subject="One",
                sender=Address("one@example.test"),
                received_at=datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
            ),
            MessageSummary(
                reference=f"{folder}-two",
                subject="Two",
                sender=Address("two@example.test"),
                received_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
            ),
        )[:limit]


def test_diagnostics_backend_exact_cache_repair_index_rebuild_and_redacted_export(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    context.state.put_reference("local-one", "message", "remote-one")
    SearchIndex(context.paths.index_database).upsert(
        MessageSummary(
            reference="old-index-row",
            subject="Old",
            sender=None,
            received_at=None,
        )
    )
    closed: list[bool] = []
    backend = LocalDiagnosticsBackend(
        context,
        gateway_factory=MetadataGateway,
        close_runtime=lambda: closed.append(True),
    )
    cancel = Event()

    def progress(_value: object) -> None:
        return None

    cache_plan = backend.plan_cache_repair(cancel=cancel, progress=progress)
    assert cache_plan.total_rows >= 2
    with pytest.raises(Exception, match="confirmation"):
        backend.repair_cache(
            cache_plan,
            confirmation_scope="wrong",
            cancel=cancel,
            progress=progress,
        )
    repaired = backend.repair_cache(
        cache_plan,
        confirmation_scope=cache_plan.confirmation_scope,
        cancel=cancel,
        progress=progress,
    )
    assert closed == [True]
    assert repaired.integrity == "ok"
    assert not any(context.state.disposable_cache_counts().values())

    index_plan = backend.plan_index_rebuild("inbox", 25, cancel=cancel, progress=progress)
    assert index_plan.existing_documents == 1
    rebuilt = backend.rebuild_index(
        index_plan,
        confirmation_scope=index_plan.confirmation_scope,
        cancel=cancel,
        progress=progress,
    )
    assert (rebuilt.removed, rebuilt.indexed, rebuilt.integrity) == (1, 2, "ok")
    assert SearchIndex(context.paths.index_database).verify().valid

    destination = tmp_path / "diagnostics.json"
    exported = backend.export_bundle(str(destination), cancel=cancel, progress=progress)
    payload = exported.read_text(encoding="utf-8")
    assert "private-person@example.test" not in payload
    assert "no credentials, message bodies, or attachment data" in payload.casefold()
    with pytest.raises(ConflictError):
        backend.export_bundle(str(destination), cancel=cancel, progress=progress)


def test_diagnostics_backend_executes_exact_read_only_views(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.state.put_reference("local-one", "message", "remote-one")
    backend = LocalDiagnosticsBackend(context, gateway_factory=MetadataGateway)
    cancel = Event()

    def progress(_value: object) -> None:
        return None

    connection = backend.snapshot(view="connection_status", cancel=cancel, progress=progress)
    assert connection.title == "Read-only connection status"
    assert "Exchange version: Synthetic" in connection.details
    assert "This read does not modify mailbox items." in connection.details

    capabilities = backend.snapshot(view="ews_capability_matrix", cancel=cancel, progress=progress)
    assert capabilities.title == "EWS operation and capability matrix"
    assert any("items / GetItem" in line for line in capabilities.details)
    assert any("server=not_probed" in line for line in capabilities.details)

    cache = backend.snapshot(view="cache_health", cancel=cancel, progress=progress)
    assert cache.title == "Disposable cache health"
    assert any("item_references:" in line for line in cache.details)

    database = backend.snapshot(view="database_integrity_check", cancel=cancel, progress=progress)
    assert database.title == "Local database integrity"
    assert "State integrity: ok" in database.details
    assert "Index valid: True" in database.details


@pytest.mark.asyncio
async def test_mounted_diagnostics_actions_preview_and_execute(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.state.put_reference("local-one", "message", "remote-one")
    SearchIndex(context.paths.index_database).upsert(MessageSummary("old", "Old", None, None))
    app = GUCmailApp(context, gateway=MetadataGateway())

    async with app.run_test(size=(100, 30)) as pilot:
        app._open_area("diagnostics")
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                isinstance(app.screen, DiagnosticsScreen) and app.screen.is_mounted
            ) and app.screen.view_state in {
                ViewState.READY,
                ViewState.ERROR,
            }:
                break
        assert isinstance(app.screen, DiagnosticsScreen)
        screen = app.screen

        screen.action_repair_cache()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ConfirmationScreen)
        assert "item_references" in app.screen.detail_text
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                not any(context.state.disposable_cache_counts().values())
                and screen.view_state is ViewState.READY
                and not screen._operation_active
                and not screen.mutation_pending
            ):
                break
        assert not any(context.state.disposable_cache_counts().values())
        assert screen.view_state is ViewState.READY and not screen.mutation_pending
        assert isinstance(app.screen, DiagnosticsScreen)
        screen = app.screen

        screen.query_one("#diagnostics-index-limit").value = "25"  # type: ignore[attr-defined]
        screen.action_rebuild_index()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ConfirmationScreen)
        assert "Bounded metadata limit: 25" in app.screen.detail_text
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                SearchIndex(context.paths.index_database).stats().documents == 2
                and screen.view_state is ViewState.READY
                and not screen._operation_active
                and not screen.mutation_pending
            ):
                break
        assert SearchIndex(context.paths.index_database).stats().documents == 2
        assert screen.view_state is ViewState.READY and not screen.mutation_pending
        assert isinstance(app.screen, DiagnosticsScreen)

        destination = tmp_path / "mounted-diagnostics.json"
        app.screen.query_one("#diagnostics-export-path").value = str(  # type: ignore[attr-defined]
            destination
        )
        app.screen.action_export_bundle()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ConfirmationScreen)
        assert "no credentials" in app.screen.detail_text.casefold()
        app.screen.dismiss(True)
        for _ in range(30):
            if destination.is_file() and screen.view_state is ViewState.READY:
                break
            await pilot.pause(0.05)
        assert destination.is_file()


@pytest.mark.asyncio
async def test_diagnostics_palette_executes_views_reconnect_and_bounded_log_browser(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    log_path = context.paths.log_dir / "gucmail.log"
    log_path.write_text(
        "INFO connected private-person@example.test\nAuthorization: Bearer should-not-escape\n",
        encoding="utf-8",
    )
    gateway = MetadataGateway()
    app = GUCmailApp(context, gateway=gateway)
    handlers = app._palette_handlers()

    async def wait_for_view(pilot: Pilot[None], view: str) -> DiagnosticsScreen:
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, DiagnosticsScreen)
                and app.screen._view == view
                and app.screen._snapshot is not None
                and app.screen._snapshot.view == view
            ):
                return app.screen
        assert isinstance(app.screen, DiagnosticsScreen)
        return app.screen

    async with app.run_test(size=(120, 35)) as pilot:
        handlers["diagnostics.doctor"](
            CommandInvocation("diagnostics.doctor"), app._palette_context()
        )
        overview = await wait_for_view(pilot, "overview")
        assert overview._snapshot is not None and overview._snapshot.checks

        handlers["diagnostics.connection"](
            CommandInvocation("diagnostics.connection"), app._palette_context()
        )
        connection = await wait_for_view(pilot, "connection_status")
        assert connection._snapshot is not None
        assert "Exchange version: Synthetic" in connection._snapshot.details

        handlers["diagnostics.ews_capabilities"](
            CommandInvocation("diagnostics.ews_capabilities"), app._palette_context()
        )
        capabilities = await wait_for_view(pilot, "ews_capability_matrix")
        assert capabilities._snapshot is not None
        assert any("GetItem" in line for line in capabilities._snapshot.details)

        for command_id, view in (
            ("diagnostics.show_server_version", "connection_status"),
            ("diagnostics.show_ews_operations", "ews_capability_matrix"),
            ("diagnostics.show_cache_health", "cache_health"),
            ("diagnostics.verify_local_database", "database_integrity_check"),
        ):
            app._palette_catalog_handler(CommandInvocation(command_id), app._palette_context())
            await wait_for_view(pilot, view)

        handlers["diagnostics.logs"](CommandInvocation("diagnostics.logs"), app._palette_context())
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, AreaScreen):
                content = str(app.screen.query_one("#area-content", Static).render())
                if "Bounded sanitized files" in content:
                    break
        assert isinstance(app.screen, AreaScreen)
        content = str(app.screen.query_one("#area-content", Static).render())
        assert "Bounded sanitized files" in content
        assert "private-person@example.test" not in content
        assert "<redacted>@example.test" in content
        assert "should-not-escape" not in content
        app.screen.dismiss(None)
        await pilot.pause()

        reconnect = catalog_route("diagnostics.reconnect")
        app._palette_catalog_handler(
            CommandInvocation(reconnect.command_id), app._palette_context()
        )
        assert gateway.reset_count == 1


@pytest.mark.parametrize(
    "command_id",
    (
        "diagnostics.repair_cache",
        "diagnostics.rebuild_index",
        "diagnostics.export_sanitized_diagnostics",
    ),
)
def test_diagnostics_palette_routes_are_real_screen_actions(command_id: str) -> None:
    route = catalog_route(command_id)
    assert route.kind is RouteKind.SCREEN_ACTION
    assert route.target.value == "diagnostics"
    assert not route.is_gate
    assert CommandInvocation(route.command_id).command_id == command_id
