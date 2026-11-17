"""Mailbox usage comes from summed folder sizes and is cached."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import exchangelib.folders as ews_folders
import pytest

from gucmail.config import AppConfig
from gucmail.ews.adapter import _FOLDER_SIZE_FIELD, EWSGateway
from gucmail.models import QuotaSource
from gucmail.services.quota import compact_quota_status, quota_service_from_settings

MIB = 1024 * 1024


class FakeCollection:
    calls: ClassVar[list[str]] = []

    def __init__(self, *, account: Any, folders: list[Any]) -> None:
        self.folders = folders

    def find_folders(self, *, depth: str) -> list[Any]:
        FakeCollection.calls.append("find")
        assert depth == "Deep"
        return [inbox, deleted, RuntimeError("access denied")]

    def get_folders(self, *, additional_fields: Any) -> list[Any]:
        FakeCollection.calls.append("get")
        assert len(additional_fields) == 1
        return list(self.folders)


root = SimpleNamespace(**{_FOLDER_SIZE_FIELD: 1 * MIB})
inbox = SimpleNamespace(**{_FOLDER_SIZE_FIELD: 70 * MIB})


class _Deleted(ews_folders.DeletedItems):
    def __init__(self) -> None:  # skip exchangelib field setup
        pass


deleted = _Deleted()
object.__setattr__(deleted, _FOLDER_SIZE_FIELD, 3 * MIB)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> EWSGateway:
    FakeCollection.calls = []
    monkeypatch.setattr(ews_folders, "FolderCollection", FakeCollection)
    account = SimpleNamespace(msg_folder_root=root)
    return EWSGateway(AppConfig(), account_factory=lambda: account)


def test_usage_is_the_sum_of_folder_sizes(gateway: EWSGateway) -> None:
    quota = gateway.get_quota()
    assert quota is not None
    assert quota.used_bytes == 74 * MIB
    assert quota.deleted_bytes == 3 * MIB
    assert quota.usage_source is QuotaSource.SERVER_REPORTED


def test_usage_is_cached_between_refreshes(gateway: EWSGateway) -> None:
    gateway.get_quota()
    gateway.get_quota()
    assert FakeCollection.calls == ["find", "get"]


def test_header_shows_usage_against_the_send_limit(gateway: EWSGateway) -> None:
    defaults = {"storage.prohibit_send_source": "known_guc_fallback"}
    report = quota_service_from_settings(
        gateway, lambda setting_id, fallback: defaults.get(setting_id, fallback)
    ).report()
    assert compact_quota_status(report) == "Mailbox 74 MiB of 92 MiB (80%)"
