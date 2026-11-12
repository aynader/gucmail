from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    DraftData,
    FolderInfo,
    Importance,
    MessageDetail,
    MessageSummary,
    QuotaInfo,
    SearchQuery,
)
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.compose import ComposeKind, ComposeService, ComposeValidationError
from gucmail.services.folders import FolderService, format_folder_path, parse_folder_path
from gucmail.services.messages import MessageService
from gucmail.services.quota import QuotaService, QuotaStatus, render_quota_report


class FolderFake:
    def __init__(self) -> None:
        self.folders = (
            FolderInfo("inbox-id", "Posteingang", "Posteingang", None, distinguished_role="inbox"),
            FolderInfo("projects-id", "Projects/2026", "Projects/2026", None),
            FolderInfo("child-id", "Receipts", "Posteingang/Receipts", "inbox-id"),
            FolderInfo("archive-id", "Archive", "Archive", None),
        )
        self.calls: list[tuple[Any, ...]] = []

    def list_folders(
        self, parent_reference: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[FolderInfo, ...]:
        self.calls.append(("list", parent_reference, limit, offset))
        matching = tuple(
            folder for folder in self.folders if folder.parent_reference == parent_reference
        )
        return matching[offset : offset + limit]

    def get_folder(self, reference: str) -> FolderInfo:
        return next(folder for folder in self.folders if folder.reference == reference)

    def create_folder(self, parent_reference: str | None, name: str) -> str:
        self.calls.append(("create", parent_reference, name))
        return "created"

    def rename_folder(self, reference: str, new_name: str) -> str:
        self.calls.append(("rename", reference, new_name))
        return "renamed"

    def move_folder(self, reference: str, destination_reference: str | None) -> str:
        self.calls.append(("move", reference, destination_reference))
        return "moved"

    def delete_folder(self, reference: str, permanent: bool = False) -> str:
        self.calls.append(("delete", reference, permanent))
        return "deleted"

    def empty_folder(self, reference: str, permanent: bool = False) -> str:
        self.calls.append(("empty", reference, permanent))
        return "emptied"


def test_folder_paths_are_escaped_and_traversal_is_rejected() -> None:
    components = parse_folder_path(r"Inbox/Projects\/2026/Back\\slash")
    assert components == ("Inbox", "Projects/2026", r"Back\slash")
    assert format_folder_path(components) == r"Inbox/Projects\/2026/Back\\slash"
    with pytest.raises(ConfigurationError):
        parse_folder_path("Inbox/../Sent")
    with pytest.raises(ConfigurationError):
        parse_folder_path("Inbox//Sent")


def test_folder_resolution_supports_localized_distinguished_role_and_scoped_confirmation() -> None:
    gateway = FolderFake()
    service = FolderService(gateway, resolution_page_size=2)
    folder = service.resolve("inbox/Receipts")
    assert folder.reference == "child-id"
    assert all(call[2] <= 2 for call in gateway.calls if call[0] == "list")

    plan = service.plan_delete("id:child-id", permanent=True)
    before = list(gateway.calls)
    dry_run = service.execute_mutation(plan, dry_run=True)
    assert not dry_run.executed
    assert gateway.calls == before
    with pytest.raises(ConfirmationRequiredError):
        service.execute_mutation(plan, confirmation_scope="delete:wrong")
    result = service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    assert result.executed
    assert gateway.calls[-1] == ("delete", "child-id", True)


def test_folder_move_is_previewed_confirmation_scoped_and_cycle_safe() -> None:
    gateway = FolderFake()
    service = FolderService(gateway)

    plan = service.plan_move("id:child-id", "id:archive-id")
    assert plan.targets == ("child-id",)
    assert plan.parameters["destination_reference"] == "archive-id"
    assert plan.requires_confirmation
    before = list(gateway.calls)
    assert not service.execute_mutation(plan, dry_run=True).executed
    assert gateway.calls == before
    with pytest.raises(ConfirmationRequiredError):
        service.execute_mutation(plan)
    result = service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    assert result.results == ("moved",)
    assert gateway.calls[-1] == ("move", "child-id", "archive-id")

    root_plan = service.plan_move("id:child-id", "/")
    assert root_plan.parameters["destination_reference"] is None
    service.execute_mutation(root_plan, confirmation_scope=root_plan.confirmation_scope)
    assert gateway.calls[-1] == ("move", "child-id", None)

    with pytest.raises(ConfigurationError, match="already below"):
        service.plan_move("id:child-id", "id:inbox-id")
    # Standard (distinguished) folders such as a localized Inbox are never movable.
    with pytest.raises(ConfigurationError, match="standard Exchange folder"):
        service.plan_move("id:inbox-id", "id:child-id")
    # Cycle safety still applies to ordinary folders.
    gateway.folders = (
        *gateway.folders,
        FolderInfo("sub-id", "Sub", "Projects/2026/Sub", "projects-id"),
    )
    with pytest.raises(ConfigurationError, match="descendants"):
        service.plan_move("id:projects-id", "id:sub-id")


def _summary(reference: str) -> MessageSummary:
    return MessageSummary(
        reference=reference,
        subject=f"Subject {reference}",
        sender=Address("sender@example.test"),
        received_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


class MessageFake:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_messages(
        self,
        folder: str = "inbox",
        limit: int = 50,
        offset: int = 0,
        order_by: str = "-datetime_received",
    ) -> tuple[MessageSummary, ...]:
        self.calls.append(("list", folder, limit, offset, order_by))
        # Deliberately over-deliver to prove the service cap is defensive.
        return tuple(_summary(str(number)) for number in range(offset, offset + limit + 3))

    def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail:
        self.calls.append(("show", reference, mark_read))
        return MessageDetail(summary=_summary(reference))

    def search_messages(
        self, query: SearchQuery, limit: int = 50, offset: int = 0
    ) -> tuple[MessageSummary, ...]:
        self.calls.append(("search", query, limit, offset))
        return (_summary("search-result"),)

    def mark_read(self, reference: str, read: bool = True) -> str:
        self.calls.append(("mark", reference, read))
        return reference

    def flag(self, reference: str, flagged: bool = True) -> str:
        self.calls.append(("flag", reference, flagged))
        return reference

    def categorize(self, reference: str, categories: tuple[str, ...], mode: str = "set") -> str:
        self.calls.append(("categorize", reference, categories, mode))
        return reference

    def set_importance(self, reference: str, importance: Importance) -> str:
        self.calls.append(("importance", reference, importance))
        return reference

    def move_message(self, reference: str, destination: str = "trash") -> str:
        self.calls.append(("move", reference, destination))
        return reference

    def copy_message(self, reference: str, destination: str) -> str:
        self.calls.append(("copy", reference, destination))
        return reference

    def delete_message(self, reference: str, permanent: bool = False) -> str:
        self.calls.append(("delete", reference, permanent))
        return reference


def test_message_reads_are_bounded_and_do_not_mark_read_by_default() -> None:
    gateway = MessageFake()
    service = MessageService(gateway)
    page = service.list_messages(limit=3)
    assert len(page.items) == 3
    assert page.next_offset == 3
    service.show_message("one")
    assert gateway.calls[-1] == ("show", "one", False)
    with pytest.raises(ConfigurationError):
        service.list_messages(limit=201)


def test_message_delete_is_previewed_dry_runnable_and_confirmation_scoped() -> None:
    gateway = MessageFake()
    service = MessageService(gateway)
    plan = service.plan_delete(["one", "two", "one"], permanent=True)
    assert plan.targets == ("one", "two")
    assert service.execute_mutation(plan, dry_run=True).results == ()
    assert not gateway.calls
    with pytest.raises(ConfirmationRequiredError):
        service.execute_mutation(plan, confirmation_scope="delete:other-targets")
    result = service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    assert result.results == ("one", "two")
    assert gateway.calls == [("delete", "one", True), ("delete", "two", True)]


def test_message_importance_categories_and_copy_execute_exact_plans() -> None:
    gateway = MessageFake()
    service = MessageService(gateway)

    service.execute_mutation(service.plan_importance(["one"], importance="HIGH"))
    service.execute_mutation(service.plan_categorize(["one"], ["Course"], mode="add"))
    service.execute_mutation(service.plan_categorize(["one"], ["Course"], mode="remove"))
    service.execute_mutation(service.plan_copy(["one"], "Archive"))

    assert gateway.calls == [
        ("importance", "one", Importance.HIGH),
        ("categorize", "one", ("Course",), "add"),
        ("categorize", "one", ("Course",), "remove"),
        ("copy", "one", "Archive"),
    ]
    with pytest.raises(ConfigurationError, match="low, normal, or high"):
        service.plan_importance(["one"], importance="urgent")


class ComposeFake:
    def __init__(self, original: MessageDetail | None = None) -> None:
        self.original = original
        self.calls: list[tuple[Any, ...]] = []

    def show_message(self, reference: str, mark_read: bool = False) -> MessageDetail:
        self.calls.append(("show", reference, mark_read))
        assert self.original is not None
        return self.original

    def send(self, draft: DraftData) -> str:
        self.calls.append(("send", draft))
        return "sent-id"

    def save_draft(self, draft: DraftData) -> str:
        self.calls.append(("save", draft))
        return "draft-id"

    def reply(self, reference: str, draft: DraftData) -> str:
        self.calls.append(("reply", reference, draft))
        return "reply-id"

    def reply_all(self, reference: str, draft: DraftData) -> str:
        self.calls.append(("reply_all", reference, draft))
        return "reply-all-id"

    def forward(self, reference: str, draft: DraftData) -> str:
        self.calls.append(("forward", reference, draft))
        return "forward-id"


def test_compose_validation_deduplicates_recipients_and_protects_empty_content() -> None:
    service = ComposeService(ComposeFake())
    draft = DraftData(
        "draft-1",
        to=[Address("person@example.test")],
        cc=[Address("PERSON@example.test")],
        subject="",
        body_text="Please see the attached report.",
    )
    validation = service.validate(draft)
    assert len(validation.draft.to) == 1
    assert not validation.draft.cc
    assert {issue.code for issue in validation.errors} == {"empty_subject"}
    assert "attachment_mentioned_but_missing" in {issue.code for issue in validation.warnings}
    with pytest.raises(ComposeValidationError):
        service.plan_send(DraftData("empty"))


@pytest.mark.parametrize(
    "recipient",
    ["first@example.test, second@example.test", "first@example.test; second@example.test"],
)
def test_compose_rejects_multiple_addresses_in_one_recipient_field(recipient: str) -> None:
    gateway = ComposeFake()
    service = ComposeService(gateway)
    draft = DraftData(
        "draft-1",
        to=cast(list[Address], [recipient]),
        subject="Report",
        body_text="Hello",
    )

    with pytest.raises(ComposeValidationError, match="exactly one explicit email address"):
        service.plan_send(draft)
    assert not gateway.calls


def test_compose_send_scope_and_changed_attachment_guard(tmp_path: Path) -> None:
    gateway = ComposeFake()
    service = ComposeService(gateway)
    attachment = tmp_path / "report.txt"
    attachment.write_text("first", encoding="utf-8")
    draft = DraftData(
        "draft-1",
        to=[Address("person@example.test")],
        subject="Report",
        body_text="Here it is.",
        attachment_paths=[attachment],
    )
    plan = service.plan_send(draft)
    assert not service.execute_send(plan, dry_run=True).sent
    assert not gateway.calls
    with pytest.raises(ConfirmationRequiredError):
        service.execute_send(plan)

    attachment.write_text("changed", encoding="utf-8")
    with pytest.raises(ConflictError):
        service.execute_send(plan, confirmation_scope=plan.confirmation_scope)
    assert not gateway.calls


def test_reply_all_preview_prefers_reply_to_excludes_self_duplicates_and_bcc() -> None:
    original = MessageDetail(
        summary=replace(_summary("original"), sender=Address("sender@example.test")),
        to=(Address("me@example.test"), Address("team@example.test")),
        cc=(Address("TEAM@example.test"), Address("cc@example.test")),
        bcc=(Address("secret@example.test"),),
        reply_to=(Address("reply@example.test"),),
        body_text="Original body",
    )
    gateway = ComposeFake(original)
    service = ComposeService(gateway)
    preview = service.preview_reply(
        "original", reply_all=True, account_addresses=["ME@example.test"]
    )
    assert preview.kind is ComposeKind.REPLY_ALL
    assert [item.email for item in preview.draft.to] == [
        "reply@example.test",
        "team@example.test",
    ]
    assert [item.email for item in preview.draft.cc] == ["cc@example.test"]
    assert all(item.email != "secret@example.test" for item in preview.draft.to)
    assert gateway.calls == [("show", "original", False)]


class AttachmentFake:
    def __init__(self, content: bytes, *, name: str = "../../payload.txt") -> None:
        self.content = content
        self.info = AttachmentInfo("attachment-1", name, len(content), "text/plain")
        self.open_count = 0

    def list_attachments(
        self, message_reference: str, limit: int = 100, offset: int = 0
    ) -> tuple[AttachmentInfo, ...]:
        return (self.info,)[offset : offset + limit]

    def open_attachment(self, message_reference: str, attachment_reference: str) -> io.BytesIO:
        self.open_count += 1
        return io.BytesIO(self.content)


def test_attachment_destination_is_safe_atomic_hashed_and_reports_progress(tmp_path: Path) -> None:
    content = b"synthetic attachment"
    gateway = AttachmentFake(content)
    service = AttachmentService(gateway, chunk_size=4096)
    progress: list[int] = []
    plan = service.plan_save("message-1", "attachment-1", tmp_path)
    assert plan.target.parent == tmp_path.resolve()
    assert plan.target.name == "payload.txt"
    assert not service.execute_save(plan, dry_run=True).path
    assert gateway.open_count == 0

    result = service.execute_save(
        plan,
        calculate_sha256=True,
        progress=lambda update: progress.append(update.bytes_downloaded),
    )
    assert result.path is not None
    assert result.path.read_bytes() == content
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert progress == [0, len(content)]
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("name", ["NUL.tar.gz", "COM¹.txt"])
def test_attachment_with_windows_device_name_saves_as_regular_file(
    tmp_path: Path, name: str
) -> None:
    service = AttachmentService(AttachmentFake(b"safe content", name=name))
    plan = service.plan_save("message", "attachment-1", tmp_path)
    assert plan.target.name == f"_{name}"
    saved = service.execute_save(plan)
    assert saved.path is not None
    assert saved.path.read_bytes() == b"safe content"


def test_attachment_collision_renames_and_overwrite_requires_scope(tmp_path: Path) -> None:
    gateway = AttachmentFake(b"new", name="file.txt")
    existing = tmp_path / "file.txt"
    existing.write_bytes(b"old")
    service = AttachmentService(gateway)
    renamed = service.plan_save("message", "attachment-1", tmp_path)
    assert renamed.target.name == "file (1).txt"
    overwrite = service.plan_save(
        "message",
        "attachment-1",
        tmp_path,
        collision=CollisionPolicy.OVERWRITE,
    )
    with pytest.raises(ConfirmationRequiredError):
        service.execute_save(overwrite)
    service.execute_save(overwrite, confirmation_scope=overwrite.confirmation_scope)
    assert existing.read_bytes() == b"new"


class QuotaFake:
    def __init__(self, quota: QuotaInfo | None) -> None:
        self.quota = quota

    def get_quota(self) -> QuotaInfo | None:
        return self.quota


def test_quota_exact_unavailable_and_local_disk_are_distinct(tmp_path: Path) -> None:
    exact = QuotaInfo(used_bytes=25, quota_bytes=100, source="ews_exact")
    report = QuotaService(QuotaFake(exact)).report(local_paths={"downloads": tmp_path})
    assert report.status is QuotaStatus.EXACT
    assert report.mailbox == exact
    assert report.local_disks[0].available
    text = render_quota_report(report, monochrome=True)
    assert "Mailbox" in text and "Local disk (downloads)" in text

    unavailable = QuotaService(QuotaFake(None)).report()
    assert unavailable.status is QuotaStatus.UNAVAILABLE
    assert unavailable.mailbox is None
    assert "Quota unavailable" in render_quota_report(unavailable)


def test_approximate_folder_sum_is_never_labeled_as_quota() -> None:
    estimate = QuotaInfo(used_bytes=25, quota_bytes=100, source="folder_sum_estimate")
    report = QuotaService(QuotaFake(estimate)).report()
    assert report.status is QuotaStatus.UNAVAILABLE
    assert report.mailbox is None
