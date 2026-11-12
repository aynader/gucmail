"""Deep, read-only SQLite schema health checks for diagnostics and repair planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gucmail.constants import SCHEMA_VERSION
from gucmail.state import StateStore

EXPECTED_SCHEMA: dict[str, frozenset[str]] = {
    "schema_migrations": frozenset({"version", "applied_at"}),
    "metadata": frozenset({"key", "value"}),
    "item_references": frozenset(
        {
            "account_key",
            "local_ref",
            "item_kind",
            "ews_id",
            "change_key",
            "folder_path",
            "created_at",
            "updated_at",
        }
    ),
    "item_reference_aliases": frozenset(
        {
            "account_key",
            "alias_ref",
            "canonical_ref",
            "item_kind",
            "created_at",
            "updated_at",
        }
    ),
    "item_reference_remote_ids": frozenset(
        {
            "account_key",
            "item_kind",
            "ews_id",
            "canonical_ref",
            "is_current",
            "first_seen_at",
            "last_seen_at",
        }
    ),
    "folder_cache": frozenset(
        {
            "ews_id",
            "change_key",
            "path",
            "parent_id",
            "display_name",
            "total_count",
            "unread_count",
            "folder_class",
            "updated_at",
        }
    ),
    "saved_searches": frozenset({"name", "query_json", "created_at", "updated_at"}),
    "sync_checkpoints": frozenset(
        {"scope", "sync_state", "last_success_at", "last_error", "updated_at"}
    ),
    "seen_items": frozenset({"event_key", "item_ref", "first_seen_at", "last_seen_at"}),
    "webhooks": frozenset(
        {
            "id",
            "name",
            "url",
            "method",
            "headers_json",
            "secret_keyring_name",
            "field_allowlist_json",
            "enabled",
            "max_retries",
            "created_at",
        }
    ),
    "webhook_deliveries": frozenset(
        {
            "id",
            "webhook_id",
            "event_id",
            "attempt",
            "status_code",
            "state",
            "error",
            "created_at",
        }
    ),
    "local_drafts": frozenset(
        {
            "reference",
            "draft_json",
            "revision",
            "server_id",
            "server_change_key",
            "state",
            "content_hash",
            "created_at",
            "updated_at",
        }
    ),
    "draft_revisions": frozenset(
        {"draft_reference", "revision", "draft_json", "content_hash", "created_at"}
    ),
    "scheduled_jobs": frozenset(
        {
            "reference",
            "draft_reference",
            "scheduled_for",
            "timezone",
            "original_expression",
            "state",
            "idempotency_key",
            "require_final_confirmation",
            "attempts",
            "next_attempt_at",
            "last_attempt_at",
            "last_error",
            "lease_owner",
            "lease_until",
            "outcome_reference",
            "recurrence_id",
            "attachment_manifest_json",
            "created_at",
            "updated_at",
        }
    ),
    "schedule_history": frozenset({"id", "job_reference", "event", "detail", "occurred_at"}),
    "recurring_rules": frozenset(
        {
            "reference",
            "template_draft_reference",
            "rule_json",
            "state",
            "next_occurrence",
            "created_at",
            "updated_at",
        }
    ),
    "local_item_metadata": frozenset(
        {
            "item_reference",
            "pinned",
            "snoozed_until",
            "reminder_at",
            "tags_json",
            "note",
            "muted",
            "waiting_for_reply",
            "updated_at",
        }
    ),
    "notification_history": frozenset(
        {
            "id",
            "event_id",
            "provider",
            "state",
            "detail",
            "attempts",
            "status_code",
            "created_at",
        }
    ),
    "backup_records": frozenset(
        {
            "backup_id",
            "path",
            "kind",
            "state",
            "item_count",
            "size_bytes",
            "encrypted",
            "verified_at",
            "created_at",
            "updated_at",
        }
    ),
    "cleanup_plans": frozenset({"plan_id", "plan_json", "state", "created_at", "updated_at"}),
    "message_templates": frozenset(
        {"name", "template_json", "enabled", "created_at", "updated_at"}
    ),
    "notification_rules": frozenset({"name", "rule_json", "enabled", "created_at", "updated_at"}),
    "saved_workspaces": frozenset({"name", "workspace_json", "created_at", "updated_at"}),
    "sender_notes": frozenset({"normalized_address", "alias", "trust_note", "updated_at"}),
    "local_audit_events": frozenset(
        {"id", "action", "target_reference", "detail_json", "occurred_at"}
    ),
    "notification_delivery_claims": frozenset(
        {"event_id", "provider", "state", "lease_until", "updated_at"}
    ),
}


@dataclass(frozen=True, slots=True)
class StateHealthIssue:
    code: str
    detail: str
    table: str | None = None


@dataclass(frozen=True, slots=True)
class StateHealthReport:
    expected_schema_version: int
    observed_schema_version: int
    migration_versions: tuple[int, ...]
    integrity: str
    table_count: int
    foreign_key_violation_count: int
    issues: tuple[StateHealthIssue, ...]

    @property
    def healthy(self) -> bool:
        return not self.issues and self.integrity == "ok"

    def to_safe_primitive(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "expected_schema_version": self.expected_schema_version,
            "observed_schema_version": self.observed_schema_version,
            "migration_versions": self.migration_versions,
            "integrity": self.integrity,
            "table_count": self.table_count,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "issues": tuple(
                {"code": issue.code, "detail": issue.detail, "table": issue.table}
                for issue in self.issues
            ),
        }


class StateHealthChecker:
    """Inspect expected schema, migration continuity, and SQLite foreign keys."""

    def __init__(self, state: StateStore) -> None:
        self._state = state

    def check(self) -> StateHealthReport:
        issues: list[StateHealthIssue] = []
        try:
            self._state.initialize()
        except Exception as exc:
            issues.append(
                StateHealthIssue(
                    "initialization_failed",
                    f"State initialization failed ({type(exc).__name__}); no repair was attempted.",
                )
            )

        integrity = "unknown"
        migration_versions: tuple[int, ...] = ()
        observed_schema_version = 0
        table_names: set[str] = set()
        foreign_key_violation_count = 0
        try:
            with self._state.connection() as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                integrity = str(quick_check[0]) if quick_check else "unknown"
                if integrity != "ok":
                    issues.append(
                        StateHealthIssue(
                            "integrity_check_failed",
                            "SQLite quick_check reported a damaged local state database.",
                        )
                    )

                table_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "schema_migrations" in table_names:
                    migration_versions = tuple(
                        int(row[0])
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        ).fetchall()
                    )
                    observed_schema_version = max(migration_versions, default=0)

                for table, expected_columns in EXPECTED_SCHEMA.items():
                    if table not in table_names:
                        issues.append(
                            StateHealthIssue(
                                "missing_table",
                                "An expected state table is missing.",
                                table,
                            )
                        )
                        continue
                    # Table names are fixed constants from EXPECTED_SCHEMA, never user input.
                    columns = {
                        str(row[1])
                        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                    }
                    missing = sorted(expected_columns - columns)
                    if missing:
                        issues.append(
                            StateHealthIssue(
                                "missing_columns",
                                "Expected column(s) are missing: " + ", ".join(missing),
                                table,
                            )
                        )

                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                foreign_key_violation_count = len(violations)
                for table in sorted({str(row[0]) for row in violations}):
                    issues.append(
                        StateHealthIssue(
                            "foreign_key_violation",
                            "One or more rows have an invalid foreign-key relationship.",
                            table,
                        )
                    )
        except Exception as exc:
            issues.append(
                StateHealthIssue(
                    "inspection_failed",
                    f"State inspection failed ({type(exc).__name__}); no repair was attempted.",
                )
            )

        present = set(migration_versions)
        expected_through_observed = set(range(1, min(observed_schema_version, SCHEMA_VERSION) + 1))
        for version in sorted(expected_through_observed - present):
            issues.append(
                StateHealthIssue(
                    "migration_gap",
                    f"Migration {version} is missing from the migration ledger.",
                    "schema_migrations",
                )
            )
        if observed_schema_version != SCHEMA_VERSION:
            issues.append(
                StateHealthIssue(
                    "schema_version_mismatch",
                    "The observed state schema version does not match this application.",
                    "schema_migrations",
                )
            )

        return StateHealthReport(
            expected_schema_version=SCHEMA_VERSION,
            observed_schema_version=observed_schema_version,
            migration_versions=migration_versions,
            integrity=integrity,
            table_count=len(table_names),
            foreign_key_violation_count=foreign_key_violation_count,
            issues=tuple(issues),
        )


__all__ = [
    "EXPECTED_SCHEMA",
    "StateHealthChecker",
    "StateHealthIssue",
    "StateHealthReport",
]
