from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest
from textual.app import App
from textual.widgets import Button, Static

from gucmail.config import AppPaths, ConfigStore
from gucmail.services.quota import (
    MEBIBYTE,
    QuotaService,
    quota_service_from_settings,
    render_quota_report,
)
from gucmail.settings import InMemoryRevisionStore, SettingsService
from gucmail.tui.area_backends import StorageAreaBackend
from gucmail.tui.storage_screen import StorageScreen, storage_battery
from waiting import wait_until


class _NoServerQuota:
    def get_quota(self) -> None:
        return None


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config" / "config.toml",
        state_database=root / "data" / "state.sqlite3",
        recovery_dir=root / "data" / "recovery",
        index_database=root / "data" / "index.sqlite3",
        backup_dir=root / "data" / "backups",
    )


def test_unknown_capacity_stays_unknown_even_with_legacy_constructor_value() -> None:
    # Secondpass section 22 supersedes the earlier generic 100 MiB UI example.
    for legacy_capacity in (None, 100 * MEBIBYTE, 128 * MEBIBYTE):
        report = QuotaService(_NoServerQuota(), fallback_capacity_bytes=legacy_capacity).report()
        assert report.mailbox is None
        assert report.effective_capacity.value_bytes is None
        assert report.capacity_is_fallback is False
        assert "fallback capacity" not in render_quota_report(report, monochrome=True)


def test_legacy_capacity_migrates_without_inventing_a_limit(tmp_path: Path) -> None:
    import tomllib

    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    original = (
        'schema_version = 4\n[gucmail]\nemail = "student@example.test"\n'
        '[settings]\n"storage.fallback_capacity_mb" = 128.0\n'
        '"storage.prohibit_send_mb" = 92.0\n'
        '"storage.prohibit_send_source" = "known_guc_fallback"\n'
    )
    paths.config_file.write_text(original, encoding="utf-8")
    store = ConfigStore(paths)
    first = store.load_document()
    assert first.schema_version == 5
    migrated = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    assert "storage.fallback_capacity_mb" not in migrated["settings"]
    assert migrated["migration_metadata"]["legacy_fallback_capacity_mb"] == 128.0
    assert any(
        p.read_text(encoding="utf-8") == original
        for p in paths.config_dir.iterdir()
        if p.is_file() and p != paths.config_file
    )
    second = SettingsService(store, revisions=InMemoryRevisionStore())
    service = quota_service_from_settings(
        _NoServerQuota(),
        lambda setting_id, fallback: second.value(setting_id).effective_value,
    )
    report = service.report()
    assert report.effective_capacity.value_bytes is None
    assert report.mailbox is not None
    assert report.mailbox.prohibit_send_bytes == 92 * MEBIBYTE
    assert report.mailbox.prohibit_send_receive_bytes is None
    assert report.mailbox.issue_warning_bytes is None
    assert report.send_threshold_percent is None


def test_storage_backend_adds_a_bounded_visible_item_usage_lower_bound(tmp_path: Path) -> None:
    messages = (
        {"reference": "m1", "size_bytes": 7 * MEBIBYTE},
        {"reference": "m2", "size_bytes": 13 * MEBIBYTE},
    )
    backend = StorageAreaBackend(
        quota_factory=lambda: QuotaService(_NoServerQuota()),
        message_loader=lambda _analysis, _limit: messages,
        local_paths={"Cache": tmp_path / "cache"},
        export_directory=tmp_path / "exports",
        maximum_scan_items=50,
    )

    snapshot = backend.load_storage(cancel=Event(), progress=lambda _progress: None)

    assert snapshot.usage_bytes == 20 * MEBIBYTE
    assert snapshot.usage_source == "Estimate from visible messages"
    assert snapshot.usage_confidence == "Low (estimate)"
    assert snapshot.usage_sample_count == 2
    assert snapshot.usage_sample_limit == 50
    assert snapshot.total_capacity_bytes is None
    assert snapshot.total_capacity_is_fallback is False


def test_offline_storage_exposes_unknown_capacity_and_local_sizes(
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "cache" / "entry.bin"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"synthetic")
    backend = StorageAreaBackend(
        quota_factory=lambda: QuotaService(_NoServerQuota()),
        message_loader=None,
        local_paths={"Cache": cache_file.parent},
        export_directory=tmp_path / "exports",
        online=False,
    )

    snapshot = backend.load_storage(cancel=Event(), progress=lambda _progress: None)

    assert snapshot.total_capacity_bytes is None
    assert snapshot.total_capacity_is_fallback is False
    assert snapshot.server_available is False
    assert snapshot.overview_rows[0].label == "Cache"
    assert snapshot.overview_rows[0].size_bytes == len(b"synthetic")


def test_storage_battery_is_stable_at_empty_partial_full_and_over_capacity() -> None:
    assert storage_battery(0, 100, width=10) == "[░░░░░░░░░░]"
    assert storage_battery(50, 100, width=10) == "[█████░░░░░]"
    assert storage_battery(100, 100, width=10) == "[██████████]"
    assert storage_battery(150, 100, width=10) == "[██████████]"


@pytest.mark.asyncio
async def test_storage_fallback_controls_remain_visible_in_a_narrow_terminal(
    tmp_path: Path,
) -> None:
    backend = StorageAreaBackend(
        quota_factory=lambda: QuotaService(_NoServerQuota()),
        message_loader=lambda _analysis, _limit: (
            {"reference": "m1", "size_bytes": 42 * MEBIBYTE},
        ),
        local_paths={"Cache": tmp_path / "cache"},
        export_directory=tmp_path / "exports",
        maximum_scan_items=50,
    )
    app = App[None]()

    async with app.run_test(size=(79, 26)) as pilot:
        screen = StorageScreen(backend)
        await app.push_screen(screen)
        await pilot.pause(0.3)

        assert screen.has_class("compact-storage")
        for selector in ("#storage-analyze", "#storage-refresh", ".area-close"):
            button = screen.query_one(selector, Button)
            assert button.display
            assert button.region.bottom <= 26

        screen.queue_snapshot_focus("quota_thresholds")
        await wait_until(
            pilot,
            lambda: screen._queued_snapshot_focus is None,
            interval=0.02,
            pause_first=True,
        )
        detail = str(screen.query_one("#storage-detail", Static).render())
        assert "Server-reported total capacity: Unknown" in detail

        screen.queue_snapshot_focus("mailbox_usage")
        await wait_until(
            pilot,
            lambda: screen._queued_snapshot_focus is None,
            interval=0.02,
            pause_first=True,
        )
        detail = str(screen.query_one("#storage-detail", Static).render())
        assert "42.0 MiB" in detail
        assert "Estimate from visible messages" in detail

        screen.queue_snapshot_focus("remaining_space")
        await wait_until(
            pilot,
            lambda: screen._queued_snapshot_focus is None,
            interval=0.02,
            pause_first=True,
        )
        detail = str(screen.query_one("#storage-detail", Static).render())
        assert "Remaining mailbox capacity: Unknown" in detail
        assert "42.0%" not in detail
        assert "58.0 MiB" not in detail
