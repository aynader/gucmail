"""A caught child bootstrap failure must remain visible to the outer audit gate."""

import json

from qa.offline.guard import Guard


def test_guard_aggregates_owned_child_events_and_excludes_earlier_runs(tmp_path, monkeypatch):
    guard = Guard(tmp_path / "private", tmp_path / "reports")
    monkeypatch.setenv("GUCMAIL_AUDIT_RUN_ID", "current-synthetic-run")
    expected = {
        "run_id": "current-synthetic-run",
        "expected": True,
        "operation": "intentional-parent-denial",
    }
    unexpected = {
        "run_id": "current-synthetic-run",
        "expected": False,
        "operation": "child-profile-rejected",
    }
    stale = {"run_id": "previous-run", "expected": False, "operation": "old-event"}
    (guard.private / "guard-1001.jsonl").write_text(json.dumps(expected) + "\n")
    (guard.private / "guard-1002.jsonl").write_text(
        json.dumps(stale) + "\n" + json.dumps(unexpected) + "\n"
    )
    events = guard.run_events()
    assert len(events) == 2 and unexpected in events and expected in events
    assert sum(not event["expected"] for event in events) == 1
