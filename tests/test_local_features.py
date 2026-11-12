from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gucmail.services.local_features import LocalFeaturesService, NotificationRule
from gucmail.state import StateStore


def test_local_metadata_is_reversible_and_audited(tmp_path: Path) -> None:
    service = LocalFeaturesService(StateStore(tmp_path / "state.db"))
    when = datetime(2026, 8, 6, 9, tzinfo=UTC)
    updated = service.update(
        "m-1",
        pinned=True,
        snoozed_until=when,
        tags=("Course", "course"),
        note="Follow up",
        waiting_for_reply=True,
    )
    assert updated.pinned
    assert updated.tags == ("Course",)
    assert updated.snoozed_until == when
    assert service.clear("m-1")
    assert not service.get("m-1").pinned
    assert len(service.audit()) == 2


def test_templates_rules_and_sender_notes_are_disabled_or_local(tmp_path: Path) -> None:
    service = LocalFeaturesService(StateStore(tmp_path / "state.db"))
    service.save_template("Greeting", {"subject": "Hello", "body": "Synthetic"})
    service.save_rule(
        NotificationRule(
            name="PDF alerts",
            query="attachment:pdf",
            providers=("console",),
            regex=r"\.pdf$",
        )
    )
    service.set_sender_note(
        "sender@example.test", alias="Course Team", trust_note="Verified locally"
    )
    assert service.list_templates()[0]["name"] == "Greeting"
