"""Transactional SQLite state that is useful but never the mailbox source of truth."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gucmail.constants import SCHEMA_VERSION
from gucmail.errors import LocalStateError
from gucmail.models import NotificationEvent, SendState, to_primitive

if TYPE_CHECKING:
    from gucmail.help.models import HelpUserState

MIGRATIONS: dict[int, str] = {
    17: """
        CREATE TABLE message_list_cache (
            account_key TEXT NOT NULL,
            folder TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            cached_at TEXT NOT NULL,
            PRIMARY KEY(account_key, folder)
        );
    """,
    16: """
        CREATE TABLE restore_operations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            chain_digest TEXT NOT NULL,
            allow_completed_duplicates INTEGER NOT NULL CHECK(allow_completed_duplicates IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_key, operation_id)
        );
        CREATE TABLE restore_targets (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            destination TEXT NOT NULL,
            replay_key TEXT NOT NULL,
            previous_token TEXT,
            status TEXT NOT NULL CHECK(status IN
                ('not_started','dispatching','completed','rejected','uncertain','cancelled')),
            result_reference TEXT,
            error_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_key, operation_id, ordinal),
            FOREIGN KEY(account_key, operation_id)
                REFERENCES restore_operations(account_key, operation_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_restore_operations_recent
            ON restore_operations(account_key, sequence DESC);
        CREATE INDEX idx_restore_replay_lookup
            ON restore_targets(account_key, replay_key, sequence DESC)
            WHERE status IN ('dispatching','completed','uncertain');
        CREATE UNIQUE INDEX idx_restore_unresolved_claim
            ON restore_targets(account_key, replay_key)
            WHERE status IN ('dispatching','uncertain');
    """,
    15: """
        CREATE INDEX idx_scheduled_draft_recent
            ON scheduled_jobs(draft_reference, created_at DESC, reference DESC);
    """,
    14: """
        CREATE TABLE IF NOT EXISTS local_drafts (
            reference TEXT PRIMARY KEY,
            draft_json TEXT NOT NULL,
            revision INTEGER NOT NULL,
            server_id TEXT,
            server_change_key TEXT,
            state TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_local_drafts_recent ON local_drafts(updated_at DESC, reference);
        CREATE INDEX idx_local_drafts_location_recent
            ON local_drafts(state, updated_at DESC, reference);
    """,
    13: """
        CREATE TABLE message_operations (
            account_key TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            plan_scope TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_key, operation_id)
        );
        CREATE TABLE message_operation_targets (
            account_key TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            reference TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('not_started','dispatching','completed','failed','uncertain','cancelled')),
            result_reference TEXT,
            error_type TEXT,
            PRIMARY KEY (account_key, operation_id, ordinal),
            FOREIGN KEY (account_key, operation_id)
                REFERENCES message_operations(account_key, operation_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_message_operations_recent
            ON message_operations(account_key, created_at DESC, operation_id);
    """,
    1: """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE item_references (
            local_ref TEXT PRIMARY KEY,
            item_kind TEXT NOT NULL,
            ews_id TEXT NOT NULL,
            change_key TEXT,
            folder_path TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_item_refs_ews ON item_references(item_kind, ews_id);
        CREATE TABLE folder_cache (
            ews_id TEXT PRIMARY KEY,
            change_key TEXT,
            path TEXT NOT NULL,
            parent_id TEXT,
            display_name TEXT NOT NULL,
            total_count INTEGER,
            unread_count INTEGER,
            folder_class TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE saved_searches (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            query_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sync_checkpoints (
            scope TEXT PRIMARY KEY,
            sync_state TEXT,
            last_success_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE seen_items (
            event_key TEXT PRIMARY KEY,
            item_ref TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            url TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'POST',
            headers_json TEXT NOT NULL DEFAULT '{}',
            secret_keyring_name TEXT,
            field_allowlist_json TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_retries INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL
        );
        CREATE TABLE webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            status_code INTEGER,
            state TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(webhook_id, event_id, attempt)
        );
        CREATE TABLE local_drafts (
            reference TEXT PRIMARY KEY,
            draft_json TEXT NOT NULL,
            revision INTEGER NOT NULL,
            server_id TEXT,
            server_change_key TEXT,
            state TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE draft_revisions (
            draft_reference TEXT NOT NULL REFERENCES local_drafts(reference) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            draft_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(draft_reference, revision)
        );
        CREATE TABLE scheduled_jobs (
            reference TEXT PRIMARY KEY,
            draft_reference TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            timezone TEXT NOT NULL,
            original_expression TEXT NOT NULL,
            state TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            require_final_confirmation INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_attempt_at TEXT,
            last_error TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            outcome_reference TEXT,
            recurrence_id TEXT,
            attachment_manifest_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_scheduled_due ON scheduled_jobs(state, scheduled_for, next_attempt_at);
        CREATE TABLE schedule_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_reference TEXT NOT NULL REFERENCES scheduled_jobs(reference) ON DELETE CASCADE,
            event TEXT NOT NULL,
            detail TEXT,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE recurring_rules (
            reference TEXT PRIMARY KEY,
            template_draft_reference TEXT NOT NULL,
            rule_json TEXT NOT NULL,
            state TEXT NOT NULL,
            next_occurrence TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE local_item_metadata (
            item_reference TEXT PRIMARY KEY,
            pinned INTEGER NOT NULL DEFAULT 0,
            snoozed_until TEXT,
            reminder_at TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            note TEXT,
            muted INTEGER NOT NULL DEFAULT 0,
            waiting_for_reply INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_history (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            state TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE backup_records (
            backup_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            encrypted INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE cleanup_plans (
            plan_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """,
    2: """
        CREATE TABLE message_templates (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            template_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_rules (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            rule_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE saved_workspaces (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            workspace_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sender_notes (
            normalized_address TEXT PRIMARY KEY,
            alias TEXT,
            trust_note TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE local_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target_reference TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX idx_local_audit_target
            ON local_audit_events(target_reference, occurred_at);
    """,
    3: """
        ALTER TABLE notification_history RENAME TO notification_history_v1;
        CREATE TABLE notification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            state TEXT NOT NULL,
            detail TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            status_code INTEGER,
            created_at TEXT NOT NULL
        );
        INSERT INTO notification_history(
            event_id, provider, state, detail, attempts, status_code, created_at
        )
        SELECT
            CASE
                WHEN substr(event_id, -(length(provider) + 1)) = ':' || provider
                THEN substr(event_id, 1, length(event_id) - length(provider) - 1)
                ELSE event_id
            END,
            provider,
            state,
            detail,
            CASE WHEN state = 'skipped' THEN 0 ELSE 1 END,
            NULL,
            created_at
        FROM notification_history_v1;
        DROP TABLE notification_history_v1;
        CREATE INDEX idx_notification_history_recent
            ON notification_history(id DESC);
        CREATE INDEX idx_notification_history_event
            ON notification_history(event_id, provider, id DESC);
        CREATE TABLE notification_delivery_claims (
            event_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            state TEXT NOT NULL,
            lease_until TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(event_id, provider)
        );
    """,
    4: """
        ALTER TABLE item_references RENAME TO item_references_v3;
        DROP INDEX IF EXISTS idx_item_refs_ews;

        CREATE TABLE item_references (
            account_key TEXT NOT NULL,
            local_ref TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            ews_id TEXT NOT NULL,
            change_key TEXT,
            folder_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, local_ref),
            UNIQUE(account_key, item_kind, ews_id)
        );

        CREATE TEMP TABLE item_reference_v4_winners AS
        WITH ranked AS (
            SELECT
                local_ref,
                item_kind,
                ews_id,
                change_key,
                folder_path,
                updated_at,
                ROW_NUMBER() OVER (
                    PARTITION BY item_kind, ews_id
                    ORDER BY updated_at ASC, local_ref ASC
                ) AS identity_rank
            FROM item_references_v3
        )
        SELECT
            local_ref,
            item_kind,
            ews_id,
            change_key,
            folder_path,
            updated_at
        FROM ranked
        WHERE identity_rank = 1;

        INSERT INTO item_references(
            account_key,
            local_ref,
            item_kind,
            ews_id,
            change_key,
            folder_path,
            created_at,
            updated_at
        )
        SELECT
            'default',
            local_ref,
            item_kind,
            ews_id,
            change_key,
            folder_path,
            updated_at,
            updated_at
        FROM item_reference_v4_winners;

        CREATE TABLE item_reference_aliases (
            account_key TEXT NOT NULL,
            alias_ref TEXT NOT NULL,
            canonical_ref TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, alias_ref),
            FOREIGN KEY(account_key, canonical_ref)
                REFERENCES item_references(account_key, local_ref)
                ON UPDATE CASCADE ON DELETE CASCADE,
            CHECK(alias_ref <> canonical_ref)
        );

        INSERT INTO item_reference_aliases(
            account_key,
            alias_ref,
            canonical_ref,
            item_kind,
            created_at,
            updated_at
        )
        SELECT
            'default',
            previous.local_ref,
            winner.local_ref,
            previous.item_kind,
            previous.updated_at,
            previous.updated_at
        FROM item_references_v3 AS previous
        JOIN item_reference_v4_winners AS winner
          ON winner.item_kind = previous.item_kind
         AND winner.ews_id = previous.ews_id
        WHERE previous.local_ref <> winner.local_ref;

        CREATE TABLE item_reference_remote_ids (
            account_key TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            ews_id TEXT NOT NULL,
            canonical_ref TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(account_key, item_kind, ews_id),
            FOREIGN KEY(account_key, canonical_ref)
                REFERENCES item_references(account_key, local_ref)
                ON UPDATE CASCADE ON DELETE CASCADE
        );

        INSERT INTO item_reference_remote_ids(
            account_key,
            item_kind,
            ews_id,
            canonical_ref,
            is_current,
            first_seen_at,
            last_seen_at
        )
        SELECT
            'default',
            item_kind,
            ews_id,
            local_ref,
            1,
            updated_at,
            updated_at
        FROM item_reference_v4_winners;

        DROP TABLE item_reference_v4_winners;
        DROP TABLE item_references_v3;

        CREATE INDEX idx_item_refs_ews
            ON item_references(account_key, item_kind, ews_id);
        CREATE INDEX idx_item_ref_alias_canonical
            ON item_reference_aliases(account_key, canonical_ref);
        CREATE INDEX idx_item_ref_history_canonical
            ON item_reference_remote_ids(account_key, canonical_ref, is_current);
    """,
    5: """
        CREATE TABLE watcher_runtime (
            account_key TEXT NOT NULL,
            scope TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            lease_id TEXT,
            owner_id TEXT,
            process_id INTEGER,
            host_name TEXT,
            state TEXT NOT NULL,
            strategy TEXT,
            connection_state TEXT,
            acquired_at TEXT,
            started_at TEXT,
            heartbeat_at TEXT,
            lease_until TEXT,
            last_success_at TEXT,
            last_event_at TEXT,
            next_poll_at TEXT,
            backoff_until TEXT,
            detected_count INTEGER NOT NULL DEFAULT 0,
            delivered_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, scope)
        );
        CREATE INDEX idx_watcher_runtime_lease
            ON watcher_runtime(lease_until, state);

        CREATE TABLE event_ledger (
            account_key TEXT NOT NULL,
            event_id TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            folder TEXT,
            item_reference TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, event_id),
            UNIQUE(account_key, dedupe_key)
        );
        CREATE INDEX idx_event_ledger_recent
            ON event_ledger(account_key, detected_at DESC);

        CREATE TABLE notification_outbox (
            account_key TEXT NOT NULL,
            event_id TEXT NOT NULL,
            folder TEXT,
            state TEXT NOT NULL,
            delivery_cycles INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, event_id),
            FOREIGN KEY(account_key, event_id)
                REFERENCES event_ledger(account_key, event_id)
                ON UPDATE CASCADE ON DELETE CASCADE
        );
        CREATE INDEX idx_notification_outbox_due
            ON notification_outbox(account_key, state, next_attempt_at, lease_until);

        CREATE TABLE notification_provider_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            event_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            state TEXT NOT NULL,
            status_code INTEGER,
            error_summary TEXT,
            retry_at TEXT,
            attempted_at TEXT NOT NULL,
            UNIQUE(account_key, event_id, provider_id, attempt_no),
            FOREIGN KEY(account_key, event_id)
                REFERENCES event_ledger(account_key, event_id)
                ON UPDATE CASCADE ON DELETE CASCADE
        );
        CREATE INDEX idx_notification_attempts_event
            ON notification_provider_attempts(
                account_key, event_id, provider_id, attempt_no DESC
            );

        CREATE TABLE notification_dead_letters (
            account_key TEXT NOT NULL,
            event_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            failed_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT,
            PRIMARY KEY(account_key, event_id, provider_id),
            FOREIGN KEY(account_key, event_id)
                REFERENCES event_ledger(account_key, event_id)
                ON UPDATE CASCADE ON DELETE CASCADE
        );
        CREATE INDEX idx_notification_dead_letters_open
            ON notification_dead_letters(account_key, resolved_at, failed_at DESC);

        CREATE TABLE command_usage (
            account_key TEXT NOT NULL,
            context TEXT NOT NULL,
            command_id TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 1,
            first_used_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            PRIMARY KEY(account_key, context, command_id)
        );
        CREATE INDEX idx_command_usage_rank
            ON command_usage(account_key, context, use_count DESC, last_used_at DESC);

        CREATE TABLE help_history (
            account_key TEXT NOT NULL,
            page_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            viewed_at TEXT NOT NULL,
            PRIMARY KEY(account_key, page_id),
            UNIQUE(account_key, position)
        );
        CREATE TABLE help_favorites (
            account_key TEXT NOT NULL,
            page_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(account_key, page_id)
        );
        CREATE TABLE help_queries (
            account_key TEXT NOT NULL,
            query TEXT NOT NULL,
            position INTEGER NOT NULL,
            searched_at TEXT NOT NULL,
            PRIMARY KEY(account_key, query),
            UNIQUE(account_key, position)
        );

        CREATE TABLE ui_preferences (
            account_key TEXT NOT NULL,
            scope TEXT NOT NULL,
            preference_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, scope, preference_key)
        );

        CREATE TABLE quota_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            metric TEXT NOT NULL,
            value_bytes INTEGER,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            detail TEXT,
            observed_at TEXT NOT NULL,
            CHECK(value_bytes IS NULL OR value_bytes >= 0)
        );
        CREATE INDEX idx_quota_observations_latest
            ON quota_observations(account_key, metric, observed_at DESC, id DESC);

        CREATE TABLE notification_profiles (
            account_key TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            folders_json TEXT NOT NULL DEFAULT '[]',
            strategy_preference TEXT,
            poll_interval_seconds INTEGER,
            privacy_mode TEXT NOT NULL DEFAULT 'private',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, profile_id)
        );
        CREATE UNIQUE INDEX idx_notification_profiles_name
            ON notification_profiles(account_key, name COLLATE NOCASE);

        CREATE TABLE notification_rule_metadata (
            account_key TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, rule_id)
        );
        CREATE INDEX idx_notification_rules_order
            ON notification_rule_metadata(account_key, profile_id, priority DESC, position);

        CREATE TABLE notification_quiet_hours (
            account_key TEXT NOT NULL,
            schedule_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            start_minute INTEGER NOT NULL,
            end_minute INTEGER NOT NULL,
            behavior TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            exceptions_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, schedule_id, weekday),
            CHECK(weekday BETWEEN 0 AND 6),
            CHECK(start_minute BETWEEN 0 AND 1439),
            CHECK(end_minute BETWEEN 0 AND 1439)
        );
        CREATE INDEX idx_notification_quiet_profile
            ON notification_quiet_hours(account_key, profile_id, weekday);

        CREATE TABLE notification_digest_schedules (
            account_key TEXT NOT NULL,
            digest_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            schedule_json TEXT NOT NULL,
            privacy_mode TEXT NOT NULL DEFAULT 'private',
            last_emitted_at TEXT,
            next_due_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, digest_id)
        );

        CREATE TABLE notification_provider_configs (
            account_key TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            secret_ref TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, provider_id)
        );

        CREATE TABLE notification_routes (
            account_key TEXT NOT NULL,
            route_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            rule_id TEXT,
            provider_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            privacy_mode TEXT NOT NULL DEFAULT 'private',
            field_allowlist_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, route_id)
        );
        CREATE INDEX idx_notification_routes_match
            ON notification_routes(
                account_key, profile_id, rule_id, enabled, priority DESC
            );
    """,
    6: """
        CREATE TABLE notification_routing_queue (
            account_key TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            schedule_id TEXT NOT NULL DEFAULT '',
            batch_key TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            error_summary TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            delivered_at TEXT,
            PRIMARY KEY(
                account_key, profile_id, provider_id, event_id, mode, schedule_id
            ),
            FOREIGN KEY(account_key, event_id)
                REFERENCES event_ledger(account_key, event_id)
                ON UPDATE CASCADE ON DELETE CASCADE,
            CHECK(mode IN ('quiet', 'digest')),
            CHECK(state IN ('pending', 'retry', 'delivered', 'dead_letter')),
            CHECK(attempts >= 0)
        );
        CREATE INDEX idx_notification_routing_queue_due
            ON notification_routing_queue(
                account_key, profile_id, state, next_attempt_at, due_at
            );
        CREATE INDEX idx_notification_routing_queue_batch
            ON notification_routing_queue(
                account_key, profile_id, provider_id, mode, schedule_id, batch_key
            );
    """,
    7: """
        ALTER TABLE saved_searches RENAME TO saved_searches_legacy;

        CREATE TABLE saved_searches (
            account_key TEXT NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            query_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            last_result_count INTEGER,
            run_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(account_key, name),
            CHECK(length(trim(name)) BETWEEN 1 AND 120),
            CHECK(last_result_count IS NULL OR last_result_count >= 0),
            CHECK(run_count >= 0)
        );
        INSERT INTO saved_searches(
            account_key, name, query_json, created_at, updated_at
        )
        SELECT 'default', name, query_json, created_at, updated_at
        FROM saved_searches_legacy;
        DROP TABLE saved_searches_legacy;

        CREATE TABLE search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            name TEXT,
            query_json TEXT NOT NULL,
            source TEXT NOT NULL,
            executed_at TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            CHECK(result_count >= 0),
            CHECK(source IN ('saved', 'interactive', 'palette', 'cli'))
        );
        CREATE INDEX idx_search_history_account_time
            ON search_history(account_key, executed_at DESC, id DESC);
    """,
    8: """
        CREATE TABLE send_attempts (
            account_key TEXT NOT NULL,
            draft_reference TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, draft_reference),
            CHECK(state IN ('sending', 'sent', 'uncertain', 'failed'))
        );
        CREATE TABLE draft_attachment_uploads (
            account_key TEXT NOT NULL,
            item_id TEXT NOT NULL,
            attachment_name TEXT NOT NULL,
            attachment_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            PRIMARY KEY(account_key, item_id, attachment_name)
        );
    """,
    9: """
        CREATE TABLE local_contact_groups (
            account_key TEXT NOT NULL,
            reference TEXT NOT NULL,
            name TEXT NOT NULL,
            members_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, reference)
        );
        CREATE INDEX idx_local_contact_group_names
            ON local_contact_groups(account_key, name COLLATE NOCASE, reference);
    """,
    10: """
        CREATE TABLE contact_history (
            account_key TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            address TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            message_reference TEXT NOT NULL,
            PRIMARY KEY(account_key, observation_id)
        );
        CREATE INDEX idx_contact_history_address
            ON contact_history(account_key, address, occurred_at DESC);
        CREATE INDEX idx_contact_history_recent
            ON contact_history(account_key, occurred_at DESC);
    """,
    11: """
        CREATE TABLE task_observations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            family_reference TEXT NOT NULL,
            original_reference TEXT NOT NULL,
            result_reference TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('edit', 'complete', 'reopen')),
            distinct_instance INTEGER NOT NULL CHECK(distinct_instance IN (0, 1)),
            outcome TEXT NOT NULL CHECK(outcome IN (
                'accepted', 'readback_verified', 'readback_unavailable', 'readback_not_requested'
            )),
            UNIQUE(account_key, operation_id)
        );
        CREATE INDEX idx_task_observations_family
            ON task_observations(account_key, family_reference, sequence DESC);
        CREATE INDEX idx_task_observations_retention
            ON task_observations(account_key, sequence DESC);
        CREATE TABLE task_history_members (
            account_key TEXT NOT NULL,
            reference TEXT NOT NULL,
            family_reference TEXT NOT NULL,
            PRIMARY KEY(account_key, reference)
        );
    """,
    12: """
        -- Some early development databases omitted this optional v1 table.
        CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            url TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'POST',
            headers_json TEXT NOT NULL DEFAULT '{}',
            secret_keyring_name TEXT,
            field_allowlist_json TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_retries INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL
        );
        ALTER TABLE webhooks ADD COLUMN delivery_options_json TEXT NOT NULL DEFAULT '{}';
    """,
}

DEFAULT_ACCOUNT_KEY = "default"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def normalize_account_key(value: str | None) -> str:
    """Return the stable, case-insensitive namespace used for mailbox-owned state."""

    normalized = (value or DEFAULT_ACCOUNT_KEY).casefold().strip()
    return normalized or DEFAULT_ACCOUNT_KEY


def _migration_statements(script: str) -> Iterator[str]:
    """Split a migration with SQLite's own completeness parser, not string heuristics."""

    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    if pending.strip():
        raise LocalStateError("A state database migration contains incomplete SQL.")


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    reference: str
    draft_reference: str
    scheduled_for: datetime
    timezone: str
    original_expression: str
    state: SendState
    idempotency_key: str
    attempts: int
    require_final_confirmation: bool
    attachment_manifest: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class WatcherRuntimeRecord:
    account_key: str
    scope: str
    profile_id: str
    lease_id: str | None
    owner_id: str | None
    process_id: int | None
    host_name: str | None
    state: str
    strategy: str | None
    connection_state: str | None
    acquired_at: datetime | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    lease_until: datetime | None
    last_success_at: datetime | None
    last_event_at: datetime | None
    next_poll_at: datetime | None
    backoff_until: datetime | None
    detected_count: int
    delivered_count: int
    failed_count: int
    error_summary: str | None
    updated_at: datetime

    def is_active(self, *, at: datetime | None = None) -> bool:
        current = _aware_utc(at or utc_now(), label="Watcher status time")
        return (
            self.lease_id is not None
            and self.lease_until is not None
            and self.lease_until > current
            and self.state not in {"stopped", "disabled"}
        )


@dataclass(frozen=True, slots=True)
class EventLedgerRecord:
    account_key: str
    event_id: str
    dedupe_key: str
    event_type: str
    occurred_at: datetime
    detected_at: datetime
    folder: str | None
    item_reference: str | None
    payload: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationOutboxRecord:
    event: EventLedgerRecord
    state: str
    delivery_cycles: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_until: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderAttemptRecord:
    id: int
    account_key: str
    event_id: str
    provider_id: str
    attempt_no: int
    state: str
    status_code: int | None
    error_summary: str | None
    retry_at: datetime | None
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    account_key: str
    event_id: str
    provider_id: str
    reason: str
    failed_at: datetime
    resolved_at: datetime | None
    resolution: str | None


@dataclass(frozen=True, slots=True)
class MonitorBatchCommitResult:
    committed: bool
    checkpoint: str | None
    inserted_event_ids: tuple[str, ...] = ()
    duplicate_count: int = 0


@dataclass(frozen=True, slots=True)
class CommandUsageRecord:
    account_key: str
    context: str
    command_id: str
    use_count: int
    first_used_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class UiPreferenceRecord:
    account_key: str
    scope: str
    key: str
    value: Any
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaObservationRecord:
    id: int
    account_key: str
    metric: str
    value_bytes: int | None
    source: str
    confidence: str
    detail: str | None
    observed_at: datetime


_SENSITIVE_JSON_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "hmac_secret",
        "password",
        "secret",
        "session_cookie",
        "token",
        "webhook_url",
    }
)
_SECRET_REFERENCE_PREFIXES = (
    "keyring://",
    "env://",
    "secret-file://",
    "container-secret://",
    "secret://",
)
_QUOTA_METRICS = frozenset(
    {
        "used",
        "total_capacity",
        "issue_warning",
        "prohibit_send",
        "prohibit_send_receive",
        "deleted_items",
        "recoverable_items",
        "archive_used",
        "archive_capacity",
    }
)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LocalStateError(f"{label} must include a time zone.")
    return value.astimezone(UTC)


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(value: str, *, label: str, maximum: int = 16_384) -> str:
    clean = value.strip()
    if not clean or "\x00" in clean:
        raise LocalStateError(f"{label} may not be empty or contain NUL characters.")
    if len(clean) > maximum:
        raise LocalStateError(f"{label} exceeds the local-state size limit.")
    return clean


def _secret_reference(value: str | None) -> str | None:
    if value is None:
        return None
    clean = _text(value, label="Secret reference", maximum=2_048)
    if not clean.casefold().startswith(_SECRET_REFERENCE_PREFIXES):
        raise LocalStateError(
            "Integration secrets must be stored as keyring, environment, or secret-file "
            "references; plaintext values are not accepted."
        )
    return clean


def _check_json_secrets(value: Any, *, provider_metadata: bool = False) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).casefold().strip().replace("-", "_")
            if key.endswith("_secret_ref") or key == "secret_ref":
                if child is not None and not isinstance(child, str):
                    raise LocalStateError("Secret references in state must be text or null.")
                _secret_reference(child)
            elif key in _SENSITIVE_JSON_KEYS or (provider_metadata and key == "url"):
                raise LocalStateError(
                    f"Plaintext secret-bearing field {raw_key!r} is not permitted in state."
                )
            _check_json_secrets(child, provider_metadata=provider_metadata)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_json_secrets(child, provider_metadata=provider_metadata)


def _json(
    value: Any,
    *,
    label: str,
    maximum_bytes: int = 1_048_576,
    provider_metadata: bool = False,
) -> str:
    _check_json_secrets(value, provider_metadata=provider_metadata)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise LocalStateError(f"{label} must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise LocalStateError(f"{label} exceeds the local-state size limit.")
    return encoded


def _json_mapping(value: str) -> Mapping[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise LocalStateError("Stored local-state metadata is not a JSON object.")
    return cast(dict[str, Any], loaded)


def _row_has_column(row: sqlite3.Row, name: str) -> bool:
    """Check sqlite Row column names; ``name in row`` checks values, not keys."""

    return name in frozenset(row.keys())


def _watcher_record(row: sqlite3.Row) -> WatcherRuntimeRecord:
    return WatcherRuntimeRecord(
        account_key=str(row["account_key"]),
        scope=str(row["scope"]),
        profile_id=str(row["profile_id"]),
        lease_id=cast(str | None, row["lease_id"]),
        owner_id=cast(str | None, row["owner_id"]),
        process_id=int(row["process_id"]) if row["process_id"] is not None else None,
        host_name=cast(str | None, row["host_name"]),
        state=str(row["state"]),
        strategy=cast(str | None, row["strategy"]),
        connection_state=cast(str | None, row["connection_state"]),
        acquired_at=_datetime(cast(str | None, row["acquired_at"])),
        started_at=_datetime(cast(str | None, row["started_at"])),
        heartbeat_at=_datetime(cast(str | None, row["heartbeat_at"])),
        lease_until=_datetime(cast(str | None, row["lease_until"])),
        last_success_at=_datetime(cast(str | None, row["last_success_at"])),
        last_event_at=_datetime(cast(str | None, row["last_event_at"])),
        next_poll_at=_datetime(cast(str | None, row["next_poll_at"])),
        backoff_until=_datetime(cast(str | None, row["backoff_until"])),
        detected_count=int(row["detected_count"]),
        delivered_count=int(row["delivered_count"]),
        failed_count=int(row["failed_count"]),
        error_summary=cast(str | None, row["error_summary"]),
        updated_at=cast(datetime, _datetime(str(row["updated_at"]))),
    )


def _event_record(row: sqlite3.Row) -> EventLedgerRecord:
    return EventLedgerRecord(
        account_key=str(row["account_key"]),
        event_id=str(row["event_id"]),
        dedupe_key=str(row["dedupe_key"]),
        event_type=str(row["event_type"]),
        occurred_at=cast(datetime, _datetime(str(row["occurred_at"]))),
        detected_at=cast(datetime, _datetime(str(row["detected_at"]))),
        folder=cast(str | None, row["folder"]),
        item_reference=cast(str | None, row["item_reference"]),
        payload=_json_mapping(str(row["payload_json"])),
        created_at=cast(datetime, _datetime(str(row["created_at"]))),
        updated_at=cast(
            datetime,
            _datetime(
                str(
                    row["event_updated_at"]
                    if _row_has_column(row, "event_updated_at")
                    else row["updated_at"]
                )
            ),
        ),
    )


def _outbox_record(row: sqlite3.Row) -> NotificationOutboxRecord:
    event = _event_record(row)
    return NotificationOutboxRecord(
        event=event,
        state=str(row["outbox_state"] if _row_has_column(row, "outbox_state") else row["state"]),
        delivery_cycles=int(row["delivery_cycles"]),
        next_attempt_at=_datetime(cast(str | None, row["next_attempt_at"])),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_until=_datetime(cast(str | None, row["lease_until"])),
        created_at=cast(
            datetime,
            _datetime(
                str(
                    row["outbox_created_at"]
                    if _row_has_column(row, "outbox_created_at")
                    else row["created_at"]
                )
            ),
        ),
        updated_at=cast(
            datetime,
            _datetime(
                str(
                    row["outbox_updated_at"]
                    if _row_has_column(row, "outbox_updated_at")
                    else row["updated_at"]
                )
            ),
        ),
    )


def _provider_attempt_record(row: sqlite3.Row) -> ProviderAttemptRecord:
    return ProviderAttemptRecord(
        id=int(row["id"]),
        account_key=str(row["account_key"]),
        event_id=str(row["event_id"]),
        provider_id=str(row["provider_id"]),
        attempt_no=int(row["attempt_no"]),
        state=str(row["state"]),
        status_code=int(row["status_code"]) if row["status_code"] is not None else None,
        error_summary=cast(str | None, row["error_summary"]),
        retry_at=_datetime(cast(str | None, row["retry_at"])),
        attempted_at=cast(datetime, _datetime(str(row["attempted_at"]))),
    )


def _dead_letter_record(row: sqlite3.Row) -> DeadLetterRecord:
    return DeadLetterRecord(
        account_key=str(row["account_key"]),
        event_id=str(row["event_id"]),
        provider_id=str(row["provider_id"]),
        reason=str(row["reason"]),
        failed_at=cast(datetime, _datetime(str(row["failed_at"]))),
        resolved_at=_datetime(cast(str | None, row["resolved_at"])),
        resolution=cast(str | None, row["resolution"]),
    )


def _command_usage_record(row: sqlite3.Row) -> CommandUsageRecord:
    return CommandUsageRecord(
        account_key=str(row["account_key"]),
        context=str(row["context"]),
        command_id=str(row["command_id"]),
        use_count=int(row["use_count"]),
        first_used_at=cast(datetime, _datetime(str(row["first_used_at"]))),
        last_used_at=cast(datetime, _datetime(str(row["last_used_at"]))),
    )


def _ui_preference_record(row: sqlite3.Row) -> UiPreferenceRecord:
    return UiPreferenceRecord(
        account_key=str(row["account_key"]),
        scope=str(row["scope"]),
        key=str(row["preference_key"]),
        value=json.loads(str(row["value_json"])),
        revision=int(row["revision"]),
        updated_at=cast(datetime, _datetime(str(row["updated_at"]))),
    )


def _quota_observation_record(row: sqlite3.Row) -> QuotaObservationRecord:
    return QuotaObservationRecord(
        id=int(row["id"]),
        account_key=str(row["account_key"]),
        metric=str(row["metric"]),
        value_bytes=int(row["value_bytes"]) if row["value_bytes"] is not None else None,
        source=str(row["source"]),
        confidence=str(row["confidence"]),
        detail=cast(str | None, row["detail"]),
        observed_at=cast(datetime, _datetime(str(row["observed_at"]))),
    )


class StateStore:
    """Open short-lived connections so processes can share state safely through WAL."""

    def __init__(self, path: Path, *, account_key: str | None = None) -> None:
        self.path = path
        self.account_key = normalize_account_key(account_key)
        self._migration_lock = threading.Lock()
        self._initialized = False

    def bind_mailbox(self, email: str, endpoint: str) -> None:
        """Claim legacy single-mailbox state once and reject cross-account reuse.

        Early schemas contain unscoped drafts, schedules and local indexes. Changing
        account_key alone cannot isolate those records. Keep the legacy data intact
        and require a separate data directory for another mailbox.
        """
        from hashlib import sha256

        from gucmail.errors import ConfigurationError

        identity = email.strip().casefold() + "\n" + endpoint.strip().rstrip("/").casefold()
        fingerprint = sha256(identity.encode("utf-8")).hexdigest()
        self.initialize()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='mailbox_owner_v1'"
            ).fetchone()
            if row is not None and row["value"] != fingerprint:
                raise ConfigurationError(
                    "This data directory belongs to another mailbox or Exchange endpoint. "
                    "Use a separate GUCMAIL_DATA_DIR for this account, or restore the "
                    "previous account settings. Existing local data was preserved."
                )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('mailbox_owner_v1', ?)",
                (fingerprint,),
            )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            for attempt in range(20):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or attempt == 19:
                        raise
                    time.sleep(min(0.01 * (2**attempt), 0.25))
            connection.execute("PRAGMA synchronous=NORMAL")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise LocalStateError(f"Could not open local state database: {exc}") from exc

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._migration_lock:
            if self._initialized:
                return
            with self.connection() as connection:
                try:
                    # Take the cross-process writer lock before reading the current version.
                    # Otherwise two first-run processes can both decide to apply migration 1.
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations ("
                        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                    )
                    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
                    current = max((int(row[0]) for row in rows), default=0)
                    if current > SCHEMA_VERSION:
                        raise LocalStateError(
                            f"State database schema {current} is newer than this application "
                            f"supports ({SCHEMA_VERSION})."
                        )
                    for version in range(current + 1, SCHEMA_VERSION + 1):
                        if version == 7:
                            # Some early development databases recorded v1/v2 while
                            # omitting optional local-state tables.  Recreate the empty
                            # legacy shape so the account-scoping migration is still
                            # transactional and data preserving when the table did exist.
                            connection.execute(
                                """
                                CREATE TABLE IF NOT EXISTS saved_searches (
                                    name TEXT PRIMARY KEY COLLATE NOCASE,
                                    query_json TEXT NOT NULL,
                                    created_at TEXT NOT NULL,
                                    updated_at TEXT NOT NULL
                                )
                                """
                            )
                        script = MIGRATIONS.get(version)
                        if script is None:
                            raise LocalStateError(f"Missing migration {version}.")
                        for statement in _migration_statements(script):
                            connection.execute(statement)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, iso()),
                        )
                    connection.execute("COMMIT")
                except sqlite3.Error as exc:
                    with contextlib.suppress(sqlite3.Error):
                        connection.execute("ROLLBACK")
                    raise LocalStateError(f"State database migration failed: {exc}") from exc
                except Exception:
                    with contextlib.suppress(sqlite3.Error):
                        connection.execute("ROLLBACK")
                    raise
            self._initialized = True

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        self.initialize()
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_reference_part(value: str, *, label: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise LocalStateError(f"{label} may not be empty or contain NUL characters.")
        if len(normalized) > 16_384:
            raise LocalStateError(f"{label} is too long for the local state database.")
        return normalized

    @staticmethod
    def _canonical_reference_row(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        local_ref: str,
        item_kind: str | None = None,
    ) -> sqlite3.Row | None:
        if item_kind is None:
            row = connection.execute(
                "SELECT * FROM item_references WHERE account_key=? AND local_ref=?",
                (account_key, local_ref),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM item_references WHERE account_key=? AND local_ref=? AND item_kind=?",
                (account_key, local_ref, item_kind),
            ).fetchone()
        if row is not None:
            return cast(sqlite3.Row, row)
        if item_kind is None:
            alias_row = connection.execute(
                """
                SELECT canonical.*
                FROM item_reference_aliases AS aliases
                JOIN item_references AS canonical
                  ON canonical.account_key = aliases.account_key
                 AND canonical.local_ref = aliases.canonical_ref
                WHERE aliases.account_key=? AND aliases.alias_ref=?
                """,
                (account_key, local_ref),
            ).fetchone()
        else:
            alias_row = connection.execute(
                """
                SELECT canonical.*
                FROM item_reference_aliases AS aliases
                JOIN item_references AS canonical
                  ON canonical.account_key = aliases.account_key
                 AND canonical.local_ref = aliases.canonical_ref
                WHERE aliases.account_key=? AND aliases.alias_ref=?
                  AND aliases.item_kind=?
                """,
                (account_key, local_ref, item_kind),
            ).fetchone()
        return cast(sqlite3.Row | None, alias_row)

    @staticmethod
    def _remote_reference_row(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        item_kind: str,
        ews_id: str,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM item_references WHERE account_key=? AND item_kind=? AND ews_id=?",
                (account_key, item_kind, ews_id),
            ).fetchone(),
        )

    @staticmethod
    def _remote_history_row(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        item_kind: str,
        ews_id: str,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM item_reference_remote_ids "
                "WHERE account_key=? AND item_kind=? AND ews_id=?",
                (account_key, item_kind, ews_id),
            ).fetchone(),
        )

    @staticmethod
    def _put_reference_alias(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        alias_ref: str,
        canonical_ref: str,
        item_kind: str,
        now: str,
    ) -> None:
        if alias_ref == canonical_ref:
            connection.execute(
                "DELETE FROM item_reference_aliases WHERE account_key=? AND alias_ref=?",
                (account_key, alias_ref),
            )
            return
        connection.execute(
            """
            INSERT INTO item_reference_aliases(
                account_key, alias_ref, canonical_ref, item_kind, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key, alias_ref) DO UPDATE SET
                canonical_ref=excluded.canonical_ref,
                item_kind=excluded.item_kind,
                updated_at=excluded.updated_at
            """,
            (account_key, alias_ref, canonical_ref, item_kind, now, now),
        )

    @classmethod
    def _merge_reference_rows(
        cls,
        connection: sqlite3.Connection,
        *,
        account_key: str,
        winner_ref: str,
        loser_ref: str,
        now: str,
    ) -> None:
        """Merge a refresh-race duplicate without invalidating either public reference."""

        if winner_ref == loser_ref:
            return
        winner = connection.execute(
            "SELECT * FROM item_references WHERE account_key=? AND local_ref=?",
            (account_key, winner_ref),
        ).fetchone()
        loser = connection.execute(
            "SELECT * FROM item_references WHERE account_key=? AND local_ref=?",
            (account_key, loser_ref),
        ).fetchone()
        if winner is None or loser is None:
            raise LocalStateError("Could not merge an incomplete local-reference identity.")
        if winner["item_kind"] != loser["item_kind"] and {
            winner["item_kind"],
            loser["item_kind"],
        } != {"message", "draft"}:
            raise LocalStateError(
                "A local-reference collision crossed item kinds; automatic repair is unsafe."
            )

        # Remove impossible shadow aliases before repointing the loser's valid aliases.
        connection.execute(
            "DELETE FROM item_reference_aliases WHERE account_key=? AND alias_ref IN (?, ?)",
            (account_key, winner_ref, loser_ref),
        )
        connection.execute(
            "UPDATE item_reference_aliases SET canonical_ref=?, updated_at=?, item_kind=? "
            "WHERE account_key=? AND canonical_ref=?",
            (winner_ref, now, winner["item_kind"], account_key, loser_ref),
        )
        connection.execute(
            """
            INSERT INTO item_reference_remote_ids(
                account_key, item_kind, ews_id, canonical_ref, is_current,
                first_seen_at, last_seen_at
            )
            SELECT
                account_key, ?, ews_id, ?, is_current, first_seen_at, ?
            FROM item_reference_remote_ids
            WHERE account_key=? AND canonical_ref=?
            ON CONFLICT(account_key, item_kind, ews_id) DO UPDATE SET
                canonical_ref=excluded.canonical_ref,
                is_current=MAX(item_reference_remote_ids.is_current, excluded.is_current),
                first_seen_at=MIN(item_reference_remote_ids.first_seen_at,
                                  excluded.first_seen_at),
                last_seen_at=MAX(item_reference_remote_ids.last_seen_at,
                                 excluded.last_seen_at)
            """,
            (winner["item_kind"], winner_ref, now, account_key, loser_ref),
        )
        connection.execute(
            "DELETE FROM item_reference_remote_ids WHERE account_key=? AND canonical_ref=?",
            (account_key, loser_ref),
        )

        # Preserve user metadata under both the soon-to-be alias and the canonical ref.
        connection.execute(
            """
            INSERT OR IGNORE INTO local_item_metadata(
                item_reference, pinned, snoozed_until, reminder_at, tags_json,
                note, muted, waiting_for_reply, updated_at
            )
            SELECT ?, pinned, snoozed_until, reminder_at, tags_json,
                   note, muted, waiting_for_reply, updated_at
            FROM local_item_metadata WHERE item_reference=?
            """,
            (winner_ref, loser_ref),
        )
        connection.execute(
            "UPDATE seen_items SET item_ref=? WHERE item_ref=?",
            (winner_ref, loser_ref),
        )
        connection.execute(
            "UPDATE local_audit_events SET target_reference=? WHERE target_reference=?",
            (winner_ref, loser_ref),
        )
        connection.execute(
            "UPDATE scheduled_jobs SET outcome_reference=? WHERE outcome_reference=?",
            (winner_ref, loser_ref),
        )
        connection.execute(
            "DELETE FROM item_references WHERE account_key=? AND local_ref=?",
            (account_key, loser_ref),
        )
        cls._put_reference_alias(
            connection,
            account_key=account_key,
            alias_ref=loser_ref,
            canonical_ref=winner_ref,
            item_kind=str(winner["item_kind"]),
            now=now,
        )

    @staticmethod
    def _set_current_remote_id(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        canonical_ref: str,
        item_kind: str,
        ews_id: str,
        change_key: str | None,
        folder_path: str | None,
        now: str,
    ) -> bool:
        current = connection.execute(
            "SELECT ews_id FROM item_references WHERE account_key=? AND local_ref=?",
            (account_key, canonical_ref),
        ).fetchone()
        if current is None:
            raise LocalStateError("The canonical local-reference record disappeared.")
        previous_ews_id = str(current["ews_id"])
        history = connection.execute(
            "SELECT canonical_ref, is_current FROM item_reference_remote_ids "
            "WHERE account_key=? AND item_kind=? AND ews_id=?",
            (account_key, item_kind, ews_id),
        ).fetchone()

        # An inactive historical ID arriving after a completed move is a stale page refresh.
        # It remains a resolvable alias but must not roll the canonical row backwards.
        if (
            previous_ews_id != ews_id
            and history is not None
            and history["canonical_ref"] == canonical_ref
            and not bool(history["is_current"])
        ):
            connection.execute(
                "UPDATE item_reference_remote_ids SET last_seen_at=? "
                "WHERE account_key=? AND item_kind=? AND ews_id=?",
                (now, account_key, item_kind, ews_id),
            )
            return False

        if previous_ews_id != ews_id:
            connection.execute(
                "UPDATE item_reference_remote_ids SET is_current=0, last_seen_at=? "
                "WHERE account_key=? AND canonical_ref=?",
                (now, account_key, canonical_ref),
            )
            connection.execute(
                "UPDATE item_references SET ews_id=?, change_key=?, folder_path=?, updated_at=? "
                "WHERE account_key=? AND local_ref=?",
                (ews_id, change_key, folder_path, now, account_key, canonical_ref),
            )
        else:
            connection.execute(
                """
                UPDATE item_references SET
                    change_key=COALESCE(?, change_key),
                    folder_path=COALESCE(?, folder_path),
                    updated_at=?
                WHERE account_key=? AND local_ref=?
                """,
                (change_key, folder_path, now, account_key, canonical_ref),
            )
        connection.execute(
            """
            INSERT INTO item_reference_remote_ids(
                account_key, item_kind, ews_id, canonical_ref, is_current,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(account_key, item_kind, ews_id) DO UPDATE SET
                canonical_ref=excluded.canonical_ref,
                is_current=1,
                last_seen_at=excluded.last_seen_at
            """,
            (account_key, item_kind, ews_id, canonical_ref, now, now),
        )
        return True

    @staticmethod
    def _reference_health(connection: sqlite3.Connection) -> dict[str, Any]:
        counts = {
            "canonical_count": "SELECT COUNT(*) FROM item_references",
            "alias_count": "SELECT COUNT(*) FROM item_reference_aliases",
            "remote_id_count": "SELECT COUNT(*) FROM item_reference_remote_ids",
            "dangling_aliases": """
                SELECT COUNT(*) FROM item_reference_aliases AS aliases
                LEFT JOIN item_references AS canonical
                  ON canonical.account_key=aliases.account_key
                 AND canonical.local_ref=aliases.canonical_ref
                WHERE canonical.local_ref IS NULL
            """,
            "dangling_remote_ids": """
                SELECT COUNT(*) FROM item_reference_remote_ids AS remote
                LEFT JOIN item_references AS canonical
                  ON canonical.account_key=remote.account_key
                 AND canonical.local_ref=remote.canonical_ref
                WHERE canonical.local_ref IS NULL
            """,
            "alias_shadows": """
                SELECT COUNT(*) FROM item_reference_aliases AS aliases
                JOIN item_references AS canonical
                  ON canonical.account_key=aliases.account_key
                 AND canonical.local_ref=aliases.alias_ref
            """,
            "kind_mismatches": """
                SELECT COUNT(*) FROM item_reference_aliases AS aliases
                JOIN item_references AS canonical
                  ON canonical.account_key=aliases.account_key
                 AND canonical.local_ref=aliases.canonical_ref
                WHERE aliases.item_kind <> canonical.item_kind
            """,
            "missing_current_ids": """
                SELECT COUNT(*) FROM item_references AS canonical
                WHERE NOT EXISTS (
                    SELECT 1 FROM item_reference_remote_ids AS remote
                    WHERE remote.account_key=canonical.account_key
                      AND remote.canonical_ref=canonical.local_ref
                      AND remote.item_kind=canonical.item_kind
                      AND remote.ews_id=canonical.ews_id
                      AND remote.is_current=1
                )
            """,
            "multiple_current_ids": """
                SELECT COUNT(*) FROM (
                    SELECT account_key, canonical_ref
                    FROM item_reference_remote_ids
                    WHERE is_current=1
                    GROUP BY account_key, canonical_ref
                    HAVING COUNT(*) <> 1
                )
            """,
        }
        result = {
            key: int(connection.execute(query).fetchone()[0]) for key, query in counts.items()
        }
        defects = (
            "dangling_aliases",
            "dangling_remote_ids",
            "alias_shadows",
            "kind_mismatches",
            "missing_current_ids",
            "multiple_current_ids",
        )
        result["ok"] = all(result[key] == 0 for key in defects)
        return result

    def health(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            reference_identity = self._reference_health(connection)
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "integrity": row[0] if row else "unknown",
            "foreign_key_errors": len(foreign_keys),
            "tables": int(tables[0]) if tables else 0,
            "schema_version": int(version[0]) if version and version[0] else 0,
            "reference_identity": reference_identity,
        }

    def reference_health(self) -> dict[str, Any]:
        """Return doctor-friendly invariants for canonical references and aliases."""

        self.initialize()
        with self.connection() as connection:
            return self._reference_health(connection)

    def begin_message_operation(
        self, operation_id: str, operation: str, scope: str, targets: Sequence[str]
    ) -> None:
        """Record intent before dispatch, without storing bodies or credentials."""
        if not targets or len(targets) > 200:
            raise LocalStateError("Message operation targets must contain 1 to 200 references.")
        now = iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO message_operations VALUES (?, ?, ?, ?, ?, ?)",
                (self.account_key, operation_id, operation, scope, now, now),
            )
            connection.executemany(
                "INSERT INTO message_operation_targets "
                "(account_key, operation_id, ordinal, reference, status) VALUES (?, ?, ?, ?, ?)",
                [
                    (self.account_key, operation_id, index, ref, "not_started")
                    for index, ref in enumerate(targets)
                ],
            )

    def record_message_target(
        self,
        operation_id: str,
        ordinal: int,
        status: str,
        *,
        result_reference: str | None = None,
        error_type: str | None = None,
    ) -> None:
        """Persist dispatch before a call and acknowledged effects afterwards."""
        if status not in {"dispatching", "completed", "failed", "uncertain", "cancelled"}:
            raise LocalStateError("Invalid message operation target status.")
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE message_operation_targets SET status=?, result_reference=?, error_type=? "
                "WHERE account_key=? AND operation_id=? AND ordinal=?",
                (status, result_reference, error_type, self.account_key, operation_id, ordinal),
            )
            if cursor.rowcount != 1:
                raise LocalStateError("The message operation target was not found.")
            connection.execute(
                "UPDATE message_operations SET updated_at=? WHERE account_key=? AND operation_id=?",
                (iso(), self.account_key, operation_id),
            )

    def get_message_operation(self, operation_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM message_operations WHERE account_key=? AND operation_id=?",
                (self.account_key, operation_id),
            ).fetchone()
            if row is None:
                return None
            targets = connection.execute(
                "SELECT reference, status, result_reference, error_type "
                "FROM message_operation_targets WHERE account_key=? AND operation_id=? "
                "ORDER BY ordinal",
                (self.account_key, operation_id),
            ).fetchall()
        result = dict(row)
        result.pop("account_key", None)
        # A dispatched call lacking a durable acknowledgment needs reconciliation.
        result["outcomes"] = [
            {
                **dict(target),
                "status": "uncertain" if target["status"] == "dispatching" else target["status"],
            }
            for target in targets
        ]
        return result

    def list_message_operations(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200 or offset < 0:
            raise LocalStateError("Message operation paging is outside safe bounds.")
        self.initialize()
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT operation_id, operation, plan_scope, created_at, updated_at "
                    "FROM message_operations WHERE account_key=? "
                    "ORDER BY created_at DESC, operation_id LIMIT ? OFFSET ?",
                    (self.account_key, limit, offset),
                )
            ]

    def record_task_observation(
        self,
        *,
        operation_id: str,
        observed_at: str,
        original_reference: str,
        result_reference: str,
        action: str,
        distinct_instance: bool,
        retained_limit: int = 10_000,
    ) -> dict[str, Any]:
        """Persist only an accepted update's directly observed local identity link."""
        from gucmail.services.task_history import ACTIONS, history_reference

        original = history_reference(original_reference)
        result = history_reference(result_reference)
        if action not in ACTIONS or type(distinct_instance) is not bool:
            raise LocalStateError("Invalid Task observation action or identity flag.")
        if distinct_instance != (original != result):
            raise LocalStateError("Task observation identities disagree with their references.")
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(c not in "0123456789abcdef" for c in operation_id)
        ):
            raise LocalStateError("Invalid opaque Task observation ID.")
        if type(retained_limit) is not int or not 1 <= retained_limit <= 10_000:
            raise LocalStateError("Task observation retention must be between 1 and 10000.")
        try:
            timestamp = _aware_utc(
                datetime.fromisoformat(observed_at), label="Task observation time"
            )
        except (TypeError, ValueError) as exc:
            raise LocalStateError("Invalid Task observation timestamp.") from exc
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT * FROM task_observations WHERE account_key=? AND operation_id=?",
                (self.account_key, operation_id),
            ).fetchone()
            if previous is not None:
                if any(
                    previous[key] != value
                    for key, value in (
                        ("original_reference", original),
                        ("result_reference", result),
                        ("action", action),
                        ("distinct_instance", int(distinct_instance)),
                    )
                ):
                    raise LocalStateError("Task observation ID was reused with different data.")
                return dict(previous)
            member = connection.execute(
                "SELECT family_reference FROM task_history_members "
                "WHERE account_key=? AND reference=?",
                (self.account_key, original),
            ).fetchone()
            family = str(member[0]) if member else original
            result_member = connection.execute(
                "SELECT family_reference FROM task_history_members "
                "WHERE account_key=? AND reference=?",
                (self.account_key, result),
            ).fetchone()
            if result_member and result_member[0] != family:
                raise LocalStateError("The returned Task belongs to another observed family.")
            cursor = connection.execute(
                """
                INSERT INTO task_observations(
                    account_key, operation_id, observed_at, family_reference,
                    original_reference, result_reference, action, distinct_instance, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted')
                """,
                (
                    self.account_key,
                    operation_id,
                    iso(timestamp),
                    family,
                    original,
                    result,
                    action,
                    int(distinct_instance),
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_history_members "
                "(account_key, reference, family_reference) "
                "VALUES (?, ?, ?)",
                ((self.account_key, ref, family) for ref in {family, original, result}),
            )
            connection.execute(
                """
                DELETE FROM task_observations WHERE account_key=? AND sequence NOT IN (
                    SELECT sequence FROM task_observations WHERE account_key=?
                    ORDER BY sequence DESC LIMIT ?
                )
                """,
                (self.account_key, self.account_key, retained_limit),
            )
            connection.execute(
                """
                DELETE FROM task_history_members WHERE account_key=? AND reference NOT IN (
                    SELECT original_reference FROM task_observations WHERE account_key=?
                    UNION SELECT result_reference FROM task_observations WHERE account_key=?
                    UNION SELECT family_reference FROM task_observations WHERE account_key=?
                )
                """,
                (self.account_key,) * 4,
            )
            row = connection.execute(
                "SELECT * FROM task_observations WHERE sequence=?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def finish_task_observation(self, operation_id: str, *, outcome: str) -> None:
        from gucmail.services.task_history import OUTCOMES

        if outcome not in OUTCOMES - {"accepted"}:
            raise LocalStateError("Invalid Task observation readback outcome.")
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE task_observations SET outcome=? WHERE account_key=? AND operation_id=? "
                "AND outcome IN ('accepted', ?)",
                (outcome, self.account_key, operation_id, outcome),
            )
            if cursor.rowcount != 1:
                raise LocalStateError(
                    "The Task observation expired or already has another outcome."
                )

    def task_observations(
        self, reference: str, *, limit: int = 21, before: int | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        from gucmail.services.task_history import history_reference

        requested = history_reference(reference)
        if type(limit) is not int or not 1 <= limit <= 201:
            raise LocalStateError("Task observation query limit must be between 1 and 201.")
        if before is not None and (type(before) is not int or before <= 0):
            raise LocalStateError("Invalid Task observation continuation sequence.")
        self.initialize()
        canonical = self.resolve_reference(requested, "task")
        if canonical:
            requested = str(canonical["canonical_ref"])
        with self.connection() as connection:
            member = connection.execute(
                "SELECT family_reference FROM task_history_members "
                "WHERE account_key=? AND reference=?",
                (self.account_key, requested),
            ).fetchone()
            family = str(member[0]) if member else requested
            rows = connection.execute(
                """
                SELECT sequence, operation_id, observed_at, family_reference,
                    original_reference, result_reference, action, distinct_instance, outcome
                FROM task_observations WHERE account_key=? AND family_reference=?
                    AND (? IS NULL OR sequence < ?)
                ORDER BY sequence DESC LIMIT ?
                """,
                (self.account_key, family, before, before, limit),
            ).fetchall()
        return family, [dict(row) for row in rows]

    @staticmethod
    def _disposable_cache_filter(table: str) -> str:
        """A fixed SQL predicate retains identity metadata used by local history."""
        columns = {
            "item_references": "local_ref",
            "item_reference_aliases": "canonical_ref",
            "item_reference_remote_ids": "canonical_ref",
        }
        if table not in columns:
            return "1"
        column = columns[table]
        return (
            "NOT (item_kind='task' AND EXISTS (SELECT 1 FROM task_history_members h "  # noqa: S608
            f"WHERE h.account_key={table}.account_key AND h.reference={table}.{column}))"
        )

    def clear_disposable_cache(self) -> dict[str, int]:
        """Clear rebuildable mailbox metadata without resetting monitor continuity."""

        self.initialize()
        cache_tables = (
            "contact_history",
            "folder_cache",
            "item_reference_aliases",
            "item_reference_remote_ids",
            "item_references",
            "message_list_cache",
        )
        counts: dict[str, int] = {}
        with self.transaction(immediate=True) as connection:
            for table in cache_tables:
                predicate = self._disposable_cache_filter(table)
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {predicate}"  # noqa: S608
                ).fetchone()[0]
                connection.execute(f"DELETE FROM {table} WHERE {predicate}")  # noqa: S608
                counts[table] = int(count)
        return counts

    def disposable_cache_counts(self) -> dict[str, int]:
        """Preview exactly the rebuildable rows that :meth:`clear_disposable_cache` removes."""

        self.initialize()
        cache_tables = (
            "contact_history",
            "folder_cache",
            "item_reference_aliases",
            "item_reference_remote_ids",
            "item_references",
            "message_list_cache",
        )
        with self.connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE "  # noqa: S608 - fixed allowlist
                        + self._disposable_cache_filter(table)
                    ).fetchone()[0]
                )
                for table in cache_tables
            }

    def put_message_list(self, folder: str, payload: list[dict[str, Any]]) -> None:
        """Remember the first page of a folder so the next start can show it at once."""

        clean_folder = _text(folder, label="Folder", maximum=1_024)
        self.initialize()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO message_list_cache(account_key, folder, payload_json, cached_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(account_key, folder) DO UPDATE SET "
                "payload_json=excluded.payload_json, cached_at=excluded.cached_at",
                (self.account_key, clean_folder, json.dumps(payload, ensure_ascii=False), iso()),
            )

    def get_message_list(self, folder: str) -> list[dict[str, Any]] | None:
        clean_folder = _text(folder, label="Folder", maximum=1_024)
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM message_list_cache WHERE account_key=? AND folder=?",
                (self.account_key, clean_folder),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row[0]))
        except ValueError:
            return None
        return payload if isinstance(payload, list) else None

    def clear_cache(self) -> dict[str, int]:
        """Backward-compatible name for clearing only disposable cache records."""

        return self.clear_disposable_cache()

    def claim_watcher_lease(
        self,
        *,
        scope: str,
        lease_id: str,
        owner_id: str,
        process_id: int,
        profile_id: str = "default",
        ttl_seconds: int = 120,
        now: datetime | None = None,
        host_name: str | None = None,
        strategy: str | None = None,
    ) -> WatcherRuntimeRecord | None:
        """Atomically claim an expired/free watcher lease.

        ``None`` means another live owner holds the account/profile scope.  Supplying the
        same lease ID renews an existing ownership record without resetting counters.
        """

        if not 5 <= ttl_seconds <= 86_400:
            raise LocalStateError("Watcher lease duration must be between 5 and 86400 seconds.")
        account = self.account_key
        clean_scope = _text(scope, label="Watcher scope", maximum=512)
        clean_lease = _text(lease_id, label="Watcher lease ID", maximum=512)
        clean_owner = _text(owner_id, label="Watcher owner ID", maximum=512)
        clean_profile = _text(profile_id, label="Watcher profile ID", maximum=512)
        if process_id <= 0:
            raise LocalStateError("Watcher process ID must be positive.")
        current = _aware_utc(now or utc_now(), label="Watcher lease time")
        current_text = iso(current)
        expires_text = iso(current + timedelta(seconds=ttl_seconds))
        clean_host = host_name.strip()[:512] if host_name else None
        clean_strategy = strategy.strip()[:200] if strategy else None
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO watcher_runtime(
                    account_key, scope, profile_id, lease_id, owner_id, process_id,
                    host_name, state, strategy, connection_state, acquired_at,
                    started_at, heartbeat_at, lease_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, 'starting', ?, ?, ?, ?, ?)
                ON CONFLICT(account_key, scope) DO UPDATE SET
                    profile_id=excluded.profile_id,
                    lease_id=excluded.lease_id,
                    owner_id=excluded.owner_id,
                    process_id=excluded.process_id,
                    host_name=excluded.host_name,
                    state='running',
                    strategy=COALESCE(excluded.strategy, watcher_runtime.strategy),
                    connection_state='starting',
                    acquired_at=CASE
                        WHEN watcher_runtime.lease_id=excluded.lease_id
                        THEN watcher_runtime.acquired_at
                        ELSE excluded.acquired_at
                    END,
                    started_at=CASE
                        WHEN watcher_runtime.lease_id=excluded.lease_id
                        THEN watcher_runtime.started_at
                        ELSE excluded.started_at
                    END,
                    heartbeat_at=excluded.heartbeat_at,
                    lease_until=excluded.lease_until,
                    error_summary=NULL,
                    updated_at=excluded.updated_at
                WHERE watcher_runtime.lease_id=excluded.lease_id
                   OR watcher_runtime.lease_until IS NULL
                   OR watcher_runtime.lease_until <= excluded.acquired_at
                """,
                (
                    account,
                    clean_scope,
                    clean_profile,
                    clean_lease,
                    clean_owner,
                    process_id,
                    clean_host,
                    clean_strategy,
                    current_text,
                    current_text,
                    current_text,
                    expires_text,
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM watcher_runtime WHERE account_key=? AND scope=?",
                (account, clean_scope),
            ).fetchone()
        return _watcher_record(cast(sqlite3.Row, row))

    def heartbeat_watcher(
        self,
        scope: str,
        lease_id: str,
        *,
        ttl_seconds: int = 120,
        now: datetime | None = None,
        state: str = "running",
        strategy: str | None = None,
        connection_state: str | None = None,
        last_success_at: datetime | None = None,
        last_event_at: datetime | None = None,
        next_poll_at: datetime | None = None,
        backoff_until: datetime | None = None,
        detected_delta: int = 0,
        delivered_delta: int = 0,
        failed_delta: int = 0,
        error_summary: str | None = None,
    ) -> WatcherRuntimeRecord | None:
        """Renew a live lease using compare-and-swap ownership semantics."""

        if not 5 <= ttl_seconds <= 86_400:
            raise LocalStateError("Watcher lease duration must be between 5 and 86400 seconds.")
        if min(detected_delta, delivered_delta, failed_delta) < 0:
            raise LocalStateError("Watcher counters may only increase.")
        clean_scope = _text(scope, label="Watcher scope", maximum=512)
        clean_lease = _text(lease_id, label="Watcher lease ID", maximum=512)
        clean_state = _text(state, label="Watcher state", maximum=100)
        current = _aware_utc(now or utc_now(), label="Watcher heartbeat time")
        current_text = iso(current)

        def optional_time(value: datetime | None, label: str) -> str | None:
            return iso(_aware_utc(value, label=label)) if value else None

        clean_error = error_summary.replace("\x00", "")[:2_000] if error_summary else None
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE watcher_runtime SET
                    state=?,
                    strategy=COALESCE(?, strategy),
                    connection_state=COALESCE(?, connection_state),
                    heartbeat_at=?,
                    lease_until=?,
                    last_success_at=COALESCE(?, last_success_at),
                    last_event_at=COALESCE(?, last_event_at),
                    next_poll_at=?,
                    backoff_until=?,
                    detected_count=detected_count+?,
                    delivered_count=delivered_count+?,
                    failed_count=failed_count+?,
                    error_summary=?,
                    updated_at=?
                WHERE account_key=? AND scope=? AND lease_id=? AND lease_until>?
                """,
                (
                    clean_state,
                    strategy.strip()[:200] if strategy else None,
                    connection_state.strip()[:200] if connection_state else None,
                    current_text,
                    iso(current + timedelta(seconds=ttl_seconds)),
                    optional_time(last_success_at, "Last successful synchronization"),
                    optional_time(last_event_at, "Last watcher event"),
                    optional_time(next_poll_at, "Next watcher poll"),
                    optional_time(backoff_until, "Watcher backoff"),
                    detected_delta,
                    delivered_delta,
                    failed_delta,
                    clean_error,
                    current_text,
                    self.account_key,
                    clean_scope,
                    clean_lease,
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM watcher_runtime WHERE account_key=? AND scope=?",
                (self.account_key, clean_scope),
            ).fetchone()
        return _watcher_record(cast(sqlite3.Row, row))

    def release_watcher_lease(
        self,
        scope: str,
        lease_id: str,
        *,
        state: str = "stopped",
        at: datetime | None = None,
        error_summary: str | None = None,
    ) -> bool:
        current = _aware_utc(at or utc_now(), label="Watcher release time")
        clean_scope = _text(scope, label="Watcher scope", maximum=512)
        clean_lease = _text(lease_id, label="Watcher lease ID", maximum=512)
        clean_state = _text(state, label="Watcher state", maximum=100)
        clean_error = error_summary.replace("\x00", "")[:2_000] if error_summary else None
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE watcher_runtime SET state=?, lease_id=NULL, owner_id=NULL,
                    process_id=NULL, heartbeat_at=?, lease_until=NULL,
                    connection_state='disconnected', error_summary=?, updated_at=?
                WHERE account_key=? AND scope=? AND lease_id=?
                """,
                (
                    clean_state,
                    iso(current),
                    clean_error,
                    iso(current),
                    self.account_key,
                    clean_scope,
                    clean_lease,
                ),
            )
        return cursor.rowcount == 1

    def watcher_status(self, scope: str) -> WatcherRuntimeRecord | None:
        clean_scope = _text(scope, label="Watcher scope", maximum=512)
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watcher_runtime WHERE account_key=? AND scope=?",
                (self.account_key, clean_scope),
            ).fetchone()
        return _watcher_record(cast(sqlite3.Row, row)) if row else None

    def list_watcher_status(self) -> tuple[WatcherRuntimeRecord, ...]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM watcher_runtime WHERE account_key=? ORDER BY scope",
                (self.account_key,),
            ).fetchall()
        return tuple(_watcher_record(row) for row in rows)

    def monitor_checkpoint(self, scope: str) -> str | None:
        """Return the current durable monitor cursor without changing it."""

        clean_scope = _text(scope, label="Monitor checkpoint scope", maximum=1_024)
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT sync_state FROM sync_checkpoints WHERE scope=?",
                (clean_scope,),
            ).fetchone()
        return cast(str | None, row[0]) if row else None

    def commit_monitor_batch(
        self,
        scope: str,
        expected_checkpoint: str | None,
        new_checkpoint: str | None,
        events: Sequence[NotificationEvent],
        *,
        detected_at: datetime | None = None,
        enqueue: bool = True,
    ) -> MonitorBatchCommitResult:
        """Commit deduplication, event outbox, and checkpoint as one transaction.

        The cursor update is compare-and-swap.  ``committed=False`` tells a competing
        watcher to discard its stale result rather than advancing over another process.
        Baseline discovery may pass ``enqueue=False`` while still persisting seen IDs.
        """

        clean_scope = _text(scope, label="Monitor checkpoint scope", maximum=1_024)
        detected = _aware_utc(detected_at or utc_now(), label="Monitor detection time")
        detected_text = iso(detected)
        event_rows: list[tuple[NotificationEvent, str]] = []
        for event in events:
            _text(event.event_id, label="Notification event ID", maximum=1_024)
            if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
                raise LocalStateError("Notification event time must include a time zone.")
            payload = _json(
                cast(Mapping[str, Any], to_primitive(event)),
                label="Notification event payload",
            )
            event_rows.append((event, payload))

        inserted: list[str] = []
        duplicates = 0
        with self.transaction(immediate=True) as connection:
            checkpoint_row = connection.execute(
                "SELECT sync_state FROM sync_checkpoints WHERE scope=?",
                (clean_scope,),
            ).fetchone()
            current = cast(str | None, checkpoint_row[0]) if checkpoint_row else None
            if current != expected_checkpoint:
                return MonitorBatchCommitResult(False, current)

            for event, payload in event_rows:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_items(
                        event_key, item_ref, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (event.event_id, event.message_reference, detected_text, detected_text),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        "UPDATE seen_items SET last_seen_at=? WHERE event_key=?",
                        (detected_text, event.event_id),
                    )
                    duplicates += 1
                    continue
                inserted.append(event.event_id)
                if enqueue:
                    connection.execute(
                        """
                        INSERT INTO event_ledger(
                            account_key, event_id, dedupe_key, event_type, occurred_at,
                            detected_at, folder, item_reference, payload_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_key, event_id) DO NOTHING
                        """,
                        (
                            self.account_key,
                            event.event_id,
                            event.event_id,
                            event.event_type,
                            iso(_aware_utc(event.occurred_at, label="Notification event time")),
                            detected_text,
                            event.folder,
                            event.message_reference,
                            payload,
                            detected_text,
                            detected_text,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO notification_outbox(
                            account_key, event_id, folder, state, next_attempt_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                        ON CONFLICT(account_key, event_id) DO NOTHING
                        """,
                        (
                            self.account_key,
                            event.event_id,
                            event.folder,
                            detected_text,
                            detected_text,
                            detected_text,
                        ),
                    )

            connection.execute(
                """
                INSERT INTO sync_checkpoints(
                    scope, sync_state, last_success_at, last_error, updated_at
                ) VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    sync_state=excluded.sync_state,
                    last_success_at=excluded.last_success_at,
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (clean_scope, new_checkpoint, detected_text, detected_text),
            )
        return MonitorBatchCommitResult(
            True,
            new_checkpoint,
            tuple(inserted),
            duplicates,
        )

    def enqueue_notification_event(
        self,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        detected_at: datetime,
        payload: Mapping[str, Any],
        folder: str | None = None,
        item_reference: str | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        """Persist a provider-neutral event and its outbox row exactly once."""

        clean_event = _text(event_id, label="Notification event ID", maximum=1_024)
        clean_dedupe = _text(
            dedupe_key or clean_event,
            label="Notification deduplication key",
            maximum=2_048,
        )
        clean_type = _text(event_type, label="Notification event type", maximum=200)
        occurred = iso(_aware_utc(occurred_at, label="Notification event time"))
        detected = iso(_aware_utc(detected_at, label="Notification detection time"))
        payload_json = _json(payload, label="Notification event payload")
        now = iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_ledger(
                    account_key, event_id, dedupe_key, event_type, occurred_at,
                    detected_at, folder, item_reference, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_key,
                    clean_event,
                    clean_dedupe,
                    clean_type,
                    occurred,
                    detected,
                    folder,
                    item_reference,
                    payload_json,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO notification_outbox(
                    account_key, event_id, folder, state, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (self.account_key, clean_event, folder, detected, now, now),
            )
        return True

    def get_event(self, event_id: str) -> EventLedgerRecord | None:
        clean_event = _text(event_id, label="Notification event ID", maximum=1_024)
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_ledger WHERE account_key=? AND event_id=?",
                (self.account_key, clean_event),
            ).fetchone()
        return _event_record(cast(sqlite3.Row, row)) if row else None

    def list_event_ledger(
        self,
        *,
        event_type: str | None = None,
        after: datetime | None = None,
        limit: int = 100,
    ) -> tuple[EventLedgerRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise LocalStateError("Event-ledger limit must be between 1 and 10000.")
        query = "SELECT * FROM event_ledger WHERE account_key=?"
        params: list[Any] = [self.account_key]
        if event_type:
            query += " AND event_type=?"
            params.append(_text(event_type, label="Notification event type", maximum=200))
        if after:
            query += " AND detected_at>=?"
            params.append(iso(_aware_utc(after, label="Event history start")))
        query += " ORDER BY detected_at DESC, event_id LIMIT ?"
        params.append(limit)
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_event_record(row) for row in rows)

    @staticmethod
    def _outbox_select() -> str:
        return """
            SELECT
                events.account_key,
                events.event_id,
                events.dedupe_key,
                events.event_type,
                events.occurred_at,
                events.detected_at,
                events.folder,
                events.item_reference,
                events.payload_json,
                events.created_at,
                events.updated_at AS event_updated_at,
                outbox.state AS outbox_state,
                outbox.delivery_cycles,
                outbox.next_attempt_at,
                outbox.lease_owner,
                outbox.lease_until,
                outbox.created_at AS outbox_created_at,
                outbox.updated_at AS outbox_updated_at
            FROM notification_outbox AS outbox
            JOIN event_ledger AS events
              ON events.account_key=outbox.account_key
             AND events.event_id=outbox.event_id
        """

    def list_notification_outbox(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
    ) -> tuple[NotificationOutboxRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise LocalStateError("Notification-outbox limit must be between 1 and 10000.")
        query = self._outbox_select() + " WHERE outbox.account_key=?"
        params: list[Any] = [self.account_key]
        if state:
            query += " AND outbox.state=?"
            params.append(_text(state, label="Notification outbox state", maximum=100))
        query += " ORDER BY outbox.created_at DESC, outbox.event_id LIMIT ?"
        params.append(limit)
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_outbox_record(row) for row in rows)

    def claim_notification_outbox(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 120,
        folder: str | None = None,
        limit: int = 100,
    ) -> tuple[NotificationOutboxRecord, ...]:
        """Claim due provider work without allowing concurrent duplicate delivery."""

        if not 1 <= limit <= 1_000:
            raise LocalStateError("Notification-outbox claim limit must be between 1 and 1000.")
        if not 5 <= lease_seconds <= 86_400:
            raise LocalStateError("Outbox lease duration must be between 5 and 86400 seconds.")
        worker = _text(worker_id, label="Notification worker ID", maximum=512)
        current = _aware_utc(now or utc_now(), label="Outbox claim time")
        current_text = iso(current)
        lease_until = iso(current + timedelta(seconds=lease_seconds))
        with self.transaction(immediate=True) as connection:
            query = """
                SELECT event_id FROM notification_outbox
                WHERE account_key=?
                  AND state IN ('pending', 'retry_scheduled', 'queued')
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                  AND (lease_until IS NULL OR lease_until<=?)
            """
            params: list[Any] = [self.account_key, current_text, current_text]
            if folder is not None:
                query += " AND folder=? COLLATE NOCASE"
                params.append(folder)
            query += " ORDER BY created_at, event_id LIMIT ?"
            params.append(limit)
            ids = [str(row[0]) for row in connection.execute(query, params).fetchall()]
            for event_id in ids:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET lease_owner=?, lease_until=?, updated_at=?
                    WHERE account_key=? AND event_id=?
                    """,
                    (worker, lease_until, current_text, self.account_key, event_id),
                )
            if not ids:
                return ()
            placeholders = ",".join("?" for _ in ids)
            rows = connection.execute(
                self._outbox_select()
                + f" WHERE outbox.account_key=? AND outbox.event_id IN ({placeholders})"
                + " ORDER BY outbox.created_at, outbox.event_id",
                (self.account_key, *ids),
            ).fetchall()
        return tuple(_outbox_record(row) for row in rows)

    def next_notification_due_at(self) -> datetime | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(COALESCE(next_attempt_at, created_at))
                FROM notification_outbox
                WHERE account_key=? AND state IN ('pending', 'retry_scheduled', 'queued')
                """,
                (self.account_key,),
            ).fetchone()
        return _datetime(cast(str | None, row[0])) if row else None

    def update_notification_outbox(
        self,
        event_id: str,
        state: str,
        *,
        at: datetime | None = None,
        next_attempt_at: datetime | None = None,
        worker_id: str | None = None,
        increment_cycle: bool = True,
    ) -> NotificationOutboxRecord:
        clean_event = _text(event_id, label="Notification event ID", maximum=1_024)
        clean_state = _text(state, label="Notification outbox state", maximum=100)
        current = _aware_utc(at or utc_now(), label="Outbox update time")
        params: tuple[Any, ...] = (
            clean_state,
            iso(_aware_utc(next_attempt_at, label="Next notification attempt"))
            if next_attempt_at
            else None,
            int(increment_cycle),
            iso(current),
            self.account_key,
            clean_event,
        )
        with self.transaction(immediate=True) as connection:
            if worker_id is None:
                cursor = connection.execute(
                    """
                    UPDATE notification_outbox SET state=?, next_attempt_at=?,
                        delivery_cycles=delivery_cycles+?, lease_owner=NULL, lease_until=NULL,
                        updated_at=? WHERE account_key=? AND event_id=?
                    """,
                    params,
                )
            else:
                owner = _text(worker_id, label="Notification worker ID", maximum=512)
                cursor = connection.execute(
                    """
                    UPDATE notification_outbox SET state=?, next_attempt_at=?,
                        delivery_cycles=delivery_cycles+?, lease_owner=NULL, lease_until=NULL,
                        updated_at=?
                    WHERE account_key=? AND event_id=? AND lease_owner=?
                    """,
                    (*params, owner),
                )
            if cursor.rowcount != 1:
                raise LocalStateError(
                    "Notification outbox entry was not found or is owned by another worker."
                )
            row = connection.execute(
                self._outbox_select() + " WHERE outbox.account_key=? AND outbox.event_id=?",
                (self.account_key, clean_event),
            ).fetchone()
        return _outbox_record(cast(sqlite3.Row, row))

    def record_provider_attempt(
        self,
        event_id: str,
        provider_id: str,
        state: str,
        *,
        attempted_at: datetime | None = None,
        status_code: int | None = None,
        error_summary: str | None = None,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> ProviderAttemptRecord:
        clean_event = _text(event_id, label="Notification event ID", maximum=1_024)
        provider = _text(provider_id, label="Notification provider ID", maximum=512).casefold()
        clean_state = _text(state, label="Notification delivery state", maximum=100)
        if status_code is not None and not 100 <= status_code <= 599:
            raise LocalStateError("Notification status code must be between 100 and 599.")
        current = _aware_utc(attempted_at or utc_now(), label="Notification attempt time")
        clean_error = error_summary.replace("\x00", "")[:2_000] if error_summary else None
        retry_text = (
            iso(_aware_utc(retry_at, label="Notification retry time")) if retry_at else None
        )
        with self.transaction(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM event_ledger WHERE account_key=? AND event_id=?",
                (self.account_key, clean_event),
            ).fetchone()
            if exists is None:
                raise LocalStateError(f"Notification event not found: {clean_event}")
            attempt_no = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0) + 1
                    FROM notification_provider_attempts
                    WHERE account_key=? AND event_id=? AND provider_id=?
                    """,
                    (self.account_key, clean_event, provider),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO notification_provider_attempts(
                    account_key, event_id, provider_id, attempt_no, state,
                    status_code, error_summary, retry_at, attempted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_key,
                    clean_event,
                    provider,
                    attempt_no,
                    clean_state,
                    status_code,
                    clean_error,
                    retry_text,
                    iso(current),
                ),
            )
            if dead_letter:
                reason = clean_error or f"Provider delivery entered {clean_state}."
                connection.execute(
                    """
                    INSERT INTO notification_dead_letters(
                        account_key, event_id, provider_id, reason, failed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(account_key, event_id, provider_id) DO UPDATE SET
                        reason=excluded.reason,
                        failed_at=excluded.failed_at,
                        resolved_at=NULL,
                        resolution=NULL
                    """,
                    (self.account_key, clean_event, provider, reason, iso(current)),
                )
            row = connection.execute(
                "SELECT * FROM notification_provider_attempts WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return _provider_attempt_record(cast(sqlite3.Row, row))

    def list_provider_attempts(
        self,
        *,
        event_id: str | None = None,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ProviderAttemptRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise LocalStateError("Provider-attempt limit must be between 1 and 10000.")
        query = "SELECT * FROM notification_provider_attempts WHERE account_key=?"
        params: list[Any] = [self.account_key]
        if event_id:
            query += " AND event_id=?"
            params.append(_text(event_id, label="Notification event ID", maximum=1_024))
        if provider_id:
            query += " AND provider_id=?"
            params.append(
                _text(provider_id, label="Notification provider ID", maximum=512).casefold()
            )
        query += " ORDER BY attempted_at DESC, id DESC LIMIT ?"
        params.append(limit)
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_provider_attempt_record(row) for row in rows)

    def list_dead_letters(
        self,
        *,
        include_resolved: bool = False,
        limit: int = 100,
    ) -> tuple[DeadLetterRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise LocalStateError("Dead-letter limit must be between 1 and 10000.")
        query = "SELECT * FROM notification_dead_letters WHERE account_key=?"
        if not include_resolved:
            query += " AND resolved_at IS NULL"
        query += " ORDER BY failed_at DESC, event_id, provider_id LIMIT ?"
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, (self.account_key, limit)).fetchall()
        return tuple(_dead_letter_record(row) for row in rows)

    def resolve_dead_letter(
        self,
        event_id: str,
        provider_id: str,
        *,
        resolution: str,
        resolved_at: datetime | None = None,
    ) -> bool:
        clean_event = _text(event_id, label="Notification event ID", maximum=1_024)
        provider = _text(provider_id, label="Notification provider ID", maximum=512).casefold()
        clean_resolution = _text(
            resolution.replace("\x00", ""),
            label="Dead-letter resolution",
            maximum=2_000,
        )
        current = _aware_utc(resolved_at or utc_now(), label="Dead-letter resolution time")
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE notification_dead_letters SET resolved_at=?, resolution=?
                WHERE account_key=? AND event_id=? AND provider_id=?
                  AND resolved_at IS NULL
                """,
                (iso(current), clean_resolution, self.account_key, clean_event, provider),
            )
        return cursor.rowcount == 1

    def record_command_usage(
        self,
        command_id: str,
        *,
        context: str = "global",
        used_at: datetime | None = None,
    ) -> CommandUsageRecord:
        command = _text(command_id, label="Command ID", maximum=1_024).casefold()
        clean_context = _text(context, label="Command context", maximum=512).casefold()
        used = _aware_utc(used_at or utc_now(), label="Command usage time")
        used_text = iso(used)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO command_usage(
                    account_key, context, command_id, use_count,
                    first_used_at, last_used_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(account_key, context, command_id) DO UPDATE SET
                    use_count=command_usage.use_count+1,
                    last_used_at=excluded.last_used_at
                """,
                (self.account_key, clean_context, command, used_text, used_text),
            )
            row = connection.execute(
                """
                SELECT * FROM command_usage
                WHERE account_key=? AND context=? AND command_id=?
                """,
                (self.account_key, clean_context, command),
            ).fetchone()
        return _command_usage_record(cast(sqlite3.Row, row))

    def list_command_usage(
        self,
        *,
        context: str | None = None,
        limit: int = 50,
    ) -> tuple[CommandUsageRecord, ...]:
        if not 1 <= limit <= 1_000:
            raise LocalStateError("Command-usage limit must be between 1 and 1000.")
        query = "SELECT * FROM command_usage WHERE account_key=?"
        params: list[Any] = [self.account_key]
        if context is not None:
            query += " AND context=?"
            params.append(_text(context, label="Command context", maximum=512).casefold())
        query += " ORDER BY use_count DESC, last_used_at DESC, command_id LIMIT ?"
        params.append(limit)
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_command_usage_record(row) for row in rows)

    def load_help_state(self) -> HelpUserState:
        """Implement the embedded Help persistence protocol without importing it eagerly."""

        from gucmail.help.models import HelpUserState

        self.initialize()
        with self.connection() as connection:
            recent = connection.execute(
                """
                SELECT page_id FROM help_history
                WHERE account_key=? ORDER BY position LIMIT 200
                """,
                (self.account_key,),
            ).fetchall()
            favorites = connection.execute(
                """
                SELECT page_id FROM help_favorites
                WHERE account_key=? ORDER BY page_id COLLATE NOCASE LIMIT 500
                """,
                (self.account_key,),
            ).fetchall()
            queries = connection.execute(
                """
                SELECT query FROM help_queries
                WHERE account_key=? ORDER BY position LIMIT 200
                """,
                (self.account_key,),
            ).fetchall()
        return HelpUserState(
            recent_page_ids=tuple(str(row[0]) for row in recent),
            favorite_page_ids=frozenset(str(row[0]) for row in favorites),
            recent_queries=tuple(str(row[0]) for row in queries),
        )

    def save_help_state(self, state: HelpUserState) -> None:
        """Replace bounded, non-sensitive Help history/favorites atomically."""

        recent = tuple(dict.fromkeys(state.recent_page_ids))[:200]
        favorites = tuple(sorted(set(state.favorite_page_ids), key=str.casefold))[:500]
        queries = tuple(dict.fromkeys(state.recent_queries))[:200]
        for page_id in (*recent, *favorites):
            _text(page_id, label="Help page ID", maximum=1_024)
        for query in queries:
            _text(query, label="Help query", maximum=2_048)
        now = iso()
        with self.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM help_history WHERE account_key=?", (self.account_key,))
            connection.execute(
                "DELETE FROM help_favorites WHERE account_key=?", (self.account_key,)
            )
            connection.execute("DELETE FROM help_queries WHERE account_key=?", (self.account_key,))
            connection.executemany(
                """
                INSERT INTO help_history(account_key, page_id, position, viewed_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (self.account_key, page_id, position, now)
                    for position, page_id in enumerate(recent)
                ),
            )
            connection.executemany(
                """
                INSERT INTO help_favorites(account_key, page_id, created_at)
                VALUES (?, ?, ?)
                """,
                ((self.account_key, page_id, now) for page_id in favorites),
            )
            connection.executemany(
                """
                INSERT INTO help_queries(account_key, query, position, searched_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (self.account_key, query, position, now)
                    for position, query in enumerate(queries)
                ),
            )

    def put_ui_preference(
        self,
        key: str,
        value: Any,
        *,
        scope: str = "global",
        expected_revision: int | None = None,
    ) -> UiPreferenceRecord:
        """Persist a non-secret UI/session value with optional compare-and-swap."""

        clean_key = _text(key, label="UI preference key", maximum=1_024).casefold()
        clean_scope = _text(scope, label="UI preference scope", maximum=512).casefold()
        payload = _json(value, label="UI preference", maximum_bytes=262_144)
        now = iso()
        with self.transaction(immediate=True) as connection:
            if expected_revision is None:
                connection.execute(
                    """
                    INSERT INTO ui_preferences(
                        account_key, scope, preference_key, value_json, revision, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(account_key, scope, preference_key) DO UPDATE SET
                        value_json=excluded.value_json,
                        revision=ui_preferences.revision+1,
                        updated_at=excluded.updated_at
                    """,
                    (self.account_key, clean_scope, clean_key, payload, now),
                )
            elif expected_revision == 0:
                try:
                    connection.execute(
                        """
                        INSERT INTO ui_preferences(
                            account_key, scope, preference_key, value_json,
                            revision, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (self.account_key, clean_scope, clean_key, payload, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LocalStateError(
                        "UI preference changed concurrently; reload it before saving."
                    ) from exc
            elif expected_revision > 0:
                cursor = connection.execute(
                    """
                    UPDATE ui_preferences SET value_json=?, revision=revision+1, updated_at=?
                    WHERE account_key=? AND scope=? AND preference_key=? AND revision=?
                    """,
                    (
                        payload,
                        now,
                        self.account_key,
                        clean_scope,
                        clean_key,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LocalStateError(
                        "UI preference changed concurrently; reload it before saving."
                    )
            else:
                raise LocalStateError("Expected UI preference revision may not be negative.")
            row = connection.execute(
                """
                SELECT * FROM ui_preferences
                WHERE account_key=? AND scope=? AND preference_key=?
                """,
                (self.account_key, clean_scope, clean_key),
            ).fetchone()
        return _ui_preference_record(cast(sqlite3.Row, row))

    def get_ui_preference(
        self,
        key: str,
        *,
        scope: str = "global",
    ) -> UiPreferenceRecord | None:
        clean_key = _text(key, label="UI preference key", maximum=1_024).casefold()
        clean_scope = _text(scope, label="UI preference scope", maximum=512).casefold()
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM ui_preferences
                WHERE account_key=? AND scope=? AND preference_key=?
                """,
                (self.account_key, clean_scope, clean_key),
            ).fetchone()
        return _ui_preference_record(cast(sqlite3.Row, row)) if row else None

    def list_ui_preferences(self, *, scope: str | None = None) -> tuple[UiPreferenceRecord, ...]:
        query = "SELECT * FROM ui_preferences WHERE account_key=?"
        params: list[Any] = [self.account_key]
        if scope is not None:
            query += " AND scope=?"
            params.append(_text(scope, label="UI preference scope", maximum=512).casefold())
        query += " ORDER BY scope, preference_key"
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_ui_preference_record(row) for row in rows)

    def remove_ui_preference(self, key: str, *, scope: str = "global") -> bool:
        clean_key = _text(key, label="UI preference key", maximum=1_024).casefold()
        clean_scope = _text(scope, label="UI preference scope", maximum=512).casefold()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM ui_preferences
                WHERE account_key=? AND scope=? AND preference_key=?
                """,
                (self.account_key, clean_scope, clean_key),
            )
        return cursor.rowcount == 1

    def record_quota_observation(
        self,
        metric: str,
        value_bytes: int | None,
        *,
        source: str,
        confidence: str,
        observed_at: datetime | None = None,
        detail: str | None = None,
    ) -> QuotaObservationRecord:
        clean_metric = _text(metric, label="Quota metric", maximum=100).casefold()
        if clean_metric not in _QUOTA_METRICS:
            raise LocalStateError(f"Unsupported quota metric: {clean_metric}")
        if value_bytes is not None and value_bytes < 0:
            raise LocalStateError("Quota observations may not contain negative byte values.")
        clean_source = _text(source, label="Quota source", maximum=100).casefold()
        clean_confidence = _text(confidence, label="Quota confidence", maximum=100).casefold()
        observed = _aware_utc(observed_at or utc_now(), label="Quota observation time")
        clean_detail = detail.replace("\x00", "")[:2_000] if detail else None
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO quota_observations(
                    account_key, metric, value_bytes, source,
                    confidence, detail, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_key,
                    clean_metric,
                    value_bytes,
                    clean_source,
                    clean_confidence,
                    clean_detail,
                    iso(observed),
                ),
            )
            row = connection.execute(
                "SELECT * FROM quota_observations WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return _quota_observation_record(cast(sqlite3.Row, row))

    def latest_quota_observations(self) -> tuple[QuotaObservationRecord, ...]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT observations.*
                FROM quota_observations AS observations
                JOIN (
                    SELECT metric, MAX(id) AS newest_id
                    FROM quota_observations
                    WHERE account_key=?
                    GROUP BY metric
                ) AS newest ON newest.newest_id=observations.id
                ORDER BY observations.metric
                """,
                (self.account_key,),
            ).fetchall()
        return tuple(_quota_observation_record(row) for row in rows)

    def quota_observation_history(
        self,
        metric: str,
        *,
        limit: int = 100,
    ) -> tuple[QuotaObservationRecord, ...]:
        clean_metric = _text(metric, label="Quota metric", maximum=100).casefold()
        if clean_metric not in _QUOTA_METRICS:
            raise LocalStateError(f"Unsupported quota metric: {clean_metric}")
        if not 1 <= limit <= 10_000:
            raise LocalStateError("Quota-history limit must be between 1 and 10000.")
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM quota_observations
                WHERE account_key=? AND metric=?
                ORDER BY observed_at DESC, id DESC LIMIT ?
                """,
                (self.account_key, clean_metric, limit),
            ).fetchall()
        return tuple(_quota_observation_record(row) for row in rows)

    def put_reference(
        self,
        local_ref: str,
        item_kind: str,
        ews_id: str,
        *,
        change_key: str | None = None,
        folder_path: str | None = None,
        account_key: str | None = None,
    ) -> str:
        """Store an EWS identity and return its canonical, move-stable local reference."""

        self.initialize()
        account = normalize_account_key(account_key or self.account_key)
        local = self._validate_reference_part(local_ref, label="Local reference")
        kind = self._validate_reference_part(item_kind, label="Item kind").casefold()
        remote = self._validate_reference_part(ews_id, label="EWS item ID")
        now = iso()
        with self.transaction(immediate=True) as connection:
            by_local = self._canonical_reference_row(
                connection,
                account_key=account,
                local_ref=local,
            )
            # A draft is an Exchange Message in the Drafts folder. Older clients
            # gave it an independent identity when the generic message list saw
            # it. Merge only this proven mail family, preserving both public refs.
            if kind in {"message", "draft"} and (
                by_local is None or by_local["item_kind"] in {"message", "draft"}
            ):
                old_remote = str(by_local["ews_id"]) if by_local is not None else remote
                siblings = connection.execute(
                    "SELECT * FROM item_references WHERE account_key=? "
                    "AND item_kind IN ('message', 'draft') AND ews_id IN (?, ?) "
                    "ORDER BY created_at, local_ref",
                    (account, remote, old_remote),
                ).fetchall()
                winner = by_local if by_local is not None else (siblings[0] if siblings else None)
                if winner is not None:
                    for sibling in siblings:
                        self._merge_reference_rows(
                            connection,
                            account_key=account,
                            winner_ref=str(winner["local_ref"]),
                            loser_ref=str(sibling["local_ref"]),
                            now=now,
                        )
                    kind = str(winner["item_kind"])
                elif by_local is None:
                    historical = connection.execute(
                        "SELECT canonical.item_kind FROM item_reference_remote_ids AS history "
                        "JOIN item_references AS canonical "
                        "ON canonical.account_key=history.account_key "
                        "AND canonical.local_ref=history.canonical_ref "
                        "WHERE history.account_key=? AND history.ews_id=? "
                        "AND history.item_kind IN ('message', 'draft') LIMIT 1",
                        (account, remote),
                    ).fetchone()
                    if historical is not None:
                        kind = str(historical["item_kind"])
            if by_local is not None and by_local["item_kind"] != kind:
                raise LocalStateError(
                    f"Local reference {local!r} is already assigned to a different item kind."
                )
            by_remote = self._remote_reference_row(
                connection,
                account_key=account,
                item_kind=kind,
                ews_id=remote,
            )
            history = self._remote_history_row(
                connection,
                account_key=account,
                item_kind=kind,
                ews_id=remote,
            )

            if by_local is not None:
                canonical = str(by_local["local_ref"])
                other_ref: str | None = None
                if by_remote is not None and by_remote["local_ref"] != canonical:
                    other_ref = str(by_remote["local_ref"])
                elif history is not None and history["canonical_ref"] != canonical:
                    other_ref = str(history["canonical_ref"])
                if other_ref is not None:
                    self._merge_reference_rows(
                        connection,
                        account_key=account,
                        winner_ref=canonical,
                        loser_ref=other_ref,
                        now=now,
                    )
                self._set_current_remote_id(
                    connection,
                    account_key=account,
                    canonical_ref=canonical,
                    item_kind=kind,
                    ews_id=remote,
                    change_key=change_key,
                    folder_path=folder_path,
                    now=now,
                )
                self._put_reference_alias(
                    connection,
                    account_key=account,
                    alias_ref=local,
                    canonical_ref=canonical,
                    item_kind=kind,
                    now=now,
                )
                return canonical

            if by_remote is not None:
                canonical = str(by_remote["local_ref"])
                self._put_reference_alias(
                    connection,
                    account_key=account,
                    alias_ref=local,
                    canonical_ref=canonical,
                    item_kind=kind,
                    now=now,
                )
                self._set_current_remote_id(
                    connection,
                    account_key=account,
                    canonical_ref=canonical,
                    item_kind=kind,
                    ews_id=remote,
                    change_key=change_key,
                    folder_path=folder_path,
                    now=now,
                )
                return canonical

            if history is not None:
                canonical = str(history["canonical_ref"])
                self._put_reference_alias(
                    connection,
                    account_key=account,
                    alias_ref=local,
                    canonical_ref=canonical,
                    item_kind=kind,
                    now=now,
                )
                self._set_current_remote_id(
                    connection,
                    account_key=account,
                    canonical_ref=canonical,
                    item_kind=kind,
                    ews_id=remote,
                    change_key=change_key,
                    folder_path=folder_path,
                    now=now,
                )
                return canonical

            connection.execute(
                """
                INSERT INTO item_references(
                    account_key, local_ref, item_kind, ews_id, change_key, folder_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account, local, kind, remote, change_key, folder_path, now, now),
            )
            connection.execute(
                """
                INSERT INTO item_reference_remote_ids(
                    account_key, item_kind, ews_id, canonical_ref, is_current,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (account, kind, remote, local, now, now),
            )
            return local

    def resolve_reference(
        self,
        local_ref: str,
        item_kind: str | None = None,
        *,
        account_key: str | None = None,
    ) -> dict[str, Any] | None:
        self.initialize()
        account = normalize_account_key(account_key or self.account_key)
        local = self._validate_reference_part(local_ref, label="Local reference")
        kind = item_kind.casefold().strip() if item_kind else None
        with self.connection() as connection:
            row = self._canonical_reference_row(
                connection,
                account_key=account,
                local_ref=local,
                item_kind=kind,
            )
            if row is None and kind in {"message", "draft"}:
                row = self._canonical_reference_row(
                    connection,
                    account_key=account,
                    local_ref=local,
                    item_kind="message" if kind == "draft" else "draft",
                )
        if row is None:
            return None
        result = dict(row)
        result["canonical_ref"] = str(row["local_ref"])
        result["requested_ref"] = local
        result["is_alias"] = local != row["local_ref"]
        return result

    def relocate_reference_folder_paths(self, old_path: str, new_path: str) -> int:
        """Rebase this account's retained folder paths without changing item identities."""

        old = self._validate_reference_part(old_path, label="Previous folder path")
        new = self._validate_reference_part(new_path, label="Replacement folder path")
        if not old.startswith("/") or not new.startswith("/") or old == "/" or new == "/":
            raise LocalStateError("Folder relocation requires non-root absolute mailbox paths.")
        if old == new:
            return 0
        prefix = old + "/"
        try:
            with self.transaction(immediate=True) as connection:
                result = connection.execute(
                    "UPDATE item_references SET folder_path=? || substr(folder_path, ?), "
                    "updated_at=? WHERE account_key=? AND "
                    "(folder_path=? OR substr(folder_path, 1, ?)=?)",
                    (new, len(old) + 1, iso(), self.account_key, old, len(prefix), prefix),
                )
                return result.rowcount
        except sqlite3.DatabaseError as exc:
            raise LocalStateError(
                "Retained folder paths could not be updated. The local transaction was rolled back."
            ) from exc

    def reference_history(
        self,
        local_ref: str,
        item_kind: str | None = None,
        *,
        account_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded local EWS-ID history for diagnostics and regression checks."""

        resolved = self.resolve_reference(
            local_ref,
            item_kind,
            account_key=account_key,
        )
        if resolved is None:
            return []
        account = normalize_account_key(account_key or self.account_key)
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT item_kind, ews_id, is_current, first_seen_at, last_seen_at
                FROM item_reference_remote_ids
                WHERE account_key=? AND canonical_ref=?
                ORDER BY first_seen_at, ews_id
                """,
                (account, resolved["canonical_ref"]),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _saved_search_name(name: str) -> str:
        normalized = str(name).strip()
        if not normalized:
            raise LocalStateError("Saved-search name may not be blank.")
        if len(normalized) > 120:
            raise LocalStateError("Saved-search name may not exceed 120 characters.")
        return normalized

    def save_search(self, name: str, query: dict[str, Any]) -> None:
        normalized_name = self._saved_search_name(name)
        now = iso()
        payload = json.dumps(query, ensure_ascii=False, sort_keys=True)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO saved_searches(
                    account_key, name, query_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_key, name) DO UPDATE SET query_json=excluded.query_json,
                    updated_at=excluded.updated_at
                """,
                (self.account_key, normalized_name, payload, now, now),
            )

    def list_searches(self) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT name, query_json, created_at, updated_at,
                       last_run_at, last_result_count, run_count
                FROM saved_searches
                WHERE account_key=?
                ORDER BY name COLLATE NOCASE
                """,
                (self.account_key,),
            ).fetchall()
        return [
            {
                "name": row["name"],
                "query": json.loads(row["query_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_run_at": row["last_run_at"],
                "last_result_count": row["last_result_count"],
                "run_count": int(row["run_count"]),
            }
            for row in rows
        ]

    def get_search(self, name: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT query_json FROM saved_searches
                WHERE account_key=? AND name=? COLLATE NOCASE
                """,
                (self.account_key, self._saved_search_name(name)),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_search_record(self, name: str) -> dict[str, Any] | None:
        normalized_name = self._saved_search_name(name)
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT name, query_json, created_at, updated_at,
                       last_run_at, last_result_count, run_count
                FROM saved_searches
                WHERE account_key=? AND name=? COLLATE NOCASE
                """,
                (self.account_key, normalized_name),
            ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "query": json.loads(row["query_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_run_at": row["last_run_at"],
            "last_result_count": row["last_result_count"],
            "run_count": int(row["run_count"]),
        }

    def rename_search(self, old_name: str, new_name: str) -> bool:
        old = self._saved_search_name(old_name)
        new = self._saved_search_name(new_name)
        with self.transaction(immediate=True) as connection:
            source = connection.execute(
                "SELECT name FROM saved_searches WHERE account_key=? AND name=? COLLATE NOCASE",
                (self.account_key, old),
            ).fetchone()
            if source is None:
                return False
            existing = connection.execute(
                "SELECT name FROM saved_searches WHERE account_key=? AND name=? COLLATE NOCASE",
                (self.account_key, new),
            ).fetchone()
            if existing is not None and str(existing["name"]).casefold() != old.casefold():
                raise LocalStateError(f"A saved search named {new!r} already exists.")
            connection.execute(
                """
                UPDATE saved_searches SET name=?, updated_at=?
                WHERE account_key=? AND name=? COLLATE NOCASE
                """,
                (new, iso(), self.account_key, old),
            )
        return True

    def duplicate_search(self, source_name: str, new_name: str) -> bool:
        source = self._saved_search_name(source_name)
        target = self._saved_search_name(new_name)
        now = iso()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT query_json FROM saved_searches
                WHERE account_key=? AND name=? COLLATE NOCASE
                """,
                (self.account_key, source),
            ).fetchone()
            if row is None:
                return False
            exists = connection.execute(
                "SELECT 1 FROM saved_searches WHERE account_key=? AND name=? COLLATE NOCASE",
                (self.account_key, target),
            ).fetchone()
            if exists is not None:
                raise LocalStateError(f"A saved search named {target!r} already exists.")
            connection.execute(
                """
                INSERT INTO saved_searches(
                    account_key, name, query_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self.account_key, target, row["query_json"], now, now),
            )
        return True

    def record_search_run(
        self,
        *,
        query: dict[str, Any],
        result_count: int,
        source: str,
        name: str | None = None,
        history_limit: int = 100,
        keep_history: bool = True,
    ) -> None:
        if result_count < 0:
            raise LocalStateError("Search result count may not be negative.")
        if source not in {"saved", "interactive", "palette", "cli"}:
            raise LocalStateError(f"Unsupported search history source: {source}")
        if not 1 <= history_limit <= 1000:
            raise LocalStateError("Search history limit must be between 1 and 1000.")
        normalized_name = self._saved_search_name(name) if name is not None else None
        payload = json.dumps(query, ensure_ascii=False, sort_keys=True)
        now = iso()
        with self.transaction(immediate=True) as connection:
            if normalized_name is not None:
                connection.execute(
                    """
                    UPDATE saved_searches
                    SET last_run_at=?, last_result_count=?, run_count=run_count + 1
                    WHERE account_key=? AND name=? COLLATE NOCASE
                    """,
                    (now, result_count, self.account_key, normalized_name),
                )
            if keep_history:
                connection.execute(
                    """
                    INSERT INTO search_history(
                        account_key, name, query_json, source, executed_at, result_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (self.account_key, normalized_name, payload, source, now, result_count),
                )
                connection.execute(
                    """
                    DELETE FROM search_history
                    WHERE account_key=? AND id NOT IN (
                        SELECT id FROM search_history WHERE account_key=?
                        ORDER BY executed_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (self.account_key, self.account_key, history_limit),
                )

    def list_search_history(self, *, limit: int = 25) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise LocalStateError("Search history limit must be between 1 and 1000.")
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, query_json, source, executed_at, result_count
                FROM search_history WHERE account_key=?
                ORDER BY executed_at DESC, id DESC LIMIT ?
                """,
                (self.account_key, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "query": json.loads(row["query_json"]),
                "source": row["source"],
                "executed_at": row["executed_at"],
                "result_count": int(row["result_count"]),
            }
            for row in rows
        ]

    def remove_search(self, name: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM saved_searches
                WHERE account_key=? AND name=? COLLATE NOCASE
                """,
                (self.account_key, self._saved_search_name(name)),
            )
        return cursor.rowcount > 0

    def checkpoint(
        self,
        scope: str,
        *,
        sync_state: str | None,
        succeeded: bool,
        error: str | None = None,
    ) -> None:
        now = iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sync_checkpoints(
                    scope, sync_state, last_success_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    sync_state=COALESCE(excluded.sync_state, sync_checkpoints.sync_state),
                    last_success_at=COALESCE(excluded.last_success_at,
                                             sync_checkpoints.last_success_at),
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (scope, sync_state, now if succeeded else None, error, now),
            )

    def reset_checkpoint(self, scope: str) -> None:
        """Remove one local checkpoint, retaining delivery history and deduplication."""
        with self.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM sync_checkpoints WHERE scope=?", (scope,))

    def remember_event(self, event_key: str, item_ref: str | None = None) -> bool:
        """Return True once for a new event key and False for duplicates."""

        now = iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO "
                "seen_items(event_key, item_ref, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (event_key, item_ref, now, now),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    "UPDATE seen_items SET last_seen_at=? WHERE event_key=?", (now, event_key)
                )
                return False
        return True

    def add_scheduled_job(
        self,
        *,
        reference: str,
        draft_reference: str,
        scheduled_for: datetime,
        timezone: str,
        original_expression: str,
        idempotency_key: str,
        require_final_confirmation: bool = False,
        attachment_manifest: Iterable[dict[str, Any]] = (),
        state: SendState = SendState.SCHEDULED,
        recurrence_id: str | None = None,
    ) -> None:
        now = iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO scheduled_jobs(
                    reference, draft_reference, scheduled_for, timezone, original_expression,
                    state, idempotency_key, require_final_confirmation, recurrence_id,
                    attachment_manifest_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference,
                    draft_reference,
                    iso(scheduled_for),
                    timezone,
                    original_expression,
                    state.value,
                    idempotency_key,
                    int(require_final_confirmation),
                    recurrence_id,
                    json.dumps(list(attachment_manifest), sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, occurred_at) VALUES (?, ?, ?)",
                (reference, "created", now),
            )

    def list_scheduled_jobs(
        self,
        *,
        states: Iterable[SendState] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and not 1 <= limit <= 10_000:
            raise LocalStateError("Scheduled-job list limit must be between 1 and 10000.")
        self.initialize()
        params: list[Any] = []
        query = "SELECT * FROM scheduled_jobs"
        if states:
            values = [state.value for state in states]
            query += " WHERE state IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        query += " ORDER BY scheduled_for, created_at"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def blocking_job_for_draft(self, reference: str) -> dict[str, Any] | None:
        """Return one local queue outcome without materializing the job history."""
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT reference, state, updated_at FROM scheduled_jobs "
                "WHERE draft_reference=? AND state NOT IN (?, ?) "
                "ORDER BY created_at DESC, reference DESC LIMIT 1",
                (reference, SendState.CANCELLED.value, SendState.FAILED.value),
            ).fetchone()
        return dict(row) if row else None

    def update_job_state(
        self,
        reference: str,
        state: SendState,
        *,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        outcome_reference: str | None = None,
        event: str | None = None,
    ) -> None:
        now = iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs SET state=?, last_error=?, next_attempt_at=?,
                    outcome_reference=COALESCE(?, outcome_reference), lease_owner=NULL,
                    lease_until=NULL, updated_at=? WHERE reference=?
                """,
                (
                    state.value,
                    error,
                    iso(next_attempt_at) if next_attempt_at else None,
                    outcome_reference,
                    now,
                    reference,
                ),
            )
            if cursor.rowcount != 1:
                raise LocalStateError(f"Scheduled message not found: {reference}")
            connection.execute(
                "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                (reference, event or state.value, error, now),
            )

    def claim_due_jobs(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[ClaimedJob]:
        current = now or utc_now()
        current_text = iso(current)
        lease_until = iso(current + timedelta(seconds=lease_seconds))
        allowed_states = (
            SendState.SCHEDULED.value,
            SendState.DUE.value,
            SendState.RETRY_SCHEDULED.value,
            SendState.WAITING_CONNECTIVITY.value,
            SendState.WAITING_AUTHENTICATION.value,
        )
        placeholders = ",".join("?" for _ in allowed_states)
        claimed: list[ClaimedJob] = []
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM scheduled_jobs
                WHERE state IN ({placeholders})
                  AND scheduled_for <= ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY scheduled_for LIMIT ?
                """,  # noqa: S608
                (*allowed_states, current_text, current_text, current_text, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE scheduled_jobs
                    SET state=?, lease_owner=?, lease_until=?, attempts=attempts+1,
                        last_attempt_at=?, updated_at=? WHERE reference=?
                    """,
                    (
                        SendState.SENDING.value,
                        worker_id,
                        lease_until,
                        current_text,
                        current_text,
                        row["reference"],
                    ),
                )
                connection.execute(
                    "INSERT INTO schedule_history(job_reference, event, occurred_at) "
                    "VALUES (?, 'claimed', ?)",
                    (row["reference"], current_text),
                )
                claimed.append(
                    ClaimedJob(
                        reference=row["reference"],
                        draft_reference=row["draft_reference"],
                        scheduled_for=datetime.fromisoformat(row["scheduled_for"]),
                        timezone=row["timezone"],
                        original_expression=row["original_expression"],
                        state=SendState.SENDING,
                        idempotency_key=row["idempotency_key"],
                        attempts=int(row["attempts"]) + 1,
                        require_final_confirmation=bool(row["require_final_confirmation"]),
                        attachment_manifest=tuple(json.loads(row["attachment_manifest_json"])),
                    )
                )
        return claimed

    def recover_stale_jobs(self, *, now: datetime | None = None) -> int:
        current = iso(now or utc_now())
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT reference FROM scheduled_jobs WHERE state=? AND lease_until < ?",
                (SendState.SENDING.value, current),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE scheduled_jobs SET state=?, lease_owner=NULL, lease_until=NULL,
                        last_error=?, updated_at=? WHERE reference=?
                    """,
                    (
                        SendState.MANUAL_REVIEW.value,
                        "Worker lease expired during send; delivery outcome must be reconciled.",
                        current,
                        row["reference"],
                    ),
                )
                connection.execute(
                    "INSERT INTO schedule_history(job_reference, event, detail, occurred_at) "
                    "VALUES (?, 'stale_lease', ?, ?)",
                    (row["reference"], "Delivery outcome may be uncertain.", current),
                )
        return len(rows)

    def schedule_history(self, reference: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT event, detail, occurred_at FROM schedule_history "
                "WHERE job_reference=? ORDER BY id",
                (reference,),
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        allowed = {
            "folder_cache",
            "item_reference_aliases",
            "item_reference_remote_ids",
            "item_references",
            "saved_searches",
            "search_history",
            "sync_checkpoints",
            "webhooks",
            "scheduled_jobs",
            "backup_records",
        }
        if table not in allowed:
            raise LocalStateError(f"Table is not available for inspection: {table}")
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        yield from (dict(row) for row in rows)
