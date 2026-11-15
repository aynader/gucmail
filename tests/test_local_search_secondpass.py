from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input, Static

from gucmail.errors import ConfigurationError
from gucmail.services.backup import BackupItem, BackupService
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.local_search import LocalSearchMode, LocalSearchService
from gucmail.tui.local_search_screen import LocalSearchScreen, ServiceLocalSearchBackend
from gucmail.tui.palette_routes import CATALOG_ROUTES, RouteKind, RouteTarget
from waiting import rounds, wait_until


def _service(path: Path) -> LocalSearchService:
    index = SearchIndex(path / "index.sqlite3")
    digest = "a" * 64
    index.upsert_many(
        (
            IndexDocument(
                "mail-one",
                subject="Synthetic enrollment reminder",
                sender="registrar@example.test",
                body="Please submit the synthetic enrollment form before Friday.",
                folder_path="Inbox/University",
                size=2_048,
                payload={"content_sha256": digest},
            ),
            IndexDocument(
                "mail-two",
                subject="Re: Synthetic enrollment reminder",
                sender="registrar@example.test",
                body="Please submit the synthetic enrollment form before Friday.",
                folder_path="Archive/University",
                size=2_100,
                payload={"content_sha256": digest},
            ),
            IndexDocument(
                "mail-near",
                subject="Synthetic enrollment reminder updated",
                sender="registrar@example.test",
                body="Please submit the synthetic enrollment form before Thursday.",
                folder_path="Inbox/University",
                size=2_000,
            ),
            IndexDocument(
                "mail-other",
                subject="Synthetic shopping receipt",
                sender="shop@example.test",
                body="Fictional purchase confirmation.",
                folder_path="Inbox/Receipts",
            ),
        )
    )
    backups = BackupService(path / "backups")
    backups.create(
        (
            BackupItem(
                "backup-message",
                "message",
                {
                    "subject": "Archived enrollment schedule",
                    "body_text": "Synthetic enrollment appointments are archived here.",
                },
                folder_path="Inbox/Old",
            ),
            BackupItem(
                "backup-task",
                "task",
                {
                    "title": "Submit enrollment evidence",
                    "notes": "This synthetic task is available without Exchange.",
                },
                folder_path="Tasks",
            ),
        ),
        backup_id="synthetic-federated",
    )
    return LocalSearchService(index, backups)


def test_local_service_federates_index_and_backup_with_truthful_sources(tmp_path: Path) -> None:
    report = _service(tmp_path).search("enrollment", mode=LocalSearchMode.ALL)

    assert report.searched_sources == ("local-index", "backup")
    assert {result.source for result in report.results} >= {
        "local-index",
        "backup · synthetic-federated",
    }
    assert {result.kind for result in report.results} >= {"indexed message", "task"}
    assert report.scanned_count == 6
    assert all(result.explanation for result in report.results)


def test_backup_search_can_be_scoped_to_the_selected_archive(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.backups.create(
        (
            BackupItem(
                "other-message",
                "message",
                {"subject": "Other enrollment archive", "body_text": "enrollment"},
            ),
        ),
        backup_id="other-archive",
    )

    report = service.search(
        "enrollment",
        mode=LocalSearchMode.BACKUPS,
        backup_id="synthetic-federated",
    )

    assert report.searched_sources == ("backup:synthetic-federated",)
    assert report.results
    assert {result.backup_id for result in report.results} == {"synthetic-federated"}
    assert all("other-archive" not in result.source for result in report.results)

    missing = service.search(
        "enrollment",
        mode=LocalSearchMode.BACKUPS,
        backup_id="missing-archive",
    )
    assert missing.results == ()
    assert "not available" in missing.notices[0]


def test_similarity_and_duplicate_modes_are_bounded_read_only_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)

    similar = service.search(
        "",
        mode=LocalSearchMode.SIMILAR,
        reference="mail-one",
        minimum_score=65,
    )
    assert similar.results[0].reference in {"mail-two", "mail-near"}
    assert all("proof" not in result.explanation.casefold() for result in similar.results)

    exact = service.search("", mode=LocalSearchMode.DUPLICATES)
    assert exact.results
    assert exact.results[0].kind == "exact duplicate group"
    assert exact.results[0].member_references == ("mail-one", "mail-two")
    assert "No cleanup action" in exact.notices[1]

    near = service.search(
        "",
        mode=LocalSearchMode.NEAR_DUPLICATES,
        minimum_score=70,
    )
    assert near.scanned_count == 4
    assert all(result.kind == "near-duplicate group" for result in near.results)


def test_optional_semantic_route_executes_private_status_without_transmission(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    report = service.search("student funding", mode=LocalSearchMode.SEMANTIC)

    assert report.results == ()
    assert report.searched_sources == ()
    assert "optional and disabled" in report.notices[0]
    assert "Settings" in report.notices[0]


def test_attachment_only_and_exact_phrase_modes_do_not_match_unrelated_mail_text(
    tmp_path: Path,
) -> None:
    index = SearchIndex(tmp_path / "attachment-index.sqlite3")
    index.upsert_many(
        (
            IndexDocument(
                "attachment-hit",
                subject="Synthetic file",
                sender="registrar@example.test",
                body="The ordinary body does not contain the target wording.",
                attachment_names="registration-form.pdf",
                attachment_text="student enrollment confirmation code",
                received_at=datetime(2026, 8, 20, tzinfo=UTC),
                size=3_000,
            ),
            IndexDocument(
                "body-only",
                subject="Enrollment confirmation code",
                body="student code enrollment confirmation",
            ),
        )
    )
    service = LocalSearchService(index, BackupService(tmp_path / "empty-backups"))

    attachments = service.search(
        "student enrollment confirmation",
        mode=LocalSearchMode.ATTACHMENTS,
        exact=True,
    )
    assert [result.reference for result in attachments.results] == ["attachment-hit"]
    assert attachments.results[0].kind == "attachment match"
    assert attachments.searched_sources == ("local-index",)

    exact = service.search(
        "student enrollment confirmation",
        mode=LocalSearchMode.INDEX,
        exact=True,
    )
    assert [result.reference for result in exact.results] == ["attachment-hit"]

    filtered = service.search(
        "",
        mode=LocalSearchMode.ATTACHMENTS,
        sender="registrar",
        after=datetime(2026, 8, 1, tzinfo=UTC),
        before=datetime(2026, 9, 1, tzinfo=UTC),
        minimum_size=2_000,
        maximum_size=4_000,
        attachment_extension=".pdf",
    )
    assert [result.reference for result in filtered.results] == ["attachment-hit"]

    with pytest.raises(ConfigurationError, match="fuzzy matching or exact phrase"):
        service.search("student", mode=LocalSearchMode.INDEX, fuzzy=True, exact=True)


class SearchHarness(App[None]):
    def __init__(self, screen: LocalSearchScreen) -> None:
        super().__init__()
        self.search_screen = screen

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(self.search_screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((120, 35), (78, 26)))
async def test_local_search_screen_runs_and_reflows_at_wide_and_narrow_sizes(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    screen = LocalSearchScreen(
        ServiceLocalSearchBackend(_service(tmp_path)),
        initial_mode=LocalSearchMode.ALL,
        initial_query="enrollment",
        auto_run=True,
    )
    app = SearchHarness(screen)

    async with app.run_test(size=size) as pilot:
        await wait_until(
            pilot,
            lambda: screen.query_one("#local-search-table", DataTable).row_count >= 3,
            interval=0.025,
            pause_first=True,
        )

        assert screen.query_one("#local-search-table", DataTable).row_count >= 3
        assert "Sources: local-index, backup" in str(
            screen.query_one("#local-search-source", Static).content
        )
        if size[0] < 92:
            assert screen.has_class("narrow")
        else:
            assert not screen.has_class("narrow")

        screen.query_one("#local-search-query", Input).value = "shopping"
        screen.configure(mode=LocalSearchMode.INDEX, run=True)
        for _ in rounds(60):
            await pilot.pause(0.025)
            table = screen.query_one("#local-search-table", DataTable)
            if table.row_count == 1:
                break
        assert screen.query_one("#local-search-table", DataTable).row_count == 1


def test_all_former_search_gates_route_to_real_local_screen_actions() -> None:
    command_ids = {
        "offline.search_offline_mail",
        "search.duplicate_message_search",
        "search.near_duplicate_search",
        "search.search_all_data",
        "search.search_backups",
        "search.semantic_search",
        "search.similar_message_search",
    }

    for command_id in command_ids:
        route = CATALOG_ROUTES[command_id]
        assert route.kind is RouteKind.SCREEN_ACTION
        assert route.target is RouteTarget.LOCAL_SEARCH
        assert route.action == "search"
        assert route.filter_values()["mode"]
    assert not any(route.is_gate for route in CATALOG_ROUTES.values())
