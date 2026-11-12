"""Real services and independent SQL readback for each bulk message operation.

The transport simulates persistence and lost acknowledgments, not Exchange.
"""

from pathlib import Path

import pytest
from qa.synthetic.mailbox import LocalMailbox

from gucmail.errors import ConfigurationError, ErrorContext, MutationExecutionError, NetworkError
from gucmail.models import Importance, MessageSummary
from gucmail.services.messages import MessageService
from gucmail.state import StateStore

OPERATIONS = (
    "mark_read",
    "mark_unread",
    "flag",
    "unflag",
    "categorize",
    "set_importance",
    "move",
    "copy",
    "delete",
    "permanent_delete",
)


class FaultedMailbox:
    def __init__(self, inner, method, position, lost_ack):
        self.inner, self.method = inner, method
        self.position, self.lost_ack, self.calls = position, lost_ack, 0

    def __getattr__(self, name):
        target = getattr(self.inner, name)  # Missing capabilities fail; no real-provider fallback.
        if name != self.method:
            return target

        def write(*args, **kwargs):
            self.calls += 1
            if self.calls == self.position and not self.lost_ack:
                raise ConfigurationError("Synthetic rejected write")
            result = target(*args, **kwargs)
            if self.calls == self.position:
                raise NetworkError(
                    "Synthetic lost acknowledgment",
                    context=ErrorContext(retry_safe=False, mutation_uncertain=True),
                )
            return result

        return write


def plan_for(service, operation, references):
    if operation in {"mark_read", "mark_unread"}:
        return service.plan_mark_read(references, read=operation == "mark_read")
    if operation in {"flag", "unflag"}:
        return service.plan_flag(references, flagged=operation == "flag")
    if operation == "categorize":
        return service.plan_categorize(references, categories=("new",))
    if operation == "set_importance":
        return service.plan_importance(references, importance="high")
    if operation in {"move", "copy"}:
        return getattr(service, "plan_" + operation)(references, "/Archive")
    return service.plan_delete(references, permanent=operation == "permanent_delete")


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("lost_ack", [False, True])
@pytest.mark.parametrize("position", [1, 2, 3])
def test_each_partial_operation_matches_durable_transport_and_journal(
    tmp_path: Path, operation: str, lost_ack: bool, position: int
):
    references = ("first", "second", "third")
    original = [
        MessageSummary(
            ref,
            ref,
            sender=None,
            received_at=None,
            is_read=operation == "mark_unread",
            flagged=operation == "unflag",
            categories=("old",),
            folder_path="/Inbox",
        )
        for ref in references
    ]
    local = LocalMailbox(tmp_path / "mailbox.sqlite")
    local.insert_many("mail", original)
    method = {
        "mark_unread": "mark_read",
        "unflag": "flag",
        "set_importance": "set_importance",
        "move": "move_message",
        "copy": "copy_message",
        "delete": "delete_message",
        "permanent_delete": "delete_message",
    }.get(operation, operation)
    gateway = FaultedMailbox(local, method, position, lost_ack)
    state = StateStore(tmp_path / "state.sqlite", account_key="synthetic")
    service = MessageService(gateway, state=state)
    plan = plan_for(service, operation, references)
    with pytest.raises(MutationExecutionError) as caught:
        service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
    failure = caught.value
    expected = ["completed"] * (position - 1) + ["uncertain" if lost_ack else "failed"]
    expected += ["not_started"] * (3 - position)
    assert [row["status"] for row in failure.outcomes] == expected
    saved = StateStore(state.path, account_key="synthetic").get_message_operation(
        failure.operation_id
    )
    assert [row["status"] for row in saved["outcomes"]] == expected
    assert gateway.calls == position
    affected = position if lost_ack else position - 1
    reopened = LocalMailbox(local.path)
    with reopened.db() as db:
        rows = {row["reference"]: row for row in db.execute("SELECT * FROM objects")}
    if operation == "copy":
        assert len(rows) == 3 + affected
        assert all(
            reopened.get("mail", ref) == item
            for ref, item in zip(references, original, strict=True)
        )
        assert sum(row["folder"] == "/Archive" for row in rows.values()) == affected
    elif operation == "permanent_delete":
        assert len(rows) == 3 - affected
        assert all(ref not in rows for ref in references[:affected])
        assert all(ref in rows for ref in references[affected:])
    else:
        for index, (ref, item) in enumerate(zip(references, original, strict=True)):
            value = reopened.get("mail", ref)
            if index >= affected:
                assert value == item
            elif operation.startswith("mark_"):
                assert value.is_read is (operation == "mark_read")
            elif operation in {"flag", "unflag"}:
                assert value.flagged is (operation == "flag")
            elif operation == "categorize":
                assert value.categories == ("new",)
            elif operation == "set_importance":
                assert value.importance is Importance.HIGH
            elif operation in {"move", "delete"}:
                assert value.folder_path == (
                    "/Archive" if operation == "move" else "/Deleted Items"
                )
