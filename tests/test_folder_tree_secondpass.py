from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static, Tree
from typer.testing import CliRunner

import gucmail.cli as cli_module
from gucmail.bootstrap import AppContext
from gucmail.cli import app as cli_app
from gucmail.command_catalog import build_core_command_registry
from gucmail.commands import CommandInvocation
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.models import FolderInfo
from gucmail.state import StateStore
from gucmail.tui.app import (
    ConfirmationScreen,
    FolderDetailsScreen,
    FolderNameScreen,
    GUCmailApp,
)
from gucmail.tui.palette_routes import catalog_route
from gucmail.tui.storage_screen import (
    QuotaThresholdView,
    StorageAnalysisView,
    StorageScreen,
    StorageSnapshotView,
    StorageUsageRow,
)
from waiting import rounds


def _context(tmp_path: Path, gateway: object | None = None) -> AppContext:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.db",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.db",
        backup_dir=tmp_path / "data" / "backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="folders@example.test")
    store.save(config)
    context = AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )
    if gateway is not None:
        context._gateway = gateway  # type: ignore[assignment]
    return context


class FolderTreeGateway:
    def __init__(self) -> None:
        self.folders: dict[str, FolderInfo] = {
            "inbox": FolderInfo(
                "inbox",
                "Posteingang",
                "/inbox",
                None,
                total_count=11,
                unread_count=3,
                child_count=1,
                folder_class="IPF.Note",
                distinguished_role="inbox",
                ews_id="ews-inbox",
                change_key="ck-inbox",
            ),
            "sent": FolderInfo(
                "sent",
                "Gesendet",
                "/sentitems",
                None,
                total_count=7,
                unread_count=0,
                child_count=0,
                distinguished_role="sentitems",
            ),
            "projects": FolderInfo(
                "projects",
                "Projekte",
                "/Projekte",
                None,
                total_count=5,
                unread_count=2,
                child_count=2,
                folder_class="IPF.Note",
            ),
            "private": FolderInfo(
                "private",
                "Privat",
                "/Privat",
                None,
                total_count=1,
                unread_count=1,
                child_count=1,
            ),
            "receipts": FolderInfo(
                "receipts",
                "Belege",
                "/inbox/Belege",
                "inbox",
                total_count=4,
                unread_count=1,
                child_count=0,
            ),
            "unicode": FolderInfo(
                "unicode",
                "研究/2026",
                "/Projekte/研究\\/2026",
                "projects",
                total_count=2,
                unread_count=1,
                child_count=1,
            ),
            "project-archive": FolderInfo(
                "project-archive",
                "Archiv",
                "/Projekte/Archiv",
                "projects",
                total_count=3,
                unread_count=0,
                child_count=0,
            ),
            "private-archive": FolderInfo(
                "private-archive",
                "Archiv",
                "/Privat/Archiv",
                "private",
                total_count=6,
                unread_count=2,
                child_count=0,
            ),
            "deep": FolderInfo(
                "deep",
                "مرحلة",
                "/Projekte/研究\\/2026/مرحلة",
                "unicode",
                total_count=0,
                unread_count=0,
                child_count=0,
            ),
        }
        self.list_calls: list[tuple[str | None, int, int]] = []
        self.created: list[tuple[str | None, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.moved: list[tuple[str, str | None]] = []
        self.deleted: list[str] = []
        self.emptied: list[str] = []
        self._next_reference = 1

    def status(self) -> dict[str, object]:
        return {"exchange_version": "Synthetic", "inbox_count": 11, "inbox_unread": 3}

    def get_quota(self) -> None:
        return None

    def list_messages(
        self, *, folder: str, limit: int, offset: int = 0, order_by: str = "-datetime_received"
    ) -> tuple[object, ...]:
        assert folder
        assert limit > 0
        return ()

    def list_folders(
        self,
        parent_reference: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[FolderInfo, ...]:
        self.list_calls.append((parent_reference, limit, offset))
        children = tuple(
            folder
            for folder in self.folders.values()
            if folder.parent_reference == parent_reference
        )
        return children[offset : offset + limit]

    def get_folder(self, reference: str) -> FolderInfo:
        return self.folders[reference]

    def create_folder(self, parent_reference: str | None, name: str) -> FolderInfo:
        self.created.append((parent_reference, name))
        reference = f"created-{self._next_reference}"
        self._next_reference += 1
        parent_path = self.folders[parent_reference].path if parent_reference else ""
        escaped_name = name.replace("\\", "\\\\").replace("/", "\\/")
        folder = FolderInfo(
            reference,
            name,
            f"{parent_path}/{escaped_name}" if parent_path else f"/{escaped_name}",
            parent_reference,
            total_count=0,
            unread_count=0,
            child_count=0,
            folder_class="IPF.Note",
        )
        self.folders[reference] = folder
        if parent_reference:
            parent = self.folders[parent_reference]
            self.folders[parent_reference] = replace(
                parent,
                child_count=(parent.child_count or 0) + 1,
            )
        return folder

    def rename_folder(self, reference: str, new_name: str) -> FolderInfo:
        self.renamed.append((reference, new_name))
        current = self.folders[reference]
        parent_path = (
            self.folders[current.parent_reference].path if current.parent_reference else ""
        )
        escaped_name = new_name.replace("\\", "\\\\").replace("/", "\\/")
        updated = replace(
            current,
            name=new_name,
            path=f"{parent_path}/{escaped_name}" if parent_path else f"/{escaped_name}",
        )
        self.folders[reference] = updated
        return updated

    def move_folder(
        self,
        reference: str,
        destination_reference: str | None,
    ) -> FolderInfo:
        self.moved.append((reference, destination_reference))
        current = self.folders[reference]
        parent_path = self.folders[destination_reference].path if destination_reference else ""
        updated = replace(
            current,
            parent_reference=destination_reference,
            path=f"{parent_path}/{current.name}" if parent_path else f"/{current.name}",
        )
        self.folders[reference] = updated
        return updated

    def delete_folder(self, reference: str, permanent: bool = False) -> None:
        assert not permanent
        self.deleted.append(reference)
        self.folders.pop(reference)

    def empty_folder(self, reference: str, permanent: bool = False) -> None:
        assert not permanent
        self.emptied.append(reference)
        self.folders[reference] = replace(
            self.folders[reference],
            total_count=0,
            unread_count=0,
        )


class FolderStorageBackend:
    def load_storage(self, **_kwargs: object) -> StorageSnapshotView:
        return StorageSnapshotView(
            None,
            "unavailable",
            QuotaThresholdView("issue_warning", "Issue warning", None, "unknown"),
            QuotaThresholdView("prohibit_send", "Prohibit send", None, "unknown"),
            QuotaThresholdView("prohibit_send_receive", "Prohibit send/receive", None, "unknown"),
            None,
            None,
            server_available=False,
        )

    def analyze_storage(
        self,
        analysis_id: str,
        **_kwargs: object,
    ) -> StorageAnalysisView:
        assert analysis_id == "folders"
        return StorageAnalysisView(
            "folders",
            "Storage by folder",
            (
                StorageUsageRow("folder:0", "/inbox", "folder", 100, 2),
                StorageUsageRow("folder:1", "/Projekte/研究\\/2026", "folder", 250, 4),
            ),
        )


async def _wait_for(
    pilot: Any,
    predicate: Callable[[], bool],
    *,
    attempts: int = 80,
) -> None:
    for _ in rounds(attempts):
        if predicate():
            await pilot.pause()
            return
        await pilot.pause(0.02)
    assert predicate()


_FOLDER_WORK = {"folder-load", "folder-mutation", "folder-move"}


def _folder_shown(app: GUCmailApp, reference: str) -> bool:
    """The folder is in the tree and no folder change or reload is still running.

    Right after a change the tree still shows the old layout; the reload that follows
    replaces those nodes, so a node found too early can vanish before it is selected.
    """
    busy = any(worker.is_running and worker.group in _FOLDER_WORK for worker in app.workers)
    return not busy and not app._folder_refresh_in_progress and reference in app._folder_nodes


def _select(app: GUCmailApp, reference: str) -> None:
    tree = app.query_one("#folder-tree", Tree)
    tree.select_node(tree.get_node_by_id(app._folder_nodes[reference]))


@pytest.mark.asyncio
async def test_textual_tree_is_lazy_nested_localized_counted_and_refresh_stable(
    tmp_path: Path,
) -> None:
    gateway = FolderTreeGateway()
    context = _context(tmp_path, gateway)
    app = GUCmailApp(context, gateway=gateway)

    async with app.run_test(size=(120, 36)) as pilot:
        await _wait_for(
            pilot,
            lambda: {"inbox", "sent", "projects", "private"} <= set(app._folder_nodes),
        )
        assert gateway.list_calls[0] == (None, 25, 0)
        assert not any(
            parent in {"projects", "private", "unicode"} for parent, _, _ in gateway.list_calls
        )
        assert "unicode" not in app._folder_nodes

        tree = app.query_one("#folder-tree", Tree)
        projects = tree.get_node_by_id(app._folder_nodes["projects"])
        assert str(projects.label).endswith("  2")
        projects.expand()
        await _wait_for(pilot, lambda: "unicode" in app._folder_nodes)
        assert ("projects", 25, 0) in gateway.list_calls
        assert "unicode" in app._folder_nodes and "deep" not in app._folder_nodes

        private = tree.get_node_by_id(app._folder_nodes["private"])
        private.expand()
        await _wait_for(pilot, lambda: "private-archive" in app._folder_nodes)
        labels = [
            str(tree.get_node_by_id(app._folder_nodes[key]).label)
            for key in (
                "project-archive",
                "private-archive",
            )
        ]
        assert all("Archiv" in label for label in labels)

        unicode_node = tree.get_node_by_id(app._folder_nodes["unicode"])
        assert "研究/2026" in str(unicode_node.label)
        unicode_node.expand()
        await _wait_for(pilot, lambda: "deep" in app._folder_nodes)
        assert "مرحلة" in str(tree.get_node_by_id(app._folder_nodes["deep"]).label)
        assert {"projects", "private", "unicode"} <= app._expanded_folders

        app._palette_handlers()["folders.open"](
            CommandInvocation.from_mapping("folders.open", {"folder": "projects"}),
            app._palette_context(),
        )
        await _wait_for(pilot, lambda: app.ui.folder == "projects")

        _select(app, "unicode")
        await pilot.pause()
        old_node_id = app._folder_nodes["unicode"]
        app._palette_catalog_handler(
            CommandInvocation("folders.refresh_folder_tree"),
            app._palette_context(),
        )
        await _wait_for(
            pilot,
            lambda: "unicode" in app._folder_nodes and app._folder_nodes["unicode"] != old_node_id,
        )
        await pilot.pause()
        assert tree.cursor_node is not None
        assert tree.cursor_node.data["reference"] == "unicode"
        assert app.ui.folder == "unicode"
        assert {"projects", "private", "unicode"} <= app._expanded_folders

        app._catalog_direct(
            catalog_route("folders.show_folder_details"),
            CommandInvocation("folders.show_folder_details"),
        )
        await pilot.pause()
        assert isinstance(app.screen, FolderDetailsScreen)
        details = str(app.screen.query_one("#folder-details-content", Static).content)
        assert "Path: /Projekte/研究\\/2026" in details
        assert "Total items: 2" in details
        await pilot.press("escape")

        app._catalog_direct(
            catalog_route("folders.show_folder_unread_count"),
            CommandInvocation("folders.show_folder_unread_count"),
        )
        assert "Unread 1 · Total 2" in str(app.query_one("#status-bar", Static).content)

        app._palette_catalog_handler(
            CommandInvocation("folders.show_folder_storage"),
            app._palette_context(),
        )
        await _wait_for(pilot, lambda: isinstance(app.screen, StorageScreen))
        assert isinstance(app.screen, StorageScreen)
        await pilot.press("escape")

    restarted = GUCmailApp(context, gateway=gateway)
    assert restarted.ui.folder == "unicode"
    assert {"projects", "unicode"} <= restarted._expanded_folders


@pytest.mark.asyncio
async def test_folder_storage_palette_focuses_the_selected_folder_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FolderTreeGateway()
    app = GUCmailApp(_context(tmp_path, gateway), gateway=gateway)
    screen = StorageScreen(FolderStorageBackend())  # type: ignore[arg-type]

    async with app.run_test(size=(120, 36)) as pilot:
        await _wait_for(pilot, lambda: "projects" in app._folder_nodes)
        monkeypatch.setattr(app, "_selected_folder_info", lambda: gateway.folders["unicode"])
        app.push_screen(screen)
        await _wait_for(pilot, lambda: screen.is_mounted)

        app._palette_catalog_handler(
            CommandInvocation("folders.show_folder_storage"),
            app._palette_context(),
        )
        await _wait_for(pilot, lambda: screen._analysis is not None)

        assert screen._selected_row == "folder:1"
        assert screen.query_one("#storage-analysis-table").cursor_row == 1
        detail = str(screen.query_one("#storage-detail", Static).content)
        assert "/Projekte/研究\\/2026" in detail


def test_folder_storage_focus_falls_back_when_leaf_name_is_ambiguous() -> None:
    rows = (
        StorageUsageRow("a", "/Projekte/Archiv", "folder", 1),
        StorageUsageRow("b", "/Privat/Archiv", "folder", 1),
    )

    assert StorageScreen._focused_row_index(rows, "/Unknown/Archiv") == 0


@pytest.mark.asyncio
async def test_textual_folder_mutations_use_prompts_and_scoped_confirmations(
    tmp_path: Path,
) -> None:
    gateway = FolderTreeGateway()
    context = _context(tmp_path, gateway)
    app = GUCmailApp(context, gateway=gateway)

    async with app.run_test(size=(120, 36)) as pilot:
        await _wait_for(pilot, lambda: _folder_shown(app, "projects"))
        _select(app, "projects")
        await pilot.pause()

        app._palette_handlers()["folders.create"](
            CommandInvocation.from_mapping("folders.create", {"folder": "Neue Ablage"}),
            app._palette_context(),
        )
        await pilot.pause()
        assert isinstance(app.screen, FolderNameScreen)
        assert app.screen.query_one("#folder-name-input", Input).value == "Neue Ablage"
        assert await pilot.click("#folder-name-submit")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert gateway.created == []
        assert await pilot.click("#confirm")
        await _wait_for(pilot, lambda: bool(gateway.created))
        assert gateway.created == [("projects", "Neue Ablage")]
        await _wait_for(pilot, lambda: _folder_shown(app, "created-1"))
        await pilot.pause()

        _select(app, "created-1")
        app._catalog_direct(
            catalog_route("folders.rename_folder"),
            CommandInvocation("folders.rename_folder"),
        )
        await pilot.pause()
        assert isinstance(app.screen, FolderNameScreen)
        app.screen.query_one("#folder-name-input", Input).value = "Umbenannt"
        assert await pilot.click("#folder-name-submit")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert await pilot.click("#confirm")
        await _wait_for(pilot, lambda: gateway.renamed == [("created-1", "Umbenannt")])
        await _wait_for(pilot, lambda: _folder_shown(app, "created-1"))

        _select(app, "created-1")
        app._catalog_direct(
            catalog_route("folders.empty_folder"),
            CommandInvocation("folders.empty_folder"),
        )
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert "every item" in app.screen.detail.casefold()
        assert await pilot.click("#cancel")
        await pilot.pause()
        assert gateway.emptied == []

        _select(app, "created-1")
        app._catalog_direct(
            catalog_route("folders.empty_folder"),
            CommandInvocation("folders.empty_folder"),
        )
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert await pilot.click("#confirm")
        await _wait_for(pilot, lambda: gateway.emptied == ["created-1"])

        await _wait_for(pilot, lambda: _folder_shown(app, "created-1"))
        _select(app, "created-1")
        app._catalog_direct(
            catalog_route("folders.move_folder"),
            CommandInvocation.from_mapping("folders.move_folder", {"folder": "id:private"}),
        )
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert await pilot.click("#confirm")
        await _wait_for(pilot, lambda: gateway.moved == [("created-1", "private")])

        await _wait_for(pilot, lambda: _folder_shown(app, "private"))
        tree = app.query_one("#folder-tree", Tree)
        tree.get_node_by_id(app._folder_nodes["private"]).expand()
        await _wait_for(pilot, lambda: _folder_shown(app, "created-1"))
        _select(app, "created-1")
        app._catalog_direct(
            catalog_route("folders.delete_folder"),
            CommandInvocation("folders.delete_folder"),
        )
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert gateway.deleted == []
        assert await pilot.click("#confirm")
        await _wait_for(pilot, lambda: gateway.deleted == ["created-1"])
        assert app.ui.folder == context.config.default_folder


@pytest.mark.asyncio
async def test_textual_expand_all_cascades_through_lazy_nodes_and_collapse_all(
    tmp_path: Path,
) -> None:
    gateway = FolderTreeGateway()
    context = _context(tmp_path, gateway)
    app = GUCmailApp(context, gateway=gateway)

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_for(pilot, lambda: "projects" in app._folder_nodes)
        assert "deep" not in app._folder_nodes

        _select(app, "projects")
        app._catalog_direct(
            catalog_route("folders.expand_folder_tree"),
            CommandInvocation("folders.expand_folder_tree"),
        )
        await _wait_for(pilot, lambda: "unicode" in app._folder_nodes)
        app._catalog_direct(
            catalog_route("folders.collapse_folder_tree"),
            CommandInvocation("folders.collapse_folder_tree"),
        )
        await _wait_for(pilot, lambda: "projects" not in app._expanded_folders)

        app._catalog_direct(
            catalog_route("folders.expand_all_folders"),
            CommandInvocation("folders.expand_all_folders"),
        )
        await _wait_for(pilot, lambda: "deep" in app._folder_nodes)
        assert {"inbox", "projects", "private", "unicode"} <= app._expanded_folders

        app._catalog_direct(
            catalog_route("folders.collapse_all_folders"),
            CommandInvocation("folders.collapse_all_folders"),
        )
        await _wait_for(pilot, lambda: not app._expanded_folders)


def test_every_folder_palette_command_has_an_executable_handler(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    handlers = app._palette_handlers()
    commands = tuple(
        command for command in build_core_command_registry().all() if command.category == "folders"
    )

    assert {command.command_id for command in commands} == {
        "folders.open",
        "folders.create",
        "folders.rename_folder",
        "folders.move_folder",
        "folders.delete_folder",
        "folders.empty_folder",
        "folders.show_folder_details",
        "folders.show_folder_storage",
        "folders.show_folder_unread_count",
        "folders.expand_folder_tree",
        "folders.collapse_folder_tree",
        "folders.expand_all_folders",
        "folders.collapse_all_folders",
        "folders.refresh_folder_tree",
    }
    assert all(command.handler_id in handlers for command in commands)


@pytest.mark.asyncio
async def test_textual_expand_all_consumes_paginated_sibling_pages(tmp_path: Path) -> None:
    gateway = FolderTreeGateway()
    gateway.folders = {
        f"folder-{index}": FolderInfo(
            f"folder-{index}",
            f"Folder {index}",
            f"/Folder {index}",
            None,
            child_count=0,
        )
        for index in range(125)
    }
    context = _context(tmp_path, gateway)
    app = GUCmailApp(context, gateway=gateway)

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_for(pilot, lambda: "folder-0" in app._folder_nodes)
        app._catalog_direct(
            catalog_route("folders.expand_all_folders"),
            CommandInvocation("folders.expand_all_folders"),
        )
        await _wait_for(pilot, lambda: "folder-124" in app._folder_nodes)

        tree = app.query_one("#folder-tree", Tree)
        root = tree.get_node_by_id(app._folder_root_node_id)
        assert all(
            not (isinstance(child.data, dict) and child.data.get("kind") == "folder_more")
            for child in root.children
        )
        assert any(offset > 0 for _parent, _limit, offset in gateway.list_calls)


@pytest.mark.asyncio
async def test_reconnect_restores_nested_selection_and_expanded_state(tmp_path: Path) -> None:
    gateway = FolderTreeGateway()
    context = _context(tmp_path, gateway)
    app = GUCmailApp(context, gateway=gateway)

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_for(pilot, lambda: "projects" in app._folder_nodes)
        tree = app.query_one("#folder-tree", Tree)
        tree.get_node_by_id(app._folder_nodes["projects"]).expand()
        await _wait_for(pilot, lambda: "unicode" in app._folder_nodes)
        tree.get_node_by_id(app._folder_nodes["unicode"]).expand()
        await _wait_for(pilot, lambda: "deep" in app._folder_nodes)
        _select(app, "unicode")
        await _wait_for(pilot, lambda: app.ui.folder == "unicode")
        previous_node = app._folder_nodes["unicode"]

        app.action_reconnect()
        await _wait_for(
            pilot,
            lambda: (
                "unicode" in app._folder_nodes
                and app._folder_nodes["unicode"] != previous_node
                and app._folder_selection_to_restore is None
            ),
        )

        assert tree.cursor_node is not None
        assert tree.cursor_node.data["reference"] == "unicode"
        assert {"projects", "unicode"} <= app._expanded_folders


def test_cli_tree_paginates_all_root_children_up_to_the_explicit_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folders = tuple(
        FolderInfo(f"folder-{index}", f"Folder {index}", f"/Folder {index}", None, child_count=0)
        for index in range(501)
    )
    calls: list[tuple[int, int]] = []

    class Gateway:
        def list_folders(
            self,
            parent_reference: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> tuple[FolderInfo, ...]:
            assert parent_reference is None
            calls.append((limit, offset))
            return folders[offset : offset + limit]

    monkeypatch.setattr(cli_module.core, "gateway", lambda _ctx: Gateway())
    paths = _context(tmp_path).paths
    environment = {
        "GUCMAIL_CONFIG_DIR": str(paths.config_dir),
        "GUCMAIL_DATA_DIR": str(paths.data_dir),
        "GUCMAIL_CACHE_DIR": str(paths.cache_dir),
        "GUCMAIL_LOG_DIR": str(paths.log_dir),
    }
    result = CliRunner().invoke(
        cli_app,
        ["--json", "folders", "tree", "--depth", "1", "--limit", "600"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["data"]) == 501
    assert calls == [(200, 0), (200, 200), (200, 400)]
